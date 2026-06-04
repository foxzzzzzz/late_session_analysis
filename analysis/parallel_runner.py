"""并行LLM调用调度器

在Stage4(14:56:30-14:57:50)期间，30只标的并发调用LLM
每只15秒超时，超时或失败的标的降级到规则评分

降级标记: 结果中 fallback=True 表示该标的未经过LLM分析
"""
import json
import time
import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed
from typing import Optional

from analysis.llm_client import LLMClient
from analysis.prompts import SYSTEM_PROMPT, make_stock_prompt

logger = logging.getLogger(__name__)

# 旧格式decision→分数映射 (向后兼容)
_DECISION_SCORE_MAP = {'buy': 85, 'hold': 55, 'skip': 15}


class ParallelLLMRunner:
    """并行LLM调用调度器"""

    def __init__(self, client: LLMClient, max_workers: int = 8, timeout_per_stock: float = 30.0):
        self.client = client
        self.max_workers = max_workers
        self.timeout_per_stock = timeout_per_stock

    def analyze_batch(self, contexts: list) -> dict[str, dict]:
        """并行分析一批股票

        Args:
            contexts: Top 30 StockContext列表

        Returns:
            {code: {decision, confidence, llm_score, risk_flags, key_factors, reason}} 字典
        """
        if not contexts:
            return {}

        t0 = time.time()
        results = {}
        success_count = 0
        fail_count = 0

        n_stocks = len(contexts)
        n_workers = min(self.max_workers, n_stocks)
        # 总超时 = 单只超时 × (总股票数/并发数) + 缓冲
        total_timeout = self.timeout_per_stock * (n_stocks / n_workers) + 30

        executor = ThreadPoolExecutor(max_workers=n_workers)
        futures = {
            executor.submit(self._analyze_one, ctx): ctx.code
            for ctx in contexts
        }
        try:
            for future in as_completed(futures, timeout=total_timeout):
                code = futures[future]
                try:
                    result = future.result(timeout=self.timeout_per_stock)
                    if result:
                        result['fallback'] = False
                        results[code] = result
                        success_count += 1
                    else:
                        results[code] = _fallback_result("LLM超时或失败")
                        fail_count += 1
                except Exception as e:
                    logger.debug(f"LLM并行调用异常 {code}: {e}")
                    results[code] = _fallback_result("调用异常")
                    fail_count += 1
        except TimeoutError:
            logger.warning("LLM并行分析整体超时，未完成标的降级")
        finally:
            for future, code in futures.items():
                if code not in results:
                    future.cancel()
                    results[code] = _fallback_result("LLM整体超时")
                    fail_count += 1
            executor.shutdown(wait=False, cancel_futures=True)

        elapsed = time.time() - t0
        logger.info(f"LLM并行分析: {success_count}成功/{fail_count}失败 "
                     f"({len(contexts)}只, {elapsed:.1f}s)")

        return results

    def _analyze_one(self, ctx) -> Optional[dict]:
        """分析单只股票"""
        prompt = make_stock_prompt(ctx)
        response = self.client.chat(SYSTEM_PROMPT, prompt)

        if not response:
            return None

        return self._parse_response(response, ctx.code)

    @staticmethod
    def _parse_response(response: str, code: str) -> Optional[dict]:
        """解析LLM的JSON响应，支持新旧格式+截断修复"""
        data = None

        # 尝试直接解析
        try:
            data = json.loads(response)
        except json.JSONDecodeError:
            pass

        # 尝试从文本中提取JSON
        if data is None:
            try:
                start = response.find('{')
                end = response.rfind('}') + 1
                if start >= 0 and end > start:
                    data = json.loads(response[start:end])
            except (json.JSONDecodeError, KeyError):
                pass

        # 尝试修复截断的JSON (LLM输出被截断时常见)
        if data is None:
            data = ParallelLLMRunner._try_fix_truncated_json(response)

        if data is None or not isinstance(data, dict):
            logger.warning(f"LLM响应解析失败 {code}: {response[:100]}")
            return None

        # 验证必需字段
        if 'decision' not in data:
            logger.warning(f"LLM响应缺少decision字段 {code}: {response[:100]}")
            return None

        # 提取核心字段
        result = {
            'decision': data.get('decision', 'skip'),
            'confidence': data.get('confidence', 'C'),
            'reason': data.get('reason', ''),
        }

        # 新字段: llm_score (优先使用，否则从decision映射)
        if 'llm_score' in data and isinstance(data['llm_score'], (int, float)):
            result['llm_score'] = float(data['llm_score'])
        else:
            result['llm_score'] = float(_DECISION_SCORE_MAP.get(result['decision'], 15))

        # 新字段: risk_flags
        risk_flags = data.get('risk_flags', [])
        result['risk_flags'] = risk_flags if isinstance(risk_flags, list) else []

        # 新字段: key_factors
        key_factors = data.get('key_factors', [])
        result['key_factors'] = key_factors if isinstance(key_factors, list) else []

        return result

    @staticmethod
    def _try_fix_truncated_json(response: str) -> Optional[dict]:
        """尝试修复LLM输出截断导致的残缺JSON"""
        start = response.find('{')
        if start < 0:
            return None

        fragment = response[start:]
        open_braces = fragment.count('{') - fragment.count('}')
        open_brackets = fragment.count('[') - fragment.count(']')
        in_string = fragment.count('"') % 2 == 1

        fixed = fragment
        if in_string:
            fixed += '"'
        fixed += ']' * open_brackets
        fixed += '}' * open_braces

        try:
            return json.loads(fixed)
        except json.JSONDecodeError:
            return None


def _fallback_result(reason: str) -> dict:
    """LLM失败时的兜底结果"""
    return {
        "decision": "skip",
        "confidence": "C",
        "reason": reason,
        "llm_score": 0.0,
        "risk_flags": [],
        "key_factors": [],
        "fallback": True,
    }
