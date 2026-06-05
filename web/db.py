"""DB initialisation and seed-data helpers."""

from __future__ import annotations

import os
import json
from dataclasses import MISSING
from dataclasses import fields, is_dataclass
from typing import Any

from flask import Flask
from flask_sqlalchemy import SQLAlchemy

from web.models import db, SystemConfig as DbConfig

# re-export for convenience
__all__ = ["db", "init_db", "seed_system_config"]


def _collect_dataclass_fields(
    dataclass_cls: Any,
    prefix: str = "",
    category: str = "threshold",
    overrides: dict[str, tuple[str, str]] | None = None,
) -> list[dict[str, str]]:
    """Walk dataclass fields (including nested) and return seed rows."""
    rows: list[dict[str, str]] = []
    overrides = overrides or {}

    for f in fields(dataclass_cls):
        key = f"{prefix}{f.name}" if prefix else f.name
        if f.default is not MISSING:
            default = f.default
        elif f.default_factory is not MISSING:  # type: ignore[attr-defined]
            default = f.default_factory()  # type: ignore[misc]
        else:
            default = ""

        # skip internal / private fields
        if f.name.startswith("_") or key.startswith("_"):
            continue
        if isinstance(default, type):
            continue

        # nested dataclass? → recurse
        if is_dataclass(type(default)):
            rows.extend(
                _collect_dataclass_fields(type(default), f"{key}_", category, overrides)
            )
            continue

        # determine type & category
        desc = key
        cat = category
        if key in overrides:
            cat, desc = overrides[key]

        if isinstance(default, bool):
            vt = "bool"
            val = str(default).lower()
        elif isinstance(default, int):
            vt = "int"
            val = str(default)
        elif isinstance(default, float):
            vt = "float"
            val = str(default)
        elif isinstance(default, (list, dict)):
            vt = "json"
            val = json.dumps(default, ensure_ascii=False)
        else:
            vt = "str"
            val = str(default) if default else ""

        rows.append(
            {"key": key, "value": val, "value_type": vt, "category": cat, "description": desc}
        )

    return rows


def seed_system_config() -> int:
    """Populate system_config with defaults from SystemConfig & BacktestConfig.

    Safe to call repeatedly — skips existing keys.
    """
    from orchestration.config import SystemConfig
    from backtest.config import BacktestConfig

    # Build override map: which config keys go to which category
    # backtest-specific fields
    bt_overrides: dict[str, tuple[str, str]] = {}
    for name in [
        "start_date", "end_date", "skip_s0", "cache_dir", "no_cache",
        "use_5min_data", "max_5min_workers", "rate_limit_per_sec",
        "backtest_type", "decision_time", "live_snapshot_dir", "capital_flow_mode",
        "entry_price_type", "exit_price_type", "slippage_bps", "commission_rate",
        "stop_loss_pct", "take_profit_pct", "max_positions", "output_dir",
    ]:
        bt_overrides[name] = ("threshold_backtest", f"回测: {name}")

    # schedule
    sched_overrides = {
        "schedule_enabled": ("schedule", "调度: 是否启用"),
        "schedule_time": ("schedule", "调度: 执行时间"),
    }

    # LLM / API
    api_overrides = {
        "llm_api_key": ("api", "LLM API Key"),
        "llm_api_base": ("api", "LLM API Base URL"),
        "llm_model": ("api", "LLM Model"),
    }

    live = SystemConfig()
    bt = BacktestConfig()

    rows: list[dict[str, str]] = []
    rows.extend(
        _collect_dataclass_fields(SystemConfig, category="threshold_live", overrides={**sched_overrides, **api_overrides})
    )
    rows.extend(
        _collect_dataclass_fields(BacktestConfig, category="threshold_backtest", overrides=bt_overrides)
    )

    count = 0
    for row in rows:
        if DbConfig.query.filter_by(key=row["key"]).first() is None:
            db.session.add(DbConfig(**row))  # type: ignore[arg-type]
            count += 1
    db.session.commit()
    return count


def init_db(app: Flask) -> None:
    """Create tables and seed data on first run."""
    db.init_app(app)

    with app.app_context():
        db.create_all()

        # seed if empty
        if DbConfig.query.count() == 0:
            n = seed_system_config()
            app.logger.info(f"Seeded {n} system_config rows")
