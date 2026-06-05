"""APScheduler integration for daily 14:29 pipeline + 15:30 P&L update."""

from __future__ import annotations

import logging
import os
from datetime import datetime, date
from flask import Flask

logger = logging.getLogger(__name__)


def _scheduled_log(run_id: int, stage: str, level: str, message: str) -> None:
    from web.models import db, PipelineLog

    db.session.add(PipelineLog(
        run_id=run_id,
        stage=stage,
        level=level,
        message=message,
        timestamp=datetime.now(),
    ))
    db.session.commit()


def _prepare_scheduled_run(today: date):
    from web.models import db, PipelineRun, PipelineLog, DailyRecommendation, SimulatedTrade

    existing = PipelineRun.query.filter_by(trading_date=today).first()
    if existing and existing.status in ("running", "completed", "pending"):
        return None, f"Pipeline already ran today ({today}), skipping"

    if existing:
        old_recs = DailyRecommendation.query.filter_by(run_id=existing.id).all()
        old_rec_ids = [rec.id for rec in old_recs]
        if old_rec_ids:
            SimulatedTrade.query.filter(
                SimulatedTrade.recommendation_id.in_(old_rec_ids)
            ).delete(synchronize_session=False)
        DailyRecommendation.query.filter_by(run_id=existing.id).delete(synchronize_session=False)
        PipelineLog.query.filter_by(run_id=existing.id).delete(synchronize_session=False)
        existing.status = "running"
        existing.started_at = datetime.now()
        existing.finished_at = None
        existing.stages_json = None
        run = existing
    else:
        run = PipelineRun(
            trading_date=today,
            status="running",
            started_at=datetime.now(),
        )
        db.session.add(run)
    db.session.commit()
    return run, ""


def _execute_scheduled_pipeline(app: Flask) -> None:
    """Run the scheduled pipeline once, reusing failed same-day runs."""
    with app.app_context():
        from web.models import db, PipelineRun
        from web.services.pipeline_service import PipelineService, save_pipeline_results

        today = date.today()

        if today.weekday() >= 5:
            app.logger.info(f"{today} is weekend, skipping pipeline")
            return

        run, skip_reason = _prepare_scheduled_run(today)
        if run is None:
            app.logger.info(skip_reason)
            return

        _scheduled_log(run.id, "SYS", "INFO", f"Scheduled pipeline starting for {today}")
        app.logger.info(f"Scheduled pipeline starting for {today}")

        def on_log(stage: str, level: str, message: str):
            _scheduled_log(run.id, stage, level, message)

        try:
            service = PipelineService(log_callback=on_log)
            results = service.run(test_mode=False)
            if results.get("error"):
                raise RuntimeError(str(results["error"]))

            save_pipeline_results(run.id, results)

            run = db.session.get(PipelineRun, run.id)
            if run:
                run.status = "completed"
                run.finished_at = datetime.now()
                db.session.commit()

            app.logger.info(f"Pipeline completed for {today}")

        except Exception as e:
            import traceback

            app.logger.error(f"Pipeline failed for {today}: {e}")
            app.logger.error(traceback.format_exc()[-500:])
            _scheduled_log(run.id, "SYS", "ERROR", f"Scheduled pipeline failed: {e}")
            _scheduled_log(run.id, "SYS", "ERROR", traceback.format_exc()[-500:])
            try:
                run = db.session.get(PipelineRun, run.id)
                if run:
                    run.status = "failed"
                    run.finished_at = datetime.now()
                    db.session.commit()
            except Exception:
                pass


def init_scheduler(app: Flask) -> None:
    """Set up APScheduler for daily auto-execution on trading days.

    Jobs:
    - 14:29 — Pipeline execution (S0 → S4)
    - 15:30 — Update simulated positions P&L and recommendation tracking
    - 16:00 — Update recommendation tracking (fallback, after data settles)

    Called from app factory after DB is ready.
    """
    if os.getenv("WEB_SCHEDULER_ENABLED", "true").lower() not in ("true", "1", "yes"):
        app.config["SCHEDULER_ACTIVE"] = False
        app.logger.info("APScheduler disabled by WEB_SCHEDULER_ENABLED")
        return

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except ImportError:
        app.config["SCHEDULER_ACTIVE"] = False
        app.logger.warning("APScheduler not installed — daily auto-run disabled")
        return

    scheduler = BackgroundScheduler(daemon=True)

    # ── 14:29 Pipeline Job ─────────────────────────────────

    def daily_pipeline_job():
        _execute_scheduled_pipeline(app)

    # ── 15:30 P&L Update Job ───────────────────────────────

    def daily_pnl_update():
        with app.app_context():
            from web.services.simulation_service import SimulationService

            today = date.today()
            if today.weekday() >= 5:
                return

            app.logger.info(f"Running daily P&L update for {today}")
            try:
                pos = SimulationService.update_all_open_positions()
                track = SimulationService.update_recommendation_tracking()
                app.logger.info(
                    f"P&L update: {pos['updated']} updated, "
                    f"{pos['stopped_out']} stopped, {pos['take_profit']} took profit, "
                    f"{track} tracking records"
                )
            except Exception as e:
                app.logger.error(f"P&L update failed: {e}")

    # ── Register Jobs ──────────────────────────────────────

    scheduler.add_job(
        daily_pipeline_job,
        "cron",
        hour=14,
        minute=29,
        timezone="Asia/Shanghai",
        misfire_grace_time=300,  # allow 5 min late
        id="daily_pipeline",
        name="Daily Late Session Pipeline",
        replace_existing=True,
    )

    scheduler.add_job(
        daily_pnl_update,
        "cron",
        hour=15,
        minute=30,
        timezone="Asia/Shanghai",
        misfire_grace_time=300,
        id="daily_pnl_update",
        name="Daily P&L and Tracking Update",
        replace_existing=True,
    )

    scheduler.start()
    app.config["SCHEDULER_ACTIVE"] = True
    app.config["SCHEDULER"] = scheduler
    app.config["SCHEDULER_NEXT_RUN_TIME"] = scheduler.get_job("daily_pipeline").next_run_time
    app.logger.info("APScheduler started (Asia/Shanghai): pipeline at 14:29, P&L update at 15:30")
    app.logger.info(f"Scheduler running: {scheduler.running}, next: {scheduler.get_job('daily_pipeline').next_run_time}")
