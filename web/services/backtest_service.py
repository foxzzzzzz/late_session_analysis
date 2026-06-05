"""Backtest execution service — wraps BacktestEngine for web use."""

from __future__ import annotations

import json
import logging
from datetime import datetime, date
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

LogCallback = Callable[[str, str, str], None]


class BacktestService:
    """Runs backtest with progress callbacks."""

    def __init__(self, log_callback: LogCallback | None = None):
        self._callback = log_callback

    def _emit(self, stage: str, level: str, message: str):
        if self._callback:
            try:
                self._callback(stage, level, message)
            except Exception:
                pass
        getattr(logger, level.lower(), logger.info)(f"[BT:{stage}] {message}")

    def run(
        self,
        start_date: str,
        end_date: str,
        backtest_type: str = "historical",
        capital_flow_mode: str = "none",
        regime: str = "auto",
        max_positions: int = 5,
    ) -> dict:
        """Execute a backtest and return summary results.

        Returns dict with: status, summary (dict of performance metrics),
        output_dir, trades_count.
        """
        from backtest.config import BacktestConfig
        from web.models import SystemConfig as DbConfig

        # Parse dates
        try:
            sd = datetime.strptime(start_date, "%Y%m%d").date()
            ed = datetime.strptime(end_date, "%Y%m%d").date()
        except (ValueError, TypeError):
            self._emit("BT", "ERROR", f"Invalid date range: {start_date}-{end_date}")
            return {"status": "error", "error": "Invalid date range"}

        self._emit("BT", "INFO", f"Starting backtest: {sd} → {ed}, type={backtest_type}, flow={capital_flow_mode}, regime={regime}")

        if backtest_type == "live_replay":
            return {
                "status": "error",
                "error": "live_replay snapshot execution is not implemented yet; use historical/proxy until replay loader is wired",
            }

        # Build config
        try:
            config = BacktestConfig()

            # Apply DB overrides FIRST (so API params can override them)
            _skipped_keys = {
                "start_date", "end_date", "backtest_type", "capital_flow_mode",
                "regime_mode", "max_positions",
            }
            for row in DbConfig.query.filter(DbConfig.category.like("threshold_backtest%")).all():
                if row.key in _skipped_keys:
                    continue  # skip date/type keys, API params win
                try:
                    current = getattr(config, row.key, None)
                    if current is not None:
                        if row.value_type == "float":
                            setattr(config, row.key, float(row.value))
                        elif row.value_type == "int":
                            setattr(config, row.key, int(row.value))
                        elif row.value_type == "bool":
                            setattr(config, row.key, row.value.lower() in ("true", "1", "yes"))
                        elif row.value_type == "json":
                            setattr(config, row.key, json.loads(row.value) if row.value else None)
                        else:
                            setattr(config, row.key, row.value)
                except (ValueError, TypeError):
                    pass

            # API parameters OVERRIDE DB defaults
            config.start_date = start_date
            config.end_date = end_date
            config.backtest_type = backtest_type
            config.capital_flow_mode = capital_flow_mode
            config.max_positions = max_positions

            if regime and regime != "auto":
                config.regime_mode = regime

            self._emit("BT", "INFO", "BacktestConfig created with DB overrides")
        except Exception as e:
            self._emit("BT", "ERROR", f"Failed to create config: {e}")
            return {"status": "error", "error": str(e)}

        # Run engine
        try:
            from backtest.engine import BacktestEngine
            self._emit("BT", "INFO", "Initializing BacktestEngine...")
            engine = BacktestEngine(config)
            self._emit("BT", "INFO", "Running backtest (this may take several minutes)...")
            engine_result = engine.run()
            self._emit("BT", "INFO", "Backtest execution complete")
        except Exception as e:
            self._emit("BT", "ERROR", f"Backtest engine failed: {e}")
            import traceback
            self._emit("BT", "ERROR", traceback.format_exc()[-500:])
            return {"status": "error", "error": str(e)}

        # Extract results
        try:
            summary = _extract_summary(engine, engine_result)
            output_dir = str(getattr(config, "output_dir", "./backtest_reports"))
            trade_log = getattr(engine, "trade_log", None)
            if trade_log and hasattr(trade_log, "closed_trades"):
                trades_count = len(trade_log.closed_trades())
            else:
                trades_count = int(engine_result.get("total_trades", 0)) if isinstance(engine_result, dict) else 0

            self._emit("BT", "INFO",
                       f"Results: {trades_count} trades, "
                       f"win_rate={summary.get('win_rate', 0)}%, "
                       f"cumulative={summary.get('cumulative_return', 0)}%")

            return {
                "status": "completed",
                "summary": summary,
                "output_dir": output_dir,
                "trades_count": trades_count,
            }
        except Exception as e:
            self._emit("BT", "ERROR", f"Failed to extract results: {e}")
            return {"status": "completed", "summary": {}, "output_dir": "", "trades_count": 0}


def _extract_summary(engine, engine_result: dict | None = None) -> dict:
    """Extract performance summary from a completed BacktestEngine."""
    summary: dict = {}

    engine_result = engine_result or {}
    metrics = engine_result.get("metrics", {}) if isinstance(engine_result, dict) else {}
    if metrics:
        summary.update({
            "total_trades": metrics.get("total_trades", 0),
            "win_rate": metrics.get("win_rate", 0),
            "cumulative_return": metrics.get("total_return_pct", 0),
            "sharpe_ratio": metrics.get("sharpe_ratio", 0),
            "max_drawdown": metrics.get("max_drawdown_pct", 0),
            "calmar_ratio": metrics.get("calmar_ratio", 0),
            "profit_factor": metrics.get("profit_factor", 0),
            "avg_return": metrics.get("avg_return_pct", 0),
        })

    try:
        if hasattr(engine, "_performance") and engine._performance:
            perf = engine._performance
            summary["total_trades"] = getattr(perf, "total_trades", 0)
            summary["win_rate"] = round(getattr(perf, "win_rate", 0) * 100, 1)
            summary["cumulative_return"] = round(getattr(perf, "cumulative_return_pct", 0), 2)
            summary["sharpe_ratio"] = round(getattr(perf, "sharpe_ratio", 0), 2)
            summary["max_drawdown"] = round(getattr(perf, "max_drawdown_pct", 0), 2)
            summary["calmar_ratio"] = round(getattr(perf, "calmar_ratio", 0), 2)
            summary["profit_factor"] = round(getattr(perf, "profit_factor", 0), 2)
            summary["avg_return"] = round(getattr(perf, "avg_return_pct", 0), 2)
    except Exception:
        summary["note"] = "Performance object not fully parsed"

    # Also try to get summary from trade_log
    try:
        trade_log = getattr(engine, "trade_log", None)
        if trade_log and hasattr(trade_log, "days"):
            trades = []
            for dr in trade_log.days:
                if hasattr(dr, "trades"):
                    trades.extend(dr.trades)
            if trades:
                winning = [t for t in trades if getattr(t, "return_pct", 0) > 0]
                summary["trades_from_results"] = len(trades)
                if not summary.get("win_rate"):
                    summary["win_rate"] = round(len(winning) / len(trades) * 100, 1) if trades else 0
                returns = [getattr(t, "return_pct", 0) for t in trades]
                if not summary.get("cumulative_return"):
                    summary["cumulative_return"] = round(sum(returns), 2)
    except Exception:
        pass

    return summary
