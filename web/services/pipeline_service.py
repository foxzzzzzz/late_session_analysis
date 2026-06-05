"""Pipeline execution service — wraps LateSessionPipeline for web use."""

from __future__ import annotations

import logging
import json
from datetime import datetime, date
from typing import Callable

logger = logging.getLogger(__name__)

# Type for log callback: (stage, level, message) -> None
LogCallback = Callable[[str, str, str], None]


class PipelineService:
    """Thin wrapper that runs the pipeline and emits structured log events."""

    def __init__(self, log_callback: LogCallback | None = None):
        self._callback = log_callback

    def _emit(self, stage: str, level: str, message: str):
        if self._callback:
            try:
                self._callback(stage, level, message)
            except Exception:
                pass
        getattr(logger, level.lower(), logger.info)(f"[{stage}] {message}")

    def run(self, test_mode: bool = True, regime_override: str | None = None) -> dict:
        """Execute the full pipeline and return summary stats.

        Returns dict with keys: regime, stages (dict), recommendations (list).
        """
        from orchestration.config import SystemConfig
        from web.models import SystemConfig as DbConfig

        self._emit("SYS", "INFO", f"Pipeline starting (test_mode={test_mode})")

        # Build config with DB overrides
        config = SystemConfig()
        config.test_mode = test_mode
        count = 0
        for row in DbConfig.query.filter(DbConfig.category.like("threshold_live%")).all():
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
                    count += 1
            except (ValueError, TypeError):
                self._emit("SYS", "WARN", f"Cannot override {row.key}={row.value}")

        if regime_override and regime_override != "auto":
            config.regime_mode = regime_override
        self._emit("SYS", "INFO", f"Config loaded ({count} DB overrides)")

        # Init pipeline
        try:
            from orchestration.pipeline import LateSessionPipeline
            pipeline = LateSessionPipeline(config, test_mode=test_mode)
            self._emit("SYS", "INFO", "Pipeline instance created")
        except Exception as e:
            self._emit("SYS", "ERROR", f"Failed to create pipeline: {e}")
            return {"regime": "unknown", "stages": {}, "recommendations": [], "error": str(e)}

        # Run
        try:
            self._emit("SYS", "INFO", "Starting pipeline.run()...")
            pipeline.run(stages=None)
            self._emit("SYS", "INFO", "Pipeline completed successfully")
        except Exception as e:
            self._emit("SYS", "ERROR", f"Pipeline failed: {e}")
            import traceback
            self._emit("SYS", "ERROR", traceback.format_exc()[-500:])
            return {"regime": getattr(config, "regime_mode", "unknown"), "stages": {}, "recommendations": [], "error": str(e)}

        # Extract results
        regime = config.regime_mode

        # Stage pass counts from tracker
        stages = {}
        if hasattr(pipeline, "tracker") and pipeline.tracker:
            tracker = pipeline.tracker
            for s in ["S0", "S1", "S2", "S3", "S4"]:
                stages[s] = getattr(tracker, f"{s.lower()}_pass", 0)

        # Recommendations from the last S4 run
        recommendations = []
        if hasattr(pipeline, "top30") and pipeline.top30:
            for ctx in pipeline.top30:
                level = ctx.recommendation or "watch"
                if level in ("strong_buy", "buy", "watch"):
                    recommendations.append({
                        "code": ctx.code,
                        "name": ctx.name,
                        "level": level,
                        "final_score": ctx.final_score,
                        "rule_score": getattr(ctx, "rule_score", 0),
                        "llm_score": getattr(ctx, "llm_score", 0),
                        "sector": getattr(ctx, "sector_name", "") or "",
                        "entry_price": ctx.price or 0,
                    })

        self._emit("SYS", "INFO",
                   f"Results: {len(recommendations)} recommendations "
                   f"(S0={stages.get('S0','?')} S1={stages.get('S1','?')} "
                   f"S2={stages.get('S2','?')} S3={stages.get('S3','?')} "
                   f"S4={stages.get('S4','?')})")

        return {"regime": regime, "stages": stages, "recommendations": recommendations}


def save_pipeline_results(run_id: int, results: dict) -> None:
    """Persist pipeline results to database."""
    import json
    from web.models import db, PipelineRun, DailyRecommendation

    run = db.session.get(PipelineRun, run_id)
    if not run:
        return

    run.regime = results.get("regime", "unknown")
    run.stages_json = json.dumps(results.get("stages", {}), ensure_ascii=False)

    # Save recommendations
    for rec in results.get("recommendations", []):
        dr = DailyRecommendation(
            run_id=run_id,
            code=rec["code"],
            name=rec["name"],
            level=rec["level"],
            final_score=rec["final_score"],
            rule_score=rec.get("rule_score", 0),
            llm_score=rec.get("llm_score", 0),
            sector=rec.get("sector", ""),
            entry_price=rec.get("entry_price", 0),
            recommendation_date=run.trading_date,
        )
        db.session.add(dr)

    db.session.commit()

    # Auto-create simulated trades for strong_buy
    _auto_create_simulated_trades(run_id)


def _auto_create_simulated_trades(run_id: int) -> int:
    """Create simulated trades for strong_buy recommendations."""
    from web.models import db, DailyRecommendation, SimulatedTrade

    recs = DailyRecommendation.query.filter_by(run_id=run_id, level="strong_buy").all()
    count = 0
    for rec in recs:
        existing = SimulatedTrade.query.filter_by(recommendation_id=rec.id).first()
        if existing:
            continue
        trade = SimulatedTrade(
            recommendation_id=rec.id,
            entry_date=rec.recommendation_date,
            entry_price=rec.entry_price,
            status="open",
            notional=10000.0,
            shares=int(10000.0 / rec.entry_price) if rec.entry_price > 0 else 0,
        )
        db.session.add(trade)
        count += 1
    db.session.commit()
    return count
