"""发布事务、旁路故障及在途HTTP截止；仅临时库和本地HTTP。"""
import threading
import time
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock

import pytest
import requests
from sqlalchemy import event, text

from analysis.merger import merge_and_rank
from screening.context import StockContext
from tests.test_pipeline_progress import make_web_app


@pytest.mark.parametrize('operation', ['INSERT', 'UPDATE', 'START'])
def test_sidecar_storage_failure_cannot_abort_formal_publication(tmp_path, monkeypatch, operation):
    from web.models import db, PipelineRun, PipelineSidecar, DailyRecommendation, SimulatedTrade
    from web.services.pipeline_service import PipelineService
    app = make_web_app(tmp_path, monkeypatch)
    c = StockContext(code='000001', name='测试', price=10, total_score=80)
    merge_and_rank([c], {})
    class Pipeline:
        def __init__(self, *args, **kwargs):
            self.regime, self.tracker, self.top30 = 'neutral', MagicMock(stages={}), [c]
            self.llm_runner = object()
        def run(self, stages=None):
            self.on_rules_ready(self.top30)
        def start_sidecar(self, on_complete=None):
            assert DailyRecommendation.query.count() == SimulatedTrade.query.count() == 1
            assert PipelineRun.query.one().status == 'completed'
    monkeypatch.setattr('orchestration.pipeline.LateSessionPipeline', Pipeline)
    with app.app_context():
        run = PipelineRun(trading_date=date(2026, 10, 30), status='running')
        db.session.add(run); db.session.commit()
        run_id = run.id
        if operation == 'UPDATE':
            db.session.add(PipelineSidecar(run_id=run_id, generation='old',
                           run_started_at=run.started_at, status='failed'))
            db.session.commit()
        sql_operation = 'UPDATE' if operation == 'START' else operation
        condition = "WHEN NEW.status = 'running' " if operation == 'START' else ''
        db.session.execute(text(f"CREATE TRIGGER fail_sidecar BEFORE {sql_operation} ON pipeline_sidecars {condition}"
                                "BEGIN SELECT RAISE(FAIL, 'sidecar unavailable'); END"))
        db.session.commit()
        service = PipelineService(); service.run_id = run_id
        result = service.run()
        assert not result.get('error') and service._published_results is not None
        assert DailyRecommendation.query.count() == SimulatedTrade.query.count() == 1
        assert db.session.get(PipelineRun, run_id).status == 'completed'
        # 旁路故障后session仍可使用，后续正常查询/报告不被污染。
        assert db.session.execute(text('SELECT 1')).scalar() == 1


def test_sidecar_start_failure_keeps_published_results(tmp_path, monkeypatch):
    from web.models import db, PipelineRun, PipelineSidecar, DailyRecommendation
    from web.services.pipeline_service import PipelineService
    app = make_web_app(tmp_path, monkeypatch)
    c = StockContext(code='a', name='测试', price=10, total_score=80)
    merge_and_rank([c], {})
    class Pipeline:
        def __init__(self, *args, **kwargs):
            self.regime, self.tracker, self.top30 = 'neutral', MagicMock(stages={}), [c]
            self.llm_runner = object()
        def run(self, stages=None):
            self.on_rules_ready(self.top30)
        def start_sidecar(self, **kwargs):
            raise RuntimeError('thread unavailable')
    monkeypatch.setattr('orchestration.pipeline.LateSessionPipeline', Pipeline)
    with app.app_context():
        run = PipelineRun(trading_date=date(2026, 10, 30), status='running')
        db.session.add(run); db.session.commit()
        service = PipelineService(); service.run_id = run.id
        assert not service.run().get('error')
        db.session.expire_all()
        assert PipelineRun.query.one().status == 'completed'
        assert DailyRecommendation.query.count() == 1
        assert PipelineSidecar.query.one().status == 'failed'


def result(started):
    return dict(run_started_at=started.isoformat(), regime='bear', stages={'S4': 1},
                recommendations=[dict(code='old', name='旧批', level='buy',
                                      final_score=80, entry_price=10)])


@pytest.mark.parametrize('action', ['save', 'finish_completed', 'finish_failed', 'progress'])
def test_restart_between_read_and_write_rejects_old_generation(tmp_path, monkeypatch, action):
    from web.models import db, PipelineRun, DailyRecommendation, SimulatedTrade
    from web.services.pipeline_service import save_pipeline_results, finish_pipeline_run, save_pipeline_progress
    app = make_web_app(tmp_path, monkeypatch)
    old = datetime(2026, 10, 30, 14, 35)
    with app.app_context():
        run = PipelineRun(trading_date=date(2026, 10, 30), status='completed', started_at=old)
        db.session.add(run); db.session.commit()
        run_id = run.id
        engine = db.engine
        armed = True
        def before_update(conn, cursor, statement, parameters, context, executemany):
            nonlocal armed
            if armed and statement.lstrip().upper().startswith('UPDATE PIPELINE_RUNS'):
                armed = False
                with app.app_context():
                    current = db.session.get(PipelineRun, run_id)
                    current.started_at, current.status = old+timedelta(minutes=1), 'running'
                    current.stages_json, current.regime = '{}', 'neutral'
                    db.session.commit()
        event.listen(engine, 'before_cursor_execute', before_update)
        try:
            if action == 'save':
                assert not save_pipeline_results(run_id, result(old))
            elif action.startswith('finish_'):
                assert not finish_pipeline_run(run_id, action.split('_')[1], result(old))
            else:
                assert not save_pipeline_progress(run_id, {'S4': 30}, 'bear', started_at=old)
        finally:
            event.remove(engine, 'before_cursor_execute', before_update)
        db.session.expire_all()
        current = db.session.get(PipelineRun, run_id)
        assert current.status == 'running' and current.regime == 'neutral'
        assert current.stages_json == '{}'
        assert DailyRecommendation.query.count() == SimulatedTrade.query.count() == 0


def test_formal_transaction_rolls_back_recommendations_trades_and_completion(tmp_path, monkeypatch):
    from web.models import db, PipelineRun, DailyRecommendation, SimulatedTrade
    from web.services.pipeline_service import save_pipeline_results
    app = make_web_app(tmp_path, monkeypatch)
    with app.app_context():
        run = PipelineRun(trading_date=date(2026, 10, 30), status='running')
        db.session.add(run); db.session.commit()
        run_id, started = run.id, run.started_at
        db.session.execute(text("CREATE TRIGGER fail_trade BEFORE INSERT ON simulated_trades "
                                "BEGIN SELECT RAISE(FAIL, 'trade unavailable'); END"))
        db.session.commit()
        with pytest.raises(Exception, match='trade unavailable'):
            save_pipeline_results(run_id, result(started))
        db.session.expire_all()
        assert PipelineRun.query.one().status == 'running'
        assert DailyRecommendation.query.count() == SimulatedTrade.query.count() == 0


def test_restart_waits_for_formal_transaction_before_deleting_old_rows(tmp_path, monkeypatch):
    import web.services.pipeline_service as service
    from web.models import db, PipelineRun, DailyRecommendation, SimulatedTrade
    app = make_web_app(tmp_path, monkeypatch)
    with app.app_context():
        run = PipelineRun(trading_date=date(2026, 10, 30), status='completed')
        db.session.add(run); db.session.commit()
        run_id, started = run.id, run.started_at
    entered, release, restart_done = threading.Event(), threading.Event(), threading.Event()
    errors = []
    original = service._auto_create_simulated_trades
    def paused_trade(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)
    monkeypatch.setattr(service, '_auto_create_simulated_trades', paused_trade)
    def save():
        try:
            with app.app_context():
                service.save_pipeline_results(run_id, result(started))
        except Exception as e:
            errors.append(e)
    def restart():
        try:
            with app.app_context():
                run = db.session.get(PipelineRun, run_id)
                assert service.restart_pipeline_run(run)
                restart_done.set()
        except Exception as e:
            errors.append(e)
    saver = threading.Thread(target=save)
    restarter = threading.Thread(target=restart)
    saver.start()
    try:
        assert entered.wait(2)
        restarter.start()
        assert not restart_done.wait(0.10)
    finally:
        release.set()
        saver.join(3)
        if restarter.ident is not None:
            restarter.join(3)
    assert not errors and restart_done.is_set()
    with app.app_context():
        assert PipelineRun.query.one().status == 'running'
        assert DailyRecommendation.query.count() == SimulatedTrade.query.count() == 0


def test_stale_restart_cannot_delete_current_recommendations(tmp_path, monkeypatch):
    from web.models import db, PipelineRun, DailyRecommendation
    from web.services.pipeline_service import restart_pipeline_run
    app = make_web_app(tmp_path, monkeypatch)
    with app.app_context():
        old = PipelineRun(trading_date=date(2026, 10, 30), status='completed')
        db.session.add(old); db.session.commit()
        run_id, started = old.id, old.started_at
        with app.app_context():
            current = db.session.get(PipelineRun, run_id)
            assert restart_pipeline_run(current)
            db.session.add(DailyRecommendation(run_id=run_id, code='new', name='新批',
                level='watch', final_score=60, rule_score=60, recommendation_date=current.trading_date))
            db.session.commit()
        assert old.started_at == started  # 模拟两个请求同时读到同一个旧批次。
        assert not restart_pipeline_run(old)
        assert DailyRecommendation.query.one().code == 'new'
        assert db.session.get(PipelineRun, run_id).status == 'running'


@pytest.mark.parametrize('phase', ['headers', 'body'])
def test_deadline_terminates_inflight_http_and_releases_shared_gate(monkeypatch, phase, record_property):
    from data_provider import rate_limiter
    started, disconnected = threading.Event(), threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            started.set()
            try:
                if phase == 'headers':
                    self.wfile.write(b'HTTP/1.1 200 OK\r\nX-Slow: ')
                    for _ in range(150):
                        self.wfile.write(b'x'); self.wfile.flush(); time.sleep(0.02)
                else:
                    self.send_response(200)
                    self.send_header('Content-Length', '10000')
                    self.end_headers()
                    for _ in range(150):
                        self.wfile.write(b'x'); self.wfile.flush(); time.sleep(0.02)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                disconnected.set()
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    monkeypatch.setattr(rate_limiter, '_lock', threading.Lock())
    monkeypatch.setattr(rate_limiter, '_last_em_call', 0)
    monkeypatch.setattr(rate_limiter, 'EM_MIN_INTERVAL', 0)
    # 本地请求使用1.5秒预算，为Windows spawn保留启动时间；服务器滴流长于截止。
    import multiprocessing
    children = {p.pid for p in multiprocessing.active_children()}
    before = time.monotonic()
    try:
        with pytest.raises(requests.Timeout):
            rate_limiter.em_get(f'http://127.0.0.1:{server.server_port}/slow', timeout=1,
                                deadline=before+1.5)
        elapsed = time.monotonic()-before
        assert started.is_set() and elapsed < 2.3
        assert disconnected.wait(0.5)
        assert not rate_limiter._lock.locked()
        assert {p.pid for p in multiprocessing.active_children()} <= children
        session = MagicMock()
        monkeypatch.setattr(rate_limiter, '_get_em_session', lambda: session)
        following = time.monotonic()
        rate_limiter.em_get('https://formal.test', timeout=1)
        assert time.monotonic()-following < 0.1
        record_property(f'{phase}_deadline_elapsed_seconds', elapsed)
    finally:
        server.shutdown(); server.server_close(); thread.join(1)


def test_child_startup_cannot_send_request_after_deadline(monkeypatch):
    from data_provider import rate_limiter
    sender, session = MagicMock(), MagicMock()
    factory = MagicMock()
    factory.return_value.__enter__.return_value = session
    monkeypatch.setattr(rate_limiter.requests, 'Session', factory)
    rate_limiter._isolated_get(sender, 'https://never.test', 5, time.monotonic()-1, {})
    session.get.assert_not_called()
    assert sender.send.call_args.args[0][:2] == (False, True)
    sender.close.assert_called_once()


def test_deadline_http_preserves_response_and_error_contract(monkeypatch, record_property):
    from data_provider import rate_limiter
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"ok": true}'
            self.send_response(200 if self.path.startswith('/ok') else 503)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Content-Type', 'application/json')
            self.end_headers(); self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    monkeypatch.setattr(rate_limiter, '_last_em_call', 0)
    monkeypatch.setattr(rate_limiter, 'EM_MIN_INTERVAL', 0)
    try:
        root = f'http://127.0.0.1:{server.server_port}'
        before = time.monotonic()
        response = rate_limiter.em_get(root+'/ok', params={'page': 1}, deadline=before+3)
        record_property('local_http_with_spawn_seconds', time.monotonic()-before)
        assert response.json() == {'ok': True} and 'page=1' in response.url
        response.raise_for_status()
        response = rate_limiter.em_get(root+'/unavailable', deadline=time.monotonic()+3)
        with pytest.raises(requests.HTTPError):
            response.raise_for_status()
    finally:
        server.shutdown(); server.server_close(); thread.join(1)
