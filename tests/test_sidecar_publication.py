"""正式推荐提前发布、补充结果隔离及旧批次保护。仅临时SQLite。"""
import json
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock

from tests.test_pipeline_progress import make_web_app
from analysis.merger import merge_and_rank
from screening.context import StockContext


def test_rules_commit_before_sidecar_and_backfill_never_changes_trades(tmp_path, monkeypatch):
    from web.models import db, PipelineRun, DailyRecommendation, SimulatedTrade, PipelineSidecar
    from web.services.pipeline_service import PipelineService, save_pipeline_results
    app = make_web_app(tmp_path, monkeypatch)
    c = StockContext(code='000001', name='测试', price=10, total_score=80, score_tail_strength=80)
    merge_and_rank([c], {})
    observed = []
    class FakePipeline:
        def __init__(self, *args, **kwargs):
            self.regime, self.tracker, self.top30 = 'neutral', MagicMock(stages={}), [c]
            self.llm_runner = object()
        def run(self, stages=None):
            self.on_rules_ready(self.top30)
            # 模拟规则发布后，后续报告/旁路尚未结束。
            with app.test_client() as client:
                page = client.get('/recommendations/?date=2026-10-30')
                assert page.status_code == 200
                assert '测试' in page.get_data(as_text=True)
            assert DailyRecommendation.query.count() == 1
            assert SimulatedTrade.query.count() == 1
            assert PipelineRun.query.one().status == 'completed'
            observed.append('rules_committed')
        def start_sidecar(self, on_complete=None):
            assert DailyRecommendation.query.count() == 1
            assert SimulatedTrade.query.count() == 1
            assert PipelineRun.query.one().status == 'completed'
            self.complete = on_complete
            observed.append('sidecar_started')
    monkeypatch.setattr('orchestration.pipeline.LateSessionPipeline', FakePipeline)
    with app.app_context():
        run = PipelineRun(trading_date=date(2026, 10, 30))
        db.session.add(run); db.session.commit()
        service = PipelineService(); service.run_id = run.id
        result = service.run()
        before = [(r.id, r.final_score, r.level) for r in DailyRecommendation.query.all()]
        save_pipeline_results(run.id, result)
        service._pipeline.complete({'000001': {'status': 'completed', 'fallback': False,
            'summary': '模型看空', 'events': [], 'conflicts': [], 'unknowns': [], 'llm_score': 0}})
        db.session.expire_all()
        assert before == [(r.id, r.final_score, r.level) for r in DailyRecommendation.query.all()]
        assert SimulatedTrade.query.count() == 1
        sidecar = db.session.get(PipelineSidecar, run.id)
        assert sidecar.status == 'completed'
        assert json.loads(sidecar.results_json)['000001']['summary'] == '模型看空'
        assert PipelineRun.query.one().status == 'completed'
        data = app.test_client().get(f'/pipeline/status/{run.id}').get_json()
        assert data['sidecar']['status'] == 'completed'


def test_sidecar_generation_rejects_late_result_after_same_day_restart(tmp_path, monkeypatch):
    from web.models import db, PipelineRun, PipelineSidecar
    from web.services.pipeline_service import complete_sidecar
    app = make_web_app(tmp_path, monkeypatch)
    with app.app_context():
        started = datetime(2026, 10, 30, 14, 35)
        run = PipelineRun(trading_date=date(2026, 10, 30), started_at=started, status='completed')
        db.session.add(run); db.session.flush()
        state = PipelineSidecar(run_id=run.id, generation='new', run_started_at=started,
            status='running', deadline_at=started+timedelta(seconds=30))
        db.session.add(state); db.session.commit()
        assert not complete_sidecar(app, run.id, 'old', started, {'old': {'fallback': False}})
        assert db.session.get(PipelineSidecar, run.id).results_json is None
        run.started_at = started + timedelta(minutes=1); db.session.commit()
        assert not complete_sidecar(app, run.id, 'new', started, {'old': {'fallback': False}})


def test_post_publication_error_keeps_formal_results(tmp_path, monkeypatch):
    from web.models import db, PipelineRun, DailyRecommendation
    from web.services.pipeline_service import PipelineService
    app = make_web_app(tmp_path, monkeypatch)
    c = StockContext(code='a', name='测试', total_score=80, price=10)
    merge_and_rank([c], {})
    class FakePipeline:
        def __init__(self, *args, **kwargs):
            self.regime, self.tracker, self.top30 = 'neutral', MagicMock(stages={}), [c]
            self.llm_runner = None
        def run(self, stages=None):
            self.on_rules_ready(self.top30)
            raise RuntimeError('report failed after publication')
    monkeypatch.setattr('orchestration.pipeline.LateSessionPipeline', FakePipeline)
    with app.app_context():
        run = PipelineRun(trading_date=date(2026, 10, 30))
        db.session.add(run); db.session.commit()
        service = PipelineService(); service.run_id = run.id
        result = service.run()
        assert not result.get('error')
        assert DailyRecommendation.query.count() == 1
        assert db.session.get(PipelineRun, run.id).status == 'completed'


def test_30_rules_publish_without_waiting_and_timeout_backfill_isolated(tmp_path, monkeypatch, record_property):
    import threading
    import time
    from copy import deepcopy
    from pathlib import Path
    from analysis.parallel_runner import ParallelLLMRunner
    from orchestration.pipeline import LateSessionPipeline
    from web.models import db, PipelineRun, DailyRecommendation, SimulatedTrade, PipelineSidecar
    from web.services.pipeline_service import PipelineService
    app = make_web_app(tmp_path, monkeypatch)
    monkeypatch.setenv('LLM_BATCH_TIMEOUT', '1')
    release, entered = threading.Event(), threading.Event()
    client = MagicMock()
    def chat(*args, **kwargs):
        entered.set(); release.wait(3)
        return json.dumps(dict(summary='late', events=[], conflicts=[], unknowns=[]))
    client.chat.side_effect = chat
    class FakePipeline:
        _json_safe = staticmethod(LateSessionPipeline._json_safe)
        start_sidecar = LateSessionPipeline.start_sidecar
        def __init__(self, config, test_mode):
            self.config = config
            config.report_output_dir = str(tmp_path / 'reports')
            self.regime, self.tracker, self.sidecar_thread = 'neutral', MagicMock(stages={}), None
            self.top30 = [StockContext(code=f'{i:06d}', name=f'测试{i}', price=10,
                                      total_score=60, score_tail_strength=60) for i in range(30)]
            merge_and_rank(self.top30, {})
            self._sidecar_contexts = deepcopy(self.top30)
            self.llm_runner = ParallelLLMRunner(client, max_workers=1, batch_timeout=1)
        def run(self, stages=None):
            self.on_rules_ready(self.top30)
    monkeypatch.setattr('orchestration.pipeline.LateSessionPipeline', FakePipeline)
    with app.app_context():
        run = PipelineRun(trading_date=date(2026, 10, 30))
        db.session.add(run); db.session.commit()
        service = PipelineService(); service.run_id = run.id
        before = time.monotonic()
        result = service.run()
        publication_elapsed = time.monotonic()-before
        record_property('rules_publication_seconds', publication_elapsed)
        record_property('formal_recommendations', 30)
        assert publication_elapsed < 0.7
        assert len(result['recommendations']) == 30
        assert entered.wait(0.5)
        formal = [(r.code, r.final_score, r.level) for r in DailyRecommendation.query.order_by(DailyRecommendation.code)]
        assert len(formal) == 30 and SimulatedTrade.query.count() == 30
        try:
            service._pipeline.sidecar_thread.join(2)
            assert not service._pipeline.sidecar_thread.is_alive()
            db.session.expire_all()
            state = db.session.get(PipelineSidecar, run.id)
            assert state.status == 'timed_out'
            assert len(json.loads(state.results_json)) == 30
            assert formal == [(r.code, r.final_score, r.level) for r in DailyRecommendation.query.order_by(DailyRecommendation.code)]
            assert SimulatedTrade.query.count() == 30
            audit = list(Path(tmp_path / 'reports').glob('*.llm.json'))
            assert len(audit) == 1 and len(json.loads(audit[0].read_text(encoding='utf-8'))) == 30
            persisted = state.results_json
        finally:
            release.set()
        time.sleep(0.03)
        db.session.expire_all()
        assert db.session.get(PipelineSidecar, run.id).results_json == persisted


def test_status_resets_old_generation_cursor_and_hides_old_sidecar(tmp_path, monkeypatch):
    from web.models import db, PipelineRun, PipelineSidecar, PipelineLog
    app = make_web_app(tmp_path, monkeypatch)
    with app.app_context():
        old = datetime(2026, 10, 30, 14, 35)
        run = PipelineRun(trading_date=date(2026, 10, 30), started_at=old+timedelta(minutes=1))
        db.session.add(run); db.session.flush()
        db.session.add(PipelineSidecar(run_id=run.id, generation='old', run_started_at=old,
                                      status='completed', results_json='{"old": {"summary": "old"}}'))
        db.session.add(PipelineLog(run_id=run.id, stage='S0', level='INFO', message='new'))
        db.session.commit(); run_id = run.id
    data = app.test_client().get(f'/pipeline/status/{run_id}?after=999&generation={old.isoformat()}').get_json()
    assert [x['message'] for x in data['logs']] == ['new']
    assert data['sidecar']['results'] == {}


def test_late_formal_save_cannot_reinsert_after_restart_with_cached_orm(tmp_path, monkeypatch):
    from web.models import db, PipelineRun, DailyRecommendation, SimulatedTrade
    from web.services.pipeline_service import save_pipeline_results, is_current_result
    app = make_web_app(tmp_path, monkeypatch)
    with app.app_context():
        old = datetime(2026, 10, 30, 14, 35)
        run = PipelineRun(trading_date=date(2026, 10, 30), started_at=old, status='completed')
        db.session.add(run); db.session.commit()
        run_id = run.id
        assert run.started_at == old  # 强制加载旧线程的ORM缓存。
        with app.app_context():
            current = db.session.get(PipelineRun, run_id)
            current.started_at, current.status = old+timedelta(minutes=1), 'running'
            db.session.commit()
        assert run.started_at == old
        result = {'run_started_at': old.isoformat(), 'regime': 'neutral', 'stages': {},
                  'recommendations': [{'code': 'old', 'name': '旧批', 'level': 'buy',
                                       'final_score': 80, 'entry_price': 10}]}
        assert not is_current_result(run, result)
        save_pipeline_results(run_id, result)
        assert DailyRecommendation.query.count() == SimulatedTrade.query.count() == 0
        db.session.expire_all()
        assert db.session.get(PipelineRun, run_id).status == 'running'


def test_background_batch_exception_marks_30_unassessed_without_mutating_rules(tmp_path):
    from copy import deepcopy
    from orchestration.pipeline import LateSessionPipeline
    from orchestration.config import SystemConfig
    p = LateSessionPipeline.__new__(LateSessionPipeline)
    p.config = SystemConfig(report_output_dir=str(tmp_path))
    p.llm_runner = MagicMock()
    p.llm_runner.analyze_batch.side_effect = RuntimeError('batch failed')
    p.sidecar_thread = None
    p.top30 = [StockContext(code=f'{i:06d}', name='测试', total_score=60) for i in range(30)]
    merge_and_rank(p.top30, {})
    before = deepcopy([(c.code, c.final_score, c.final_rank, c.recommendation) for c in p.top30])
    completed = []
    worker = p.start_sidecar(on_complete=completed.append)
    assert p.start_sidecar(on_complete=completed.append) is worker
    worker.join(2)
    assert not worker.is_alive() and len(completed) == 1
    assert len(completed[0]) == 30 and all(r['fallback'] for r in completed[0].values())
    assert before == [(c.code, c.final_score, c.final_rank, c.recommendation) for c in p.top30]
    assert p.llm_runner.analyze_batch.call_count == 1


def test_cli_prints_formal_report_before_waiting_for_background(monkeypatch, capsys):
    import main
    class Thread:
        def join(self, timeout=None):
            assert '报告: formal.md' in capsys.readouterr().out
            assert timeout > 0
    class Pipeline:
        def __init__(self, *args, **kwargs):
            self.sidecar_thread = Thread()
        def run(self, stages=None):
            return 'formal.md'
    monkeypatch.setattr(main, 'LateSessionPipeline', Pipeline)
    monkeypatch.setattr('sys.argv', ['main.py', '--test'])
    main.main()


def test_existing_database_adds_sidecar_table_without_changing_formal_rows(tmp_path, monkeypatch):
    import sqlite3
    instance = tmp_path / 'instance'
    instance.mkdir()
    connection = sqlite3.connect(instance / 'dashboard.db')
    connection.execute('''CREATE TABLE pipeline_runs (
        id INTEGER PRIMARY KEY, trading_date DATE NOT NULL UNIQUE, regime VARCHAR(16) NOT NULL,
        status VARCHAR(16) NOT NULL, started_at DATETIME NOT NULL, finished_at DATETIME,
        stages_json TEXT, snapshot_dir VARCHAR(256))''')
    connection.execute("INSERT INTO pipeline_runs VALUES (1,'2026-10-30','neutral','completed',"
                       "'2026-10-30 14:35:00.000000',NULL,'{}',NULL)")
    connection.commit(); connection.close()
    app = make_web_app(tmp_path, monkeypatch)
    from web.models import db, PipelineRun, PipelineSidecar
    with app.app_context():
        assert db.session.get(PipelineRun, 1).status == 'completed'
        assert PipelineSidecar.query.count() == 0
    data = app.test_client().get('/pipeline/status/1').get_json()
    assert data['sidecar']['status'] == 'not_requested'


def test_deadline_ui_waits_briefly_for_terminal_commit_but_not_forever(tmp_path, monkeypatch):
    from web.models import db, PipelineRun, PipelineSidecar
    from web.services.pipeline_service import sidecar_payload
    app = make_web_app(tmp_path, monkeypatch)
    with app.app_context():
        run = PipelineRun(trading_date=date(2026, 10, 30))
        db.session.add(run); db.session.flush()
        state = PipelineSidecar(run_id=run.id, generation='current', run_started_at=run.started_at,
            status='running', deadline_at=datetime.now()-timedelta(seconds=1), results_json='{}')
        db.session.add(state); db.session.commit()
        payload = sidecar_payload(run)
        assert payload['status'] == 'timed_out' and payload['refresh_pending']
        state.deadline_at = datetime.now()-timedelta(seconds=10); db.session.commit()
        assert not sidecar_payload(run)['refresh_pending']
