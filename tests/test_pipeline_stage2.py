"""S2 轮询循环集成测试 — 验证多轮迭代中的数据刷新、资金流首轮、min_pass保底等关键路径

这些测试覆盖了上次线上bug的精确位置：
- 5-min指标仅首轮加载 → 后续28轮复用过期数据 (fixed: 每轮都刷新)
- 需要在非交易时段也能验证S2循环行为
"""
import pytest
from unittest.mock import MagicMock, patch, call

from orchestration.pipeline import LateSessionPipeline
from orchestration.config import SystemConfig
from screening.context import StockContext
from screening.layer2_anomaly import L2Config


def make_ctx(code="000001", **kwargs):
    """构造测试用 StockContext, 默认值适合 L2 rally 通过"""
    defaults = {
        'code': code, 'name': f'测试{code}', 'price': 10.0,
        'change_pct': 3.0, 'turnover': 500_000_000, 'turnover_rate': 3.0,
        'volume': 10_000_000, 'high': 10.3, 'low': 9.7,
        'open': 9.8, 'pre_close': 9.71,
        'late_volume_ratio': 2.0, 'last_5min_volume_pct': 10.0,
        'late_price_change': 3.0, 'broke_high': False,
        'big_order_net': 5_000_000, 'big_order_ratio': 0.25,
        'active_buy_ratio': 60.0,
    }
    defaults.update(kwargs)
    return StockContext(**defaults)


def make_empty_ctx(code="000001"):
    """构造无尾盘异动的 StockContext (L2 不会通过)"""
    return StockContext(
        code=code, name=f'测试{code}', price=10.0,
        change_pct=0.5, turnover=500_000_000, turnover_rate=3.0,
        volume=10_000_000, high=10.1, low=9.9, open=10.0, pre_close=10.0,
        late_volume_ratio=0.5, last_5min_volume_pct=2.0,
        late_price_change=0.1, broke_high=False,
    )


@pytest.fixture
def pipeline():
    """构建测试用 Pipeline, mock 所有外部依赖"""
    config = SystemConfig(enable_capital_flow=False)

    with patch.object(SystemConfig, 'resolve_regime', return_value='neutral'):
        with patch.object(LateSessionPipeline, '_init_fetchers', return_value=MagicMock()):
            with patch('orchestration.pipeline.DataPreloader', return_value=MagicMock()):
                with patch('orchestration.pipeline.NewsFetcher', return_value=MagicMock()):
                    with patch('orchestration.pipeline.get_concept_analyzer', return_value=MagicMock()):
                        p = LateSessionPipeline(config, test_mode=True)

    p.tracker = MagicMock()
    p.funnel = MagicMock()
    p.funnel.config.l2 = L2Config(require_capital=False)
    p.funnel.stats = {}
    p._kline_provider = MagicMock()
    p.fetcher_mgr = MagicMock()
    p.concept_analyzer = MagicMock()
    p.news_fetcher = MagicMock()
    p._daily_metrics = {}
    p._daily_cache = {}
    p._5min_metrics = {}
    p._fund_flow_data = {}
    p._fund_flow_fetched = False
    p._s0_sector_map = {}
    p._flow_minute = None
    p._flow_sina = None
    p._flow_push2his = None
    p._time_window_active = MagicMock(return_value=True)
    return p


# ================================================================
# 5-min 指标刷新
# ================================================================

class Test5MinMetricsRefresh:
    """验证每轮 S2 迭代都刷新 5-min K线指标"""

    def test_5min_metrics_refreshed_each_iteration(self, pipeline):
        """模拟3轮迭代, 验证每轮都调用了 load_5min_batch 和 compute_late_metrics"""
        from data_provider.kline_provider import KlineProvider

        pipeline.test_mode = False  # 允许多轮迭代，由 _sleep_or_break 控制
        ctx = make_ctx("000001")
        pipeline._fetch_and_convert = MagicMock(return_value=[ctx])
        pipeline._enrich_fund_flow = MagicMock()
        pipeline._sleep_or_break = MagicMock(side_effect=[True, True, False])

        # 每轮返回非空5分钟线, L2检测通过 → 不会提前 break
        mock_df = MagicMock()
        mock_df.empty = False
        pipeline._kline_provider.load_5min_batch.return_value = {"000001": mock_df}
        # mock compute_late_metrics 返回有效指标, 否则 L2 过滤会导致提前退出
        valid_metrics = {
            "price_at_1430": 9.9, "late_price_change": 3.0,
            "late_volume_ratio": 2.0, "last_5min_volume_pct": 10.0,
            "morning_volume": 500000, "afternoon_volume": 600000,
            "last_5min_volume": 150000, "broke_high": False, "intraday_high": 10.3,
        }
        with patch.object(KlineProvider, 'compute_late_metrics', return_value=valid_metrics):
            pipeline._run_stage2([ctx])

        # 3轮迭代 → 3次调用
        assert pipeline._kline_provider.load_5min_batch.call_count == 3
        assert pipeline._fetch_and_convert.call_count == 3
        # _sleep_or_break 被调用了3次
        assert pipeline._sleep_or_break.call_count == 3

    def test_kline_not_called_when_no_provider(self, pipeline):
        """没有 _kline_provider 时不崩溃"""
        pipeline._kline_provider = None
        ctx = make_ctx("000001")
        pipeline._fetch_and_convert = MagicMock(return_value=[ctx])
        pipeline._enrich_fund_flow = MagicMock()
        pipeline._sleep_or_break = MagicMock(return_value=False)

        # 不应抛异常
        result = pipeline._run_stage2([ctx])
        assert isinstance(result, list)

    def test_kline_not_called_when_no_codes(self, pipeline):
        """候选池为空时跳过 K线加载"""
        pipeline._kline_provider.load_5min_batch = MagicMock()
        pipeline._fetch_and_convert = MagicMock(return_value=[])
        pipeline._enrich_fund_flow = MagicMock()
        pipeline._sleep_or_break = MagicMock(return_value=False)

        pipeline._run_stage2([])

        pipeline._kline_provider.load_5min_batch.assert_not_called()


# ================================================================
# 资金流向首轮专用
# ================================================================

class TestFundFlowFirstIteration:
    """验证资金流向只在首轮拉取"""

    def test_fund_flow_refresh_every_2nd_iteration(self, pipeline):
        """资金流每2轮刷新: iteration=1 + iteration=2 (3轮中调用2次)"""
        pipeline.test_mode = False  # 允许多轮迭代，由 _sleep_or_break 控制
        ctx = make_ctx("000001")
        pipeline._fetch_and_convert = MagicMock(return_value=[ctx])
        pipeline._enrich_fund_flow = MagicMock()
        pipeline._sleep_or_break = MagicMock(side_effect=[True, True, False])
        pipeline._kline_provider.load_5min_batch.return_value = {}
        # 至少有一个 flow fetcher 非 None, S2 才会进入资金流分支
        pipeline._flow_minute = MagicMock()

        pipeline._run_stage2([ctx])

        # iteration=1 和 iteration=2 各调用一次 (每2轮刷新)
        assert pipeline._enrich_fund_flow.call_count == 2

    def test_fund_flow_skipped_when_no_fetchers(self, pipeline):
        """没有资金流 fetcher 时跳过, 不崩溃"""
        ctx = make_ctx("000001")
        pipeline._fetch_and_convert = MagicMock(return_value=[ctx])
        pipeline._enrich_fund_flow = MagicMock()
        pipeline._sleep_or_break = MagicMock(return_value=False)
        pipeline._kline_provider.load_5min_batch.return_value = {}
        # _flow_minute/sina/push2his 都是 None → 不会调用 _enrich_fund_flow
        pipeline._flow_minute = None
        pipeline._flow_sina = None
        pipeline._flow_push2his = None

        pipeline._run_stage2([ctx])

        pipeline._enrich_fund_flow.assert_not_called()


# ================================================================
# 尾盘指标值传递到 context
# ================================================================

class TestLateMetricsPropagation:
    """验证 5-min 指标正确写入 StockContext"""

    def test_late_metrics_values_propagate(self, pipeline):
        """模拟: round1 返回含尾盘数据的5分钟线, 验证 context 拿到正确值"""
        from data_provider.kline_provider import KlineProvider
        import pandas as pd

        ctx = make_ctx("000001", late_price_change=0.0, late_volume_ratio=0.0,
                       last_5min_volume_pct=0.0, broke_high=False)
        pipeline._fetch_and_convert = MagicMock(return_value=[ctx])
        pipeline._enrich_fund_flow = MagicMock()
        pipeline._sleep_or_break = MagicMock(return_value=False)

        # 构造有意义的 5-min 指标
        fake_metrics = {
            "price_at_1430": 9.8,
            "late_price_change": 2.5,
            "late_volume_ratio": 1.8,
            "last_5min_volume_pct": 12.0,
            "morning_volume": 500000,
            "afternoon_volume": 600000,
            "last_5min_volume": 150000,
            "broke_high": True,
            "intraday_high": 10.5,
        }
        mock_df = MagicMock()
        mock_df.empty = False
        pipeline._kline_provider.load_5min_batch.return_value = {"000001": mock_df}

        with patch.object(KlineProvider, 'compute_late_metrics', return_value=fake_metrics):
            pipeline._run_stage2([ctx])

        # context 应该拿到正确的 5-min 指标值
        assert ctx.late_price_change == 2.5
        assert ctx.late_volume_ratio == 1.8
        assert ctx.last_5min_volume_pct == 12.0
        assert ctx.broke_high is True
        assert ctx.intraday_high == 10.5
        assert ctx.data_quality_flags.get('5min_kline') is True
        assert ctx.data_quality_flags.get('late_metrics_calculated') is True

    def test_stale_metrics_overwritten_on_next_round(self, pipeline):
        """第二轮的新数据覆盖第一轮的旧数据"""
        from data_provider.kline_provider import KlineProvider

        pipeline.test_mode = False  # 允许多轮迭代，由 _sleep_or_break 控制
        ctx = make_ctx("000001", late_price_change=5.0, late_volume_ratio=3.0)
        pipeline._fetch_and_convert = MagicMock(return_value=[ctx])
        pipeline._enrich_fund_flow = MagicMock()
        pipeline._sleep_or_break = MagicMock(side_effect=[True, False])
        mock_df = MagicMock()
        mock_df.empty = False
        pipeline._kline_provider.load_5min_batch.return_value = {"000001": mock_df}
        # Round 1: late_price_change = 3.0 → 拉升
        # Round 2: late_price_change = 0.5 → 回落
        metrics_round1 = {"price_at_1430": 9.8, "late_price_change": 3.0,
                          "late_volume_ratio": 2.0, "last_5min_volume_pct": 10.0,
                          "morning_volume": 500000, "afternoon_volume": 600000,
                          "last_5min_volume": 150000, "broke_high": False, "intraday_high": 10.2}
        metrics_round2 = {"price_at_1430": 9.8, "late_price_change": 0.5,
                          "late_volume_ratio": 1.5, "last_5min_volume_pct": 8.0,
                          "morning_volume": 500000, "afternoon_volume": 600000,
                          "last_5min_volume": 150000, "broke_high": False, "intraday_high": 10.2}

        with patch.object(KlineProvider, 'compute_late_metrics',
                          side_effect=[metrics_round1, metrics_round2]):
            pipeline._run_stage2([ctx])

        # 最终值应该是 round2 的值 (被覆盖了)
        assert ctx.late_price_change == 0.5
        assert ctx.late_volume_ratio == 1.5


# ================================================================
# 候选池清空 → 提前退出
# ================================================================

class TestEarlyExitOnEmpty:
    """L2 过滤后候选池为空 → 循环提前 break, 不空转"""

    def test_breaks_when_all_filtered(self, pipeline):
        """所有股票被 L2 过滤 → 打印日志后 break"""
        ctx = make_empty_ctx("000001")  # 不会通过 L2
        pipeline._fetch_and_convert = MagicMock(return_value=[ctx])
        pipeline._enrich_fund_flow = MagicMock()
        pipeline._sleep_or_break = MagicMock()  # 不应被调用
        pipeline._kline_provider.load_5min_batch.return_value = {}

        pipeline._run_stage2([ctx])

        # 因为候选池清空, _sleep_or_break 不该被调用
        pipeline._sleep_or_break.assert_not_called()

    def test_continues_when_some_pass(self, pipeline):
        """有股票通过 L2 → 继续循环"""
        pipeline.test_mode = False  # 允许多轮迭代，由 _sleep_or_break 控制
        ctx = make_ctx("000001")  # 会通过 L2
        pipeline._fetch_and_convert = MagicMock(return_value=[ctx])
        pipeline._enrich_fund_flow = MagicMock()
        pipeline._sleep_or_break = MagicMock(return_value=False)
        pipeline._kline_provider.load_5min_batch.return_value = {}

        result = pipeline._run_stage2([ctx])

        assert len(result) == 1
        pipeline._sleep_or_break.assert_called_once()


# ================================================================
# min_pass 保底机制
# ================================================================

class TestMinPassFallback:
    """通过数不足 min_pass 时自动放宽资金条件"""

    def test_fallback_when_below_min_pass(self, pipeline):
        """通过数 < min_pass 且 require_capital=True → 放宽重筛"""
        # 需要资金数据 → 不满足的 context 先被过滤
        pipeline.funnel.config.l2.require_capital = True
        pipeline.config.l2_min_pass = 3
        pipeline._fund_flow_fetched = True  # 标记已获取资金流

        # 只构造1只通过量+价的股票, 但资金流向不满足
        ctx = make_ctx("000001", big_order_net=-10_000_000, big_order_ratio=-0.1,
                       active_buy_ratio=30.0)
        pipeline._fetch_and_convert = MagicMock(return_value=[ctx])
        pipeline._enrich_fund_flow = MagicMock()
        pipeline._sleep_or_break = MagicMock(return_value=False)
        pipeline._kline_provider.load_5min_batch.return_value = {}

        result = pipeline._run_stage2([ctx])

        # 放宽后量+价通过 → 至少1只
        assert len(result) >= 1

    def test_no_fallback_when_above_min_pass(self, pipeline):
        """通过数 ≥ min_pass → 不触发放宽"""
        pipeline.funnel.config.l2.require_capital = False
        pipeline.config.l2_min_pass = 1

        ctx = make_ctx("000001")
        pipeline._fetch_and_convert = MagicMock(return_value=[ctx])
        pipeline._enrich_fund_flow = MagicMock()
        pipeline._sleep_or_break = MagicMock(return_value=False)
        pipeline._kline_provider.load_5min_batch.return_value = {}

        result = pipeline._run_stage2([ctx])

        # 正常通过, 不触发放宽
        assert len(result) == 1


# ================================================================
# test_mode 行为
# ================================================================

class TestTestMode:
    """test_mode=True 时只跑一轮"""

    def test_test_mode_single_iteration(self, pipeline):
        """test_mode 下 _sleep_or_break 返回 False → 一轮后退出"""
        # test_mode 已由 fixture 设为 True
        ctx = make_ctx("000001")
        pipeline._fetch_and_convert = MagicMock(return_value=[ctx])
        pipeline._enrich_fund_flow = MagicMock()
        pipeline._kline_provider.load_5min_batch.return_value = {}

        pipeline._run_stage2([ctx])

        # _fetch_and_convert 只被调用一次 (一轮)
        assert pipeline._fetch_and_convert.call_count == 1
