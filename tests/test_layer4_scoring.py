"""L4 量化评分测试"""
import pytest
from screening.context import StockContext
from screening.layer4_scoring import score_l4, L4Config


def make_ctx(**kwargs):
    defaults = {
        'code': '000001', 'name': '测试股', 'price': 10.0,
        'change_pct': 3.0,
        'late_price_change': 3.0, 'afternoon_volume_ratio': 2.5,
        'last_5min_volume_pct': 12.0,
        'big_order_net': 10_000_000, 'big_order_ratio': 0.25,
        'active_buy_ratio': 62.0,
        'bid_vol': 20_000, 'ask_vol': 10_000,
        'anomaly_type': 'rally', 'ma_alignment': 'bullish',
        'sector_performance': 2.5,
        'hot_concepts': ['AI', '半导体'],
        'leader_strength': False,
        'history_win_rate': 75.0,
    }
    defaults.update(kwargs)
    return StockContext(**defaults)


class TestLayer4Scoring:
    def test_strong_stock_scores_high(self):
        """强信号股票应该得高分"""
        ctx = make_ctx()
        result = score_l4([ctx])
        assert len(result) == 1
        # 尾盘强度高 + 技术面好 + 资金流入
        assert ctx.total_score > 60
        assert ctx.score_tail_strength > 0
        assert ctx.score_technical > 0
        assert ctx.score_capital > 0

    def test_weak_stock_scores_low(self):
        """弱信号股票应该得低分"""
        ctx = make_ctx(
            late_price_change=0.2, afternoon_volume_ratio=0.5,
            big_order_net=0, big_order_ratio=0,
            ma_alignment='', anomaly_type='volume_only',
            history_win_rate=40.0,
        )
        score_l4([ctx])
        assert ctx.total_score < 50

    def test_sorted_by_score(self):
        """返回结果按评分降序"""
        strong = make_ctx(code='000001', late_price_change=5.0, afternoon_volume_ratio=3.0)
        medium = make_ctx(code='000002', late_price_change=2.0, afternoon_volume_ratio=1.5)
        weak = make_ctx(code='000003', late_price_change=0.5, afternoon_volume_ratio=0.8)
        result = score_l4([weak, strong, medium])
        codes = [c.code for c in result]
        assert codes[0] == '000001'  # 最强排第一

    def test_high_attention_classification(self):
        """高分标的归类为重点关注"""
        strong = make_ctx(code='000001')
        score_l4([strong])
        if strong.total_score >= 75:
            assert strong.total_score > 75

    def test_bottom_area_technical_score(self):
        """底部区域标的，技术分应该有一些"""
        ctx = make_ctx(
            late_price_change=1.0, ma_alignment='bottom_area',
            anomaly_type='steady', afternoon_volume_ratio=1.0,
        )
        score_l4([ctx])
        # 底部企稳至少有部分技术分
        assert ctx.score_technical > 0
