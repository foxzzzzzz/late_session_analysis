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

        # Build config
        try:
            config = BacktestConfig()
            config.start_date = start_date
            config.end_date = end_date
            config.backtest_type = backtest_type
            config.capital_flow_mode = capital_flow_mode
            config.max_positions = max_positions

            if regime and regime != "auto":
                config.regime_mode = regime

            # Override from DB
            for row in DbConfig.query.filter(DbConfig.category.like("threshold_backtest%")).all():
                try:
                    current = getattr(config, row.key, None)
                    if current is not None:
                        if row.value_type == "float":
                            setattr(config, row.key, float(row.value))
                        elif row.value_type == "int":
                            setattr(config, row.key, int(row.value))
                        elif row.value_type == "bool":
                            setattr(config, row.key, row.value.lower() in ("true", "1", "yes"))
                        else:
                            setattr(config, row.key, row.value)
                except (ValueError, TypeError):
                    pass

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
            engine.run()
            self._emit("BT", "INFO", "Backtest execution complete")
        except Exception as e:
            self._emit("BT", "ERROR", f"Backtest engine failed: {e}")
            import traceback
            self._emit("BT", "ERROR", traceback.format_exc()[-500:])
            return {"status": "error", "error": str(e)}

        # Extract results
        try:
            summary = _extract_summary(engine)
            output_dir = str(getattr(config, "output_dir", "./backtest_reports"))
            trades_count = len(getattr(engine, "_day_results", []))

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


def _extract_summary(engine) -> dict:
    """Extract performance summary from a completed BacktestEngine."""
    summary: dict = {}

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

    # Also try to get summary from day results
    try:
        day_results = getattr(engine, "_day_results", []) or []
        if day_results:
            trades = []
            for dr in day_results:
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
