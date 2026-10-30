"""30只候选的预算、故障和迟到结果隔离；不联网。"""
import json
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
import requests

from analysis.parallel_runner import ParallelLLMRunner
from screening.context import StockContext
from data_provider import rate_limiter


def contexts():
    return [StockContext(code=f'{i:06d}', name=f'测试{i}', total_score=i) for i in range(30)]


def deadline_http(session):
    # 排队/门控单测不启动进程或联网；真实进程与socket由发布阻塞测试覆盖。
    return patch.object(rate_limiter, '_deadline_get', side_effect=lambda url, timeout, deadline, **kw:
                        session.get(url, timeout=timeout, **kw))


def test_30_queued_candidates_share_one_deadline_and_late_result_is_ignored(record_property):
    release = threading.Event()
    calls = []
    client = MagicMock()
    def chat(*args, **kwargs):
        calls.append(kwargs['timeout'])
        release.wait(2)
        return json.dumps(dict(summary='迟到', events=[], conflicts=[], unknowns=[]))
    client.chat.side_effect = chat
    cs = contexts()
    runner = ParallelLLMRunner(client, max_workers=1, batch_timeout=0.08)
    before = time.monotonic()
    try:
        result = runner.analyze_batch(cs)
        elapsed = time.monotonic()-before
        record_property('batch_elapsed_seconds', elapsed)
        record_property('configured_batch_seconds', 0.08)
        assert elapsed < 0.5
        assert len(result) == 30 and all(r['fallback'] for r in result.values())
        assert len(calls) == 1 and 0 < calls[0] <= 0.080001
        frozen = json.dumps(result)
    finally:
        release.set()
    time.sleep(0.02)
    assert json.dumps(result) == frozen
    assert [c.total_score for c in cs] == list(range(30))
    assert len(calls) == 1


def test_30_candidates_single_failure_does_not_cancel_others():
    client = MagicMock()
    def chat(system, prompt, **kwargs):
        if '(000005)' in prompt:
            raise RuntimeError('one stock failed')
        return json.dumps(dict(summary='正常', events=[], conflicts=[], unknowns=[]))
    client.chat.side_effect = chat
    result = ParallelLLMRunner(client, max_workers=4, batch_timeout=2).analyze_batch(contexts())
    assert len(result) == 30
    assert sum(not r['fallback'] for r in result.values()) == 29
    assert result['000005']['fallback']


def test_deadline_during_evidence_prevents_model_request():
    client = MagicMock()
    def loader(ctx):
        time.sleep(0.04)
        return {'items': [], 'status': 'unavailable', 'unknowns': []}
    result = ParallelLLMRunner(client, max_workers=1, batch_timeout=0.02,
                               evidence_loader=loader).analyze_batch(contexts())
    time.sleep(0.05)
    client.chat.assert_not_called()
    assert len(result) == 30


def test_http_gate_lock_wait_respects_batch_deadline():
    gate = threading.Lock(); gate.acquire()
    session = MagicMock()
    before = time.monotonic()
    errors = []
    def request():
        try:
            rate_limiter.em_get('https://example.test', deadline=time.monotonic()+0.03)
        except Exception as e:
            errors.append(e)
    with patch.object(rate_limiter, '_lock', gate), deadline_http(session):
        thread = threading.Thread(target=request)
        thread.start()
        thread.join(0.2)
        waiting = thread.is_alive()
        gate.release()
        thread.join(1)
    assert not waiting and time.monotonic()-before < 0.3
    assert len(errors) == 1 and isinstance(errors[0], requests.Timeout)
    session.get.assert_not_called()


def test_expired_http_deadline_never_sends_request():
    session = MagicMock()
    with deadline_http(session):
        with pytest.raises(requests.Timeout):
            rate_limiter.em_get('https://example.test', deadline=time.monotonic()-1)
    session.get.assert_not_called()


def test_rate_limit_slot_beyond_deadline_is_not_slept_or_requested():
    session = MagicMock()
    with patch.object(rate_limiter, '_last_em_call', time.time()), \
            patch.object(rate_limiter, 'EM_MIN_INTERVAL', 1), \
            deadline_http(session), \
            patch.object(rate_limiter.time, 'sleep') as sleep:
        with pytest.raises(requests.Timeout):
            rate_limiter.em_get('https://example.test', deadline=time.monotonic()+0.03)
    session.get.assert_not_called()
    sleep.assert_not_called()


def test_http_timeout_is_capped_by_remaining_budget():
    session = MagicMock()
    with patch.object(rate_limiter, '_last_em_call', 0), \
            deadline_http(session):
        rate_limiter.em_get('https://example.test', timeout=5, deadline=time.monotonic()+0.03)
    assert 0 < session.get.call_args.kwargs['timeout'] <= 0.030001


def test_30_candidates_with_three_notices_and_serial_gate_stop_at_batch_deadline(record_property):
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    from data_provider.news_fetcher import NewsFetcher
    now = datetime.now(ZoneInfo('Asia/Shanghai')).replace(tzinfo=None)
    published = (now-timedelta(minutes=1)).isoformat(sep=' ', timespec='seconds')
    notices = [{'art_code': str(i), 'title': 'notice', 'display_time': published} for i in range(3)]
    starts = []
    session = MagicMock()
    def get(url, **kwargs):
        starts.append(time.monotonic())
        r = MagicMock()
        r.json.return_value = ({'success': True, 'data': {'list': notices}} if '/security/ann' in url
                               else {'data': {'notice_content': 'original announcement'}})
        return r
    session.get.side_effect = get
    f = NewsFetcher()
    def evidence(ctx):
        return f.collect_evidence(ctx.code, now, max_items=3, max_chars=100, timeout=0.05,
                                  deadline=ctx.llm_analysis['_deadline'])
    client = MagicMock()
    client.chat.return_value = json.dumps(dict(summary='正常', events=[], conflicts=[], unknowns=[]))
    before = time.monotonic()
    with deadline_http(session), \
            patch.object(rate_limiter, '_last_em_call', 0), \
            patch.object(rate_limiter, 'EM_MIN_INTERVAL', 0.005), \
            patch.object(rate_limiter.random, 'uniform', return_value=0):
        result = ParallelLLMRunner(client, max_workers=4, evidence_loader=evidence,
                                   batch_timeout=0.12).analyze_batch(contexts())
        elapsed = time.monotonic()-before
        count = len(starts)
        time.sleep(0.05)  # 仍保持HTTP mock，等待在途线程退出。
        assert len(starts) == count
    record_property('batch_with_gate_elapsed_seconds', elapsed)
    record_property('http_requests_started', count)
    assert elapsed < 0.5 and len(result) == 30
    assert 0 < count < 120
    assert any(r['fallback'] for r in result.values())
