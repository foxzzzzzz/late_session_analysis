"""LLM 分析层测试"""
import pytest
from concurrent.futures import TimeoutError
from unittest.mock import patch

from screening.context import StockContext
from analysis.rule_scorer import rule_based_recommendation
from analysis.merger import merge_and_rank, _compute_weights
from analysis.prompts import make_stock_prompt
from analysis.parallel_runner import ParallelLLMRunner, _fallback_result


class TestRuleScorer:
    def test_strong_signal_returns_buy(self):
        ctx = StockContext(
            code='000001', name='强势股',
            late_price_change=4.0, late_volume_ratio=3.0,
            big_order_ratio=0.35, big_order_net=10_000_000,
            ma_alignment='bullish',
        )
        result = rule_based_recommendation(ctx)
        assert result['decision'] in ('buy', 'hold')
        assert result['confidence'] in ('A', 'B', 'C')

    def test_no_signal_returns_skip(self):
        ctx = StockContext(
            code='000001', name='僵尸股',
            late_price_change=0.1, late_volume_ratio=0.3,
            big_order_ratio=0, big_order_net=0,
            ma_alignment='',
        )
        result = rule_based_recommendation(ctx)
        assert result['decision'] == 'skip'


class TestMerger:
    def test_merge_combines_scores(self):
        ctx = StockContext(
            code='000001', name='测试', total_score=80.0,
        )
        llm_results = {
            '000001': {
                'decision': 'buy', 'confidence': 'A',
                'reason': '尾盘放量突破',
                'llm_score': 85.0, 'risk_flags': [], 'key_factors': ['放量突破'],
            },
        }
        result = merge_and_rank([ctx], llm_results)
        assert len(result) == 1
        assert ctx.final_score > 0
        assert ctx.llm_score == 85.0
        assert ctx.llm_key_factors == ['放量突破']
        assert ctx.recommendation in ('strong_buy', 'buy', 'watch', 'skip')

    def test_missing_llm_result_falls_back(self):
        ctx = StockContext(
            code='000001', name='测试', total_score=80.0,
            late_price_change=4.0, late_volume_ratio=2.0,
            big_order_ratio=0.3, big_order_net=5_000_000,
            ma_alignment='bullish',
        )
        result = merge_and_rank([ctx], {})
        assert len(result) == 1
        assert ctx.llm_confidence
        assert ctx.llm_fallback
        assert ctx.llm_score == 0.0  # fallback has 0 score

    def test_llm_fallback_flag_set(self):
        """LLM超时返回的结果应标记为降级"""
        ctx = StockContext(code='000001', name='测试', total_score=80.0)
        llm_results = {
            '000001': {
                'decision': 'skip', 'confidence': 'C',
                'reason': 'LLM超时或失败', 'fallback': True,
                'llm_score': 0.0, 'risk_flags': [], 'key_factors': [],
            },
        }
        merge_and_rank([ctx], llm_results)
        assert ctx.llm_fallback

    def test_llm_success_no_fallback(self):
        """LLM正常返回不应标记降级"""
        ctx = StockContext(code='000001', name='测试', total_score=80.0)
        llm_results = {
            '000001': {
                'decision': 'buy', 'confidence': 'A',
                'reason': '放量突破', 'fallback': False,
                'llm_score': 82.0, 'risk_flags': [], 'key_factors': ['放量'],
            },
        }
        merge_and_rank([ctx], llm_results)
        assert not ctx.llm_fallback

    def test_backward_compat_old_format(self):
        """旧格式(无llm_score)应向后兼容，从decision映射分数"""
        ctx = StockContext(code='000001', name='测试', total_score=80.0)
        llm_results = {
            '000001': {'decision': 'buy', 'confidence': 'A', 'reason': '强'},
        }
        merge_and_rank([ctx], llm_results)
        assert ctx.llm_score > 0  # buy→85
        assert ctx.llm_risk_flags == []
        assert ctx.llm_key_factors == []

    def test_rank_assignment(self):
        ctx1 = StockContext(code='000001', name='A', total_score=85.0)
        ctx2 = StockContext(code='000002', name='B', total_score=70.0)
        ctx3 = StockContext(code='000003', name='C', total_score=55.0)
        llm_results = {
            '000001': {'decision': 'buy', 'confidence': 'A', 'reason': '强',
                       'llm_score': 88.0, 'risk_flags': [], 'key_factors': []},
            '000002': {'decision': 'hold', 'confidence': 'B', 'reason': '中',
                       'llm_score': 55.0, 'risk_flags': [], 'key_factors': []},
            '000003': {'decision': 'skip', 'confidence': 'C', 'reason': '弱',
                       'llm_score': 20.0, 'risk_flags': [], 'key_factors': []},
        }
        result = merge_and_rank([ctx3, ctx2, ctx1], llm_results)
        assert result[0].final_rank == 1
        assert result[0].code == '000001'

    def test_confidence_dynamic_weights(self):
        """置信度A→LLM权重40%, B→30%, C→20%"""
        rw, lw = _compute_weights('A', False)
        assert abs(lw - 0.40) < 0.001
        assert abs(rw - 0.60) < 0.001

        rw, lw = _compute_weights('B', False)
        assert abs(lw - 0.30) < 0.001
        assert abs(rw - 0.70) < 0.001

        rw, lw = _compute_weights('C', False)
        assert abs(lw - 0.20) < 0.001
        assert abs(rw - 0.80) < 0.001

    def test_fallback_rule_weight_1(self):
        """LLM回退时规则权重=1.0"""
        rw, lw = _compute_weights('C', True)
        assert rw == 1.0
        assert lw == 0.0

    def test_confidence_weighted_final_score(self):
        """验证逐只动态权重计算: rule=80, LLM=90, A→融合=80×0.6+90×0.4=84"""
        ctx = StockContext(code='000001', name='测试', total_score=80.0)
        llm_results = {
            '000001': {'decision': 'buy', 'confidence': 'A', 'reason': '强',
                       'llm_score': 90.0, 'risk_flags': [], 'key_factors': [],
                       'fallback': False},
        }
        merge_and_rank([ctx], llm_results)
        expected = 80.0 * 0.60 + 90.0 * 0.40  # = 84.0
        assert abs(ctx.final_score - expected) < 0.01


class TestParallelRunner:
    def test_parse_response_new_format(self):
        """新格式完整字段解析"""
        response = '{"decision": "buy", "confidence": "A", "llm_score": 85, "risk_flags": ["追高风险"], "key_factors": ["放量突破"], "reason": "强"}'
        result = ParallelLLMRunner._parse_response(response, '000001')
        assert result['decision'] == 'buy'
        assert result['llm_score'] == 85.0
        assert result['risk_flags'] == ['追高风险']
        assert result['key_factors'] == ['放量突破']

    def test_parse_response_old_format(self):
        """旧格式(无llm_score)向后兼容 — 从decision映射"""
        response = '{"decision": "buy", "confidence": "A", "reason": "强"}'
        result = ParallelLLMRunner._parse_response(response, '000001')
        assert result['decision'] == 'buy'
        assert result['llm_score'] == 85.0  # buy→85
        assert result['risk_flags'] == []
        assert result['key_factors'] == []

    def test_parse_response_hold_maps_to_55(self):
        response = '{"decision": "hold", "confidence": "B", "reason": "一般"}'
        result = ParallelLLMRunner._parse_response(response, '000001')
        assert result['llm_score'] == 55.0

    def test_parse_response_skip_maps_to_15(self):
        response = '{"decision": "skip", "confidence": "C", "reason": "弱"}'
        result = ParallelLLMRunner._parse_response(response, '000001')
        assert result['llm_score'] == 15.0

    def test_parse_response_extracts_json_from_text(self):
        """从markdown包裹中提取JSON"""
        response = '一些文本\n```json\n{"decision": "buy", "confidence": "A", "llm_score": 78, "risk_flags": [], "key_factors": ["因子1"], "reason": "测试"}\n```'
        result = ParallelLLMRunner._parse_response(response, '000001')
        assert result is not None
        assert result['decision'] == 'buy'
        assert result['llm_score'] == 78.0

    def test_fallback_result_format(self):
        result = _fallback_result("测试失败")
        assert result['fallback'] is True
        assert result['decision'] == 'skip'
        assert result['llm_score'] == 0.0
        assert result['risk_flags'] == []
        assert result['key_factors'] == []

    def test_batch_timeout_falls_back_for_all_missing_results(self):
        """整体超时时，不应抛出到流水线；未完成标的全部降级"""
        contexts = [
            StockContext(code='000001', name='A'),
            StockContext(code='000002', name='B'),
        ]
        runner = ParallelLLMRunner(client=None, max_workers=1, timeout_per_stock=0.01)

        with patch('analysis.parallel_runner.as_completed', side_effect=TimeoutError):
            results = runner.analyze_batch(contexts)

        assert set(results) == {'000001', '000002'}
        assert all(r['fallback'] for r in results.values())


class TestPrompts:
    def test_prompt_generation(self):
        ctx = StockContext(
            code='000001', name='平安银行', price=12.50,
            change_pct=3.5, late_price_change=2.8,
            late_volume_ratio=2.0, last_5min_volume_pct=10.0,
            turnover_rate=4.0, turnover=800_000_000,
            big_order_net=5_000_000, big_order_ratio=0.25,
            active_buy_ratio=60.0,
            ma_alignment='bullish', position_20d=30.0,
            anomaly_type='rally',
            sector='银行', sector_performance=1.5,
            total_score=78.0, history_win_rate=70.0,
        )
        prompt = make_stock_prompt(ctx)
        assert '平安银行' in prompt
        assert '000001' in prompt
        assert '12.50' in prompt
        assert '尾盘拉升' in prompt

    def test_prompt_includes_new_fields(self):
        """增强后的prompt应包含L3/题材/规则评分等新字段"""
        ctx = StockContext(
            code='000001', name='测试', price=10.0,
            market_regime='bull',
            hot_concepts=['AI', '芯片'],
            leader_strength=True,
            score_tail_strength=25.0, score_technical=20.0,
            score_capital=15.0, score_ma_system=10.0, score_market_env=8.0,
            total_score=78.0,
            volatility=0.03, sector_rank_pct=15.0,
            ma5=10.0, ma10=9.8, ma20=9.5, ma60=9.0,
            yang_days_4=3, consecutive_close_rise=2,
            has_bad_news=False, is_unlock_date=False,
        )
        prompt = make_stock_prompt(ctx)
        assert '牛市' in prompt
        assert 'AI' in prompt
        assert '芯片' in prompt
        assert '板块龙头' in prompt
        assert 'A尾盘=25' in prompt
        assert 'B形态=20' in prompt
        assert '规则引擎评分' in prompt
