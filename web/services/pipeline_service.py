"""Pipeline execution service — wraps LateSessionPipeline for web use."""

from __future__ import annotations

import logging
import json
import threading
from contextlib import contextmanager
from datetime import datetime, date
from typing import Callable

logger = logging.getLogger(__name__)

# Type for log callback: (stage, level, message) -> None
LogCallback = Callable[[str, str, str], None]


class PipelineService:
    """Thin wrapper that runs the pipeline and emits structured log events."""

    def __init__(self, log_callback: LogCallback | None = None):
        self._callback = log_callback
        self.run_id = None
        self._pipeline = None
        self._completed_stages = {}
        self._published_results = None
        self._run_started_at = None
        self._sidecar_generation = None
        self._sidecar_start_attempted = False

    @contextmanager
    def _capture_runtime_logs(self):
        """仅转发本次执行线程的业务日志，避免混入其他任务或递归转发。"""
        service = self
        owner = threading.get_ident()

        class RuntimeHandler(logging.Handler):
            stage = 'SYS'

            def emit(self, record):
                if record.thread != owner or not record.name.startswith(
                        ('orchestration.', 'data_provider.', 'screening.', 'analysis.')):
                    return
                self.stage = getattr(record, 'pipeline_stage', self.stage)
                try:
                    if not service._is_current_run():
                        return
                    counts = getattr(record, 'pipeline_stage_counts', None)
                    if counts is not None:
                        service._completed_stages = counts.copy()
                        if service.run_id is not None:
                            save_pipeline_progress(service.run_id, counts,
                                                   getattr(service._pipeline, 'regime', None),
                                                   started_at=service._run_started_at)
                    if service._callback:
                        service._callback(self.stage, record.levelname, record.getMessage())
                except Exception:
                    from web.models import db
                    db.session.rollback()
                    logger.exception('Failed to persist live pipeline progress')

        handler = RuntimeHandler(level=logging.INFO)
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            yield
        finally:
            root.removeHandler(handler)

    def _emit(self, stage: str, level: str, message: str):
        if self._callback:
            try:
                if not self._is_current_run():
                    return
                self._callback(stage, level, message)
            except Exception:
                pass
        getattr(logger, level.lower(), logger.info)(f"[{stage}] {message}")

    def run(self, test_mode: bool = True, regime_override: str | None = None) -> dict:
        with self._capture_runtime_logs():
            return self._run(test_mode, regime_override)

    def _run(self, test_mode: bool = True, regime_override: str | None = None) -> dict:
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

        self._config = config
        if self.run_id is not None:
            from web.models import db, PipelineRun
            run = db.session.get(PipelineRun, self.run_id)
            self._run_started_at = run.started_at if run else None

        # Init pipeline
        try:
            from orchestration.pipeline import LateSessionPipeline
            pipeline = LateSessionPipeline(config, test_mode=test_mode)
            self._pipeline = pipeline
            pipeline.on_rules_ready = self._publish_rules
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
            if self._published_results is not None:
                self._emit('SYS', 'WARNING', f'规则已发布，后续报告异常不撤销推荐: {type(e).__name__}')
                self._start_sidecar()
                return self._published_results
            self._emit("SYS", "ERROR", f"Pipeline failed: {e}")
            import traceback
            self._emit("SYS", "ERROR", traceback.format_exc()[-500:])
            return {"regime": getattr(pipeline, "regime", "unknown"), "stages": self._completed_stages.copy(), "recommendations": [], "error": str(e)}

        if self._published_results is None:
            self._publish_rules(getattr(pipeline, 'top30', []))
        self._start_sidecar()
        return self._published_results

    def _is_current_run(self):
        if self.run_id is None or self._run_started_at is None:
            return True
        from web.models import PipelineRun
        started = PipelineRun.query.with_entities(PipelineRun.started_at).filter_by(id=self.run_id).scalar()
        return started == self._run_started_at

    def _publish_rules(self, contexts):
        if self._published_results is not None:
            return
        self._pipeline.top30 = contexts
        results = self._collect_results(self._pipeline)
        results['run_started_at'] = self._run_started_at.isoformat() if self._run_started_at else None
        if not self._is_current_run():
            results['stale'] = True
            self._published_results = results
            return
        if self.run_id is not None:
            if not save_pipeline_results(self.run_id, results):
                results['stale'] = True
                self._published_results = results
                return
        # 正式推荐、模拟交易与completed已在同一事务提交，后续旁路失败不传播。
        import time
        self._pipeline._sidecar_deadline = time.monotonic() + self._config.llm_batch_timeout
        self._published_results = results
        self._emit('S4', 'INFO', '正式规则推荐已发布并保存；旁路不会修改正式结果')
        try:
            self._initialize_sidecar(contexts)
            self._start_sidecar()
        except Exception as e:
            from web.models import db
            db.session.rollback()
            self._sidecar_start_attempted = True
            self._emit('LLM', 'WARNING', f'旁路状态初始化失败，未评估；正式结果已保存: {type(e).__name__}')

    def _initialize_sidecar(self, contexts):
        if self.run_id is not None:
            from uuid import uuid4
            from datetime import timedelta
            from web.models import db, PipelineSidecar
            # 与重跑争用同一数据库写锁；不能以旧批次初始化覆盖新旁路。
            if not _batch_query(self.run_id, self._published_results).update(
                    {'id': self.run_id}, synchronize_session=False):
                db.session.rollback()
                return
            self._sidecar_generation = uuid4().hex
            state = db.session.get(PipelineSidecar, self.run_id)
            if state is None:
                state = PipelineSidecar(run_id=self.run_id)
                db.session.add(state)
            state.generation = self._sidecar_generation
            state.run_started_at = self._run_started_at
            enabled = bool(getattr(self._pipeline, 'llm_runner', None))
            state.status = 'pending' if enabled else 'disabled'
            import time
            state.deadline_at = datetime.now() + timedelta(
                seconds=max(0, self._pipeline._sidecar_deadline-time.monotonic()))
            state.finished_at = None
            state.results_json = json.dumps({c.code: c.llm_analysis for c in contexts}, ensure_ascii=False)
            db.session.commit()

    def _start_sidecar(self):
        if (self._sidecar_start_attempted or not getattr(self._pipeline, 'llm_runner', None)
                or not callable(getattr(self._pipeline, 'start_sidecar', None))):
            return
        self._sidecar_start_attempted = True
        try:
            if not self._is_current_run():
                return
            if self.run_id is None:
                self._pipeline.start_sidecar()
                return
            from flask import current_app
            from web.models import db, PipelineSidecar
            app = current_app._get_current_object()
            run_id, generation, started = self.run_id, self._sidecar_generation, self._run_started_at
            current_run = _batch_query(run_id, self._published_results).exists()
            updated = (PipelineSidecar.query.filter_by(run_id=run_id, generation=generation,
                                                      run_started_at=started, status='pending')
                       .filter(current_run).update({'status': 'running'}, synchronize_session=False))
            db.session.commit()
            if not updated:
                return
            self._pipeline.start_sidecar(on_complete=lambda results: complete_sidecar(
                app, run_id, generation, started, results))
        except Exception as e:
            from web.models import db
            db.session.rollback()
            self._emit('LLM', 'WARNING', f'旁路启动失败，正式结果不变: {type(e).__name__}')
            if self.run_id is not None:
                try:
                    from flask import current_app
                    complete_sidecar(current_app._get_current_object(), self.run_id,
                        self._sidecar_generation, self._run_started_at,
                        {c.code: {'fallback': True, 'summary': '未评估',
                                 'unknowns': ['旁路启动失败']} for c in self._pipeline.top30})
                except Exception:
                    db.session.rollback()

    def _collect_results(self, pipeline):
        # 只读取规则快照，不按旁路结果二次计算推荐。
        regime = pipeline.regime

        # Stage pass counts from tracker
        stages = {}
        if hasattr(pipeline, "tracker") and pipeline.tracker:
            tracker = pipeline.tracker
            for stage_name, stage_info in tracker.stages.items():
                short = stage_name.split("_")[0]  # "S0_板块预筛选" → "S0"
                if stage_info.end_time is not None:
                    stages[short] = stage_info.output_count

        # Recommendations from the last S4 run
        recommendations = []
        if hasattr(pipeline, "top30") and pipeline.top30:
            self._emit("S4", "INFO", f"=== S4 通过股票详情 (共 {len(pipeline.top30)} 只) ===")
            for i, ctx in enumerate(pipeline.top30, 1):
                level = ctx.recommendation or "watch"
                analysis = getattr(ctx, 'llm_analysis', {})
                llm_status = '未评估' if analysis.get('fallback', True) else '已完成'
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
                           f"正式评分={final_s:.1f} (规则) | LLM旁路={llm_status} | "
                           f"等级={level} | 板块={sector}{dims}")
                if analysis:
                    self._emit('LLM', 'INFO', f'{code} {name} 旁路研究（不参与推荐）：' + json.dumps(
                        {key: analysis.get(key) for key in
                         ('as_of', 'summary', 'events', 'conflicts', 'unknowns')}, ensure_ascii=False))

                if level in ("strong_buy", "buy", "watch"):
                    recommendations.append({
                        "code": code, "name": name, "level": level,
                        "final_score": final_s, "rule_score": rule_s,
                        "sector": sector, "entry_price": price,
                    })

        self._emit("SYS", "INFO",
                   f"Results: {len(recommendations)} recommendations "
                   f"(S0={stages.get('S0','?')} S1={stages.get('S1','?')} "
                   f"S2={stages.get('S2','?')} S3={stages.get('S3','?')} "
                   f"S4={stages.get('S4','?')})")

        return {"regime": regime, "stages": stages, "recommendations": recommendations}


def is_current_result(run, results, service=None):
    expected = results.get('run_started_at') or getattr(service, '_run_started_at', None)
    if isinstance(expected, datetime):
        expected = expected.isoformat()
    if results.get('stale'):
        return False
    if expected is None:
        return True
    from web.models import PipelineRun
    # 不信任旧线程ORM identity map中的started_at，直接读当前数据库列。
    actual = PipelineRun.query.with_entities(PipelineRun.started_at).filter_by(id=run.id).scalar()
    return actual is not None and expected == actual.isoformat()


def complete_sidecar(app, run_id, generation, started, results):
    """只更新旁路表和日志；旧generation或旧启动时间的迟到结果丢弃。"""
    from web.models import db, PipelineRun, PipelineSidecar, PipelineLog
    with app.app_context():
        success = sum(not r.get('fallback', True) for r in results.values())
        timed_out = any(any('截止' in str(u) or '超时' in str(u) for u in r.get('unknowns', []))
                        for r in results.values())
        status = ('completed' if success == len(results) else 'partial' if success
                  else 'timed_out' if timed_out else 'failed')
        current_run = PipelineRun.query.filter_by(id=run_id, started_at=started).exists()
        updated = (PipelineSidecar.query.filter_by(run_id=run_id, generation=generation,
                                                  run_started_at=started).filter(current_run)
                   .update({'status': status, 'finished_at': datetime.now(),
                            'results_json': json.dumps(results, ensure_ascii=False)}, synchronize_session=False))
        if not updated:
            db.session.rollback()
            return False
        db.session.add(PipelineLog(run_id=run_id, stage='LLM', level='INFO',
            message=f'旁路补充完成: {status}, {success}/{len(results)}；正式分数、等级和交易记录未修改'))
        db.session.commit()
        return True


def sidecar_payload(run):
    from web.models import db, PipelineSidecar
    state = db.session.get(PipelineSidecar, run.id)
    if state is None or state.run_started_at != run.started_at:
        return {'status': 'not_requested', 'results': {}}
    status = state.status
    from datetime import timedelta
    from flask import current_app
    # 截止后仅多观察一个已有轮询周期，收齐终态补写；不延长模型/HTTP预算。
    refresh_pending = (status in ('pending', 'running') and state.deadline_at is not None
        and datetime.now() < state.deadline_at + timedelta(
            seconds=current_app.config['PIPELINE_POLL_SECONDS']))
    if status in ('pending', 'running') and state.deadline_at and datetime.now() >= state.deadline_at:
        status = 'timed_out'
    results = json.loads(state.results_json or '{}')
    if status == 'timed_out':
        results = {code: ({**r, 'status': 'not_evaluated', 'summary': '未评估',
                          'unknowns': r.get('unknowns', []) + ['整批截止未收到结果，不代表无风险']}
                         if r.get('status') == 'pending' else r) for code, r in results.items()}
    return {'status': status, 'refresh_pending': refresh_pending, 'results': {code: {key: r.get(key) for key in
            ('status', 'summary', 'events', 'conflicts', 'unknowns')} for code, r in results.items()}}


def _batch_query(run_id, results, service=None):
    """批次条件必须放在写入SQL上，不能依赖此前的SELECT或ORM缓存。"""
    from web.models import PipelineRun
    expected = results.get('run_started_at') or getattr(service, '_run_started_at', None)
    if isinstance(expected, str):
        expected = datetime.fromisoformat(expected)
    query = PipelineRun.query.filter_by(id=run_id)
    if results.get('stale'):
        return query.filter(False)
    if expected is not None:
        query = query.filter_by(started_at=expected)
    return query


def finish_pipeline_run(run_id, status, results, service=None):
    """原子收尾；正式已发布后不再次更新完成时间或降级为failed。"""
    from web.models import db, PipelineRun
    try:
        updated = (_batch_query(run_id, results, service).filter(PipelineRun.status != 'completed')
                   .update({'status': status, 'finished_at': datetime.now()}, synchronize_session=False))
        db.session.commit()
        return bool(updated)
    except Exception:
        db.session.rollback()
        raise


def restart_pipeline_run(run):
    """先原子认领写锁，再在同一事务清除旧批次结果。"""
    from web.models import db, PipelineRun, PipelineLog, DailyRecommendation, SimulatedTrade
    try:
        updated = (PipelineRun.query.filter_by(id=run.id, started_at=run.started_at, status=run.status)
                   .filter(PipelineRun.status.in_(('completed', 'failed')))
                   .update({'status': 'running', 'started_at': datetime.now(), 'finished_at': None,
                            'stages_json': None}, synchronize_session=False))
        if not updated:
            db.session.rollback()
            return False
        rec_ids = db.session.query(DailyRecommendation.id).filter_by(run_id=run.id)
        SimulatedTrade.query.filter(SimulatedTrade.recommendation_id.in_(rec_ids)).delete(synchronize_session=False)
        DailyRecommendation.query.filter_by(run_id=run.id).delete(synchronize_session=False)
        PipelineLog.query.filter_by(run_id=run.id).delete(synchronize_session=False)
        db.session.commit()
        db.session.expire(run)
        return True
    except Exception:
        db.session.rollback()
        raise


def save_pipeline_progress(run_id: int, stages: dict, regime: str | None = None, started_at=None):
    """阶段结束即提交；未结束阶段保持缺失，避免把未运行显示为0。"""
    from web.models import db, PipelineRun
    values = {'stages_json': json.dumps(stages, ensure_ascii=False)}
    if regime:
        values['regime'] = regime
    try:
        updated = (_batch_query(run_id, {'run_started_at': started_at})
                   .filter(PipelineRun.status != 'completed').update(values, synchronize_session=False))
        db.session.commit()
        return bool(updated)
    except Exception:
        db.session.rollback()
        raise


def save_pipeline_results(run_id: int, results: dict):
    """推荐、模拟交易与完成状态一次提交；批次条件UPDATE先获取数据库写锁。"""
    import json
    from web.models import db, PipelineRun, DailyRecommendation

    try:
        if not _batch_query(run_id, results).update({'id': run_id}, synchronize_session=False):
            db.session.rollback()
            return False
        run = db.session.get(PipelineRun, run_id, populate_existing=True)
        run.regime = results.get('regime', 'unknown')
        run.stages_json = json.dumps(results.get('stages', {}), ensure_ascii=False)
        run.status = 'completed'
        run.finished_at = run.finished_at or datetime.now()
        existing = {row.code for row in DailyRecommendation.query.filter_by(run_id=run_id).all()}
        for rec in results.get('recommendations', []):
            if rec['code'] in existing:
                continue
            dr = DailyRecommendation(
                run_id=run_id, code=rec['code'], name=rec['name'], level=rec['level'],
                final_score=rec['final_score'], rule_score=rec.get('rule_score', 0),
                llm_score=0,  # 历史非空列占位，新结果不生成LLM评分。
                sector=rec.get('sector', ''), entry_price=rec.get('entry_price', 0),
                recommendation_date=run.trading_date)
            db.session.add(dr)
            existing.add(rec['code'])
        _auto_create_simulated_trades(run_id)
        db.session.commit()
        return True
    except Exception:
        db.session.rollback()
        raise


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
    # 由save_pipeline_results统一提交，失败不能留下半批正式数据。
    db.session.flush()
    return count
