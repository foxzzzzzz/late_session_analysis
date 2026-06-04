"""Fund-flow metadata tests."""
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from orchestration.config import SystemConfig
from orchestration.pipeline import LateSessionPipeline
from screening.context import StockContext


class FakeFlowFetcher:
    def __init__(self, data):
        self.data = data

    def enrich_batch(self, codes):
        return {code: self.data[code] for code in codes if code in self.data}


def make_pipeline():
    config = SystemConfig(enable_capital_flow=True)
    config.enable_live_snapshots = False
    with patch.object(SystemConfig, "resolve_regime", return_value="neutral"):
        with patch.object(LateSessionPipeline, "_init_fetchers", return_value=MagicMock()):
            with patch("orchestration.pipeline.DataPreloader", return_value=MagicMock()):
                with patch("orchestration.pipeline.NewsFetcher", return_value=MagicMock()):
                    with patch("orchestration.pipeline.get_concept_analyzer", return_value=MagicMock()):
                        p = LateSessionPipeline(config, test_mode=True)
    p._sina_baseline = {}
    return p


def make_ctx(code="000001"):
    return StockContext(
        code=code,
        name="测试",
        price=10.0,
        change_pct=1.0,
        turnover=100_000_000,
        turnover_rate=2.0,
        volume=1_000_000,
        high=10.2,
        low=9.8,
        open=9.9,
        pre_close=9.8,
        vol_ratio=1.5,
    )


def test_current_minute_or_sina_flow_is_marked_realtime():
    today = datetime.now().strftime("%Y-%m-%d")
    pipeline = make_pipeline()
    pipeline._flow_minute = FakeFlowFetcher({
        "000001": {"mainForce": 300, "data_date": today}
    })
    pipeline._flow_sina = FakeFlowFetcher({
        "000001": {
            "mainForce": 200,
            "active_buy_ratio": 61.0,
            "data_date": today,
            "r0_in": 100,
            "r0_out": 50,
        }
    })
    pipeline._flow_push2his = None
    ctx = make_ctx("000001")

    pipeline._enrich_fund_flow([ctx])

    assert ctx.data_quality_flags["fund_flow"] is True
    assert ctx.data_quality_flags["fund_flow_source"] == "minute"
    assert ctx.data_quality_flags["fund_flow_data_date"] == today
    assert ctx.data_quality_flags["fund_flow_is_realtime"] is True
    assert "minute" in ctx.data_quality_flags["fund_flow_sources_seen"]
    assert "sina" in ctx.data_quality_flags["fund_flow_sources_seen"]


def test_stale_push2his_flow_is_not_marked_realtime():
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    pipeline = make_pipeline()
    pipeline._flow_minute = None
    pipeline._flow_sina = None
    pipeline._flow_push2his = FakeFlowFetcher({
        "000001": {"mainForce": 300, "active_buy_ratio": 60.0, "data_date": yesterday}
    })
    ctx = make_ctx("000001")

    pipeline._enrich_fund_flow([ctx])

    assert ctx.data_quality_flags["fund_flow"] is False
    assert ctx.data_quality_flags.get("fund_flow_source") != "push2his"
    assert ctx.data_quality_flags.get("fund_flow_is_realtime") is not True
