"""Backtest execution and results page."""

import json
import threading
from datetime import datetime, date
from flask import Blueprint, render_template, request, jsonify, Response
from web.models import db, BacktestRun

bp = Blueprint("backtest", __name__)


@bp.route("/")
def index():
    """Backtest page with form and history."""
    past_runs = (
        BacktestRun.query
        .order_by(BacktestRun.started_at.desc())
        .limit(20)
        .all()
    )

    # Default date range: last 3 months
    from datetime import timedelta
    today = date.today()
    default_end = today.isoformat().replace("-", "")
    default_start = (today - timedelta(days=90)).isoformat().replace("-", "")

    return render_template(
        "backtest.html",
        past_runs=past_runs,
        default_start=default_start,
        default_end=default_end,
    )


def _run_backtest_in_thread(run_id: int, params: dict):
    """Execute backtest in background thread."""
    from web.services.backtest_service import BacktestService

    try:
        # Update run status
        run = db.session.get(BacktestRun, run_id)
        if not run:
            return

        run.status = "running"
        db.session.commit()

        service = BacktestService()
        results = service.run(
            start_date=params.get("start_date", "20260101"),
            end_date=params.get("end_date", "20260531"),
            backtest_type=params.get("backtest_type", "historical"),
            capital_flow_mode=params.get("capital_flow_mode", "none"),
            regime=params.get("regime", "auto"),
            max_positions=params.get("max_positions", 5),
        )

        # Save results
        run = db.session.get(BacktestRun, run_id)
        if run:
            run.status = results.get("status", "completed")
            run.finished_at = datetime.now()
            run.summary_json = json.dumps(results.get("summary", {}), ensure_ascii=False, default=str)
            run.output_dir = results.get("output_dir", "")
            db.session.commit()

    except Exception:
        run = db.session.get(BacktestRun, run_id)
        if run:
            run.status = "failed"
            run.finished_at = datetime.now()
            db.session.commit()


@bp.route("/run", methods=["POST"])
def run_backtest():
    """Trigger a backtest execution."""
    data = request.get_json() or {}

    try:
        sd = datetime.strptime(data.get("start_date", "20260101"), "%Y%m%d").date()
        ed = datetime.strptime(data.get("end_date", "20260531"), "%Y%m%d").date()
    except (ValueError, TypeError):
        return jsonify({"status": "error", "message": "Invalid date format, use YYYYMMDD"}), 400

    run = BacktestRun(
        start_date=sd,
        end_date=ed,
        backtest_type=data.get("backtest_type", "historical"),
        capital_flow_mode=data.get("capital_flow_mode", "none"),
        regime=data.get("regime", "auto"),
        status="pending",
        started_at=datetime.now(),
    )
    db.session.add(run)
    db.session.commit()

    # Start in background
    thread = threading.Thread(
        target=_run_backtest_in_thread,
        args=(run.id, data),
        daemon=True,
    )
    thread.start()

    return jsonify({"status": "ok", "run_id": run.id})


@bp.route("/<int:run_id>")
def run_detail(run_id: int):
    """View a specific backtest run's results."""
    run = db.session.get(BacktestRun, run_id)
    if not run:
        return "Not found", 404

    # Parse summary for display
    summary = {}
    if run.summary_json:
        try:
            summary = json.loads(run.summary_json)
        except (json.JSONDecodeError, TypeError):
            pass

    return render_template("backtest_detail.html", run=run, summary=summary)
