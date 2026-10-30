"""Pipeline execution page with SSE log streaming."""

import json
import os
import queue
import threading
from datetime import datetime, date
from flask import Blueprint, render_template, request, Response, jsonify, current_app
from web.models import db, PipelineRun, PipelineLog

bp = Blueprint("pipeline", __name__)

# Thread-safe queue for SSE log streaming
_log_queues: dict[int, queue.Queue] = {}
_app_ref = None  # set by create_app()


def set_app(app):
    global _app_ref
    _app_ref = app


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


def _safe_log(app, run_id: int, stage: str, level: str, message: str) -> None:
    """Persist a pipeline log even from thread startup/error paths."""
    try:
        with app.app_context():
            _log_handler(run_id, stage, level, message)
    except Exception:
        q = _log_queues.get(run_id)
        if q:
            q.put({"stage": stage, "level": level, "message": message})


def _mark_stale_startup_if_needed(run: PipelineRun) -> None:
    """Fail runs that never emitted a startup log after a short timeout."""
    if not run or run.status != "running" or not run.started_at:
        return

    timeout = int(os.getenv("WEB_PIPELINE_START_TIMEOUT_SECONDS", "60"))
    age = (datetime.now() - run.started_at).total_seconds()
    if age < timeout:
        return

    log_count = PipelineLog.query.filter_by(run_id=run.id).count()
    if log_count > 0:
        return

    run.status = "failed"
    run.finished_at = datetime.now()
    db.session.add(PipelineLog(
        run_id=run.id,
        stage="SYS",
        level="ERROR",
        message=f"No startup log after {timeout}s; background pipeline thread likely failed to start",
        timestamp=datetime.now(),
    ))
    db.session.commit()


def _run_pipeline_in_thread(app, run_id: int, test_mode: bool = True):
    """Execute the real pipeline in a background thread."""
    import traceback as _tb

    _safe_log(app, run_id, "SYS", "INFO", "Thread started, entering app context...")

    try:
        from web.services.pipeline_service import PipelineService, save_pipeline_results, finish_pipeline_run

        def on_log(stage: str, level: str, message: str):
            _log_handler(run_id, stage, level, message)

        with app.app_context():
            service = PipelineService(log_callback=on_log)
            service.run_id = run_id

            try:
                results = service.run(test_mode=test_mode)
                if results.get("error"):
                    _log_handler(run_id, "SYS", "ERROR", str(results["error"]))
                    finish_pipeline_run(run_id, 'failed', results, service)
                    return
                save_pipeline_results(run_id, results)

                finish_pipeline_run(run_id, 'completed', results, service)

            except Exception as e:
                _log_handler(run_id, "SYS", "ERROR", f"Pipeline crashed: {e}")
                _log_handler(run_id, "SYS", "ERROR", _tb.format_exc()[-500:])
                try:
                    finish_pipeline_run(run_id, 'failed', locals().get('results', {}), service)
                except Exception:
                    pass

    except Exception as outer_e:
        _safe_log(app, run_id, "SYS", "ERROR", f"Thread init failed: {outer_e}")
        _safe_log(app, run_id, "SYS", "ERROR", _tb.format_exc()[-500:])
        try:
            with app.app_context():
                from web.services.pipeline_service import finish_pipeline_run
                finish_pipeline_run(run_id, 'failed', locals().get('results', {}), locals().get('service'))
        except Exception:
            pass

    finally:
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

    snap_cfg = (
        DbConfig.query.filter_by(key="live.live_snapshot_dir").first()
        or DbConfig.query.filter_by(key="live_snapshot_dir").first()
    )
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
    force = request.json.get("force", False) if request.json else False
    today = date.today()

    # Check if already run today. Completed/running runs are protected, failed runs
    # can be retried by reusing the same unique trading_date row.
    existing = PipelineRun.query.filter_by(trading_date=today).first()
    if existing and existing.status in ("running", "pending"):
        return jsonify({"status": "error", "message": f"Pipeline already running today ({today})"}), 409
    if existing and existing.status == "completed" and not force:
        return jsonify({"status": "error", "message": f"Pipeline already ran today ({today}). Use force=true to re-run."}), 409
    if existing:
        from web.services.pipeline_service import restart_pipeline_run
        if not restart_pipeline_run(existing):
            return jsonify({'status': 'error', 'message': 'Pipeline generation changed; retry later'}), 409
        run = existing
    else:
        run = PipelineRun(
            trading_date=today,
            status="running",
            started_at=datetime.now(),
        )
        db.session.add(run)
    db.session.commit()

    # Create queue for this run
    q = queue.Queue()
    _log_queues[run.id] = q

    # Verify queue works from main thread
    q.put({"stage": "SYS", "level": "INFO", "message": "Queue initialized"})

    # Store test_mode in queue so stream endpoint can pick it up
    q.put({"test_mode": test_mode})

    return jsonify({"status": "ok", "run_id": run.id})


@bp.route("/stream/<int:run_id>")
def stream(run_id: int):
    """SSE endpoint for real-time log streaming — executes pipeline inline."""
    q = _log_queues.get(run_id)
    if q is None:
        return _stream_from_db(run_id, app=current_app._get_current_object())

    # Check if this is a fresh run that needs execution
    run = db.session.get(PipelineRun, run_id)
    if run and run.status in ("running",) and PipelineLog.query.filter_by(run_id=run_id).count() == 0:
        # Fresh run: execute pipeline inline while streaming
        # Pass the actual app object so background thread doesn't need current_app
        _app = current_app._get_current_object()
        return Response(
            _stream_and_run(_app, run_id, q, run),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
        )

    return Response(
        _stream_from_queue(run_id, q, app=current_app._get_current_object()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


def _stream_and_run(app, run_id: int, q: queue.Queue, run: PipelineRun):
    """Run pipeline in a thread, stream SSE with heartbeat polling."""
    import traceback as _tb
    from web.services.pipeline_service import PipelineService, save_pipeline_results, finish_pipeline_run

    yield "event: connected\ndata: {}\n\n"

    # Read test_mode from the queue (stored by run_pipeline)
    test_mode = True
    try:
        meta = q.get_nowait()
        if isinstance(meta, dict) and "test_mode" in meta:
            test_mode = meta["test_mode"]
    except queue.Empty:
        pass

    # The pipeline runs in a thread and pushes to DB+queue via _emit callback.
    # The SSE generator polls the queue and sends heartbeats.
    finished = threading.Event()

    def _run():
        with app.app_context():
            from web.models import db as _db, PipelineLog as _PL

            def _emit(stage: str, level: str, message: str):
                msg = str(message)[:2000]
                try:
                    log = _PL(run_id=run_id, stage=stage, level=level, message=msg, timestamp=datetime.now())
                    _db.session.add(log)
                    _db.session.commit()
                except Exception:
                    try:
                        _db.session.rollback()
                    except Exception:
                        pass
                q.put({"stage": stage, "level": level, "message": msg})

            try:
                service = PipelineService(log_callback=_emit)
                service.run_id = run_id
                results = service.run(test_mode=test_mode)
                if results.get('error'):
                    raise RuntimeError(str(results['error']))
                save_pipeline_results(run_id, results)
                finish_pipeline_run(run_id, 'completed', results, service)
            except Exception as e:
                _emit("SYS", "ERROR", f"Pipeline failed: {e}")
                _emit("SYS", "ERROR", _tb.format_exc()[-800:])
                try:
                    finish_pipeline_run(run_id, 'failed', locals().get('results', {}), locals().get('service'))
                except Exception:
                    pass
            finally:
                finished.set()

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    # Poll queue + send heartbeats until pipeline finishes
    while not finished.is_set():
        try:
            msg = q.get(timeout=5)
            payload = json.dumps(msg, ensure_ascii=False)
            yield f"data: {payload}\n\n"
        except queue.Empty:
            yield "event: heartbeat\ndata: {}\n\n"

    # Drain remaining messages
    while True:
        try:
            msg = q.get_nowait()
            payload = json.dumps(msg, ensure_ascii=False)
            yield f"data: {payload}\n\n"
        except queue.Empty:
            break

    run_obj = db.session.get(PipelineRun, run_id)
    status = run_obj.status if run_obj else "unknown"
    yield f"event: done\ndata: {{\"status\": \"{status}\"}}\n\n"


def _stream_from_queue(run_id: int, q: queue.Queue, app=None):
    """Yield SSE events from live queue."""
    yield "event: connected\ndata: {}\n\n"
    while True:
        try:
            msg = q.get(timeout=30)
            payload = json.dumps(msg, ensure_ascii=False)
            yield f"data: {payload}\n\n"
        except queue.Empty:
            # Send heartbeat
            yield "event: heartbeat\ndata: {}\n\n"
            # Check if run is done (inside app context)
            run_status = "running"
            if app:
                try:
                    with app.app_context():
                        run = db.session.get(PipelineRun, run_id)
                        if run:
                            run_status = run.status
                except Exception:
                    pass
            if run_status != "running":
                yield f"event: done\ndata: {{\"status\": \"{run_status}\"}}\n\n"
                break


def _stream_from_db(run_id: int, app=None):
    """Stream all logs from DB for finished runs."""
    return Response(
        _db_log_generator(run_id, app=app),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


def _db_log_generator(run_id: int, app=None):
    """Replay logs from DB as SSE."""
    # Eager-load DB data while context is still active
    logs = []
    run_status = "unknown"
    try:
        if app:
            with app.app_context():
                logs = (
                    PipelineLog.query
                    .filter_by(run_id=run_id)
                    .order_by(PipelineLog.id)
                    .all()
                )
                run = db.session.get(PipelineRun, run_id)
                if run:
                    run_status = run.status
    except Exception:
        pass

    yield "event: connected\ndata: {}\n\n"
    for log in logs:
        payload = json.dumps(
            {"stage": log.stage, "level": log.level, "message": log.message},
            ensure_ascii=False,
        )
        yield f"data: {payload}\n\n"
    yield f"event: done\ndata: {{\"status\": \"{run_status}\"}}\n\n"


@bp.route("/<int:run_id>")
def run_detail(run_id: int):
    """View a specific pipeline run's details."""
    run = db.session.get(PipelineRun, run_id)
    if not run:
        return "Not found", 404
    _mark_stale_startup_if_needed(run)
    run = db.session.get(PipelineRun, run_id)

    logs = (
        PipelineLog.query
        .filter_by(run_id=run_id)
        .order_by(PipelineLog.id)
        .all()
    )

    return render_template("pipeline_detail.html", run=run, logs=logs)


@bp.route('/status/<int:run_id>')
def run_status(run_id: int):
    """分页增量日志及已完成阶段；定时/手动执行均读同一数据库。"""
    from web.services.pipeline_service import sidecar_payload
    after = request.args.get('after', '0')
    if not after.isdigit():
        return jsonify(error='invalid cursor'), 400
    run = db.session.get(PipelineRun, run_id)
    if run is None:
        return jsonify(error='not found'), 404
    generation = run.started_at.isoformat()
    if request.args.get('generation') not in (None, generation):
        after = '0'  # 同日重跑可能重用日志ID，旧游标不能跳过新批次日志。
    limit = int(os.getenv('WEB_PIPELINE_LOG_PAGE_SIZE', '200'))
    logs = (PipelineLog.query.filter(PipelineLog.run_id == run_id, PipelineLog.id > int(after))
            .order_by(PipelineLog.id).limit(limit).all())
    last = PipelineLog.query.filter_by(run_id=run_id).order_by(PipelineLog.id.desc()).first()
    response = jsonify(
        id=run.id, status=run.status, regime=run.regime,
        run_generation=generation,
        stages=json.loads(run.stages_json or '{}'),
        finished_at=run.finished_at.strftime('%H:%M:%S') if run.finished_at else None,
        total_logs=PipelineLog.query.filter_by(run_id=run_id).count(),
        last_progress_at=last.timestamp.strftime('%H:%M:%S') if last else None,
        cursor=logs[-1].id if logs else int(after),
        logs=[dict(id=log.id, stage=log.stage, level=log.level, message=log.message,
                   timestamp=log.timestamp.strftime('%H:%M:%S')) for log in logs],
        sidecar=sidecar_payload(run),
    )
    response.headers['Cache-Control'] = 'no-store'
    return response
