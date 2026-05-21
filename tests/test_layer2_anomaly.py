"""L2 尾盘异动识别测试"""
import pytest
from screening.context import StockContext
from screening.layer2_anomaly import screen_l2_anomaly, L2Config


def make_ctx(**kwargs):
    defaults = {
        'code': '000001', 'name': '测试股', 'price': 10.0,
        'change_pct': 3.0, 'turnover': 500_000_000, 'turnover_rate': 3.0,
        'volume': 10_000_000, 'high': 10.3, 'low': 9.7,
        'open': 9.8, 'pre_close': 9.71,
        'late_volume_ratio': 2.0, 'last_5min_volume_pct': 10.0,
        'late_price_change': 3.0, 'broke_high': False,
        'big_order_net': 5_000_000, 'big_order_ratio': 0.25,
        'daily_avg_big_order_ratio': 0.15,
        'active_buy_ratio': 60.0,
        'bid_vol': 10_000, 'ask_vol': 5_000, 'cancel_rate': 10.0,
    }
    defaults.update(kwargs)
    return StockContext(**defaults)


class TestLayer2Anomaly:
    def test_late_rally_passes(self):
        """尾盘拉升 > 2%, 放量 > 1.5x → 通过"""
        ctx = make_ctx(late_price_change=3.0, late_volume_ratio=2.0)
        result = screen_l2_anomaly([ctx])
        assert len(result) == 1
        assert ctx.l2_passed
        assert ctx.anomaly_type == 'rally'

    def test_breakout_passes(self):
        """突破日内高点 → 通过"""
        ctx = make_ctx(broke_high=True, late_volume_ratio=2.0, late_price_change=0.5,
                       price_at_1430=9.9, open=9.8, pre_close=9.71)
        result = screen_l2_anomaly([ctx])
        assert len(result) == 1
        assert ctx.anomaly_type == 'breakout'

    def test_no_volume_fails(self):
        """无放量 → 不通过"""
        ctx = make_ctx(late_volume_ratio=0.8, last_5min_volume_pct=3.0)
        result = screen_l2_anomaly([ctx])
        assert len(result) == 0

    def test_high_last_5min_volume_passes(self):
        """最后5分钟放量(>8%) + 价格形态 → 通过"""
        ctx = make_ctx(
            late_volume_ratio=1.2,
            last_5min_volume_pct=12.0,
            late_price_change=2.5,
        )
        result = screen_l2_anomaly([ctx])
        assert len(result) == 1

    def test_capital_check_optional(self):
        """资金面数据不完整时，不检查资金面"""
        ctx = make_ctx(
            big_order_net=-10_000_000,  # 大单流出
            active_buy_ratio=30.0,       # 主动买入很低
            late_volume_ratio=2.0,  # 但有放量拉升
            late_price_change=3.0,
        )
        # MVP默认不检查资金面(require_capital=False)
        result = screen_l2_anomaly([ctx])
        assert len(result) == 1

    def test_multiple_anomaly_types(self):
        """多类型异动时取最强"""
        ctx = make_ctx(
            late_price_change=4.0,
            broke_high=True,
            late_volume_ratio=3.0,
        )
        screen_l2_anomaly([ctx])
        assert ctx.anomaly_type in ('rally', 'breakout')
