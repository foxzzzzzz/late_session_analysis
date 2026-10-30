import json
import logging
import threading
import pytest
from datetime import date, datetime

def make_web_app(tmp_path, monkeypatch):
    monkeypatch.setenv('WEB_INSTANCE_DIR', str(tmp_path / 'instance'))
    monkeypatch.setenv('WEB_SCHEDULER_ENABLED', 'false')
    from web.app import create_app
    app = create_app()
    app.config.update(TESTING=True)
    return app


def test_stage_progress_is_visible_before_run_finishes(tmp_path, monkeypatch, caplog):
    from web.models import db, PipelineRun
    from web.routes.pipeline import _log_handler
    from web.services.pipeline_service import PipelineService
    from orchestration.stage_tracker import StageTracker

    app = make_web_app(tmp_path, monkeypatch)
    caplog.set_level(logging.INFO)
    observed = []

    class FakePipeline:
        def __init__(self, config, test_mode):
            self.regime = 'bear'
            self.tracker = StageTracker()
            self.top30 = []

        def run(self, stages=None):
            self.tracker.stage_start('S0_板块预筛选', 5000)
            self.tracker.stage_end('S0_板块预筛选', 579)
            logging.getLogger('data_provider.kline_provider').info('日线加载中')
            with app.test_client() as client:
                response = client.get(f'/pipeline/status/{run_id}?after=0')
                assert response.status_code == 200
                observed.append(response.get_json())
            self.tracker.stage_start('S1_K线扫描', 579)
            raise RuntimeError('test failure after S0')

    monkeypatch.setattr('orchestration.pipeline.LateSessionPipeline', FakePipeline)
    original_handlers = list(logging.getLogger().handlers)
    with app.app_context():
        run = PipelineRun(trading_date=date.today(), status='running', started_at=datetime.now())
        db.session.add(run)
        db.session.commit()
        run_id = run.id
        service = PipelineService(log_callback=lambda *args: _log_handler(run_id, *args))
        service.run_id = run_id
        result = service.run()
        assert result['stages'] == {'S0': 579}
        assert 'error' in result
    assert list(logging.getLogger().handlers) == original_handlers
    assert observed[0]['status'] == 'running'
    assert observed[0]['stages'] == {'S0': 579}
    assert observed[0]['regime'] == 'bear'
    assert any('日线加载中' in log['message'] for log in observed[0]['logs'])


def test_status_cursor_and_completed_page(tmp_path, monkeypatch):
    from web.models import db, PipelineRun, PipelineLog
    app = make_web_app(tmp_path, monkeypatch)
    with app.app_context():
        run = PipelineRun(trading_date=date.today(), status='completed',
                          stages_json=json.dumps({'S0': 0}), finished_at=datetime.now())
        db.session.add(run)
        db.session.flush()
        first = PipelineLog(run_id=run.id, stage='S0', level='INFO', message='<script>bad</script>')
        db.session.add(first)
        db.session.flush()
        first_id = first.id
        db.session.add(PipelineLog(run_id=run.id, stage='SYS', level='INFO', message='done'))
        db.session.commit()
        run_id = run.id
    client = app.test_client()
    response = client.get(f'/pipeline/status/{run_id}?after={first_id}').get_json()
    assert [x['message'] for x in response['logs']] == ['done']
    assert response['stages'] == {'S0': 0}
    assert response['total_logs'] == 2
    assert client.get('/pipeline/status/999999').status_code == 404
    assert client.get(f'/pipeline/status/{run_id}?after=bad').status_code == 400
    page = client.get(f'/pipeline/{run_id}').get_data(as_text=True)
    assert '&lt;script&gt;bad&lt;/script&gt;' in page
    assert 'pipeline-progress.js' in page


def test_runtime_capture_excludes_other_threads():
    from web.services.pipeline_service import PipelineService
    events = []
    service = PipelineService(log_callback=lambda *args: events.append(args))
    root = logging.getLogger()
    old_level = root.level
    root.setLevel(logging.INFO)
    try:
        with service._capture_runtime_logs():
            logger = logging.getLogger('orchestration.pipeline')
            logger.info('own run')
            thread = threading.Thread(target=lambda: logger.info('other run'))
            thread.start()
            thread.join()
        assert [event[2] for event in events] == ['own run']
    finally:
        root.setLevel(old_level)


@pytest.mark.parametrize('scheduled', [True, False])
def test_execution_paths_preserve_progress_on_failure(tmp_path, monkeypatch, caplog, scheduled):
    from web.models import db, PipelineRun, PipelineLog
    from orchestration.stage_tracker import StageTracker
    app = make_web_app(tmp_path, monkeypatch)
    caplog.set_level(logging.INFO)

    class FakePipeline:
        def __init__(self, config, test_mode):
            self.tracker = StageTracker()
            self.regime = 'bear'

        def run(self, stages=None):
            self.tracker.stage_start('S0_板块预筛选', 5000)
            self.tracker.stage_end('S0_板块预筛选', 0)
            raise RuntimeError('failure after completed stage')

    monkeypatch.setattr('orchestration.pipeline.LateSessionPipeline', FakePipeline)
    monkeypatch.setattr('time.sleep', lambda seconds: None)
    if scheduled:
        from web.scheduler import _execute_scheduled_pipeline
        # 固定交易日，不依赖测试执行当天是否为周末。
        class TradingDate(date):
            @classmethod
            def today(cls):
                return cls(2026, 10, 30)
        monkeypatch.setattr('web.scheduler.date', TradingDate)
        _execute_scheduled_pipeline(app)
    else:
        from web.routes.pipeline import _run_pipeline_in_thread
        with app.app_context():
            run = PipelineRun(trading_date=date.today(), status='running')
            db.session.add(run)
            db.session.commit()
            run_id = run.id
        _run_pipeline_in_thread(app, run_id)
    with app.app_context():
        run = PipelineRun.query.one()
        assert run.status == 'failed'
        assert json.loads(run.stages_json) == {'S0': 0}
        assert any('完成' in log.message for log in PipelineLog.query.filter_by(run_id=run.id))
    response = app.test_client().get('/api/recent-runs').get_json()
    assert response['runs'][0]['stages'] == {'S0': 0}
