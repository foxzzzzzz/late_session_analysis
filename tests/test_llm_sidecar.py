"""LLM旁路隔离、证据约束及缺失表达验收。"""
import json
from datetime import datetime
from unittest.mock import MagicMock, patch
from copy import deepcopy

import pytest

from analysis.merger import merge_and_rank
from analysis.parallel_runner import ParallelLLMRunner
from analysis.prompts import make_stock_prompt
from data_provider.news_fetcher import NewsFetcher
from orchestration.config import SystemConfig
from orchestration.pipeline import LateSessionPipeline
from screening.context import StockContext


@pytest.mark.parametrize('results', [
    {},
    {'a': {'fallback': True}},
    {'a': {'decision': 'skip', 'confidence': 'A', 'llm_score': -999,
           'summary': '重大风险', 'events': [], 'conflicts': [], 'unknowns': []}},
    {'a': {'llm_score': 10000}, 'b': {'fallback': True}},
])
def test_llm_never_changes_formal_results(results):
    contexts = [StockContext(code='b', name='B', total_score=54),
                StockContext(code='a', name='A', total_score=80)]
    ranked = merge_and_rank(contexts, results)
    assert [(c.code, c.final_score, c.final_rank, c.recommendation) for c in ranked] == [
        ('a', 80, 1, 'strong_buy'), ('b', 54, 2, 'watch')]
    assert all(c.llm_score is None for c in ranked)


@pytest.mark.parametrize('capital,expected', [('today', (85, 75, 60)), ('none', (75, 65, 50))])
def test_thresholds_depend_on_capital_only(capital, expected, monkeypatch):
    monkeypatch.setenv('REGIME_MODE', 'neutral')
    p = LateSessionPipeline.__new__(LateSessionPipeline)
    p.config = SystemConfig(regime_mode='neutral')
    p.regime = 'neutral'
    p.capital_data_date = capital
    assert p._recommendation_thresholds() == expected


def test_prompt_distinguishes_unknown_and_measured_zero():
    c = StockContext(code='a', name='A', sector='银行')
    missing = make_stock_prompt(c)
    assert '近5日收阳率: 未获取' in missing
    assert '相似形态胜率' not in missing
    assert '大单净流入: 未获取' in missing
    assert '排名: 未获取' in missing
    assert '规则引擎评分' not in missing
    c.data_quality_flags.update(history_sample_count=5, fund_flow=True,
        fund_flow_source='sina', fund_flow_data_date='2026-10-30',
        fund_flow_fetched_at='2026-10-30T14:56:00', sector_rank=True)
    actual = make_stock_prompt(c)
    assert '近5日收阳率: 0%' in actual
    assert '样本5根' in actual
    assert '大单净流入: +0万' in actual
    assert '2026-10-30' in actual and 'sina' in actual
    assert 'top100%' in actual


def response(**kwargs):
    return json.dumps(dict(summary='有待核实', events=[], conflicts=[], unknowns=[], **kwargs), ensure_ascii=False)


def test_parser_rejects_fabricated_or_unquoted_evidence():
    evidence = [{'id': 'E1', 'text': '公司拟减持股份', 'published_at': '2026-10-29T18:00:00',
                 'url': 'https://example.com/notice', 'source': 'eastmoney_announcement'}]
    events = [dict(classification='fact', subject='股东', description='减持计划', evidence_id='E1', quote='拟减持'),
              dict(classification='fact', subject='公司', description='亏损', evidence_id='E1', quote='巨额亏损'),
              dict(classification='rumor', subject='公司', description='传闻', evidence_id='E9', quote='传闻')]
    parsed = ParallelLLMRunner._parse_response(json.dumps(dict(summary='待复核', events=events,
        conflicts=['量价强与减持计划存在冲突'], unknowns=[])), 'a', evidence)
    assert len(parsed['events']) == 1
    assert parsed['events'][0]['quote_matched'] is True
    assert len(parsed['unknowns']) == 2


def test_parser_rejects_incomplete_schema_and_truncated_json():
    assert ParallelLLMRunner._parse_response('{"decision":"buy"}', 'a') is None
    assert ParallelLLMRunner._parse_response('{"summary":"风险", "events":[', 'a') is None
    assert ParallelLLMRunner._parse_response(response().replace('"events": []', '"events": "bad"'), 'a') is None


def mock_response(payload):
    r = MagicMock()
    r.json.return_value = payload
    return r


def test_evidence_uses_publication_time_and_original_text():
    fetcher = NewsFetcher()
    notices = [dict(art_code='a', title='计划', display_time='2026-10-30 14:00:00:123'),
               dict(art_code='future', title='盘后', display_time='2026-10-30 18:00:00:000'),
               dict(art_code='unknown', title='未知', notice_date='2026-10-30 00:00:00')]
    with patch.object(fetcher, 'fetch_announcements', return_value=notices), patch(
        'data_provider.news_fetcher.em_get', return_value=mock_response({'data': {
            'notice_content': '公司公告原文：拟减持股份', 'attach_url': 'https://pdf.dfcfw.com/a.pdf'}})) as get:
        evidence = fetcher.collect_evidence('a', datetime(2026, 10, 30, 14, 57),
            max_items=3, max_chars=1000, timeout=3)
    assert len(evidence['items']) == 1
    assert evidence['items'][0]['published_at'] == '2026-10-30T14:00:00.123000'
    assert '拟减持' in evidence['items'][0]['text']
    assert get.call_count == 1
    assert evidence['unknowns']


def test_evidence_failure_is_unknown_not_no_risk():
    f = NewsFetcher()
    with patch.object(f, 'fetch_announcements', return_value=[]):
        result = f.collect_evidence('a', datetime(2026, 10, 30, 14, 57),
            max_items=3, max_chars=1000, timeout=3)
    assert result['status'] == 'unavailable'
    assert result['items'] == []
    assert result['unknowns']


def test_sidecar_runner_preserves_prompt_and_evidence_on_failure():
    c = StockContext(code='a', name='A')
    client = MagicMock()
    client.chat.return_value = None
    runner = ParallelLLMRunner(client, max_workers=1,
        evidence_loader=lambda ctx: {'status': 'unavailable', 'items': [], 'unknowns': ['公告未获取']})
    result = runner.analyze_batch([c])['a']
    assert result['fallback']
    assert '公告未获取' in result['unknowns']
    assert result['input_prompt']
    assert result['as_of']


@pytest.mark.parametrize('mode', ['disabled', 'success', 'partial', 'failed', 'exception'])
def test_stage4_formal_results_are_identical(mode):
    from screening.layer4_scoring import L4Config
    p = LateSessionPipeline.__new__(LateSessionPipeline)
    p.config = SystemConfig(regime_mode='neutral')
    p.regime, p.capital_data_date = 'neutral', 'today'
    p.tracker = MagicMock()
    p._s0_sector_map, p._kline_provider = {}, None
    p.northbound_sentiment, p.concept_analyzer = None, None
    p.funnel = MagicMock()
    p.funnel.config.l4 = L4Config()
    p.funnel.get_top.side_effect = lambda scored, n: scored[:n]
    candidates = [StockContext(code='a', name='A', total_score=80),
                  StockContext(code='b', name='B', total_score=54)]
    p._fetch_and_convert = MagicMock(side_effect=lambda codes: deepcopy(candidates))
    p._enrich_contexts = p._enrich_leader_strength = p._write_stage_snapshot = MagicMock()
    p.llm_runner = None if mode == 'disabled' else MagicMock()
    if p.llm_runner:
        if mode == 'exception':
            p.llm_runner.analyze_batch.side_effect = RuntimeError('offline')
        else:
            p.llm_runner.analyze_batch.return_value = {
                'a': {'fallback': mode == 'failed', 'llm_score': 0, 'decision': 'skip'},
                'b': {'fallback': mode in ('failed', 'partial'), 'llm_score': 100, 'decision': 'buy'}}
    with patch('screening.layer4_scoring.score_l4', side_effect=lambda contexts, *args: contexts):
        result = p._run_stage4(candidates)
    assert [(c.code, c.final_score, c.recommendation) for c in result] == [
        ('a', 80, 'buy'), ('b', 54, 'skip')]


def test_full_report_has_unselected_sidecar_without_llm_scores():
    from report.renderer import render_report
    from tests.test_template_fields import TestStrongBuyRendering
    fixture = TestStrongBuyRendering()
    c = StockContext(code='skip', name='未入选', total_score=20, recommendation='skip')
    merge_and_rank([c], {})
    text = render_report([], [], [], fixture._make_stats(), analysis_stocks=[c],
        market_overview={'indices': [], 'breadth': {'up': 0}, 'hot_sectors': []})
    assert '未入选' in text and '未评估' in text
    assert 'LLM评分' not in text and 'LLM建议买入' not in text
    assert '成功/失败均不调整推荐阈值' in text


def test_report_persists_all_candidates_and_sidecar_inputs(tmp_path):
    p = LateSessionPipeline.__new__(LateSessionPipeline)
    p.config = SystemConfig(report_output_dir=str(tmp_path), enable_md_to_image=False)
    p.tracker = MagicMock(pipeline_start=0, pipeline_end=1, llm_success=0, llm_total=1)
    p.funnel = MagicMock(stats={})
    p.concept_analyzer = MagicMock(is_analyzed=False)
    p.fetcher_mgr = MagicMock()
    p.fetcher_mgr.get_active_name.return_value = 'fixture'
    p.llm_client, p.northbound_sentiment, p.capital_data_date = None, None, 'today'
    p.market_overview = {'indices': [], 'breadth': {'up': 0}, 'hot_sectors': []}
    c = StockContext(code='skip', name='未入选', total_score=20)
    c.data_quality_flags['fund_flow'] = True  # 真实观测的0资金流，不是缺失
    merge_and_rank([c], {'skip': {'fallback': True, 'summary': '未评估',
        'input_prompt': '冻结的输入', 'evidence': [{'id': 'E1', 'text': '公告摘录'}]}})
    path = p._generate_report([c])
    from pathlib import Path
    stored = json.loads(Path(path).with_suffix('.llm.json').read_text(encoding='utf-8'))
    assert stored[0]['recommendation'] == 'skip'
    assert stored[0]['llm_score'] is None
    assert stored[0]['llm_analysis']['input_prompt'] == '冻结的输入'
    assert stored[0]['llm_analysis']['evidence'][0]['text'] == '公告摘录'
    assert '资金流向数据完全缺失' not in Path(path).read_text(encoding='utf-8')


def test_web_logs_show_sidecar_and_do_not_restore_llm_score(tmp_path, monkeypatch):
    from tests.test_pipeline_progress import make_web_app
    from web.services.pipeline_service import PipelineService
    from web.models import db, PipelineRun, DailyRecommendation, PipelineLog
    from web.routes.pipeline import _log_handler
    from datetime import date
    app = make_web_app(tmp_path, monkeypatch)
    c = StockContext(code='a', name='测试', total_score=80, score_tail_strength=80)
    merge_and_rank([c], {'a': {'summary': '计划尚未实施', 'events': [], 'conflicts': [],
                              'unknowns': ['未证实收益影响'], 'fallback': False}})
    class FakePipeline:
        def __init__(self, *args, **kwargs):
            self.regime, self.tracker, self.top30 = 'neutral', MagicMock(stages={}), [c]
        def run(self, stages=None):
            return None
    monkeypatch.setattr('orchestration.pipeline.LateSessionPipeline', FakePipeline)
    with app.app_context():
        run = PipelineRun(trading_date=date(2026, 10, 30))
        db.session.add(run)
        db.session.commit()
        service = PipelineService(log_callback=lambda *args: _log_handler(run.id, *args))
        result = service.run()
        assert 'llm_score' not in result['recommendations'][0]
        from web.services.pipeline_service import save_pipeline_results
        save_pipeline_results(run.id, result)
        rec = DailyRecommendation.query.one()
        assert rec.final_score == 80 and rec.llm_score == 0
        logs = '\n'.join(x.message for x in PipelineLog.query.all())
        assert 'LLM旁路=已完成' in logs and '计划尚未实施' in logs
        assert 'LLM=0.0' not in logs


def test_original_text_failure_and_limits_are_preserved():
    import requests
    f = NewsFetcher()
    notices = [dict(art_code=str(i), title='公告', display_time='2026-10-30 14:00:00:000')
               for i in range(10)]
    with patch.object(f, 'fetch_announcements', return_value=notices), patch(
        'data_provider.news_fetcher.em_get', side_effect=requests.Timeout) as get:
        result = f.collect_evidence('a', datetime(2026, 10, 30, 14, 57),
            max_items=2, max_chars=100, timeout=3)
    assert get.call_count == 2
    assert result['status'] == 'unavailable'
    assert any('原文获取失败' in item for item in result['unknowns'])


def test_ai_summary_is_not_accepted_as_original_evidence():
    f = NewsFetcher()
    notice = dict(art_code='a', title='公告', display_time='2026-10-30 14:00:00:000')
    with patch.object(f, 'fetch_announcements', return_value=[notice]), patch(
        'data_provider.news_fetcher.em_get', return_value=mock_response({'data': {
            'notice_content': 'AI整理的摘要', 'is_ai_summary': 1}})):
        result = f.collect_evidence('a', datetime(2026, 10, 30, 14, 57),
            max_items=2, max_chars=100, timeout=3)
    assert not result['items']
    assert any('AI摘要' in item for item in result['unknowns'])


def test_card_marks_missing_data_instead_of_showing_zero():
    from report.renderer import render_stock_card
    c = StockContext(code='a', name='A')
    missing = render_stock_card(c)
    assert '| 大单净流入 | 未获取 |' in missing
    assert '| 尾盘量比 | 未获取 |' in missing
    c.data_quality_flags.update(fund_flow=True, late_metrics_calculated=True)
    measured = render_stock_card(c)
    assert '| 大单净流入 | 0万 |' in measured
    assert '| 尾盘量比 | 0.0x |' in measured
