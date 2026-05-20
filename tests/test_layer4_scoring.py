"""L4 量化评分测试 — 对齐 尾盘策略0514.txt 5维模型

维度: A尾盘强度(30) + BK线形态(25) + C资金面(20) + D均线系统(15) + E市场环境(10)
阈值: >85 strong_buy, 75-85 buy, 60-75 watch, <60 skip
"""
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
        'broke_high': True, 'anomaly_type': 'rally',
        'yang_days_4': 4, 'consecutive_close_rise': 3,
        'body_amplifying': True, 'volatility': 20.0,
        'ma_alignment': 'bullish', 'ma5_accelerating': True,
        'ma5': 9.8,  # price/ma5 = 1.02 → strong
        'sector_performance': 2.5,
        'hot_concepts': ['AI', '半导体'],
        'leader_strength': True,
    }
    defaults.update(kwargs)
    return StockContext(**defaults)


class TestLayer4Scoring:

    def test_strong_stock_scores_high(self):
        """强信号股票应该得高分 (>75)"""
        ctx = make_ctx()
        score_l4([ctx], capital_data_date='today')
        assert ctx.total_score > 75
        assert ctx.score_tail_strength > 0   # A维度
        assert ctx.score_technical > 0        # B维度 (K线形态)
        assert ctx.score_capital > 0          # C维度

    def test_weak_stock_scores_low(self):
        """弱信号股票应该得低分 (<60)"""
        ctx = make_ctx(
            late_price_change=0.2, afternoon_volume_ratio=0.5,
            big_order_net=0, big_order_ratio=0,
            ma_alignment='', anomaly_type='volume_only',
            broke_high=False, yang_days_4=0, consecutive_close_rise=0,
            body_amplifying=False, volatility=50.0,
            sector_performance=-2.0, hot_concepts=[],
        )
        score_l4([ctx], capital_data_date='today')
        assert ctx.total_score < 60

    def test_sorted_by_score(self):
        """返回结果按评分降序"""
        strong = make_ctx(code='000001', late_price_change=5.0, afternoon_volume_ratio=3.0)
        medium = make_ctx(code='000002', late_price_change=2.0, afternoon_volume_ratio=1.5)
        weak = make_ctx(code='000003', late_price_change=0.5, afternoon_volume_ratio=0.8)
        result = score_l4([weak, strong, medium], capital_data_date='today')
        codes = [c.code for c in result]
        assert codes[0] == '000001'

    def test_strong_buy_threshold(self):
        """得分 >85 应为 strong_buy"""
        ctx = make_ctx(
            late_price_change=5.0, afternoon_volume_ratio=3.5,
            last_5min_volume_pct=18.0,
            big_order_net=20_000_000, big_order_ratio=0.35,
            bid_vol=50_000, ask_vol=10_000,
            yang_days_4=4, consecutive_close_rise=5,
            broke_high=True, body_amplifying=True,
            ma_alignment='bullish', ma5_accelerating=True,
            ma5=9.5,  # price/ma5=1.05
            sector_performance=4.0, hot_concepts=['AI', '芯片', '新能源'],
            leader_strength=True,
            anomaly_type='breakout',
        )
        score_l4([ctx], capital_data_date='today')
        # 超强信号判定
        assert ctx.recommendation == 'strong_buy'
        assert ctx.total_score > 85

    def test_no_capital_redistributes(self):
        """无资金流时C维度归零，A/B/D/E得分放大"""
        ctx = make_ctx()
        # 无资金流
        score_l4([ctx], capital_data_date='none')
        assert ctx.score_capital == 0.0
        # A维度应放大到 >30 (max 37)
        assert ctx.score_tail_strength > 20

    def test_recommendation_tiers(self):
        """验证各级别判定阈值"""
        config = L4Config(high_attention_threshold=85, buy_threshold=75,
                          medium_attention_threshold=60)
        # strong_buy: >85
        s1 = make_ctx(code='s1', late_price_change=5.0, afternoon_volume_ratio=4.0,
                      big_order_net=20_000_000, big_order_ratio=0.4,
                      bid_vol=100_000, ask_vol=10_000,
                      yang_days_4=4, consecutive_close_rise=5,
                      broke_high=True, body_amplifying=True,
                      ma_alignment='bullish', ma5_accelerating=True, ma5=9.5,
                      sector_performance=5.0, hot_concepts=['AI','芯片','新能源'],
                      leader_strength=True, anomaly_type='breakout')
        # buy: 75-85
        s2 = make_ctx(code='s2', late_price_change=2.5, afternoon_volume_ratio=2.5,
                      big_order_net=7_000_000, big_order_ratio=0.2,
                      yang_days_4=3, consecutive_close_rise=3,
                      broke_high=True, body_amplifying=False, volatility=25,
                      ma5_accelerating=True, ma_alignment='above_ma5_strong')
        # watch: 60-75
        s3 = make_ctx(code='s3', late_price_change=2.0, afternoon_volume_ratio=2.2,
                      big_order_net=5_500_000, big_order_ratio=0.15,
                      yang_days_4=3, consecutive_close_rise=3,
                      broke_high=False, body_amplifying=False, volatility=25,
                      ma_alignment='above_ma5_strong', ma5_accelerating=False, ma5=9.98,
                      sector_performance=2.0, hot_concepts=['AI', '半导体'], anomaly_type='rally')

        result = score_l4([s1, s3, s2], config, capital_data_date='today')
        recs = {c.code: c.recommendation for c in result}
        assert recs['s1'] == 'strong_buy'
        assert recs['s2'] == 'buy'
        assert recs['s3'] == 'watch'

    def test_kline_dimension_scores(self):
        """B维度K线形态子分计算"""
        ctx = make_ctx(yang_days_4=4, consecutive_close_rise=4,
                       body_amplifying=True, broke_high=True,
                       volatility=15)
        score_l4([ctx], capital_data_date='today')
        # 接近满分的K线形态: 25 (标准) or more
        assert ctx.score_technical >= 20

    def test_ma_dimension_scores(self):
        """D维度均线系统子分计算"""
        ctx = make_ctx(
            late_price_change=1.0, afternoon_volume_ratio=1.0,
            big_order_net=0, big_order_ratio=0,
            yang_days_4=2, consecutive_close_rise=1,
            broke_high=False, body_amplifying=False,
            ma_alignment='bullish', ma5_accelerating=True,
            ma5=9.8,  # price/ma5 = 1.02 → 3pts
        )
        score_l4([ctx], capital_data_date='today')
        # D维度应有显著得分: 多头8 + MA5加速4 + 站稳MA5 3 = 15
        # score_history 用于兼容存D维度的分数
        assert ctx.score_history >= 12
