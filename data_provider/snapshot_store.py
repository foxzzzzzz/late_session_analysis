"""Live-visible data snapshot persistence.

The store is intentionally small and append-friendly: every stage iteration is
written as one JSON Lines record so live runs can be replayed later without a
database dependency.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from typing import Any


class SnapshotStore:
    """Persist and read live pipeline stage snapshots."""

    schema_version = 1

    def __init__(self, root: str | Path = "live_snapshots", run_id: str | None = None):
        self.root = Path(root)
        self.run_id = run_id or datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid4().hex[:8]

    def build_record(
        self,
        *,
        trading_date: str,
        stage: str,
        iteration: int,
        codes: list[str] | None = None,
        quotes: list[dict[str, Any]] | None = None,
        late_metrics: dict[str, Any] | None = None,
        fund_flow: dict[str, Any] | None = None,
        filter_result: dict[str, Any] | None = None,
        data_quality: dict[str, Any] | None = None,
        decision_time: str = "",
        fetched_at: str | None = None,
    ) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "trading_date": trading_date,
            "stage": stage,
            "iteration": iteration,
            "fetched_at": fetched_at or datetime.now().isoformat(timespec="seconds"),
            "decision_time": decision_time,
            "codes": codes or [],
            "quotes": quotes or [],
            "late_metrics": late_metrics or {},
            "fund_flow": fund_flow or {},
            "filter_result": filter_result or {},
            "data_quality": data_quality or {},
        }

    def write_stage_snapshot(self, **kwargs) -> Path:
        record = self.build_record(**kwargs)
        fetched_at = str(record["fetched_at"]).replace("-", "").replace(":", "").replace("T", "_")
        filename = f"{fetched_at}_round{record['iteration']}.jsonl"
        path = self.root / record["trading_date"] / record["stage"] / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        return path

    def read_stage_snapshots(self, trading_date: str, stage: str) -> list[dict[str, Any]]:
        stage_dir = self.root / trading_date / stage
        if not stage_dir.exists():
            return []
        records: list[dict[str, Any]] = []
        for path in sorted(stage_dir.glob("*.jsonl")):
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        records.append(json.loads(line))
        return records
