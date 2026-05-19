"""并行LLM调用调度器

在Stage4(14:56:30-14:57:50)期间，30只标的并发调用LLM
每只15秒超时，超时或失败的标的降级到规则评分

降级标记: 结果中 fallback=True 表示该标的未经过LLM分析
"""
import json
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from analysis.llm_client import LLMClient
from analysis.prompts import SYSTEM_PROMPT, make_stock_prompt

logger = logging.getLogger(__name__)


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
            {code: {decision, confidence, reason}} 字典
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

        with ThreadPoolExecutor(max_workers=n_workers) as executor:
            futures = {
                executor.submit(self._analyze_one, ctx): ctx.code
                for ctx in contexts
            }

            for future in as_completed(futures, timeout=total_timeout):
                code = futures[future]
                try:
                    result = future.result(timeout=self.timeout_per_stock)
                    if result:
                        result['fallback'] = False
                        results[code] = result
                        success_count += 1
                    else:
                        results[code] = {"decision": "skip", "confidence": "C",
                                         "reason": "LLM超时或失败", "fallback": True}
                        fail_count += 1
                except Exception as e:
                    logger.debug(f"LLM并行调用异常 {code}: {e}")
                    results[code] = {"decision": "skip", "confidence": "C",
                                     "reason": "调用异常", "fallback": True}
                    fail_count += 1

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

        # 解析JSON响应
        return self._parse_response(response, ctx.code)

    @staticmethod
    def _parse_response(response: str, code: str) -> Optional[dict]:
        """解析LLM的JSON响应"""
        try:
            # 尝试直接解析
            result = json.loads(response)
            if all(k in result for k in ['decision', 'confidence', 'reason']):
                return result
        except json.JSONDecodeError:
            pass

        # 尝试从文本中提取JSON
        try:
            start = response.find('{')
            end = response.rfind('}') + 1
            if start >= 0 and end > start:
                result = json.loads(response[start:end])
                if all(k in result for k in ['decision', 'confidence', 'reason']):
                    return result
        except (json.JSONDecodeError, KeyError):
            pass

        logger.warning(f"LLM响应解析失败 {code}: {response[:100]}")
        return None
