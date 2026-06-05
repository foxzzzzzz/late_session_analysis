import json
from datetime import date
from pathlib import Path

import pytest


def make_web_app(tmp_path, monkeypatch):
    monkeypatch.setenv("WEB_INSTANCE_DIR", str(tmp_path / "instance"))
    monkeypatch.setenv("WEB_SCHEDULER_ENABLED", "false")
    from web.app import create_app

    app = create_app()
    app.config.update(TESTING=True)
    return app


def test_seed_config_handles_default_factory_values():
    from web.db import _collect_dataclass_fields
    from orchestration.config import SystemConfig

    rows = {
        row["key"]: row
        for row in _collect_dataclass_fields(SystemConfig, category="threshold_live")
    }

    assert rows["data_providers"]["value_type"] == "json"
    assert json.loads(rows["data_providers"]["value"])[0] == "tencent"
    assert rows["target_sectors"]["value_type"] == "json"
    assert json.loads(rows["target_sectors"]["value"]) == []


def test_api_save_ignores_readonly_env_sync(tmp_path, monkeypatch):
    app = make_web_app(tmp_path, monkeypatch)

    def fail_write(*args, **kwargs):
        raise PermissionError("readonly")

    monkeypatch.setattr(Path, "write_text", fail_write)

    with app.test_client() as client:
        resp = client.post(
            "/config/api/save",
            json={"llm_model": "test-model", "llm_api_base": "https://example.test"},
        )

    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ok"


def test_backtest_service_uses_engine_run_metrics(tmp_path, monkeypatch):
    app = make_web_app(tmp_path, monkeypatch)

    class FakeEngine:
        def __init__(self, config):
            self.config = config
            self.trade_log = type(
                "TradeLog",
                (),
                {
                    "days": [],
                    "closed_trades": lambda self: [object(), object()],
                },
            )()

        def run(self):
            return {
                "metrics": {
                    "total_trades": 2,
                    "win_rate": 50.0,
                    "total_return_pct": 3.25,
                    "max_drawdown_pct": 1.2,
                    "sharpe_ratio": 1.1,
                    "calmar_ratio": 2.0,
                    "avg_return_pct": 1.625,
                },
                "total_trades": 2,
            }

    monkeypatch.setattr("backtest.engine.BacktestEngine", FakeEngine)

    from web.services.backtest_service import BacktestService

    with app.app_context():
        result = BacktestService().run(
            start_date="20260601",
            end_date="20260602",
            backtest_type="historical",
            capital_flow_mode="proxy",
        )

    assert result["status"] == "completed"
    assert result["trades_count"] == 2
    assert result["summary"]["total_trades"] == 2
    assert result["summary"]["cumulative_return"] == 3.25


def test_web_live_replay_fails_fast_until_snapshot_replay_is_implemented(tmp_path, monkeypatch):
    app = make_web_app(tmp_path, monkeypatch)

    from web.services.backtest_service import BacktestService

    with app.app_context():
        result = BacktestService().run(
            start_date="20260601",
            end_date="20260602",
            backtest_type="live_replay",
            capital_flow_mode="replay",
        )

    assert result["status"] == "error"
    assert "not implemented" in result["error"]


def test_pipeline_route_allows_rerun_after_failed_same_day(tmp_path, monkeypatch):
    app = make_web_app(tmp_path, monkeypatch)

    from web.models import db, PipelineRun

    with app.app_context():
        db.session.add(PipelineRun(trading_date=date.today(), status="failed"))
        db.session.commit()

    class NoopThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr("threading.Thread", NoopThread)

    with app.test_client() as client:
        resp = client.post("/pipeline/run", json={"test_mode": True})

    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ok"
