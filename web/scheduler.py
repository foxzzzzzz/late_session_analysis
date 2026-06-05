"""APScheduler integration for daily 14:29 pipeline + 15:30 P&L update."""

from __future__ import annotations

import logging
from datetime import datetime, date
from flask import Flask

logger = logging.getLogger(__name__)


def init_scheduler(app: Flask) -> None:
    """Set up APScheduler for daily auto-execution on trading days.

    Jobs:
    - 14:29 — Pipeline execution (S0 → S4)
    - 15:30 — Update simulated positions P&L and recommendation tracking
    - 16:00 — Update recommendation tracking (fallback, after data settles)

    Called from app factory after DB is ready.
    """
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except ImportError:
        app.logger.warning("APScheduler not installed — daily auto-run disabled")
        return

    scheduler = BackgroundScheduler(daemon=True)

    # ── 14:29 Pipeline Job ─────────────────────────────────

    def daily_pipeline_job():
        with app.app_context():
            from web.models import db, PipelineRun
            from web.services.pipeline_service import PipelineService, save_pipeline_results

            today = date.today()

            # Check if already run today
            existing = PipelineRun.query.filter_by(trading_date=today).first()
            if existing:
                app.logger.info(f"Pipeline already ran today ({today}), skipping")
                return

            # Skip weekends
            if today.weekday() >= 5:
                app.logger.info(f"{today} is weekend, skipping pipeline")
                return

            app.logger.info(f"Scheduled pipeline starting for {today}")

            # Create run record
            run = PipelineRun(
                trading_date=today,
                status="running",
                started_at=datetime.now(),
            )
            db.session.add(run)
            db.session.commit()

            try:
                service = PipelineService()
                results = service.run(test_mode=False)
                save_pipeline_results(run.id, results)

                run = db.session.get(PipelineRun, run.id)
                if run:
                    run.status = "completed"
                    run.finished_at = datetime.now()
                    db.session.commit()

                app.logger.info(f"Pipeline completed for {today}")

            except Exception as e:
                app.logger.error(f"Pipeline failed for {today}: {e}")
                import traceback
                app.logger.error(traceback.format_exc()[-500:])
                try:
                    run = db.session.get(PipelineRun, run.id)
                    if run:
                        run.status = "failed"
                        run.finished_at = datetime.now()
                        db.session.commit()
                except Exception:
                    pass

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
        id="daily_pipeline",
        name="Daily Late Session Pipeline",
        replace_existing=True,
    )

    scheduler.add_job(
        daily_pnl_update,
        "cron",
        hour=15,
        minute=30,
        id="daily_pnl_update",
        name="Daily P&L and Tracking Update",
        replace_existing=True,
    )

    scheduler.start()
    app.logger.info("APScheduler started: pipeline at 14:29, P&L update at 15:30")
