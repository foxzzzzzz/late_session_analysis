import json
from dataclasses import fields
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


def test_init_db_backfills_missing_regime_config_without_overwriting(tmp_path, monkeypatch):
    from web.models import db, SystemConfig as DbConfig

    app = make_web_app(tmp_path, monkeypatch)

    with app.app_context():
        bull_key = "live.regime.bull.l4_high_threshold"
        existing = DbConfig.query.filter_by(key=bull_key).first()
        assert existing is not None

        existing.value = "91.0"
        DbConfig.query.filter_by(key="live.regime.bear.l4_buy_threshold").delete()
        db.session.commit()

    restarted = make_web_app(tmp_path, monkeypatch)

    with restarted.app_context():
        preserved = DbConfig.query.filter_by(key=bull_key).first()
        backfilled = DbConfig.query.filter_by(key="live.regime.bear.l4_buy_threshold").first()

    assert preserved.value == "91.0"
    assert backfilled is not None
    assert backfilled.value == "62.0"


def test_web_seed_defaults_match_runtime_live_and_backtest_configs(tmp_path, monkeypatch):
    from orchestration.config import SystemConfig
    from backtest.config import BacktestConfig
    from web.models import SystemConfig as DbConfig

    app = make_web_app(tmp_path, monkeypatch)
    live = SystemConfig()
    backtest = BacktestConfig()

    with app.app_context():
        live_l4 = DbConfig.query.filter_by(key="live.l4_high_threshold").first()
        bt_l4 = DbConfig.query.filter_by(key="backtest.l4_high_threshold").first()
        model = DbConfig.query.filter_by(key="llm_model").first()
        bull_l4 = DbConfig.query.filter_by(key="live.regime.bull.l4_high_threshold").first()

    assert live_l4.value == str(live.l4_high_threshold)
    assert bt_l4.value == str(backtest.l4_high_threshold)
    assert live_l4.value != bt_l4.value
    assert model.value == live.llm_model
    assert bull_l4.value == str(live._regime_overrides["bull"]["l4_high_threshold"])


def test_all_web_seeded_defaults_match_runtime_config_instances(tmp_path, monkeypatch):
    from orchestration.config import SystemConfig
    from backtest.config import BacktestConfig
    from web.models import SystemConfig as DbConfig

    app = make_web_app(tmp_path, monkeypatch)
    live = SystemConfig()
    backtest = BacktestConfig()

    api_keys = {"llm_api_key", "llm_api_base", "llm_model"}
    schedule_keys = {"schedule_enabled", "schedule_time"}

    def expected_string(value):
        if isinstance(value, bool):
            return str(value).lower()
        if isinstance(value, (list, dict)):
            return json.dumps(value, ensure_ascii=False)
        return str(value) if value is not None else ""

    with app.app_context():
        for f in fields(SystemConfig):
            if f.name.startswith("_"):
                continue
            key = f.name if f.name in api_keys or f.name in schedule_keys else f"live.{f.name}"
            row = DbConfig.query.filter_by(key=key).first()
            assert row is not None, key
            assert row.value == expected_string(getattr(live, f.name)), key

        for f in fields(BacktestConfig):
            if f.name.startswith("_"):
                continue
            key = f"backtest.{f.name}"
            row = DbConfig.query.filter_by(key=key).first()
            assert row is not None, key
            assert row.value == expected_string(getattr(backtest, f.name)), key


def test_config_page_hides_shadowed_legacy_threshold_keys(tmp_path, monkeypatch):
    from web.models import db, SystemConfig as DbConfig
    from web.routes.config_thresholds import _hide_shadowed_legacy

    app = make_web_app(tmp_path, monkeypatch)

    with app.app_context():
        db.session.add(DbConfig(
            key="l4_high_threshold",
            value="999",
            value_type="float",
            category="threshold_live",
            description="legacy",
        ))
        db.session.commit()
        rows = DbConfig.query.filter(DbConfig.category.like("threshold_live%")).all()
        visible = _hide_shadowed_legacy(rows, "live")

    keys = {row.key for row in visible}
    assert "live.l4_high_threshold" in keys
    assert "l4_high_threshold" not in keys


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
