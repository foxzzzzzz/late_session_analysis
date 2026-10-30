"""并行旁路分析；失败只影响研究结果，不影响正式推荐。"""
import json
import time
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed
from typing import Optional

from analysis.prompts import SYSTEM_PROMPT, make_stock_prompt

logger = logging.getLogger(__name__)


class ParallelLLMRunner:
    def __init__(self, client, max_workers: int = 8, timeout_per_stock: float = 30.0,
                 evidence_loader=None, batch_timeout: float = 30.0):
        self.client = client
        self.max_workers = max_workers
        self.timeout_per_stock = timeout_per_stock
        self.evidence_loader = evidence_loader
        self.batch_timeout = batch_timeout

    def analyze_batch(self, contexts: list, deadline=None) -> dict[str, dict]:
        if not contexts:
            return {}
        t0 = time.monotonic()
        deadline = min(deadline, t0 + self.batch_timeout) if deadline is not None else t0 + self.batch_timeout
        # 整批固定分析时点，不随排队/重试而接纳稍后披露的公告。
        as_of = datetime.now(ZoneInfo('Asia/Shanghai')).replace(tzinfo=None).isoformat(timespec='seconds')
        for ctx in contexts:
            ctx.llm_analysis = {'as_of': ctx.llm_analysis.get('as_of', as_of), 'evidence': [], 'unknowns': []}
        if deadline <= time.monotonic():
            return {c.code: {**c.llm_analysis, **_fallback_result('旁路整批截止，未发起请求')} for c in contexts}
        workers = min(self.max_workers, len(contexts))
        executor = ThreadPoolExecutor(max_workers=workers)
        futures = {executor.submit(self._analyze_one, c, deadline): c for c in contexts}
        results = {}
        try:
            for future in as_completed(futures, timeout=max(0, deadline-time.monotonic())):
                ctx = futures[future]
                try:
                    result = future.result()
                    if time.monotonic() <= deadline:
                        results[ctx.code] = result
                except Exception as e:
                    logger.warning(f'LLM旁路失败 [{ctx.code}]: {type(e).__name__}')
                    results[ctx.code] = {**ctx.llm_analysis, **_fallback_result('旁路调用异常')}
        except TimeoutError:
            logger.warning('LLM旁路整体超时，保留正式规则结果')
        finally:
            for future, ctx in futures.items():
                if ctx.code not in results:
                    future.cancel()
                    results[ctx.code] = {**ctx.llm_analysis, **_fallback_result('旁路整体超时')}
            executor.shutdown(wait=False, cancel_futures=True)
        success = sum(not r.get('fallback', False) for r in results.values())
        logger.info(f'LLM旁路分析: {success}成功/{len(contexts)-success}未评估 ({time.monotonic()-t0:.1f}s)')
        return results

    def _analyze_one(self, ctx, deadline=None) -> dict:
        # 工作线程使用局部上下文，整体超时后不会再修改正式结果对象。
        from copy import deepcopy
        local = deepcopy(ctx)
        a = dict(local.llm_analysis)
        if deadline is not None and time.monotonic() >= deadline:
            return _fallback_result('旁路整批截止，未发起请求')
        local.llm_analysis['_deadline'] = deadline
        if self.evidence_loader:
            try:
                evidence = self.evidence_loader(local)
                a.update(evidence=evidence['items'], evidence_status=evidence['status'],
                         unknowns=evidence['unknowns'])
            except Exception as e:
                logger.warning(f'旁路证据获取失败 [{ctx.code}]: {type(e).__name__}')
                a.update(evidence=[], evidence_status='unavailable', unknowns=['公告证据获取失败'])
        else:
            a.update(evidence=[], evidence_status='unavailable', unknowns=['公告证据未获取'])
        local.llm_analysis = a
        prompt = make_stock_prompt(local)
        a.update(input_prompt=prompt, system_prompt=SYSTEM_PROMPT,
                 model=str(getattr(self.client, '_model_id', 'unknown')))
        try:
            remaining = min(self.timeout_per_stock, deadline-time.monotonic()) if deadline is not None else self.timeout_per_stock
            if remaining <= 0:
                return {**a, **_fallback_result('旁路整批截止，未发起模型请求')}
            response = self.client.chat(SYSTEM_PROMPT, prompt, timeout=remaining)
            a['raw_response'] = response
            parsed = self._parse_response(response, local.code, a['evidence']) if response else None
        except Exception as e:
            logger.warning(f'LLM旁路调用异常 [{ctx.code}]: {type(e).__name__}')
            parsed = None
        if parsed is None:
            result = _fallback_result('LLM调用或完整JSON校验失败')
            result['unknowns'] += a['unknowns']
            return {**a, **result}
        parsed['unknowns'] = a['unknowns'] + parsed['unknowns']
        return {**a, **parsed, 'status': 'completed', 'fallback': False}

    @staticmethod
    def _parse_response(response: str, code: str, evidence=None) -> Optional[dict]:
        # 允许Markdown包装，但不修补残缺JSON，不从推理片段恢复交易结论。
        try:
            start, end = response.index('{'), response.rindex('}') + 1
            data = json.loads(response[start:end])
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict) or not isinstance(data.get('summary'), str):
            return None
        if not all(isinstance(data.get(k), list) for k in ('events', 'conflicts', 'unknowns')):
            return None
        if not all(isinstance(s, str) for k in ('conflicts', 'unknowns') for s in data[k]):
            return None
        sources = {e['id']: e for e in evidence or []}
        events, unknowns = [], list(data['unknowns'])
        for event in data['events']:
            if not isinstance(event, dict):
                unknowns.append('模型事件格式不合法，未采信')
                continue
            fields = ('classification', 'subject', 'description', 'evidence_id', 'quote')
            if not all(isinstance(event.get(k), str) and event[k].strip() for k in fields):
                unknowns.append('模型事件缺少对象/描述/引用，未采信')
                continue
            source = sources.get(event['evidence_id'])
            match_mode = None
            if source:
                if event['quote'] in source['text']:
                    match_mode = 'exact'
                elif ''.join(event['quote'].split()) in ''.join(source['text'].split()):
                    # 只忽略空白；不折叠标点/数字/大小写，不做近似或语义匹配。
                    match_mode = 'whitespace_only'
            if (event['classification'] not in ('fact', 'rumor', 'inference') or not source
                    or match_mode is None):
                unknowns.append(f"模型事件引用无法核对，未采信: {event['description']}")
                continue
            events.append({**{k: event[k] for k in fields}, 'quote_matched': True,
                           'quote_match_mode': match_mode,
                           'source_url': source['url'], 'published_at': source['published_at']})
        return {'summary': data['summary'], 'events': events,
                'conflicts': data['conflicts'], 'unknowns': unknowns}


def _fallback_result(reason: str) -> dict:
    return {'status': 'not_evaluated', 'summary': '未评估', 'fallback': True,
            'events': [], 'conflicts': [], 'unknowns': [reason]}
