"""Pipeline execution page with SSE log streaming."""

import json
import queue
import threading
from datetime import datetime, date
from flask import Blueprint, render_template, request, Response, jsonify
from web.models import db, PipelineRun, PipelineLog

bp = Blueprint("pipeline", __name__)

# Thread-safe queue for SSE log streaming
_log_queues: dict[int, queue.Queue] = {}


def _log_handler(run_id: int, stage: str, level: str, message: str):
    """Callback that writes log to DB and pushes to SSE queue."""
    # Write to DB
    log = PipelineLog(
        run_id=run_id,
        stage=stage,
        level=level,
        message=message,
        timestamp=datetime.now(),
    )
    db.session.add(log)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()

    # Push to SSE
    q = _log_queues.get(run_id)
    if q:
        q.put({"stage": stage, "level": level, "message": message})


def _run_pipeline_in_thread(run_id: int, test_mode: bool = True):
    """Execute the real pipeline in a background thread."""
    from web.services.pipeline_service import PipelineService, save_pipeline_results

    def on_log(stage: str, level: str, message: str):
        _log_handler(run_id, stage, level, message)

    service = PipelineService(log_callback=on_log)

    try:
        results = service.run(test_mode=test_mode)

        # Save results to DB
        save_pipeline_results(run_id, results)

        # Update run status
        run = db.session.get(PipelineRun, run_id)
        if run:
            run.status = "completed"
            run.finished_at = datetime.now()
            db.session.commit()

    except Exception as e:
        _log_handler(run_id, "SYS", "ERROR", f"Pipeline thread crashed: {e}")
        import traceback
        _log_handler(run_id, "SYS", "ERROR", traceback.format_exc()[-300:])
        run = db.session.get(PipelineRun, run_id)
        if run:
            run.status = "failed"
            run.finished_at = datetime.now()
            db.session.commit()
    finally:
        # Clean up queue after a delay
        import time
        time.sleep(2)
        _log_queues.pop(run_id, None)


@bp.route("/")
def index():
    """Pipeline execution page."""
    runs = (
        PipelineRun.query
        .order_by(PipelineRun.trading_date.desc())
        .limit(20)
        .all()
    )

    # Snapshot stats
    from pathlib import Path
    from web.models import SystemConfig as DbConfig

    snap_cfg = DbConfig.query.filter_by(key="live_snapshot_dir").first()
    snap_dir = Path(snap_cfg.value if snap_cfg else "./live_snapshots")
    snap_days = 0
    snap_files = 0
    if snap_dir.exists():
        snap_days = len([d for d in snap_dir.iterdir() if d.is_dir()])
        snap_files = sum(1 for _ in snap_dir.rglob("*.jsonl"))

    return render_template(
        "pipeline.html",
        runs=runs,
        snap_days=snap_days,
        snap_files=snap_files,
        snap_dir=str(snap_dir),
    )


@bp.route("/run", methods=["POST"])
def run_pipeline():
    """Trigger a pipeline execution."""
    test_mode = request.json.get("test_mode", True) if request.json else True
    today = date.today()

    # Check if already run today
    existing = PipelineRun.query.filter_by(trading_date=today).first()
    if existing:
        return jsonify({"status": "error", "message": f"Pipeline already ran today ({today})"}), 409

    run = PipelineRun(
        trading_date=today,
        status="running",
        started_at=datetime.now(),
    )
    db.session.add(run)
    db.session.commit()

    # Create queue for this run
    _log_queues[run.id] = queue.Queue()

    # Start background thread
    thread = threading.Thread(
        target=_run_pipeline_in_thread,
        args=(run.id, test_mode),
        daemon=True,
    )
    thread.start()

    return jsonify({"status": "ok", "run_id": run.id})


@bp.route("/stream/<int:run_id>")
def stream(run_id: int):
    """SSE endpoint for real-time log streaming."""
    q = _log_queues.get(run_id)
    if q is None:
        # Fall back to DB for finished runs
        return _stream_from_db(run_id)
    return Response(
        _stream_from_queue(run_id, q),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


def _stream_from_queue(run_id: int, q: queue.Queue):
    """Yield SSE events from live queue."""
    yield "event: connected\ndata: {}\n\n"
    while True:
        try:
            msg = q.get(timeout=30)
            payload = json.dumps(msg, ensure_ascii=False)
            yield f"data: {payload}\n\n"
        except queue.Empty:
            # Send heartbeat
            yield ": heartbeat\n\n"
            # Check if run is done
            run = db.session.get(PipelineRun, run_id)
            if run and run.status != "running":
                yield f"event: done\ndata: {{\"status\": \"{run.status}\"}}\n\n"
                break


def _stream_from_db(run_id: int):
    """Stream all logs from DB for finished runs."""
    return Response(
        _db_log_generator(run_id),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


def _db_log_generator(run_id: int):
    """Replay logs from DB as SSE."""
    logs = (
        PipelineLog.query
        .filter_by(run_id=run_id)
        .order_by(PipelineLog.id)
        .all()
    )
    run = db.session.get(PipelineRun, run_id)
    yield "event: connected\ndata: {}\n\n"
    for log in logs:
        payload = json.dumps(
            {"stage": log.stage, "level": log.level, "message": log.message},
            ensure_ascii=False,
        )
        yield f"data: {payload}\n\n"
    if run:
        yield f"event: done\ndata: {{\"status\": \"{run.status}\"}}\n\n"


@bp.route("/<int:run_id>")
def run_detail(run_id: int):
    """View a specific pipeline run's details."""
    run = db.session.get(PipelineRun, run_id)
    if not run:
        return "Not found", 404

    logs = (
        PipelineLog.query
        .filter_by(run_id=run_id)
        .order_by(PipelineLog.id)
        .all()
    )

    return render_template("pipeline_detail.html", run=run, logs=logs)
