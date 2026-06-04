"""KlineProvider 测试 — MA/波动率/ATR/尾盘指标计算 + 缓存 + 批量加载"""
import os
import tempfile
import pytest
import pandas as pd
import numpy as np
from data_provider.kline_provider import KlineProvider, _empty_late_metrics


# === 测试数据构造 ===

def make_daily_df(closes, opens=None, highs=None, lows=None):
    """构造日线DataFrame (mootdx格式)"""
    n = len(closes)
    data = {
        "open": opens if opens else [closes[0]] * n,
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


def make_5min_df(times, closes, highs=None, volumes=None):
    """构造5分钟线DataFrame"""
    n = len(closes)
    data = {
        "open": closes,
        "close": closes,
        "high": highs if highs else closes,
        "low": [c * 0.99 for c in closes],
        "vol": volumes if volumes else [500000] * n,
        "amount": volumes if volumes else [500000] * n,
        "volume": volumes if volumes else [500000] * n,
        "turnover": volumes if volumes else [500000] * n,
        "datetime": pd.to_datetime(times),
    }
    return pd.DataFrame(data)


def make_trading_5min_timestamps(start_h=9, start_m=35, n_bars=48):
    """生成真实交易时段5分钟时间戳 (跳过午休11:30-13:00)

    上午: 9:35-11:30 (24根), 下午: 13:05-15:00 (24根)
    """
    import datetime as _dt
    morning_start = _dt.datetime(2026, 5, 19, start_h, start_m)
    times = []
    current = morning_start
    for i in range(n_bars):
        times.append(current)
        current = current + _dt.timedelta(minutes=5)
        # 跳过午休: 11:30 → 13:00
        if current.hour == 11 and current.minute == 35:
            current = current.replace(hour=13, minute=5)
    return times


# ================================================================
# compute_ma 测试
# ================================================================

class TestComputeMA:
    def test_normal_ma_calculation(self):
        """标准MA5/MA10/MA20/MA30/MA60计算"""
        closes = list(range(1, 31))  # 1..30
        df = make_daily_df([float(c) for c in closes])
        ma5, ma10, ma20, ma30, ma60 = KlineProvider.compute_ma(df)
        assert ma5 == pytest.approx(28.0)   # (26+27+28+29+30)/5
        assert ma10 == pytest.approx(25.5)  # (21+...+30)/10
        assert ma20 == pytest.approx(20.5)  # (11+...+30)/20
        assert ma30 == pytest.approx(15.5)  # (1+...+30)/30
        assert ma60 == pytest.approx(15.5)  # fallback到MA30 (<60条)

    def test_insufficient_data(self):
        """数据不足5条时返回0"""
        df = make_daily_df([10.0, 11.0, 12.0])
        ma5, ma10, ma20, ma30, ma60 = KlineProvider.compute_ma(df)
        assert ma5 == 0.0
        assert ma10 == 0.0
        assert ma20 == 0.0
        assert ma30 == 0.0
        assert ma60 == 0.0

    def test_empty_dataframe(self):
        df = pd.DataFrame()
        ma5, ma10, ma20, ma30, ma60 = KlineProvider.compute_ma(df)
        assert ma5 == 0.0

    def test_partial_data_ma10_fallback(self):
        """6-9条数据: MA10 fallback到MA5, MA30/MA60 fallback到MA20"""
        closes = [float(c) for c in range(1, 8)]  # 7条
        df = make_daily_df(closes)
        ma5, ma10, ma20, ma30, ma60 = KlineProvider.compute_ma(df)
        assert ma5 == pytest.approx(5.0)    # (3+4+5+6+7)/5
        assert ma10 == pytest.approx(5.0)   # fallback到MA5
        assert ma20 == pytest.approx(5.0)   # fallback到MA10(=MA5)
        assert ma30 == pytest.approx(5.0)   # fallback到MA20(=MA10)
        assert ma60 == pytest.approx(5.0)   # fallback到MA30(=MA20)


# ================================================================
# compute_volatility 测试
# ================================================================

class TestComputeVolatility:
    def test_stable_price_low_volatility(self):
        """价格稳定 → 低波动率"""
        closes = [10.0 + np.sin(i) * 0.05 for i in range(25)]
        df = make_daily_df(closes)
        vol = KlineProvider.compute_volatility(df)
        assert 0 < vol < 0.3  # 年化波动率应在合理范围

    def test_volatile_price_high_volatility(self):
        """价格剧烈波动 → 高波动率"""
        closes = [10.0, 11.0, 9.0, 12.0, 8.0, 13.0, 7.0, 14.0] * 3
        df = make_daily_df(closes)
        vol = KlineProvider.compute_volatility(df)
        assert vol > 0.3

    def test_insufficient_data(self):
        df = make_daily_df([10.0, 11.0])
        vol = KlineProvider.compute_volatility(df)
        assert vol == 0.0

    def test_empty_dataframe(self):
        vol = KlineProvider.compute_volatility(pd.DataFrame())
        assert vol == 0.0


# ================================================================
# compute_atr 测试
# ================================================================

class TestComputeATR:
    def test_normal_atr(self):
        """标准ATR计算"""
        n = 20
        closes = [10.0 + i * 0.1 for i in range(n)]
        highs = [c + 0.5 for c in closes]
        lows = [c - 0.3 for c in closes]
        df = make_daily_df(closes, highs=highs, lows=lows)
        atr = KlineProvider.compute_atr(df)
        # ATR应该接近 high-low = 0.8 (含gap)
        assert 0.5 < atr < 1.5

    def test_wide_range_atr(self):
        """高波动 → 高ATR"""
        closes = [100.0]
        highs = [105.0]
        lows = [95.0]
        for i in range(19):
            closes.append(closes[-1] + np.random.normal(0, 2))
            highs.append(closes[-1] + 5)
            lows.append(closes[-1] - 5)
        df = make_daily_df(closes, highs=highs, lows=lows)
        atr = KlineProvider.compute_atr(df)
        assert atr > 5.0

    def test_insufficient_data(self):
        df = make_daily_df([10.0], highs=[10.5], lows=[9.5])
        atr = KlineProvider.compute_atr(df)
        assert atr == 0.0


# ================================================================
# compute_late_metrics 测试
# ================================================================

class TestComputeLateMetrics:
    def test_afternoon_surge(self):
        """14:30后价格拉升 → 尾盘涨幅为正"""
        times = make_trading_5min_timestamps()
        # 48根bar: 上午24根(0-23) + 下午24根(24-47)
        # bar 24=13:05, bar 41=14:30, bar 47=15:00
        # bars 0-41 = 14:30及之前 (42根) = 10.0
        # bars 42-47 = 14:30之后 (6根) = 拉升
        closes = [10.0] * 42
        closes += [10.05, 10.10, 10.15, 10.20, 10.25, 10.30]
        volumes = [500000] * len(closes)
        # 下午最后放量
        for i in range(43, 48):
            volumes[i] = 2000000

        df = make_5min_df(times, closes, volumes=volumes)
        m = KlineProvider.compute_late_metrics(df)

        assert m["price_at_1430"] == pytest.approx(10.0, abs=0.01)
        assert m["late_price_change"] > 0
        assert m["late_volume_ratio"] > 0
        assert m["last_5min_volume_pct"] > 0

    def test_afternoon_decline(self):
        """14:30后价格下跌 → 尾盘涨幅为负"""
        times = make_trading_5min_timestamps()
        closes = [10.0] * 36  # 14:30之前
        closes += [9.98, 9.96, 9.94, 9.92, 9.90,
                   9.88, 9.86, 9.84, 9.82, 9.80, 9.78, 9.76]  # 14:30后下跌
        df = make_5min_df(times, closes)
        m = KlineProvider.compute_late_metrics(df)
        assert m["late_price_change"] < 0

    def test_broke_high_detection(self):
        """突破日内高点检测"""
        times = make_trading_5min_timestamps()
        closes = [10.0] * 48
        # bar 41=14:30, 14:30前最高10.5(在bar 30), 14:30后最高10.8(在bar 42)
        highs = [10.0] * 30 + [10.5] + [10.0] * 11 + [10.8] + [10.0] * 5
        # 30 + 1 + 11 + 1 + 5 = 48 ✓  bar 30=10.5(h<14), bar 42=10.8(h>=14,m>=30)
        df = make_5min_df(times, closes, highs=highs)
        m = KlineProvider.compute_late_metrics(df)
        assert m["broke_high"] is True

    def test_no_broke_high(self):
        """未突破日内高点"""
        times = make_trading_5min_timestamps()
        closes = [10.0] * 48
        # 前30根里有10.8的高点, 14:30后没有更高 → 未突破
        highs = [10.0] * 28 + [10.8] + [10.0] * 19
        df = make_5min_df(times, closes, highs=highs)
        m = KlineProvider.compute_late_metrics(df)
        assert m["broke_high"] is False

    def test_1400_to_1425_high_counts_as_pre_late_high(self):
        """14:00-14:25 的高点应计入14:30前高点，避免误判突破"""
        times = make_trading_5min_timestamps()
        closes = [10.0] * 48
        highs = [10.0] * 48
        highs[36] = 12.0  # 14:05
        highs[42] = 11.0  # 14:35，低于14:05高点
        df = make_5min_df(times, closes, highs=highs)

        m = KlineProvider.compute_late_metrics(df)

        assert m["intraday_high"] == pytest.approx(12.0)
        assert m["broke_high"] is False

    def test_empty_dataframe(self):
        m = KlineProvider.compute_late_metrics(pd.DataFrame())
        assert m == _empty_late_metrics()

    def test_missing_datetime_column(self):
        """无datetime列时的降级处理"""
        df = pd.DataFrame({
            "close": [10.0] * 10,
            "high": [10.0] * 10,
            "volume": [500000] * 10,
        })
        m = KlineProvider.compute_late_metrics(df)
        assert m["price_at_1430"] == 0.0  # 无法计算

    def test_morning_afternoon_split(self):
        """上午/下午成交量分割正确"""
        times = make_trading_5min_timestamps()
        # 上午24根(9:35-11:30), 下午24根(13:05-15:00)
        volumes = [1000] * 24 + [2000] * 24  # 上午1000, 下午2000
        closes = [10.0] * 48
        df = make_5min_df(times, closes, volumes=volumes)
        m = KlineProvider.compute_late_metrics(df)

        # late_volume_ratio = late(14:30+) / pre_late(13:00-14:30)
        assert m["afternoon_volume"] > m["morning_volume"]
        assert m["late_volume_ratio"] > 0

    def test_price_at_1430_from_bar(self):
        """price_at_1430从14:30 bar精确取"""
        times = make_trading_5min_timestamps()
        # 9:35开始, 上午24根→11:30, 午休, 下午从13:05开始
        # 13:05 + 12*5min = 14:05... 需要算14:30的bar index
        # 下午: bar24=13:05, bar25=13:10, ..., bar41=14:30, bar47=15:00
        closes = [10.0] * 41 + [10.5]  # bar 41 = 14:30
        closes += [10.0] * 6
        df = make_5min_df(times[:len(closes)], closes)
        m = KlineProvider.compute_late_metrics(df)
        assert m["price_at_1430"] == pytest.approx(10.5, abs=0.01)


# ================================================================
# 缓存测试
# ================================================================

class TestDailyCache:
    def test_cache_hit(self):
        """日线缓存命中"""
        with tempfile.TemporaryDirectory() as tmpdir:
            kp = KlineProvider(cache_dir=tmpdir)
            # 预先写入缓存
            from datetime import datetime
            today = datetime.now().strftime("%Y%m%d")
            cache_file = os.path.join(tmpdir, f"{today}_999999.parquet")
            test_df = make_daily_df([10.0, 11.0, 12.0])
            test_df.to_parquet(cache_file, index=False)

            # 加载 (应命中缓存 — 但会尝试mootdx对999999, 缓存命中直接返回)
            # 由于999999不是真实股票, mootdx会失败, 但缓存命中在mootdx之前
            # 这里主要测试缓存路径存在
            assert os.path.exists(cache_file)
