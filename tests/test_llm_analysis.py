"""LLM旁路分析层回归测试（原权重/决策映射已退出正式链路）。"""
import json
from concurrent.futures import TimeoutError
from unittest.mock import MagicMock, patch

from screening.context import StockContext
from analysis.rule_scorer import rule_based_recommendation
from analysis.merger import merge_and_rank
from analysis.prompts import make_stock_prompt
from analysis.parallel_runner import ParallelLLMRunner, _fallback_result


class TestRuleScorer:
    def test_strong_signal_returns_buy(self):
        ctx = StockContext(code='000001', name='强势股', late_price_change=4.0,
            late_volume_ratio=3.0, big_order_ratio=0.35, big_order_net=10_000_000,
            ma_alignment='bullish')
        result = rule_based_recommendation(ctx)
        assert result['decision'] in ('buy', 'hold')
        assert result['confidence'] in ('A', 'B', 'C')

    def test_no_signal_returns_skip(self):
        ctx = StockContext(code='000001', name='僵尸股', late_price_change=0.1,
            late_volume_ratio=0.3)
        assert rule_based_recommendation(ctx)['decision'] == 'skip'


def test_missing_result_does_not_fabricate_llm_decision():
    ctx = StockContext(code='000001', name='测试', total_score=80)
    merge_and_rank([ctx], {})
    assert ctx.llm_fallback
    assert ctx.llm_score is None
    assert ctx.llm_decision == ''
    assert ctx.llm_confidence == ''
    assert ctx.llm_analysis['status'] == 'not_evaluated'


def test_parse_markdown_json():
    data = dict(summary='证据不足', events=[], conflicts=[], unknowns=['公告缺失'])
    parsed = ParallelLLMRunner._parse_response('```json\n' + json.dumps(data) + '\n```', 'a')
    assert parsed == data


def test_fallback_is_not_a_zero_score_or_skip_instruction():
    result = _fallback_result('超时')
    assert result['fallback']
    assert result['summary'] == '未评估'
    assert 'llm_score' not in result and 'decision' not in result


def test_batch_timeout_returns_all_candidates():
    contexts = [StockContext(code='a', name='A'), StockContext(code='b', name='B')]
    runner = ParallelLLMRunner(client=None, max_workers=1, timeout_per_stock=0.01)
    with patch('analysis.parallel_runner.as_completed', side_effect=TimeoutError):
        results = runner.analyze_batch(contexts)
    assert set(results) == {'a', 'b'}
    assert all(r['fallback'] for r in results.values())


def test_runner_success_records_evidence_and_input():
    client = MagicMock()
    client.chat.return_value = json.dumps(dict(summary='暂缺公告', events=[], conflicts=[], unknowns=[]))
    c = StockContext(code='000001', name='平安银行', price=12.5)
    result = ParallelLLMRunner(client, max_workers=1).analyze_batch([c])[c.code]
    assert not result['fallback']
    assert result['evidence_status'] == 'unavailable'
    assert result['unknowns']
    assert '平安银行' in result['input_prompt']
    assert result['raw_response'] == client.chat.return_value
    assert result['system_prompt']


def test_prompt_does_not_anchor_on_rule_score():
    ctx = StockContext(code='000001', name='测试', total_score=78,
        score_tail_strength=25, market_regime='bull')
    prompt = make_stock_prompt(ctx)
    assert '总分' not in prompt and 'A尾盘=25' not in prompt
    assert 'bull' in prompt
