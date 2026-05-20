"""K线形态预筛选测试 — 13项策略检查全覆盖"""
import pytest
import pandas as pd
from screening.context import StockContext
from screening.layer_kline import (
    screen_kline, KlineConfig,
    _round1_basic_filter, _round2_deep_verify,
    _check_atr_range, _check_consecutive_up, _check_up_frequency,
    _check_yang_ratio, _check_close_momentum, _check_single_day_pct,
    _check_pct_vs_atr, _check_no_sharp_drop, _check_no_continuous_decline,
    _check_no_body_shrink, _check_high_break, _check_close_vs_open,
    _check_upper_shadow,
)


# === 测试数据构造 ===

def make_daily_df(closes, opens=None, highs=None, lows=None):
    """构造日线DataFrame (mootdx格式, datetime列)"""
    n = len(closes)
    data = {
        "open": opens if opens else closes,
        "close": closes,
        "high": highs if highs else closes,
        "low": lows if lows else closes,
        "vol": [1000000] * n,
        "amount": [c * 1000000 for c in closes],
        "volume": [1000000] * n,
    }
    df = pd.DataFrame(data)
    df["datetime"] = pd.date_range("2026-01-01", periods=n, freq="D")
    df["turnover"] = df["amount"]
    return df


def make_ctx(code="000001", change_pct=1.0, price=10.0, high=10.2, open_p=9.9, pre_close=10.0):
    return StockContext(
        code=code, name="测试股票",
        price=price, change_pct=change_pct,
        high=high, open=open_p, pre_close=pre_close,
    )


# ================================================================
# Round 1: 基础过滤 (7项)
# ================================================================

class TestRound1ATRRange:
    def test_atr_in_range_passes(self):
        """ATR/Close 在 2%~8.5% 之间"""
        closes = [10.0 + i * 0.02 for i in range(25)]  # 小幅波动
        highs = [c + 0.3 for c in closes]
        lows = [c - 0.2 for c in closes]
        df = make_daily_df(closes, highs=highs, lows=lows)
        ctx = make_ctx()
        cfg = KlineConfig(min_atr_pct=2.0, max_atr_pct=8.5)
        assert _check_atr_range(ctx, cfg, df)

    def test_atr_too_low_fails(self):
        """ATR/Close < 2% → 不通过 (大盘股无波动)"""
        # 创建极低波动数据: 价格在10.0附近微小波动, ATR约0.01, ATR%约0.1%
        closes = [10.0 + i * 0.001 for i in range(25)]
        highs = [c + 0.005 for c in closes]
        lows = [c - 0.005 for c in closes]
        df = make_daily_df(closes, highs=highs, lows=lows)
        ctx = make_ctx()
        cfg = KlineConfig(min_atr_pct=2.0, max_atr_pct=8.5)
        assert not _check_atr_range(ctx, cfg, df)

    def test_atr_too_high_fails(self):
        """ATR/Close > 8.5% → 不通过 (过度投机)"""
        closes = [100.0]
        highs = [120.0]
        lows = [80.0]
        for _ in range(19):
            closes.append(closes[-1] + 5)
            highs.append(closes[-1] + 20)
            lows.append(closes[-1] - 20)
        df = make_daily_df(closes, highs=highs, lows=lows)
        ctx = make_ctx()
        cfg = KlineConfig(min_atr_pct=2.0, max_atr_pct=8.5)
        assert not _check_atr_range(ctx, cfg, df)


class TestRound1ConsecutiveUp:
    def test_no_consecutive_up_passes(self):
        """没连涨 → 通过"""
        closes = [10.0, 9.9, 9.8, 9.9, 10.0]
        df = make_daily_df(closes)
        cfg = KlineConfig(max_consecutive_up=5)
        assert _check_consecutive_up(make_ctx(), cfg, df)

    def test_too_many_consecutive_up_fails(self):
        """连涨超5天 → 不通过"""
        closes = list(range(1, 10))  # 1,2,3,4,5,6,7,8,9 全部递增
        df = make_daily_df([float(c) for c in closes])
        cfg = KlineConfig(max_consecutive_up=5)
        assert not _check_consecutive_up(make_ctx(), cfg, df)

    def test_exactly_limit_passes(self):
        """刚好5天连涨 → 通过"""
        closes = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
        df = make_daily_df(closes)
        cfg = KlineConfig(max_consecutive_up=5)
        assert _check_consecutive_up(make_ctx(), cfg, df)


class TestRound1UpFrequency:
    def test_normal_frequency_passes(self):
        """9天涨4天 → 通过"""
        closes = [10.0, 10.2, 9.9, 10.1, 9.8, 10.3, 10.0, 10.5, 10.2, 10.0]
        df = make_daily_df(closes)
        cfg = KlineConfig(max_up_in_9days=6)
        assert _check_up_frequency(make_ctx(), cfg, df)

    def test_too_many_up_days_fails(self):
        """9天涨7天 → 不通过"""
        closes = [float(i) for i in range(1, 11)]  # 全递增, 9天涨9天
        df = make_daily_df(closes)
        cfg = KlineConfig(max_up_in_9days=6)
        assert not _check_up_frequency(make_ctx(), cfg, df)

    def test_short_data_passes(self):
        """数据不足5天 → 放行"""
        df = make_daily_df([10.0, 11.0])
        cfg = KlineConfig(max_up_in_9days=6)
        assert _check_up_frequency(make_ctx(), cfg, df)


class TestRound1YangRatio:
    def test_enough_yang_days_passes(self):
        """近4天有足够阳线 + 今日阳 → 通过"""
        closes = [10.0, 10.5, 10.2, 10.8, 10.3, 11.0]  # 最近4完成: 10.0→10.5↑, 10.5→10.2↓, 10.2→10.8↑, 10.8→10.3↓
        opens = [9.9, 10.4, 10.3, 10.1, 10.7, 10.2]
        df = make_daily_df(closes, opens=opens)
        ctx = make_ctx(change_pct=2.0)  # 今日阳
        cfg = KlineConfig(min_yang_ratio_4d=0.25)  # 只要1/4
        assert _check_yang_ratio(ctx, cfg, df)

    def test_today_not_yang_fails(self):
        """今日非阳线(跌) → 不通过"""
        closes = [10.0, 10.5, 10.3, 10.8, 10.2, 11.0]
        opens = [9.9, 10.4, 10.2, 10.7, 10.1, 10.9]
        df = make_daily_df(closes, opens=opens)
        ctx = make_ctx(change_pct=-1.0)  # 今日阴
        cfg = KlineConfig(min_yang_ratio_4d=0.25)
        assert not _check_yang_ratio(ctx, cfg, df)

    def test_insufficient_yang_history_fails(self):
        """近4天阳线不够 → 不通过"""
        closes = [10.0, 9.8, 9.6, 9.4, 9.2, 9.0]  # 全部下跌
        opens = [10.1, 10.0, 9.8, 9.6, 9.4, 9.2]  # 全部高开低走(阴)
        df = make_daily_df(closes, opens=opens)
        ctx = make_ctx(change_pct=1.0)  # 今日阳但历史差
        cfg = KlineConfig(min_yang_ratio_4d=0.25)
        assert not _check_yang_ratio(ctx, cfg, df)


class TestRound1CloseMomentum:
    def test_momentum_passes(self):
        """连续2天收盘涨 → 通过"""
        closes = [10.0, 9.9, 10.0, 10.2, 10.5, 10.8]
        df = make_daily_df(closes)
        cfg = KlineConfig(min_consecutive_close_rise=2)
        assert _check_close_momentum(make_ctx(), cfg, df)

    def test_no_momentum_fails(self):
        """无连续收盘涨 → 不通过"""
        closes = [10.0, 9.8, 9.6, 9.5]
        df = make_daily_df(closes)
        cfg = KlineConfig(min_consecutive_close_rise=1)
        assert not _check_close_momentum(make_ctx(), cfg, df)

    def test_min_zero_skips(self):
        """min=0 → 跳过检查"""
        closes = [10.0, 9.8]
        df = make_daily_df(closes)
        cfg = KlineConfig(min_consecutive_close_rise=0)
        assert _check_close_momentum(make_ctx(), cfg, df)


class TestRound1SingleDayPct:
    def test_normal_pct_passes(self):
        assert _check_single_day_pct(make_ctx(change_pct=3.0), KlineConfig(max_single_day_pct=6.5))

    def test_limit_up_fails(self):
        assert not _check_single_day_pct(make_ctx(change_pct=9.98), KlineConfig(max_single_day_pct=6.5))

    def test_negative_pct_passes(self):
        """下跌也通过 (不因跌幅淘汰)"""
        assert _check_single_day_pct(make_ctx(change_pct=-3.0), KlineConfig(max_single_day_pct=6.5))


class TestRound1PctVsATR:
    def test_pct_within_atr_passes(self):
        closes = [10.0 + i * 0.02 for i in range(25)]
        highs = [c + 0.3 for c in closes]
        lows = [c - 0.2 for c in closes]
        df = make_daily_df(closes, highs=highs, lows=lows)
        cfg = KlineConfig(max_atr_multiple=2.0)
        assert _check_pct_vs_atr(make_ctx(change_pct=1.0), cfg, df)

    def test_pct_exceeds_atr_fails(self):
        closes = [10.0] * 25
        highs = [10.5] * 25
        lows = [9.5] * 25
        df = make_daily_df(closes, highs=highs, lows=lows)
        cfg = KlineConfig(max_atr_multiple=2.0)
        # ATR ≈ 1.0, ATR% ≈ 10%. 10% * 2 = 20%. change_pct=30% > 20%
        assert not _check_pct_vs_atr(make_ctx(change_pct=50.0, price=10.0), cfg, df)


# ================================================================
# Round 2: 深度验证 (6项)
# ================================================================

class TestRound2NoSharpDrop:
    def test_normal_returns_passes(self):
        closes = [10.0, 10.5, 10.8, 11.2, 11.5]  # 加速涨
        df = make_daily_df(closes)
        assert _check_no_sharp_drop(make_ctx(), KlineConfig(), df)

    def test_sharp_drop_fails(self):
        # 需要4+条数据: +10%, +2%, -8% → -8% < 2%*0.5=1% → 骤降
        closes = [10.0, 11.0, 11.22, 10.32]
        df = make_daily_df(closes)
        assert not _check_no_sharp_drop(make_ctx(), KlineConfig(max_drop_ratio=0.5), df)

    def test_short_data_passes(self):
        df = make_daily_df([10.0, 11.0])
        assert _check_no_sharp_drop(make_ctx(), KlineConfig(), df)


class TestRound2NoContinuousDecline:
    def test_no_decline_passes(self):
        closes = [10.0, 10.5, 10.3, 11.0, 10.8, 12.0]  # 波动, 不连续递减
        df = make_daily_df(closes)
        assert _check_no_continuous_decline(make_ctx(), KlineConfig(), df)

    def test_continuous_decline_fails(self):
        # 需要6+条: 最近3天收益连续递减 (5% → 3% → 1%)
        closes = [10.0, 10.2, 10.5, 10.8, 10.95, 11.06]
        df = make_daily_df(closes)
        assert not _check_no_continuous_decline(make_ctx(), KlineConfig(), df)


class TestRound2NoBodyShrink:
    def test_consistent_bodies_passes(self):
        closes = [10.0, 10.5, 10.8, 11.2, 11.5, 12.0]
        opens = [9.8, 10.2, 10.5, 10.9, 11.2, 11.7]
        df = make_daily_df(closes, opens=opens)
        assert _check_no_body_shrink(make_ctx(), KlineConfig(), df)

    def test_shrinking_bodies_fails(self):
        closes = [10.0, 11.0, 11.5, 11.8, 11.9]
        opens = [9.5, 10.2, 10.9, 11.4, 11.6]  # bodies: 0.5, 0.8, 0.6, 0.4, 0.3 (后面连续缩小)
        df = make_daily_df(closes, opens=opens)
        assert not _check_no_body_shrink(make_ctx(), KlineConfig(), df)


class TestRound2HighBreak:
    def test_high_break_passes(self):
        closes = [10.0] * 6
        highs = [10.2, 10.3, 10.1, 10.5, 10.2, 10.3]  # 前3天最高10.5
        df = make_daily_df(closes, highs=highs)
        ctx = make_ctx(high=10.8)  # 今日高 > 前3天最高
        assert _check_high_break(ctx, KlineConfig(), df)

    def test_no_high_break_fails(self):
        highs = [10.2, 10.3, 10.1, 10.8, 10.2, 10.3]  # 前3天最高10.8
        df = make_daily_df([10.0] * 6, highs=highs)
        ctx = make_ctx(high=10.5)  # 今日高 < 前3天最高
        assert not _check_high_break(ctx, KlineConfig(), df)


class TestRound2CloseVsOpen:
    def test_close_above_prev_opens_passes(self):
        closes = [10.0] * 6
        opens = [9.8, 9.7, 9.6, 9.5, 9.8, 9.9]  # 前3天开盘: 9.5, 9.8, 9.9
        df = make_daily_df(closes, opens=opens)
        ctx = make_ctx(price=10.5)  # > 所有前3天开盘
        assert _check_close_vs_open(ctx, KlineConfig(), df)

    def test_close_below_prev_open_fails(self):
        opens = [9.8, 9.7, 9.6, 9.5, 10.5, 10.0]  # 前3天开盘含10.5
        df = make_daily_df([10.0] * 6, opens=opens)
        ctx = make_ctx(price=10.2)  # < 10.5
        assert not _check_close_vs_open(ctx, KlineConfig(), df)


class TestRound2UpperShadow:
    def test_small_shadow_passes(self):
        closes = [10.0] * 6
        opens = [9.9] * 6
        highs = [10.05] * 6  # body=0.1, upper_shadow=0.05, ratio=0.5 < 0.6
        df = make_daily_df(closes, opens=opens, highs=highs)
        assert _check_upper_shadow(make_ctx(), KlineConfig(), df)

    def test_long_shadow_fails(self):
        closes = [10.0] * 5 + [10.1]
        opens = [10.0] * 5 + [10.0]
        highs = [10.0] * 5 + [11.0]  # 上影线 0.9, 实体0.1 → 比值9.0 > 0.6
        df = make_daily_df(closes, opens=opens, highs=highs)
        assert not _check_upper_shadow(make_ctx(), KlineConfig(max_upper_shadow_ratio=0.6), df)


# ================================================================
# 集成测试
# ================================================================

def _make_healthy_price_series(n=30):
    """构造健康的日线序列: 满足全部R1策略要求
    末段4天连涨≥1.2% (动量), 整体连涨≤5, 近9天涨≤6, 近4天≥3阳"""
    closes = [10.0]
    # 前段 (indices 1-20): 温和交替
    for i in range(1, 21):
        if i % 3 == 0:
            closes.append(closes[-1] * 1.005)  # +0.5%
        else:
            closes.append(closes[-1] * 0.998)  # -0.2%
    # 末段 (indices 21-29): 构造满足动量+连涨约束的序列
    closes.append(closes[-1] * 0.995)   # 21: down
    closes.append(closes[-1] * 0.995)   # 22: down
    closes.append(closes[-1] * 0.995)   # 23: down
    for _ in range(5):                  # 24-28: 5连涨 (动量用末4天, 连涨=5≤5)
        closes.append(closes[-1] * 1.012)  # +1.2%
    closes.append(closes[-1] * 0.998)   # 29: down (打断连涨, 末4=26-29用27→28的涨)
    return closes


class TestScreenKlineIntegration:
    def test_full_pipeline_with_good_stock(self):
        """好股票完整通过两轮"""
        closes = _make_healthy_price_series(30)
        opens = [c - 0.05 for c in closes]  # 每天低开高走(阳线)
        highs = [c + 0.3 for c in closes]
        lows = [c - 0.2 for c in closes]
        df = make_daily_df(closes, opens=opens, highs=highs, lows=lows)
        daily_cache = {"000001": df}
        ctx = make_ctx(change_pct=1.5, price=closes[-1], high=highs[-1], open_p=opens[-1])

        cfg = KlineConfig(min_atr_pct=1.0)  # 放低ATR阈值
        result = screen_kline([ctx], cfg, daily_cache)
        assert len(result) == 1
        assert ctx.kline_passed

    def test_bad_stock_fails_round1(self):
        """差股票Round 1淘汰"""
        closes = [10.0, 9.8, 9.6, 9.4, 9.2, 9.0]  # 持续下跌
        opens = [10.1, 10.0, 9.8, 9.6, 9.4, 9.2]  # 全部阴线
        df = make_daily_df(closes, opens=opens)
        daily_cache = {"000001": df}
        ctx = make_ctx(change_pct=-1.0)  # 今日跌

        result = screen_kline([ctx], KlineConfig(), daily_cache)
        assert len(result) == 0
        assert not ctx.kline_passed

    def test_no_daily_data_passes_by_default(self):
        """无日线数据时默认通过 (不因数据缺失淘汰)"""
        ctx = make_ctx()
        result = screen_kline([ctx], KlineConfig(skip_on_missing_data=True), {})
        assert len(result) == 1

    def test_no_daily_data_fails_when_strict(self):
        """skip_on_missing_data=False 时淘汰"""
        ctx = make_ctx()
        result = screen_kline([ctx], KlineConfig(skip_on_missing_data=False), {})
        assert len(result) == 0

    def test_multiple_stocks_mixed(self):
        """混合批量测试"""
        # 好股票
        good_closes = _make_healthy_price_series(30)
        good_opens = [c - 0.03 for c in good_closes]
        good_highs = [c + 0.3 for c in good_closes]
        good_lows = [c - 0.2 for c in good_closes]
        good_df = make_daily_df(good_closes, opens=good_opens, highs=good_highs, lows=good_lows)

        # 差股票
        bad_closes = [10.0 - i * 0.1 for i in range(30)]  # 持续下跌
        bad_opens = [c + 0.05 for c in bad_closes]
        bad_df = make_daily_df(bad_closes, opens=bad_opens)

        daily_cache = {"000001": good_df, "000002": bad_df}
        good_ctx = make_ctx(code="000001", change_pct=2.0, price=good_closes[-1])
        bad_ctx = make_ctx(code="000002", change_pct=-2.0)

        cfg = KlineConfig(min_atr_pct=1.0)
        result = screen_kline([good_ctx, bad_ctx], cfg, daily_cache)
        assert len(result) == 1
        assert result[0].code == "000001"
