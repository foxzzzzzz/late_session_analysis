"""_enrich_contexts 数据富化测试 — 验证所有数据源正确写入 StockContext

_enrich_contexts 是 Pipeline 的数据聚合枢纽 (300行), 此前的线上bug说明:
- 如果 5-min 指标存入了 _5min_metrics 但 _enrich_contexts 没正确读取, context 里就是0
- 如果 stale 数据没被覆盖, 第二轮迭代用的还是第一轮的过期值
"""
import pytest
from unittest.mock import MagicMock, patch

import pandas as pd

from orchestration.pipeline import LateSessionPipeline
from orchestration.config import SystemConfig
from screening.context import StockContext
from data_provider.kline_provider import KlineProvider


def make_ctx(code="000001", **kwargs):
    defaults = {
        'code': code, 'name': f'测试{code}', 'price': 10.0,
        'change_pct': 1.0, 'turnover': 500_000_000, 'turnover_rate': 3.0,
        'volume': 10_000_000, 'high': 10.3, 'low': 9.7,
        'open': 9.9, 'pre_close': 9.9,
    }
    defaults.update(kwargs)
    return StockContext(**defaults)


@pytest.fixture
def pipeline():
    """构建 pipeline, mock 所有构造依赖"""
    config = SystemConfig(enable_capital_flow=False)
    fake_preloader = MagicMock()
    fake_preloader.unlock_stocks = set()
    fake_preloader.hot_concepts = {}
    fake_preloader.get_sector_performance.return_value = 0.0
    with patch.object(SystemConfig, 'resolve_regime', return_value='neutral'):
        with patch.object(LateSessionPipeline, '_init_fetchers', return_value=MagicMock()):
            with patch('orchestration.pipeline.DataPreloader', return_value=fake_preloader):
                with patch('orchestration.pipeline.NewsFetcher', return_value=MagicMock()):
                    with patch('orchestration.pipeline.get_concept_analyzer', return_value=MagicMock()):
                        p = LateSessionPipeline(config, test_mode=True)
    p.tracker = MagicMock()
    p.funnel = MagicMock()
    p.fetcher_mgr = MagicMock()
    p.concept_analyzer = MagicMock()
    p.news_fetcher = MagicMock()
    # 数据容器初始化为空
    p._daily_metrics = {}
    p._daily_cache = {}
    p._5min_metrics = {}
    p._fund_flow_data = {}
    p._s0_sector_map = {}
    return p


# ================================================================
# 日线指标
# ================================================================

class TestDailyMetrics:
    def test_ma_and_volatility_applied(self, pipeline):
        """日线 MA/波动率/yang_days 正确写入 context"""
        pipeline._daily_metrics["000001"] = {
            "ma5": 9.8, "ma10": 9.6, "ma20": 9.5, "ma30": 9.3, "ma60": 9.0,
            "volatility": 2.5,
            "yang_days_4": 3,
            "body_amplifying": True,
            "consecutive_close_rise": 2,
            "position_20d": 65.0,
        }
        ctx = make_ctx("000001")
        pipeline._enrich_contexts([ctx])

        assert ctx.ma5 == 9.8
        assert ctx.ma10 == 9.6
        assert ctx.ma20 == 9.5
        assert ctx.ma30 == 9.3
        assert ctx.ma60 == 9.0
        assert ctx.volatility == 2.5
        assert ctx.yang_days_4 == 3
        assert ctx.body_amplifying is True
        assert ctx.consecutive_close_rise == 2
        assert ctx.position_20d == 65.0
        assert ctx.data_quality_flags.get('daily_kline') is True
        assert ctx.data_quality_flags.get('ma_calculated') is True
        assert ctx.data_quality_flags.get('volatility_calculated') is True

    def test_ma5_acceleration_and_volume_shrink(self, pipeline):
        """MA5加速 和 缩量 从 _daily_cache 计算"""
        daily_df = pd.DataFrame({
            "open": [10.0] * 10, "close": list(range(9, 10)) + [10.0] * 9,
            "high": [10.2] * 10, "low": [9.8] * 10,
            "vol": [1000000] * 10, "volume": [1000000] * 10,
            "amount": [10000000] * 10,
        })
        pipeline._daily_cache["000001"] = daily_df
        pipeline._daily_metrics["000001"] = {
            "ma5": 9.8, "ma10": 9.6, "ma20": 9.5, "ma30": 9.3, "ma60": 9.0,
            "volatility": 2.0, "yang_days_4": 2, "body_amplifying": False,
            "consecutive_close_rise": 1, "position_20d": 50.0,
        }
        ctx = make_ctx("000001")
        pipeline._enrich_contexts([ctx])

        # ma5_accelerating 和 volume_shrinking 被计算并写入
        assert isinstance(ctx.ma5_accelerating, bool)
        assert isinstance(ctx.volume_shrinking, bool)

    def test_no_daily_metrics_no_crash(self, pipeline):
        """没有日线数据时不抛异常, flag 保持默认 False"""
        ctx = make_ctx("000001")
        pipeline._enrich_contexts([ctx])

        assert ctx.ma5 == 0.0
        # data_quality_flags 始终存在, 初始为 False, 有数据时才翻 True
        assert ctx.data_quality_flags.get('daily_kline') is False


# ================================================================
# 5分钟线指标
# ================================================================

class Test5MinMetrics:
    def test_late_metrics_applied(self, pipeline):
        """5-min 尾盘指标正确写入 context"""
        pipeline._5min_metrics["000001"] = {
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
        ctx = make_ctx("000001")
        pipeline._enrich_contexts([ctx])

        assert ctx.price_at_1430 == 9.8
        assert ctx.late_price_change == 2.5
        assert ctx.late_volume_ratio == 1.8
        assert ctx.last_5min_volume_pct == 12.0
        assert ctx.morning_volume == 500000
        assert ctx.afternoon_volume == 600000
        assert ctx.last_5min_volume == 150000
        assert ctx.broke_high is True
        assert ctx.intraday_high == 10.5
        assert ctx.data_quality_flags.get('5min_kline') is True
        assert ctx.data_quality_flags.get('late_metrics_calculated') is True

    def test_stale_5min_data_overwritten(self, pipeline):
        """第二次调用 _enrich_contexts 新值覆盖旧值"""
        pipeline._5min_metrics["000001"] = {
            "price_at_1430": 9.8, "late_price_change": 3.0,
            "late_volume_ratio": 2.0, "last_5min_volume_pct": 10.0,
            "morning_volume": 500000, "afternoon_volume": 600000,
            "last_5min_volume": 150000, "broke_high": False, "intraday_high": 10.2,
        }
        ctx = make_ctx("000001")
        pipeline._enrich_contexts([ctx])
        assert ctx.late_price_change == 3.0

        # 第二轮: 更新指标 → 新值应覆盖旧值
        pipeline._5min_metrics["000001"] = {
            "price_at_1430": 9.8, "late_price_change": 0.5,
            "late_volume_ratio": 1.2, "last_5min_volume_pct": 8.0,
            "morning_volume": 500000, "afternoon_volume": 700000,
            "last_5min_volume": 120000, "broke_high": True, "intraday_high": 10.4,
        }
        pipeline._enrich_contexts([ctx])

        assert ctx.late_price_change == 0.5   # 被覆盖
        assert ctx.late_volume_ratio == 1.2   # 被覆盖
        assert ctx.broke_high is True         # 从 False 变为 True
        assert ctx.intraday_high == 10.4

    def test_no_5min_metrics_no_crash(self, pipeline):
        """没有 5-min 数据时不抛异常, flag 保持 False"""
        ctx = make_ctx("000001")
        pipeline._enrich_contexts([ctx])

        assert ctx.late_price_change == 0.0
        assert ctx.data_quality_flags.get('5min_kline') is False


# ================================================================
# 数据质量标记
# ================================================================

class TestDataQualityFlags:
    def test_all_flags_set_when_data_available(self, pipeline):
        """所有数据源都有时, 对应 flag 全部设置"""
        pipeline._daily_metrics["000001"] = {
            "ma5": 10.0, "ma10": 9.9, "ma20": 9.8, "ma30": 9.7, "ma60": 9.5,
            "volatility": 2.0, "yang_days_4": 2, "body_amplifying": False,
            "consecutive_close_rise": 1, "position_20d": 50.0,
        }
        pipeline._5min_metrics["000001"] = {
            "price_at_1430": 9.9, "late_price_change": 1.0,
            "late_volume_ratio": 1.5, "last_5min_volume_pct": 8.0,
            "morning_volume": 500000, "afternoon_volume": 600000,
            "last_5min_volume": 120000, "broke_high": False, "intraday_high": 10.1,
        }
        from datetime import datetime
        pipeline._fund_flow_data["000001"] = {
            "mainForce": 500,  # 万元
            "active_buy_ratio": 60.0,
            "data_date": datetime.now().strftime("%Y-%m-%d"),
        }
        ctx = make_ctx("000001")
        pipeline._enrich_contexts([ctx])

        assert ctx.data_quality_flags.get('daily_kline') is True
        assert ctx.data_quality_flags.get('ma_calculated') is True
        assert ctx.data_quality_flags.get('volatility_calculated') is True
        assert ctx.data_quality_flags.get('5min_kline') is True
        assert ctx.data_quality_flags.get('late_metrics_calculated') is True
        assert ctx.data_quality_flags.get('fund_flow') is True

    def test_flags_absent_when_no_data(self, pipeline):
        """无数据源时 flag 保持默认 False (所有 key 始终存在)"""
        ctx = make_ctx("000001")
        pipeline._enrich_contexts([ctx])

        # key 始终存在 (StockContext 默认值), 无数据时保持 False
        assert 'daily_kline' in ctx.data_quality_flags
        assert ctx.data_quality_flags['daily_kline'] is False
        assert ctx.data_quality_flags['5min_kline'] is False
        assert ctx.data_quality_flags['fund_flow'] is False


# ================================================================
# 板块/题材/解禁
# ================================================================

class TestSectorAndConcept:
    def test_sector_backfill_from_s0_map(self, pipeline):
        """S0 板块映射 → sector 字段回填"""
        pipeline._s0_sector_map["000001"] = "半导体"
        ctx = make_ctx("000001", sector="")  # sector 初始为空
        pipeline._enrich_contexts([ctx])

        assert ctx.sector == "半导体"

    def test_sector_not_overwritten_if_already_set(self, pipeline):
        """已有 sector 值时不被 S0 映射覆盖"""
        # 注意: 当前 _enrich_contexts 只在 ctx.sector 为空时回填
        pipeline._s0_sector_map["000001"] = "半导体"
        ctx = make_ctx("000001", sector="元器件")
        pipeline._enrich_contexts([ctx])

        assert ctx.sector == "元器件"  # 保持原值

    def test_sector_performance_from_preloader(self, pipeline):
        """板块涨跌幅从 preloader 写入"""
        pipeline.preloader.get_sector_performance.return_value = 3.5
        ctx = make_ctx("000001", sector="半导体")
        pipeline._enrich_contexts([ctx])

        assert ctx.sector_performance == 3.5

    def test_concept_tags_applied(self, pipeline):
        """热点题材从 preloader 写入"""
        pipeline.preloader.hot_concepts = {"000001": ["芯片", "5G"]}
        ctx = make_ctx("000001")
        pipeline._enrich_contexts([ctx])

        assert ctx.hot_concepts == ["芯片", "5G"]
        assert ctx.leader_strength is True

    def test_unlock_date_flag(self, pipeline):
        """解禁标记从 preloader 写入"""
        pipeline.preloader.unlock_stocks = {"000001"}
        ctx = make_ctx("000001")
        pipeline._enrich_contexts([ctx])

        assert ctx.is_unlock_date is True

    def test_sector_rank_pct_calculated(self, pipeline):
        """板块排名百分位计算"""
        pipeline.preloader.sector_performance = {
            "半导体": 3.5, "元器件": 2.0, "软件": 1.0, "医药": -1.0,
        }
        ctx = make_ctx("000001", sector="元器件")  # 排名第2/4 → 50%
        pipeline._enrich_contexts([ctx])

        assert ctx.sector_rank_pct == pytest.approx(50.0, abs=0.1)


# ================================================================
# 关键价位检测
# ================================================================

class TestKeyLevelDetection:
    def test_integer_round_level(self, pipeline):
        """价格在整数关口 ±1% 内 → near_key_level"""
        ctx = make_ctx("000001", price=10.05)  # 10 的 ±1% 内
        pipeline._enrich_contexts([ctx])
        assert ctx.near_key_level is True

    def test_not_near_round_level(self, pipeline):
        """价格远离整数关口 → near_key_level 为 False"""
        ctx = make_ctx("000001", price=12.5)  # 不在任何关口 ±1%
        pipeline._enrich_contexts([ctx])
        assert ctx.near_key_level is False

    def test_near_ma20(self, pipeline):
        """价格在 MA20 ±2% 内"""
        pipeline._daily_metrics["000001"] = {
            "ma5": 9.8, "ma10": 9.7, "ma20": 10.0, "ma30": 9.5, "ma60": 9.0,
            "volatility": 2.0, "yang_days_4": 2, "body_amplifying": False,
            "consecutive_close_rise": 1, "position_20d": 50.0,
        }
        ctx = make_ctx("000001", price=10.15)  # MA20=10.0, ±2%=[9.8,10.2]
        pipeline._enrich_contexts([ctx])
        assert ctx.near_key_level is True

    def test_far_from_ma(self, pipeline):
        """价格远离 MA → near_key_level 为 False"""
        pipeline._daily_metrics["000001"] = {
            "ma5": 10.0, "ma10": 10.0, "ma20": 10.0, "ma30": 10.0, "ma60": 10.0,
            "volatility": 2.0, "yang_days_4": 2, "body_amplifying": False,
            "consecutive_close_rise": 1, "position_20d": 50.0,
        }
        ctx = make_ctx("000001", price=13.0)  # 远离 MA20
        pipeline._enrich_contexts([ctx])
        assert ctx.near_key_level is False


# ================================================================
# 连续涨停 + 胜率
# ================================================================

class TestDailyCacheDerived:
    def test_consecutive_limit_ups(self, pipeline):
        """从日线缓存计算连续涨停天数"""
        # 最近2天涨停
        daily_df = pd.DataFrame({
            "open": [9.0, 10.0, 11.0],
            "close": [9.9, 10.99, 12.09],  # +10%, +10% (涨停)
            "high": [10.0, 11.0, 12.1],
            "low": [9.0, 10.0, 11.0],
            "vol": [1000000] * 3,
            "volume": [1000000] * 3,
            "amount": [10000000] * 3,
        })
        pipeline._daily_cache["000001"] = daily_df
        pipeline._daily_metrics["000001"] = {
            "ma5": 10.0, "ma10": 9.5, "ma20": 9.0, "ma30": 8.5, "ma60": 8.0,
            "volatility": 2.0, "yang_days_4": 2, "body_amplifying": False,
            "consecutive_close_rise": 2, "position_20d": 50.0,
        }
        ctx = make_ctx("000001", price=12.0, is_st=False)
        pipeline._enrich_contexts([ctx])

        # 沪深主板涨停为10%, 最近2天涨停 → consecutive_limit_ups ≥ 1
        assert ctx.consecutive_limit_ups >= 1

    def test_history_win_rate(self, pipeline):
        """从日线缓存计算近5日收阳率"""
        opens = [10.0, 10.0, 10.0, 11.0, 11.0]
        closes = [10.5, 10.5, 10.5, 10.5, 10.5]  # 前3赢(10.5>10.0), 后2输(10.5<11.0)
        daily_df = pd.DataFrame({
            "open": opens,
            "close": closes,
            "high": [11.0] * 5,
            "low": [9.5] * 5,
            "vol": [1000000] * 5,
            "volume": [1000000] * 5,
            "amount": [10000000] * 5,
        })
        pipeline._daily_cache["000001"] = daily_df
        ctx = make_ctx("000001")
        pipeline._enrich_contexts([ctx])

        assert ctx.history_win_rate == pytest.approx(60.0, abs=1.0)


# ================================================================
# 边界条件
# ================================================================

class TestEdgeCases:
    def test_empty_data_sources_no_crash(self, pipeline):
        """所有数据源为空, _enrich_contexts 不抛异常"""
        ctx1 = make_ctx("000001")
        ctx2 = make_ctx("000002")
        pipeline._enrich_contexts([ctx1, ctx2])
        # 不抛异常就是通过

    def test_multiple_contexts_enriched(self, pipeline):
        """多只股票同时富化, 各自拿到正确的数据"""
        pipeline._5min_metrics["000001"] = {
            "price_at_1430": 9.8, "late_price_change": 3.0,
            "late_volume_ratio": 2.0, "last_5min_volume_pct": 12.0,
            "morning_volume": 500000, "afternoon_volume": 600000,
            "last_5min_volume": 150000, "broke_high": False, "intraday_high": 10.2,
        }
        pipeline._5min_metrics["000002"] = {
            "price_at_1430": 20.0, "late_price_change": -1.0,
            "late_volume_ratio": 0.5, "last_5min_volume_pct": 3.0,
            "morning_volume": 300000, "afternoon_volume": 400000,
            "last_5min_volume": 80000, "broke_high": False, "intraday_high": 20.5,
        }
        ctx1 = make_ctx("000001")
        ctx2 = make_ctx("000002")
        pipeline._enrich_contexts([ctx1, ctx2])

        assert ctx1.late_price_change == 3.0
        assert ctx2.late_price_change == -1.0
        assert ctx1.data_quality_flags.get('5min_kline') is True
        assert ctx2.data_quality_flags.get('5min_kline') is True
