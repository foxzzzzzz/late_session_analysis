"""Pipeline live snapshot wiring tests."""
from unittest.mock import MagicMock, patch

from orchestration.config import SystemConfig
from orchestration.pipeline import LateSessionPipeline
from screening.context import StockContext


def make_pipeline():
    config = SystemConfig(enable_capital_flow=False)
    config.enable_live_snapshots = True
    config.live_snapshot_dir = "./live_snapshots_test"
    with patch.object(SystemConfig, "resolve_regime", return_value="neutral"):
        with patch.object(LateSessionPipeline, "_init_fetchers", return_value=MagicMock()):
            with patch("orchestration.pipeline.DataPreloader", return_value=MagicMock()):
                with patch("orchestration.pipeline.NewsFetcher", return_value=MagicMock()):
                    with patch("orchestration.pipeline.get_concept_analyzer", return_value=MagicMock()):
                        p = LateSessionPipeline(config, test_mode=True)
    p._snapshot_store = MagicMock()
    return p


def make_ctx(code="000001"):
    return StockContext(
        code=code,
        name=f"测试{code}",
        price=10.0,
        change_pct=2.0,
        turnover=100_000_000,
        turnover_rate=2.0,
        volume=1_000_000,
        high=10.2,
        low=9.8,
        open=9.9,
        pre_close=9.8,
        late_price_change=2.1,
        late_volume_ratio=1.8,
        last_5min_volume_pct=9.0,
        l2_passed=True,
        total_score=73.0,
        recommendation="buy",
    )


def test_write_stage_snapshot_serializes_contexts_and_metrics():
    pipeline = make_pipeline()
    ctx = make_ctx("000001")
    pipeline._5min_metrics = {"000001": {"late_price_change": 2.1}}

    pipeline._write_stage_snapshot(
        stage="S2",
        iteration=1,
        contexts=[ctx],
        input_codes=["000001", "000002"],
        passed_contexts=[ctx],
        filter_extra={"relaxed_capital": True},
        decision_time="14:57",
    )

    kwargs = pipeline._snapshot_store.write_stage_snapshot.call_args.kwargs
    assert kwargs["stage"] == "S2"
    assert kwargs["iteration"] == 1
    assert kwargs["codes"] == ["000001", "000002"]
    assert kwargs["quotes"][0]["code"] == "000001"
    assert kwargs["late_metrics"]["000001"]["late_price_change"] == 2.1
    assert kwargs["filter_result"]["relaxed_capital"] is True
    assert kwargs["filter_result"]["passed_codes"] == ["000001"]


def test_write_stage_snapshot_ignores_store_errors():
    pipeline = make_pipeline()
    pipeline._snapshot_store.write_stage_snapshot.side_effect = OSError("disk full")

    pipeline._write_stage_snapshot(
        stage="S4",
        iteration=2,
        contexts=[make_ctx("000001")],
        input_codes=["000001"],
        passed_contexts=[],
        filter_extra={},
    )

    pipeline._snapshot_store.write_stage_snapshot.assert_called_once()
