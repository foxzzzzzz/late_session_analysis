"""LLM 分析层测试"""
import pytest
from screening.context import StockContext
from analysis.rule_scorer import rule_based_recommendation
from analysis.merger import merge_and_rank
from analysis.prompts import make_stock_prompt


class TestRuleScorer:
    def test_strong_signal_returns_buy(self):
        ctx = StockContext(
            code='000001', name='强势股',
            late_price_change=4.0, afternoon_volume_ratio=3.0,
            big_order_ratio=0.35, big_order_net=10_000_000,
            ma_alignment='bullish',
        )
        result = rule_based_recommendation(ctx)
        assert result['decision'] in ('buy', 'hold')
        assert result['confidence'] in ('A', 'B', 'C')

    def test_no_signal_returns_skip(self):
        ctx = StockContext(
            code='000001', name='僵尸股',
            late_price_change=0.1, afternoon_volume_ratio=0.3,
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
            '000001': {'decision': 'buy', 'confidence': 'A', 'reason': '尾盘放量突破'},
        }
        result = merge_and_rank([ctx], llm_results)
        assert len(result) == 1
        assert ctx.final_score > 0
        assert ctx.recommendation in ('strong_buy', 'buy', 'watch', 'skip')

    def test_missing_llm_result_falls_back(self):
        ctx = StockContext(
            code='000001', name='测试', total_score=80.0,
            late_price_change=4.0, afternoon_volume_ratio=2.0,
            big_order_ratio=0.3, big_order_net=5_000_000,
            ma_alignment='bullish',
        )
        result = merge_and_rank([ctx], {})
        assert len(result) == 1
        assert ctx.llm_confidence  # 规则兜底也填充了
        assert ctx.llm_fallback  # 应标记为降级

    def test_llm_fallback_flag_set(self):
        """LLM超时返回的结果应标记为降级"""
        ctx = StockContext(code='000001', name='测试', total_score=80.0)
        llm_results = {
            '000001': {'decision': 'skip', 'confidence': 'C',
                       'reason': 'LLM超时或失败', 'fallback': True},
        }
        merge_and_rank([ctx], llm_results)
        assert ctx.llm_fallback

    def test_llm_success_no_fallback(self):
        """LLM正常返回不应标记降级"""
        ctx = StockContext(code='000001', name='测试', total_score=80.0)
        llm_results = {
            '000001': {'decision': 'buy', 'confidence': 'A',
                       'reason': '放量突破', 'fallback': False},
        }
        merge_and_rank([ctx], llm_results)
        assert not ctx.llm_fallback

    def test_rank_assignment(self):
        ctx1 = StockContext(code='000001', name='A', total_score=85.0)
        ctx2 = StockContext(code='000002', name='B', total_score=70.0)
        ctx3 = StockContext(code='000003', name='C', total_score=55.0)
        llm_results = {
            '000001': {'decision': 'buy', 'confidence': 'A', 'reason': '强'},
            '000002': {'decision': 'hold', 'confidence': 'B', 'reason': '中'},
            '000003': {'decision': 'skip', 'confidence': 'C', 'reason': '弱'},
        }
        result = merge_and_rank([ctx3, ctx2, ctx1], llm_results)
        assert result[0].final_rank == 1
        assert result[0].code == '000001'


class TestPrompts:
    def test_prompt_generation(self):
        ctx = StockContext(
            code='000001', name='平安银行', price=12.50,
            change_pct=3.5, late_price_change=2.8,
            afternoon_volume_ratio=2.0, last_5min_volume_pct=10.0,
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
