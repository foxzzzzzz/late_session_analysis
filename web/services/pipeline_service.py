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
        rows = DbConfig.query.filter(DbConfig.category.like("threshold_live%")).all()
        scoped_keys = {row.key for row in rows if row.key.startswith("live.")}
        for row in rows:
            try:
                key = row.key
                if key.startswith("live.regime."):
                    parts = key.split(".", 3)  # live.regime.bull.kline_min_yang_ratio_4d
                    if len(parts) == 4:
                        regime_name, field_key = parts[2], parts[3]
                        if regime_name in config._regime_overrides:
                            if row.value_type == "float":
                                config._regime_overrides[regime_name][field_key] = float(row.value)
                            elif row.value_type == "int":
                                config._regime_overrides[regime_name][field_key] = int(row.value)
                            elif row.value_type == "bool":
                                config._regime_overrides[regime_name][field_key] = row.value.lower() in ("true", "1", "yes")
                            else:
                                config._regime_overrides[regime_name][field_key] = row.value
                            count += 1
                    continue

                if key.startswith("live."):
                    key = key.split(".", 1)[1]
                elif f"live.{key}" in scoped_keys:
                    continue  # legacy unscoped row; scoped row wins

                # Legacy regime override keys: regime_bull_kline_min_yang_ratio_4d
                if key.startswith("regime_"):
                    parts = row.key.split("_", 2)  # ['regime', 'bull', 'kline_min_yang_ratio_4d']
                    if len(parts) == 3:
                        regime_name, field_key = parts[1], parts[2]
                        if f"live.regime.{regime_name}.{field_key}" in scoped_keys:
                            continue
                        if regime_name in config._regime_overrides:
                            if row.value_type == "float":
                                config._regime_overrides[regime_name][field_key] = float(row.value)
                            elif row.value_type == "int":
                                config._regime_overrides[regime_name][field_key] = int(row.value)
                            elif row.value_type == "bool":
                                config._regime_overrides[regime_name][field_key] = row.value.lower() in ("true", "1", "yes")
                            else:
                                config._regime_overrides[regime_name][field_key] = row.value
                            count += 1
                    continue

                current = getattr(config, key, None)
                if current is not None:
                    if row.value_type == "float":
                        setattr(config, key, float(row.value))
                    elif row.value_type == "int":
                        setattr(config, key, int(row.value))
                    elif row.value_type == "bool":
                        setattr(config, key, row.value.lower() in ("true", "1", "yes"))
                    elif row.value_type == "json":
                        setattr(config, key, json.loads(row.value) if row.value else None)
                    else:
                        setattr(config, key, row.value)
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
            for stage_name, stage_info in tracker.stages.items():
                short = stage_name.split("_")[0]  # "S0_板块预筛选" → "S0"
                stages[short] = stage_info.output_count

        # Recommendations from the last S4 run
        recommendations = []
        if hasattr(pipeline, "top30") and pipeline.top30:
            self._emit("S4", "INFO", f"=== S4 通过股票详情 (共 {len(pipeline.top30)} 只) ===")
            for i, ctx in enumerate(pipeline.top30, 1):
                level = ctx.recommendation or "watch"
                llm_s = getattr(ctx, "llm_score", 0)
                final_s = ctx.final_score
                name = ctx.name
                code = ctx.code
                price = ctx.price or 0
                sector = getattr(ctx, "sector_name", "") or ""

                # Per-dimension scores
                dim_attrs = {
                    "A": "score_tail_strength", "B": "score_technical",
                    "C": "score_capital", "D": "score_ma_system", "E": "score_market_env",
                }
                dims = ""
                rule_s = 0.0
                for dim, attr in dim_attrs.items():
                    val = getattr(ctx, attr, 0)
                    rule_s += val
                    dims += f" {dim}={val:.0f}"

                self._emit("S4", "INFO",
                           f"  #{i} {code} {name} | 价格={price:.2f} | "
                           f"综合={final_s:.1f} (规则={rule_s:.1f} LLM={llm_s:.1f}) | "
                           f"等级={level} | 板块={sector}{dims}")

                if level in ("strong_buy", "buy", "watch"):
                    recommendations.append({
                        "code": code, "name": name, "level": level,
                        "final_score": final_s, "rule_score": rule_s, "llm_score": llm_s,
                        "sector": sector, "entry_price": price,
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

    # Auto-create simulated trades for strong_buy + buy
    _auto_create_simulated_trades(run_id)


def _auto_create_simulated_trades(run_id: int) -> int:
    """Create simulated trades for strong_buy and buy recommendations."""
    from web.models import db, DailyRecommendation, SimulatedTrade

    recs = DailyRecommendation.query.filter(
        DailyRecommendation.run_id == run_id,
        DailyRecommendation.level.in_(["strong_buy", "buy"]),
    ).all()
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
