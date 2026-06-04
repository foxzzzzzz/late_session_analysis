"""回测引擎关键路径测试 — 止损止盈、14:59截断、板块回填

回测引擎的上次优化加入了止损止盈风控和板块强度回填, 但完全未经测试。
这些测试模拟 T+1 退出逻辑, 无需真实历史数据。
"""
import pytest
from unittest.mock import MagicMock, patch

import pandas as pd

from backtest.engine import BacktestEngine
from backtest.config import BacktestConfig
from screening.context import StockContext
from backtest.trade_log import Trade
from backtest.data_loader import BacktestDataLoader, compute_s2_metrics


# ================================================================
# 止损/止盈 优先级测试
# ================================================================

def make_buy_ctx(code="000001", price=10.0):
    """构造买入信号 context"""
    return StockContext(
        code=code, name=f"测试{code}", price=price,
        change_pct=3.0, turnover=500_000_000, turnover_rate=3.0,
        volume=10_000_000, high=price * 1.03, low=price * 0.97,
        open=price * 0.99, pre_close=price * 0.98,
    )


def make_next_snapshot_df(codes_prices: dict):
    """构造次日快照 DataFrame, 格式与 DataLoader.get_daily_snapshot 一致

    codes_prices: {code: {"open": x, "high": x, "low": x}}
    """
    rows = []
    for code, prices in codes_prices.items():
        rows.append({
            "code": code,
            "open": prices["open"],
            "high": prices["high"],
            "low": prices["low"],
            "开盘": prices["open"],
            "最高": prices["high"],
            "最低": prices["low"],
        })
    return pd.DataFrame(rows)


class TestStopLossTakeProfit:
    def test_stop_loss_triggers(self):
        """次日最低价触及止损价 → exit_reason='stop_loss'"""
        config = BacktestConfig(stop_loss_pct=-5.0, take_profit_pct=5.0,
                                slippage_bps=0, commission_rate=0)  # 0滑点便于验证
        engine = BacktestEngine(config)
        engine.loader = MagicMock()
        engine.daily_bars = {}  # _calculate_trades 需要此属性

        ctx = make_buy_ctx("000001", price=10.0)
        # entry = 10.0 (0滑点), stop_price = 10.0 * 0.95 = 9.5
        # low=9.4 → 触及止损
        next_df = make_next_snapshot_df({"000001": {"open": 9.7, "high": 9.8, "low": 9.4}})
        engine.loader.get_daily_snapshot.return_value = next_df

        trades = engine._calculate_trades([ctx], "20260401", "20260402")

        assert len(trades) == 1
        assert trades[0].exit_reason == "stop_loss"
        assert trades[0].code == "000001"

    def test_take_profit_triggers(self):
        """次日最高价触及止盈价 → exit_reason='take_profit'"""
        config = BacktestConfig(stop_loss_pct=-5.0, take_profit_pct=5.0,
                                slippage_bps=0, commission_rate=0)
        engine = BacktestEngine(config)
        engine.loader = MagicMock()
        engine.daily_bars = {}

        ctx = make_buy_ctx("000001", price=10.0)
        # entry = 10.0, profit_price = 10.0 * 1.05 = 10.5
        # high=10.6 → 触及止盈, low=9.8 → 未触及止损
        next_df = make_next_snapshot_df({"000001": {"open": 10.2, "high": 10.6, "low": 9.8}})
        engine.loader.get_daily_snapshot.return_value = next_df

        trades = engine._calculate_trades([ctx], "20260401", "20260402")

        assert len(trades) == 1
        assert trades[0].exit_reason == "take_profit"

    def test_next_open_when_neither_triggers(self):
        """止损止盈都未触发 → exit_reason='next_open'"""
        config = BacktestConfig(stop_loss_pct=-5.0, take_profit_pct=5.0,
                                slippage_bps=0, commission_rate=0)
        engine = BacktestEngine(config)
        engine.loader = MagicMock()
        engine.daily_bars = {}

        ctx = make_buy_ctx("000001", price=10.0)
        # entry = 10.0, stop=9.5, profit=10.5
        # high=10.3, low=9.7 → 都未触及
        next_df = make_next_snapshot_df({"000001": {"open": 10.1, "high": 10.3, "low": 9.7}})
        engine.loader.get_daily_snapshot.return_value = next_df

        trades = engine._calculate_trades([ctx], "20260401", "20260402")

        assert len(trades) == 1
        assert trades[0].exit_reason == "next_open"

    def test_stop_loss_priority_over_take_profit(self):
        """low触及止损 AND high触及止盈 → 止损优先 (日内先跌后涨)"""
        config = BacktestConfig(stop_loss_pct=-5.0, take_profit_pct=5.0,
                                slippage_bps=0, commission_rate=0)
        engine = BacktestEngine(config)
        engine.loader = MagicMock()
        engine.daily_bars = {}

        ctx = make_buy_ctx("000001", price=10.0)
        # entry = 10.0, stop_price=9.5, profit_price=10.5
        # low=9.3 (触及止损), high=10.8 (触及止盈) → 止损优先
        next_df = make_next_snapshot_df({"000001": {"open": 9.6, "high": 10.8, "low": 9.3}})
        engine.loader.get_daily_snapshot.return_value = next_df

        trades = engine._calculate_trades([ctx], "20260401", "20260402")

        assert len(trades) == 1
        assert trades[0].exit_reason == "stop_loss"

    def test_stop_loss_exact_boundary(self):
        """low 恰好等于 stop_price → 触发止损 (≤)"""
        config = BacktestConfig(stop_loss_pct=-5.0, take_profit_pct=5.0,
                                slippage_bps=0, commission_rate=0)
        engine = BacktestEngine(config)
        engine.loader = MagicMock()
        engine.daily_bars = {}

        ctx = make_buy_ctx("000001", price=10.0)
        # stop_price = 9.5, low 恰好 9.5
        next_df = make_next_snapshot_df({"000001": {"open": 10.0, "high": 10.1, "low": 9.5}})
        engine.loader.get_daily_snapshot.return_value = next_df

        trades = engine._calculate_trades([ctx], "20260401", "20260402")

        assert len(trades) == 1
        assert trades[0].exit_reason == "stop_loss"

    def test_slippage_applied_to_entry_and_exit(self):
        """验证滑点对买入价和卖出价的影响"""
        config = BacktestConfig(stop_loss_pct=-5.0, take_profit_pct=5.0,
                                slippage_bps=5.0, commission_rate=0.025)
        engine = BacktestEngine(config)
        engine.loader = MagicMock()
        engine.daily_bars = {}

        ctx = make_buy_ctx("000001", price=10.0)
        next_df = make_next_snapshot_df({"000001": {"open": 10.1, "high": 10.3, "low": 9.7}})
        engine.loader.get_daily_snapshot.return_value = next_df

        trades = engine._calculate_trades([ctx], "20260401", "20260402")

        assert len(trades) == 1
        # entry = 10.0 * (1 + 5/10000) = 10.005
        # exit = 10.1 * (1 - 5/10000) = 10.09495
        # return_pct 应包含滑点 + 佣金影响
        assert trades[0].return_pct != 0.0


# ================================================================
# 14:59 cutoff
# ================================================================

class TestCutoff1459:
    """验证 5-min bar 截断到 14:59"""

    def test_bars_after_1459_excluded(self):
        """14:59 之后的 bar 被过滤, 14:55 的保留"""
        date_str = "20260401"
        cutoff = pd.Timestamp("2026-04-01 14:59:00")

        # 构造含 15:00 bar 的 5分钟线
        df = pd.DataFrame({
            "time": pd.to_datetime([
                "2026-04-01 14:50:00", "2026-04-01 14:55:00", "2026-04-01 15:00:00"
            ]),
            "open": [10.0, 10.1, 10.2],
            "close": [10.1, 10.2, 10.3],
            "high": [10.15, 10.25, 10.35],
            "low": [9.95, 10.05, 10.15],
            "vol": [1000, 2000, 3000],
            "amount": [10000, 20000, 30000],
        })

        time_col = pd.to_datetime(df.iloc[:, 0])
        df_cut = df[time_col <= cutoff]

        # 15:00 bar 被排除, 14:50 和 14:55 保留
        assert len(df_cut) == 2
        assert "14:50" in str(df_cut.iloc[0, 0])
        assert "14:55" in str(df_cut.iloc[1, 0])

    def test_all_bars_before_1459_preserved(self):
        """所有 bar 都在 14:59 之前 → 全部保留"""
        date_str = "20260401"
        cutoff = pd.Timestamp("2026-04-01 14:59:00")

        df = pd.DataFrame({
            "time": pd.to_datetime([
                "2026-04-01 13:00:00", "2026-04-01 14:30:00", "2026-04-01 14:55:00"
            ]),
            "open": [9.9, 10.0, 10.1],
            "close": [10.0, 10.1, 10.2],
            "high": [10.05, 10.15, 10.25],
            "low": [9.85, 9.95, 10.05],
            "vol": [1000, 1000, 1000],
            "amount": [10000, 10000, 10000],
        })

        time_col = pd.to_datetime(df.iloc[:, 0])
        df_cut = df[time_col <= cutoff]

        assert len(df_cut) == 3

    def test_decision_time_cutoff_excludes_later_visible_bars(self):
        """配置 decision_time=14:55 时, 14:55 之后的bar不能进入S2回测"""
        config = BacktestConfig()
        config.decision_time = "14:55"
        engine = BacktestEngine(config)

        cutoff = engine._decision_cutoff("20260401")
        df = pd.DataFrame({
            "time": pd.to_datetime([
                "2026-04-01 14:50:00",
                "2026-04-01 14:55:00",
                "2026-04-01 15:00:00",
            ]),
            "open": [10.0, 10.1, 10.2],
            "close": [10.1, 10.2, 10.3],
        })

        df_cut = df[pd.to_datetime(df["time"]) <= cutoff]

        assert len(df_cut) == 2
        assert str(cutoff) == "2026-04-01 14:55:00"


class TestBacktestS2Metrics:
    def test_1400_to_1425_high_counts_as_pre_late_high(self):
        """回测S2也应把14:00-14:25高点计入突破基准"""
        df = pd.DataFrame({
            "time": pd.to_datetime([
                "2026-04-01 09:35:00",
                "2026-04-01 13:05:00",
                "2026-04-01 14:05:00",
                "2026-04-01 14:30:00",
                "2026-04-01 14:55:00",
            ]),
            "open": [10.0] * 5,
            "close": [10.0] * 5,
            "high": [10.0, 10.0, 12.0, 11.0, 11.0],
            "low": [9.8] * 5,
            "volume": [1000] * 5,
            "turnover": [10000] * 5,
        })

        metrics = compute_s2_metrics(df)

        assert metrics["intraday_high"] == pytest.approx(12.0)
        assert metrics["broke_high"] is False

    def test_late_volume_ratio_uses_latest_late_bar(self):
        """回测S2量比口径应与实盘一致：最新尾盘bar / 13:00-14:30均bar"""
        df = pd.DataFrame({
            "time": pd.to_datetime([
                "2026-04-01 13:05:00",
                "2026-04-01 13:10:00",
                "2026-04-01 14:30:00",
                "2026-04-01 14:55:00",
            ]),
            "open": [10.0] * 4,
            "close": [10.0] * 4,
            "high": [10.1] * 4,
            "low": [9.9] * 4,
            "volume": [1000, 1000, 1000, 3000],
            "turnover": [10000, 10000, 10000, 30000],
        })

        metrics = compute_s2_metrics(df)

        assert metrics["late_volume_ratio"] == pytest.approx(3.0)


class TestBacktestModes:
    def test_backtest_config_accepts_new_modes(self):
        config = BacktestConfig()
        config.backtest_type = "live_replay"
        config.capital_flow_mode = "replay"
        config.live_snapshot_dir = "./snapshots"

        assert config.backtest_type == "live_replay"
        assert config.capital_flow_mode == "replay"
        assert config.live_snapshot_dir == "./snapshots"

    def test_proxy_capital_flow_marks_context_as_proxy(self):
        config = BacktestConfig()
        config.capital_flow_mode = "proxy"
        engine = BacktestEngine(config)
        ctx = StockContext(
            code="000001",
            name="测试",
            turnover=100_000_000,
            late_volume_ratio=2.0,
            late_price_change=1.5,
        )

        engine._apply_proxy_capital_flow([ctx])

        assert ctx.big_order_net > 0
        assert ctx.big_order_ratio > 0
        assert ctx.active_buy_ratio > 50
        assert ctx.data_quality_flags["fund_flow_source"] == "proxy"
        assert ctx.data_quality_flags["fund_flow_is_realtime"] is False


# ================================================================
# 板块强度回填
# ================================================================

class TestSectorPerf:
    def test_compute_sector_perf_normal(self):
        """多板块多股票 → 正确计算平均涨跌幅"""
        config = BacktestConfig()
        engine = BacktestEngine(config)
        engine.sector_codes = {
            "半导体": ["000001", "000002"],
            "元器件": ["000003"],
        }

        snapshot = pd.DataFrame({
            "code": ["000001", "000002", "000003"],
            "change_pct": [3.0, 5.0, -1.0],
        })

        result = engine._compute_sector_perf(snapshot)

        assert "半导体" in result
        assert "元器件" in result
        assert result["半导体"] == pytest.approx(4.0)  # (3+5)/2
        assert result["元器件"] == pytest.approx(-1.0)

    def test_compute_sector_perf_empty_snapshot(self):
        """空快照 → 返回空 dict"""
        config = BacktestConfig()
        engine = BacktestEngine(config)
        engine.sector_codes = {"半导体": ["000001"]}

        result = engine._compute_sector_perf(pd.DataFrame())

        assert result == {}

    def test_compute_sector_perf_no_sector_codes(self):
        """无板块映射 → 返回空 dict"""
        config = BacktestConfig()
        engine = BacktestEngine(config)
        engine.sector_codes = {}

        snapshot = pd.DataFrame({
            "code": ["000001"], "change_pct": [3.0],
        })

        result = engine._compute_sector_perf(snapshot)

        assert result == {}

    def test_compute_sector_perf_missing_codes(self):
        """板块成分股不在快照中 → 跳过该板块"""
        config = BacktestConfig()
        engine = BacktestEngine(config)
        engine.sector_codes = {
            "半导体": ["000001", "999999"],  # 999999 不在快照中
        }

        snapshot = pd.DataFrame({
            "code": ["000001"], "change_pct": [3.0],
        })

        result = engine._compute_sector_perf(snapshot)

        # 半导体板块只有 000001 在快照中, 平均 = 3.0
        assert result.get("半导体") == pytest.approx(3.0)

    def test_compute_sector_perf_uses_fallback_column(self):
        """快照使用中文列名 '涨跌幅' 而非 'change_pct'"""
        config = BacktestConfig()
        engine = BacktestEngine(config)
        engine.sector_codes = {"半导体": ["000001"]}

        snapshot = pd.DataFrame({
            "code": ["000001"], "涨跌幅": [4.5],
        })

        result = engine._compute_sector_perf(snapshot)

        assert result.get("半导体") == pytest.approx(4.5)


# ================================================================
# 无次日数据 → 空 trades
# ================================================================

class TestMissingNextDay:
    def test_no_next_date_returns_empty(self):
        """next_date 为 None → 无 trades"""
        config = BacktestConfig()
        engine = BacktestEngine(config)
        engine.loader = MagicMock()
        engine.daily_bars = {}

        ctx = make_buy_ctx("000001")
        trades = engine._calculate_trades([ctx], "20260401", None)

        assert trades == []

    def test_empty_next_snapshot_returns_empty(self):
        """次日无数据 → 无 trades"""
        config = BacktestConfig()
        engine = BacktestEngine(config)
        engine.loader = MagicMock()
        engine.daily_bars = {}
        engine.loader.get_daily_snapshot.return_value = pd.DataFrame()

        ctx = make_buy_ctx("000001")
        trades = engine._calculate_trades([ctx], "20260401", "20260402")

        assert trades == []

    def test_code_not_in_next_snapshot_skipped(self):
        """code 不在次日快照中 → 跳过该股票"""
        config = BacktestConfig(stop_loss_pct=-5.0, take_profit_pct=5.0,
                                slippage_bps=0, commission_rate=0)
        engine = BacktestEngine(config)
        engine.loader = MagicMock()
        engine.daily_bars = {}

        ctx = make_buy_ctx("000001", price=10.0)
        # 次日快照中没有 000001
        next_df = make_next_snapshot_df({"000002": {"open": 10.0, "high": 10.5, "low": 9.5}})
        engine.loader.get_daily_snapshot.return_value = next_df

        trades = engine._calculate_trades([ctx], "20260401", "20260402")

        assert trades == []

    def test_max_positions_limit(self):
        """超过 max_positions 的买入信号被截断"""
        config = BacktestConfig(stop_loss_pct=-5.0, take_profit_pct=5.0,
                                slippage_bps=0, commission_rate=0, max_positions=2)
        engine = BacktestEngine(config)
        engine.loader = MagicMock()
        engine.daily_bars = {}

        ctx1 = make_buy_ctx("000001", price=10.0)
        ctx2 = make_buy_ctx("000002", price=20.0)
        ctx3 = make_buy_ctx("000003", price=30.0)

        next_df = make_next_snapshot_df({
            "000001": {"open": 10.1, "high": 10.5, "low": 9.8},
            "000002": {"open": 20.2, "high": 21.0, "low": 19.5},
            "000003": {"open": 30.3, "high": 31.0, "low": 29.5},
        })
        engine.loader.get_daily_snapshot.return_value = next_df

        trades = engine._calculate_trades([ctx1, ctx2, ctx3], "20260401", "20260402")

        # max_positions=2 → 只产生2笔交易
        assert len(trades) == 2
