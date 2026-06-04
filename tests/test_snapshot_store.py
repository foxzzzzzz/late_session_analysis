"""Live snapshot store tests."""
import json

from data_provider.snapshot_store import SnapshotStore


def test_write_and_read_stage_snapshot(tmp_path):
    store = SnapshotStore(root=tmp_path, run_id="run-1")

    path = store.write_stage_snapshot(
        trading_date="20260604",
        stage="S2",
        iteration=1,
        codes=["000001", "600000"],
        quotes=[{"code": "000001", "price": 10.5}],
        late_metrics={"000001": {"late_price_change": 1.2}},
        fund_flow={"000001": {"mainForce": 120.0}},
        filter_result={"input_count": 2, "output_count": 1, "passed_codes": ["000001"]},
        data_quality={"source": "test"},
        decision_time="14:57",
        fetched_at="2026-06-04T14:52:01",
    )

    assert path.exists()
    raw = json.loads(path.read_text(encoding="utf-8").strip())
    assert raw["schema_version"] == 1
    assert raw["run_id"] == "run-1"
    assert raw["stage"] == "S2"

    records = store.read_stage_snapshots("20260604", "S2")

    assert len(records) == 1
    assert records[0]["codes"] == ["000001", "600000"]
    assert records[0]["late_metrics"]["000001"]["late_price_change"] == 1.2
    assert records[0]["filter_result"]["passed_codes"] == ["000001"]


def test_read_stage_snapshots_returns_empty_for_missing_stage(tmp_path):
    store = SnapshotStore(root=tmp_path, run_id="run-1")

    assert store.read_stage_snapshots("20260604", "S4") == []
