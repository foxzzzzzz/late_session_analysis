"""5阶段主控流水线

时间线:
  S0 14:25-14:30 板块预筛选 (1次)
  S1 14:30-14:50 初始扫描 (每3分钟,候选池,L1+K线)
  S2 14:50-14:55 加速监控 (每1分钟,候选池,L2+资金流向)
  S3 14:55       L3静态验证 (单次, 均线/波动率/利空/解禁)
  S4 14:55-14:58 L4多轮评分 (每10秒刷新分时, L4评分+LLM融合)
"""
import time
import logging
from datetime import datetime, timedelta
from dataclasses import asdict, is_dataclass
from typing import Optional

from data_provider.base import RealtimeQuote
from data_provider.manager import DataFetcherManager
from data_provider.preloader import DataPreloader
from data_provider.efinance_fetcher import EfinanceFetcher
from data_provider.akshare_fetcher import AkshareFetcher
from data_provider.sector_fetcher import SectorBasedFetcher
from data_provider.sina_fetcher import SinaFetcher
from data_provider.tencent_fetcher import TencentFetcher
from data_provider.eastmoney_flow_fetcher import EastmoneyFlowFetcher
from data_provider.northbound_fetcher import get_northbound_sentiment
from data_provider.concept_analyzer import get_concept_analyzer
from data_provider.kline_provider import KlineProvider
from data_provider.board_utils import get_limit_pct
from data_provider.news_fetcher import NewsFetcher
from screening.context import StockContext
from screening.funnel import FunnelPipeline, FunnelConfig
from screening.cache import StockMetricsCache
from analysis.llm_client import LLMClient
from analysis.parallel_runner import ParallelLLMRunner
from analysis.merger import merge_and_rank
from report.renderer import render_report, save_report, md_to_image
from orchestration.config import SystemConfig
from orchestration.stage_tracker import StageTracker

logger = logging.getLogger(__name__)


class LateSessionPipeline:
    """尾盘分析7阶段流水线"""

    def __init__(self, config: SystemConfig, test_mode: bool = False):
        self.config = config
        self.test_mode = test_mode
        self.tracker = StageTracker()
        self.cache = StockMetricsCache()
        self.preloader = DataPreloader()

        # 市场状态判定 (14:25前拉取上证日线)
        self.regime = config.resolve_regime()
        logger.info(
            f"市场状态: {self.regime} "
            f"(模式: {config.regime_mode})"
        )

        # 初始化数据源
        self.fetcher_mgr = self._init_fetchers()

        # 初始化漏斗 (根据市场状态选择阈值)
        screening_configs = config.get_screening_configs(self.regime)
        self.funnel = FunnelPipeline(
            config=FunnelConfig(**screening_configs),
            preloader=self.preloader,
            cache=self.cache,
        )

        # K线配置 (S1)
        self.kline_config = screening_configs.get('kline')

        # 资金流向 — 双源并发: 分钟线(实时mainForce) + 新浪(active_buy_ratio)
        self._flow_minute = None     # EastmoneyMinuteFlowFetcher
        self._flow_sina = None       # SinaFundFlowFetcher
        self._flow_push2his = None   # EastmoneyFlowFetcher (降级)
        if config.enable_capital_flow:
            from data_provider.sina_fund_flow import SinaFundFlowFetcher
            from data_provider.eastmoney_minute_flow import EastmoneyMinuteFlowFetcher
            self._flow_minute = EastmoneyMinuteFlowFetcher(timeout=8.0, max_workers=4)
            self._flow_sina = SinaFundFlowFetcher(timeout=8.0, max_workers=8)
            self._flow_push2his = EastmoneyFlowFetcher(timeout=15.0, max_workers=2)

        # 初始化题材分析器
        self.concept_analyzer = get_concept_analyzer()

        # 北向资金情绪 (S3阶段获取)
        self.northbound_sentiment: Optional[dict] = None

        # 利空公告检测 (S3阶段, 东方财富公告API)
        self.news_fetcher = NewsFetcher(lookback_days=3)

        # 资金流向数据日期 (today/yesterday/none)
        self.capital_data_date: str = "none"

        self._snapshot_store = None
        if getattr(config, "enable_live_snapshots", True):
            try:
                from data_provider.snapshot_store import SnapshotStore
                self._snapshot_store = SnapshotStore(config.live_snapshot_dir)
            except Exception as e:
                logger.warning(f"Live snapshot store init failed: {e}")

        # S0 板块映射 (股票代码 → 板块名)
        self._s0_sector_map: dict[str, str] = {}

        # K线数据提供器 (S0之后初始化，S0才知道候选池)
        self._kline_provider: Optional[KlineProvider] = None
        self._daily_metrics: dict[str, dict] = {}  # code → {ma5, ma10, ma20, volatility, atr}
        self._daily_cache: dict[str, "pd.DataFrame"] = {}  # code → 原始日线DataFrame (供K线形态用)
        self._5min_metrics: dict[str, dict] = {}   # code → {price_at_1430, late_price_change, ...}

        # 资金流向是否已拉取 (S2仅首次)
        self._fund_flow_fetched: bool = False
        self._fund_flow_data: dict[str, dict] = {}  # S2获取的原始数据，供S3回填

        # Sina双时间点差分基线 (尾盘实时 active_buy_ratio)
        self._sina_baseline: dict[str, dict] = {}  # {code: {r0_in, r0_out}} 首轮快照

        # 初始化LLM (如果配置了)
        self.llm_client: Optional[LLMClient] = None
        self.llm_runner: Optional[ParallelLLMRunner] = None
        if config.need_llm():
            self.llm_client = LLMClient(
                provider=config.llm_provider,
                model=config.llm_model,
                api_key=config.llm_api_key,
                api_base=config.llm_api_base,
                max_tokens=config.llm_max_tokens,
                temperature=config.llm_temperature,
            )
            self.llm_runner = ParallelLLMRunner(self.llm_client, max_workers=4)

    def _init_fetchers(self) -> DataFetcherManager:
        fetchers = []
        for name in self.config.data_providers:
            if name == "sector_based":
                fetchers.append(SectorBasedFetcher(
                    sectors=self.config.target_sectors or None,
                    min_sleep=self.config.rate_limit_min_sleep,
                    max_sleep=self.config.rate_limit_max_sleep,
                ))
            elif name == "sina":
                fetchers.append(SinaFetcher())
            elif name == "tencent":
                fetchers.append(TencentFetcher())
            elif name == "efinance":
                fetchers.append(EfinanceFetcher())
            elif name == "akshare":
                fetchers.append(AkshareFetcher())
        return DataFetcherManager(fetchers)

    # ============================================================
    # 时间循环辅助
    # ============================================================

    def _time_window_active(self, end_time_str: str) -> bool:
        """检查是否仍在时间窗口内"""
        if self.test_mode:
            return False
        try:
            now = datetime.now()
            h, m = map(int, end_time_str.split(":"))
            end = now.replace(hour=h, minute=m, second=0, microsecond=0)
            return now < end
        except (ValueError, AttributeError):
            return False

    def _sleep_or_break(self, end_time_str: str, interval: int) -> bool:
        """休眠 interval 秒或直到窗口结束。返回 True 表示应继续循环"""
        if self.test_mode:
            return False
        if not self._time_window_active(end_time_str):
            return False
        # 计算实际可休眠时间
        now = datetime.now()
        h, m = map(int, end_time_str.split(":"))
        end = now.replace(hour=h, minute=m, second=0, microsecond=0)
        remaining = (end - now).total_seconds()
        if remaining <= interval:
            return False
        time.sleep(min(interval, remaining))
        return True

    # ============================================================
    # 主流程
    # ============================================================

    def run(self, stages: list[int] = None) -> str:
        """运行流水线

        Args:
            stages: 要运行的阶段列表。
                    0=S0板块预筛选, 1=S1 L1+K线, 2=S2 L2+资金流向,
                    3=S3 L3+均线, 4=S4 L4评分+LLM
                    默认 test模式 [1,2,3,4], 实盘模式 [0,1,2,3,4]

        Returns:
            报告文件路径
        """
        if stages is None:
            stages = [0, 1, 2, 3, 4]

        self.tracker.start()

        # 预加载
        if self.preloader and not self.preloader.is_loaded():
            self.preloader.load_all()

        # 市场状态重新判定 (preloader加载后有了行业数据，可计算市场宽度因子)
        if self.preloader and self.preloader.sector_performance:
            new_regime = self.config.resolve_regime(
                sector_performance=self.preloader.sector_performance
            )
            if new_regime != self.regime:
                logger.info(
                    f"市场状态更新: {self.regime} → {new_regime} (加入市场宽度因子后)"
                )
                self.regime = new_regime
                screening_configs = self.config.get_screening_configs(self.regime)
                self.funnel = FunnelPipeline(
                    config=FunnelConfig(**screening_configs),
                    preloader=self.preloader,
                    cache=self.cache,
                )

        # 题材热度分析 (基于预加载的热点数据)
        if self.preloader and self.preloader.hot_concepts:
            self.concept_analyzer.analyze(self.preloader.hot_concepts)

        try:
            contexts: list[StockContext] = []
            top30: list[StockContext] = []

            # === S0: 板块预筛选 ===
            if 0 in stages:
                s0_codes = self._run_stage0()
                # 加载S0候选池的真实日线数据
                if s0_codes and len(s0_codes) > 0:
                    self._kline_provider = KlineProvider()
                    logger.info(f"加载 {len(s0_codes)} 只候选股票的日线数据...")
                    self._daily_cache = self._kline_provider.load_daily_batch(s0_codes, bars=30)
                    for code, df in self._daily_cache.items():
                        if not df.empty:
                            self._daily_metrics[code] = {
                                'ma5': KlineProvider.compute_ma(df)[0],
                                'ma10': KlineProvider.compute_ma(df)[1],
                                'ma20': KlineProvider.compute_ma(df)[2],
                                'ma30': KlineProvider.compute_ma(df)[3],
                                'ma60': KlineProvider.compute_ma(df)[4],
                                'volatility': KlineProvider.compute_volatility(df),
                                'atr': KlineProvider.compute_atr(df),
                                'yang_days_4': KlineProvider.count_yang_days_4(df),
                                'body_amplifying': KlineProvider.check_body_amplifying(df),
                                'consecutive_close_rise': KlineProvider.compute_consecutive_close_rise(df),
                                'position_20d': KlineProvider.compute_position_20d(df),
                            }
                    logger.info(f"日线指标计算完成: {len(self._daily_metrics)} 只")
            else:
                s0_codes = None

            # === S1: K线形态 + L1准入 ===
            if 1 in stages:
                contexts = self._run_stage1(s0_codes)

            # === S2: 尾盘异常 + 资金流向 ===
            if 2 in stages:
                contexts = self._run_stage2(contexts)

            # === S3: 均线静态验证 (单次) ===
            if 3 in stages:
                l3_passed = self._run_stage3(contexts)
            else:
                l3_passed = list(contexts)

            # === S4: L4多轮评分 + LLM (每10秒循环, 14:55-14:58) ===
            if 4 in stages:
                top30 = self._run_stage4(l3_passed)
                self.top30 = top30  # expose for web dashboard

            self.tracker.finish()

            # 大盘概况 (S4结束后拉取，越晚越接近收盘价)
            self.market_overview = self._fetch_market_overview()

            # 生成报告
            return self._generate_report(top30)

        except Exception as e:
            logger.error(f"流水线异常: {e}", exc_info=True)
            raise

    # ============================================================
    # 数据获取与转换
    # ============================================================

    @staticmethod
    def _json_safe(value):
        if is_dataclass(value):
            return LateSessionPipeline._json_safe(asdict(value))
        if isinstance(value, dict):
            return {str(k): LateSessionPipeline._json_safe(v) for k, v in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [LateSessionPipeline._json_safe(v) for v in value]
        if hasattr(value, "item"):
            try:
                return value.item()
            except Exception:
                pass
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return str(value)

    @staticmethod
    def _context_snapshot(ctx: StockContext) -> dict:
        fields = [
            "code", "name", "price", "change_pct", "turnover", "turnover_rate",
            "volume", "high", "low", "open", "pre_close", "limit_up",
            "limit_down", "sector", "market_cap", "pe_ttm", "pb", "vol_ratio",
            "amplitude", "bid_vol", "ask_vol", "late_price_change",
            "late_volume_ratio", "last_5min_volume_pct", "price_at_1430",
            "intraday_high", "broke_high", "big_order_net", "big_order_ratio",
            "active_buy_ratio", "late_active_buy_ratio", "anomaly_type",
            "l1_passed", "kline_passed", "l2_passed", "l3_passed",
            "total_score", "final_score", "recommendation",
        ]
        data = {name: getattr(ctx, name, None) for name in fields}
        data["data_quality_flags"] = dict(getattr(ctx, "data_quality_flags", {}) or {})
        return LateSessionPipeline._json_safe(data)

    def _write_stage_snapshot(
        self,
        *,
        stage: str,
        iteration: int,
        contexts: list[StockContext],
        input_codes: list[str] | None = None,
        passed_contexts: list[StockContext] | None = None,
        filter_extra: dict | None = None,
        decision_time: str = "",
    ):
        if not self._snapshot_store:
            return
        try:
            passed_contexts = passed_contexts or []
            codes = input_codes if input_codes is not None else [c.code for c in contexts]
            passed_codes = [c.code for c in passed_contexts]
            filter_result = {
                "input_count": len(codes),
                "output_count": len(passed_codes),
                "passed_codes": passed_codes,
            }
            if filter_extra:
                filter_result.update(filter_extra)
            data_quality = {
                "data_source": self.fetcher_mgr.get_active_name() if self.fetcher_mgr else "none",
                "capital_data_date": self.capital_data_date,
            }
            self._snapshot_store.write_stage_snapshot(
                trading_date=datetime.now().strftime("%Y%m%d"),
                stage=stage,
                iteration=iteration,
                codes=list(codes),
                quotes=[self._context_snapshot(c) for c in contexts],
                late_metrics=self._json_safe(self._5min_metrics),
                fund_flow=self._json_safe(getattr(self, "_fund_flow_data", {})),
                filter_result=self._json_safe(filter_result),
                data_quality=self._json_safe(data_quality),
                decision_time=decision_time,
            )
        except Exception as e:
            logger.warning(f"Live snapshot write failed ({stage} round {iteration}): {e}")

    def _fetch_and_convert(self, codes: list[str] = None) -> list[StockContext]:
        """拉取数据并转换为StockContext

        Args:
            codes: 指定股票代码列表。None 表示拉取全市场。
        """
        if codes is not None:
            # codes=[] → 不拉取任何数据；codes=None → 拉全市场
            if not codes:
                return []
            quotes = self.fetcher_mgr.fetch_codes(codes)
        else:
            quotes = self.fetcher_mgr.fetch_snapshot()

        contexts = []
        for q in quotes:
            ctx = StockContext(
                code=q.code,
                name=q.name,
                price=q.price,
                change_pct=q.change_pct,
                turnover=q.turnover,
                turnover_rate=q.turnover_rate,
                volume=q.volume,
                high=q.high,
                low=q.low,
                open=q.open,
                pre_close=q.pre_close,
                limit_up=q.limit_up,
                limit_down=q.limit_down,
                is_st=q.is_st,
                is_suspended=q.is_suspended,
                sector=q.sector,
                market_cap=q.market_cap,
                pe_ttm=q.pe_ttm,
                pb=q.pb,
                vol_ratio=q.vol_ratio,
                amplitude=q.amplitude,
                bid_vol=q.bid_vol,
                ask_vol=q.ask_vol,
            )
            contexts.append(ctx)
        return contexts

    def _enrich_contexts(self, contexts: list[StockContext]):
        """用预加载/日线/5分钟线数据增强StockContext

        所有数据来自真实数据源:
          - MA/波动率: 日线K线 (KlineProvider, mootdx TCP)
          - 尾盘指标: 5分钟K线 (KlineProvider, mootdx TCP)
          - 解禁/题材/板块: 预加载 (DataPreloader)
          - 资金流向: 东财 push2his/push2 (EastmoneyFlowFetcher)
        """
        for ctx in contexts:
            ctx.market_regime = self.regime
            # === 日线指标: MA5/MA10/MA20, 波动率 ===
            dm = self._daily_metrics.get(ctx.code)
            if dm:
                ctx.ma5 = dm['ma5']
                ctx.ma10 = dm['ma10']
                ctx.ma20 = dm['ma20']
                ctx.ma30 = dm.get('ma30', 0.0)
                ctx.ma60 = dm.get('ma60', 0.0)
                ctx.volatility = dm['volatility']
                ctx.yang_days_4 = dm.get('yang_days_4', 0)
                ctx.body_amplifying = dm.get('body_amplifying', False)
                ctx.consecutive_close_rise = dm.get('consecutive_close_rise', 0)
                ctx.position_20d = dm.get('position_20d', 0.0)
                ctx.data_quality_flags['daily_kline'] = True
                ctx.data_quality_flags['ma_calculated'] = True
                ctx.data_quality_flags['volatility_calculated'] = True

                # MA5 渐进加速
                if ctx.code in self._daily_cache:
                    ctx.ma5_accelerating = KlineProvider.compute_ma5_acceleration(
                        self._daily_cache[ctx.code]
                    )
                    ctx.volume_shrinking = KlineProvider.check_volume_shrink(
                        self._daily_cache[ctx.code]
                    )

            # === 5分钟线尾盘指标 ===
            fm = self._5min_metrics.get(ctx.code)
            if fm:
                ctx.price_at_1430 = fm['price_at_1430']
                ctx.late_price_change = fm['late_price_change']
                ctx.late_volume_ratio = fm['late_volume_ratio']
                ctx.last_5min_volume_pct = fm['last_5min_volume_pct']
                ctx.morning_volume = fm['morning_volume']
                ctx.afternoon_volume = fm['afternoon_volume']
                ctx.last_5min_volume = fm['last_5min_volume']
                ctx.broke_high = fm['broke_high']
                ctx.intraday_high = fm['intraday_high']
                ctx.data_quality_flags['5min_kline'] = True
                ctx.data_quality_flags['late_metrics_calculated'] = True

            # === 解禁检查 (来自预加载) ===
            if self.preloader and ctx.code in self.preloader.unlock_stocks:
                ctx.is_unlock_date = True

            # === 热点题材 (来自预加载) ===
            if self.preloader and ctx.code in self.preloader.hot_concepts:
                ctx.hot_concepts = self.preloader.hot_concepts[ctx.code]
                if ctx.hot_concepts:
                    ctx.leader_strength = True

            # === 回填板块信息 (S0建立映射 → TencentFetcher无板块字段) ===
            if not ctx.sector and ctx.code in self._s0_sector_map:
                ctx.sector = self._s0_sector_map[ctx.code]

            # === 写入板块涨跌幅 ===
            if ctx.sector and self.preloader:
                ctx.sector_performance = self.preloader.get_sector_performance(ctx.sector)

            # === 从日线计算: 连续涨停天数 ===
            if dm and ctx.code in self._daily_cache:
                df = self._daily_cache[ctx.code]
                if not df.empty and len(df) >= 2:
                    limit_pct = get_limit_pct(ctx.code, ctx.is_st)
                    count = 0
                    for i in range(1, min(10, len(df))):
                        prev_close = float(df['close'].iloc[-i-1])
                        day_close = float(df['close'].iloc[-i])
                        if prev_close > 0:
                            day_change = (day_close - prev_close) / prev_close * 100
                            if day_change >= limit_pct * 0.95:
                                count += 1
                            else:
                                break
                    ctx.consecutive_limit_ups = count

            # === 近5日收阳率 ===
            if ctx.code in self._daily_cache:
                df = self._daily_cache[ctx.code]
                if not df.empty and len(df) >= 3:
                    recent = df.tail(5)
                    wins = sum(
                        1 for _, row in recent.iterrows()
                        if float(row.get('close', 0)) > float(row.get('open', 0))
                    )
                    ctx.history_win_rate = wins / len(recent) * 100

            # === 接近关键价位 (整数关口/均线) ===
            if ctx.price > 0:
                # 整数关口 ±1%: 10/20/50/100/200/500
                round_levels = [10, 20, 50, 100, 200, 500]
                for rl in round_levels:
                    if abs(ctx.price - rl) / rl <= 0.01:
                        ctx.near_key_level = True
                        break
                # MA20 ±2%
                if ctx.ma20 > 0 and abs(ctx.price - ctx.ma20) / ctx.ma20 * 100 <= 2.0:
                    ctx.near_key_level = True
                # MA60 ±2%
                if ctx.ma60 > 0 and abs(ctx.price - ctx.ma60) / ctx.ma60 * 100 <= 2.0:
                    ctx.near_key_level = True

            # === 板块排名百分位 ===
            if self.preloader and ctx.sector:
                all_perf = self.preloader.sector_performance
                if all_perf and ctx.sector in all_perf and len(all_perf) > 1:
                    ranked = sorted(all_perf.values(), reverse=True)
                    my_pct = all_perf[ctx.sector]
                    try:
                        rank = next(
                            i for i, v in enumerate(ranked) if v == my_pct
                        ) + 1
                        ctx.sector_rank_pct = rank / len(ranked) * 100
                    except StopIteration:
                        pass

            # === S3资金流回填 (从S2缓存恢复, 仅当日数据) ===
            if self._fund_flow_data:
                fd = self._fund_flow_data.get(ctx.code, {})
                if fd and fd.get("data_date", "") == datetime.now().strftime("%Y-%m-%d"):
                    ctx.big_order_net = fd.get("mainForce", 0) * 10000
                    ctx.big_order_ratio = (
                        abs(ctx.big_order_net) / max(ctx.turnover, 1)
                        if ctx.turnover > 0 else 0
                    )
                    ctx.active_buy_ratio = fd.get("active_buy_ratio", 50.0)
                    ctx.data_quality_flags['fund_flow'] = True

    def _enrich_bad_news(self, contexts: list[StockContext]):
        """S3阶段: 利空公告排查 (东方财富公告API)

        对每只股票查询近3日公告标题，匹配利空关键词。
        API限流: 每请求 ≥ 0.5s
        """
        checked = 0
        bad_count = 0
        for ctx in contexts:
            # 跳过已有解禁标记的股票（已排除）
            if ctx.is_unlock_date:
                continue
            try:
                ctx.has_bad_news = self.news_fetcher.check_bad_news(ctx.code)
                checked += 1
                if ctx.has_bad_news:
                    bad_count += 1
            except Exception as e:
                logger.debug(f"利空公告查询失败 [{ctx.code}]: {e}")

        if checked > 0:
            logger.info(
                f"利空排查: 检查 {checked} 只, "
                f"发现 {bad_count} 只有利空公告"
            )

    def _enrich_leader_strength(
        self,
        contexts: list[StockContext],
        sector_codes: dict[str, list[str]],
    ):
        """S3阶段: 板块内排名 → 龙头效应增强

        在当前pipeline contexts中，按板块计算每只股票的：
          - 市值排名百分位 (top 30% → 龙头)
          - 涨幅排名百分位 (top 20% → 龙头)
          - 成交额排名百分位 (top 25% → 龙头)

        满足任一条件即视为板块龙头。
        不依赖额外API调用，使用contexts中已有的实时数据。
        """
        if not sector_codes:
            return

        # 按板块分组当前contexts
        sector_contexts: dict[str, list[StockContext]] = {}
        for ctx in contexts:
            sec = ctx.sector or self._s0_sector_map.get(ctx.code, '')
            if sec:
                sector_contexts.setdefault(sec, []).append(ctx)

        leader_count = 0
        for sec, members in sector_contexts.items():
            if len(members) < 3:
                # 板块样本太少，统一标记为非龙头
                continue

            # 市值排名 (非零值参与排名)
            caps = [(c, c.market_cap) for c in members if c.market_cap > 0]
            caps_sorted = sorted(caps, key=lambda x: x[1], reverse=True)
            cap_ranks = {
                c.code: (i + 1) / len(caps_sorted) * 100
                for i, (c, _) in enumerate(caps_sorted)
            }

            # 涨幅排名
            changes_sorted = sorted(members, key=lambda c: c.change_pct, reverse=True)
            change_ranks = {
                c.code: (i + 1) / len(changes_sorted) * 100
                for i, c in enumerate(changes_sorted)
            }

            # 成交额排名
            turns_sorted = sorted(
                [c for c in members if c.turnover > 0],
                key=lambda c: c.turnover, reverse=True,
            )
            turnover_ranks = {
                c.code: (i + 1) / len(turns_sorted) * 100
                for i, c in enumerate(turns_sorted)
            }

            # 判定: 任一维度进入前列即为龙头
            for ctx in members:
                if ctx.leader_strength:
                    continue  # 已通过题材热度认定为龙头，保留

                cap_r = cap_ranks.get(ctx.code, 100)
                chg_r = change_ranks.get(ctx.code, 100)
                to_r = turnover_ranks.get(ctx.code, 100)

                if cap_r <= 30 or chg_r <= 20 or to_r <= 25:
                    ctx.leader_strength = True
                    leader_count += 1

        if leader_count > 0:
            logger.info(f"龙头效应: {leader_count} 只认定为板块龙头")

    def _log_low_count(self, contexts, label: str, threshold: int = 10):
        """通过数 < threshold 时输出股票明细"""
        if len(contexts) >= threshold:
            return
        if not contexts:
            logger.info(f"{label}: 0 只通过")
            return
        logger.info(f"{label}: {len(contexts)} 只 ->")
        for ctx in contexts:
            if hasattr(ctx, 'name'):
                logger.info(f"  {ctx.code} {ctx.name}")
            else:
                logger.info(f"  {ctx}")

    # ============================================================
    # S0: 板块预筛选
    # ============================================================

    def _run_stage0(self) -> list[str]:
        """S0: 板块预筛选 → 候选股票代码列表"""
        self.tracker.stage_start("S0_板块预筛选", 5000)

        from data_provider.sector_filter import SectorFilter
        sf = SectorFilter(self.preloader, self.config)

        # 熊市扩展候选池: 动态选取涨幅top-10行业 (vs 中性/牛市固定5个)
        max_sectors = None
        if self.regime == "bear":
            max_sectors = getattr(self.config, 's0_sector_count_bear', 10)
            logger.info(f"S0 熊市模式: 行业扩展至 top-{max_sectors}")

        codes, sector_map = sf.filter(max_sectors=max_sectors)
        self._s0_sector_map = sector_map

        self.funnel.stats['s0_count'] = len(codes)
        self.tracker.stage_end("S0_板块预筛选", len(codes))
        logger.info(f"S0 完成: {len(codes)} 只候选股票")
        self._log_low_count(codes, "S0 板块预筛选")
        return codes

    # ============================================================
    # S1: K线形态 + L1准入 (每3分钟循环, 14:30-14:50)
    # ============================================================

    def _run_stage1(self, s0_codes: list[str] = None) -> list[StockContext]:
        """S1: K线形态预筛选 + L1基础准入 (14:30, 单次执行)"""
        self.tracker.stage_start("S1_K线扫描", len(s0_codes) if s0_codes else 5000)

        # 拉取量价数据 + L1基础准入
        contexts = self._fetch_and_convert(s0_codes)
        self._enrich_contexts(contexts)

        from screening.layer1_access import screen_l1_access
        contexts = screen_l1_access(contexts, self.funnel.config.l1)
        self.funnel.stats['l1_count'] = len(contexts)
        logger.info(f"S1 L1通过: {len(contexts)} 只")
        self._log_low_count(contexts, "S1 L1准入")

        # K线形态预筛选
        if self.kline_config:
            from screening.layer_kline import screen_kline
            contexts = screen_kline(contexts, self.kline_config, self._daily_cache)
            self.funnel.stats['s1_kline_count'] = len(contexts)
            logger.info(f"S1 K线形态通过: {len(contexts)} 只")
            self._log_low_count(contexts, "S1 K线形态")

        self._write_stage_snapshot(
            stage="S1",
            iteration=1,
            contexts=contexts,
            input_codes=s0_codes or [c.code for c in contexts],
            passed_contexts=contexts,
        )

        self.tracker.stage_end("S1_K线扫描", len(contexts))
        return contexts

    @staticmethod
    def _strip_incomplete_5min_bar(df):
        """丢弃当前未完成的5分钟bar，避免空bar拉低尾盘指标"""
        import pandas as pd
        from datetime import datetime

        if df is None or df.empty:
            return df

        for col in ("datetime", "time"):
            if col in df.columns:
                times = pd.to_datetime(df[col])
                break
        else:
            return df

        now = datetime.now()
        current_window = now.replace(second=0, microsecond=0)
        current_window = current_window.replace(minute=current_window.minute // 5 * 5)

        if times.iloc[-1] >= current_window:
            return df.iloc[:-1]
        return df

    # ============================================================
    # S2: 尾盘异常 + 资金流向 (每1分钟循环, 14:50-14:55)
    # ============================================================

    def _run_stage2(self, contexts: list[StockContext]) -> list[StockContext]:
        """S2: 尾盘异常检测 + 资金流向富化 + 5分钟线尾盘指标"""
        self.tracker.stage_start("S2_尾盘异常", len(contexts))

        loop_interval = self.config.s2_loop_interval
        iteration = 0
        codes = [c.code for c in contexts]
        last_round = self.test_mode

        while True:
            iteration += 1

            if iteration > 1:
                last_round = not self._time_window_active(self.config.s2_window_end)

            if last_round:
                logger.info(f"S2 第{iteration}轮扫描 (最终轮)...")
            else:
                logger.info(f"S2 第{iteration}轮扫描...")

            # 刷新量价数据
            contexts = self._fetch_and_convert(codes)

            # 5分钟K线尾盘指标 — 每轮刷新 (14:30后新bar持续生成, 指标随时间变化)
            if self._kline_provider and codes:
                logger.info(f"刷新 {len(codes)} 只5分钟K线...")
                min5_cache = self._kline_provider.load_5min_batch(codes, bars=48)
                for code, df in min5_cache.items():
                    if not df.empty:
                        df = self._strip_incomplete_5min_bar(df)
                        if not df.empty:
                            self._5min_metrics[code] = KlineProvider.compute_late_metrics(df)
                logger.info(f"5分钟线指标计算完成: {len(self._5min_metrics)} 只")

            # 一次性应用所有富化数据 (日线 + 5分钟尾盘指标 + 板块 + 题材)
            self._enrich_contexts(contexts)

            # 资金流向 — 首轮 + 每2分钟刷新 (提高Sina差分覆盖率)
            if iteration == 1 or iteration % 2 == 0:
                if self._flow_minute or self._flow_sina or self._flow_push2his:
                    self._fund_flow_fetched = False
                    self._enrich_fund_flow(contexts)
                    self.capital_data_date = (
                        "today" if self._fund_flow_data else "none"
                    )

            # L2 尾盘异常检测
            from screening.layer2_anomaly import screen_l2_anomaly, L2Config
            has_capital = self._fund_flow_fetched
            l2_candidates = list(contexts)  # 保留全量，后续可能需要放宽重筛
            contexts = screen_l2_anomaly(
                l2_candidates, self.funnel.config.l2,
                has_depth_data=False,
                has_capital_data=has_capital,
                last_round=last_round,
            )

            # 最低保障: 通过数不足 min_pass 时，放宽资金条件重筛
            min_pass = self.config.l2_min_pass
            relaxed_capital = False
            if len(contexts) < min_pass and has_capital and self.funnel.config.l2.require_capital:
                import dataclasses
                relaxed = L2Config(**dataclasses.asdict(self.funnel.config.l2))
                relaxed.require_capital = False
                logger.warning(
                    f"L2 最低保障: 仅通过 {len(contexts)} 只 (<{min_pass}), "
                    f"放宽资金条件重筛 (量+价通过即可)"
                )
                contexts = screen_l2_anomaly(
                    l2_candidates, relaxed,
                    has_depth_data=False,
                    has_capital_data=False,
                    last_round=last_round,
                )
                logger.info(f"L2 放宽后: {len(contexts)} 只通过")
                self._log_low_count(contexts, "S2 L2(放宽)")
                relaxed_capital = True

            self.funnel.stats['l2_count'] = len(contexts)

            self._write_stage_snapshot(
                stage="S2",
                iteration=iteration,
                contexts=l2_candidates,
                input_codes=codes,
                passed_contexts=contexts,
                filter_extra={"relaxed_capital": relaxed_capital},
                decision_time=self.config.s2_window_end,
            )

            logger.info(
                f"S2 L2通过: {len(contexts)} 只 "
                f"(资金流向: {'已获取' if self._fund_flow_fetched else '未获取'})"
            )
            self._log_low_count(contexts, "S2 L2异常")

            if not contexts:
                logger.info("S2 候选池清空，提前结束循环")
                break

            if last_round:
                break

            if not self._sleep_or_break(self.config.s2_window_end, loop_interval):
                break

        self.tracker.stage_end("S2_尾盘异常", len(contexts))
        return contexts

    def _enrich_fund_flow(self, contexts: list[StockContext]):
        """资金流向富化 — 双源并发 + 字段合并 (S2每5分钟, S3首轮调用)

        并发拉取:
          A. 分钟线 (push2 fflow/kline klt=1) → 实时 mainForce/super/large/retail
          B. 新浪 (MoneyFlow) → active_buy_ratio + 备选 mainForce

        合并: mainForce优先A, active_buy_ratio始终用B; B失败降级到push2his
        """
        if self._fund_flow_fetched:
            return

        self._sina_delta_cache: dict[str, float] = {}  # 每轮清空，仅保留本次delta

        l2_candidates = list(contexts)
        enrich_limit = self.config.max_capital_enrich
        l2_candidates.sort(
            key=lambda c: abs(c.change_pct) * c.vol_ratio * c.turnover,
            reverse=True,
        )
        enrich_candidates = l2_candidates[:enrich_limit]

        if len(l2_candidates) > enrich_limit:
            logger.info(
                f"资金流向: 候选 {len(l2_candidates)} 只, "
                f"仅对前 {enrich_limit} 只做资金富化"
            )

        codes = [c.code for c in enrich_candidates]
        minute_data: dict[str, dict] = {}
        sina_data: dict[str, dict] = {}
        push2his_data: dict[str, dict] = {}

        # 并发拉取分钟线 + 新浪
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as pool:
            if self._flow_minute:
                f_min = pool.submit(self._flow_minute.enrich_batch, codes)
            else:
                f_min = None
            if self._flow_sina:
                f_sina = pool.submit(self._flow_sina.enrich_batch, codes)
            else:
                f_sina = None

            if f_min:
                minute_data = f_min.result() or {}
            if f_sina:
                sina_data = f_sina.result() or {}

        # 新浪失败 → 降级 push2his (收盘后有 active_buy_ratio)
        if not sina_data and self._flow_push2his:
            logger.info("资金流向: 新浪返回空，降级到 push2his...")
            push2his_data = self._flow_push2his.enrich_batch(codes) or {}

        n_minute = len(minute_data)
        n_sina = len(sina_data)
        n_his = len(push2his_data)
        logger.info(
            f"资金流向: 分钟线={n_minute} 新浪={n_sina} push2his={n_his}"
        )

        today_str = datetime.now().strftime("%Y-%m-%d")

        # 保存合并数据供 S3 回填 (三个源都可能有部分股票，合并而非二选一)
        # push2his 仅保留当日数据 — 交易时段klt=101只有昨日数据，不能混入当日决策
        self._fund_flow_data = {}
        all_codes = set()
        all_codes.update(minute_data.keys())
        all_codes.update(sina_data.keys())
        all_codes.update(push2his_data.keys())
        for code in all_codes:
            md = minute_data.get(code, {})
            sd = sina_data.get(code, {})
            hd = push2his_data.get(code, {})
            hd_today = hd if hd.get("data_date", "") == today_str else {}
            sources_seen = []
            if md:
                sources_seen.append("minute")
            if sd:
                sources_seen.append("sina")
            if hd:
                sources_seen.append("push2his")
            # 合并: mainForce 优先分钟线 → 新浪 → push2his(仅当日)
            self._fund_flow_data[code] = {
                "mainForce": md.get("mainForce") or sd.get("mainForce") or hd_today.get("mainForce") or 0,
                "active_buy_ratio": sd.get("active_buy_ratio") or hd_today.get("active_buy_ratio", 50.0),
                "data_date": sd.get("data_date") or hd_today.get("data_date") or md.get("data_date", ""),
                "sources_seen": sources_seen,
                "fetched_at": datetime.now().isoformat(timespec="seconds"),
                "is_realtime": any(
                    d.get("data_date", "") == today_str for d in (md, sd) if d
                ),
            }

        # Sina双时间点差分: 每次刷新后计算最近区间增量并更新基线 (滚动基线，不累积)
        if sina_data:
            for code, sd in sina_data.items():
                r0_in = sd.get("r0_in", 0)
                r0_out = sd.get("r0_out", 0)
                if r0_in <= 0 and r0_out <= 0:
                    continue
                if code in self._sina_baseline:
                    bl = self._sina_baseline[code]
                    delta_in = r0_in - bl["r0_in"]
                    delta_out = r0_out - bl["r0_out"]
                    delta_total = delta_in + delta_out
                    if delta_total > 0:
                        # 存入临时字典，后续 enrich 时写入 ctx
                        if not hasattr(self, '_sina_delta_cache'):
                            self._sina_delta_cache: dict[str, float] = {}
                        self._sina_delta_cache[code] = delta_in / delta_total * 100
                # 滚动基线: 每次刷新后更新
                self._sina_baseline[code] = {"r0_in": r0_in, "r0_out": r0_out}
            logger.info(
                f"Sina滚动基线: {len(self._sina_baseline)} 只 (本次delta有效: "
                f"{len(getattr(self, '_sina_delta_cache', {}))} 只)"
            )

        today_count = 0

        for ctx in enrich_candidates:
            md = minute_data.get(ctx.code, {})
            sd = sina_data.get(ctx.code, {})
            hd = push2his_data.get(ctx.code, {})

            # mainForce: 优先分钟线(实时) → 新浪 → push2his
            main_force = None
            flow_detail = {}
            if md:
                main_force = md.get("mainForce")
                flow_detail = md
                ctx.data_quality_flags['fund_flow_source'] = 'minute'
            if main_force is None and sd:
                main_force = sd.get("mainForce")
                flow_detail = sd
                ctx.data_quality_flags['fund_flow_source'] = 'sina'
            if main_force is None and hd:
                # push2his 日线交易时段只返昨日数据 — 必须校验日期，避免昨日资金流入当日决策
                if hd.get("data_date", "") == today_str:
                    main_force = hd.get("mainForce")
                    flow_detail = hd
                    ctx.data_quality_flags['fund_flow_source'] = 'push2his'

            if main_force is None:
                continue

            ctx.data_quality_flags['fund_flow'] = True
            dates = [d.get("data_date", "") for d in (md, sd, hd) if d]
            source = ctx.data_quality_flags.get('fund_flow_source', 'none')
            ctx.data_quality_flags['fund_flow_data_date'] = (
                flow_detail.get("data_date", "")
                or sd.get("data_date", "")
                or hd.get("data_date", "")
            )
            ctx.data_quality_flags['fund_flow_fetched_at'] = datetime.now().isoformat(timespec="seconds")
            ctx.data_quality_flags['fund_flow_is_realtime'] = (
                source in ("minute", "sina") and ctx.data_quality_flags['fund_flow_data_date'] == today_str
            )
            ctx.data_quality_flags['fund_flow_sources_seen'] = [
                name for name, data in (
                    ("minute", md), ("sina", sd), ("push2his", hd)
                ) if data
            ]
            ctx.big_order_net = main_force * 10000
            ctx.big_order_ratio = (
                abs(ctx.big_order_net) / max(ctx.turnover, 1)
                if ctx.turnover > 0 else 0
            )

            # active_buy_ratio: 始终从新浪取 (已修正为 r0_in/(r0_in+r0_out)*100)
            # 新浪失败 → push2his 降级 (仅当日数据)
            ab_source = sd or (hd if hd.get("data_date", "") == today_str else None)
            if ab_source:
                ctx.active_buy_ratio = ab_source.get("active_buy_ratio", 50.0)

            # 尾盘实时 active_buy_ratio: Sina滚动基线差分 (最近区间增量)
            delta_cache = getattr(self, '_sina_delta_cache', {})
            if ctx.code in delta_cache:
                ctx.late_active_buy_ratio = delta_cache[ctx.code]

            # 日期验证: 分钟线或新浪有当日日期即算有效
            if any(d == today_str for d in dates):
                today_count += 1

        self.capital_data_date = "today" if today_count > 0 else "none"
        date_label = f"当日 {today_count} 只" if today_count else "无当日数据"
        sources = []
        if minute_data:
            sources.append("分钟线")
        if sina_data:
            sources.append("新浪")
        if push2his_data:
            sources.append("push2his")
        logger.info(
            f"资金流向: 富化 {today_count}/{len(enrich_candidates)} 只 "
            f"({date_label}, 来源: {'+'.join(sources) if sources else 'none'})"
        )
        # 尾盘 active_buy_ratio 分布 (Sina差分, 有基线后才有效)
        late_ab_vals = sorted(
            [c.late_active_buy_ratio for c in enrich_candidates
             if c.late_active_buy_ratio > 0]
        )
        if late_ab_vals:
            n = len(late_ab_vals)
            p25 = late_ab_vals[n // 4]
            p50 = late_ab_vals[n // 2]
            p75 = late_ab_vals[n * 3 // 4]
            logger.info(
                f"尾盘 active_buy_ratio(Sina差分) 分布: n={n} "
                f"p25={p25:.1f} p50={p50:.1f} p75={p75:.1f}"
            )
        self._fund_flow_fetched = True

    def _enrich_fund_flow_from_cache(self, contexts: list[StockContext]):
        """用已缓存的 _fund_flow_data 回填资金流数据到 contexts。

        S3 用 _fetch_and_convert 重建 contexts 后会丢失 S2 已拉取的资金流数据。
        此方法仅回填已有缓存，不发起 API 请求，避免 API 限流后数据归零。
        """
        if not self._fund_flow_data:
            return
        today_str = datetime.now().strftime("%Y-%m-%d")
        applied = 0
        for ctx in contexts:
            if ctx.code not in self._fund_flow_data:
                continue
            fd = self._fund_flow_data[ctx.code]
            main_force = fd.get("mainForce", 0)
            if main_force == 0:
                continue
            ctx.big_order_net = main_force * 10000
            ctx.big_order_ratio = (
                abs(ctx.big_order_net) / max(ctx.turnover, 1)
                if ctx.turnover > 0 else 0
            )
            ctx.active_buy_ratio = fd.get("active_buy_ratio", 50.0)
            ctx.data_quality_flags['fund_flow'] = True
            ctx.data_quality_flags['fund_flow_source'] = 's2_cache'
            ctx.data_quality_flags['fund_flow_data_date'] = fd.get("data_date", "")
            ctx.data_quality_flags['fund_flow_is_realtime'] = (
                fd.get("data_date", "") == today_str
            )
            applied += 1
        if applied > 0:
            logger.info(
                f"资金流向(S2缓存回填): {applied}/{len(contexts)} 只, "
                f"来源: {fd.get('sources_seen', [])}"
            )

    # ============================================================
    # S3: 均线静态验证 (单次执行, 14:55)
    # ============================================================

    def _run_stage3(self, contexts: list[StockContext]) -> list[StockContext]:
        """S3: L3静态指标验证 — 单次执行，价格敏感检查已移至L4"""
        self.tracker.stage_start("S3_均线验证", len(contexts))

        # 刷新量价数据 + 5分钟K线
        codes = [c.code for c in contexts]
        contexts = self._fetch_and_convert(codes)
        if self._kline_provider and codes:
            min5_cache = self._kline_provider.load_5min_batch(codes, bars=48)
            for code, df in min5_cache.items():
                if not df.empty:
                    df = self._strip_incomplete_5min_bar(df)
                    if not df.empty:
                        self._5min_metrics[code] = KlineProvider.compute_late_metrics(df)

        # 资金流向 — 仅在S2未取到时首次拉取; S2已有则复用, 避免API限流后数据丢失
        prev_fund_flow = self._fund_flow_data.copy() if self._fund_flow_data else {}
        if self._flow_minute or self._flow_sina or self._flow_push2his:
            if not self._fund_flow_fetched:
                self._enrich_fund_flow(contexts)
                self.capital_data_date = "today" if self._fund_flow_data else "none"
            # 补丁: S3用新contexts重建后, 回填S2已有的资金流数据到当前contexts
            if prev_fund_flow and not self._fund_flow_data:
                self._fund_flow_data = prev_fund_flow
            self._enrich_fund_flow_from_cache(contexts)

        self._enrich_contexts(contexts)

        # 利空公告排查
        self._enrich_bad_news(contexts)

        # 板块内排名 → 龙头效应增强
        sector_codes: dict[str, list[str]] = {}
        for code, sec in self._s0_sector_map.items():
            sector_codes.setdefault(sec, []).append(code)
        self._enrich_leader_strength(contexts, sector_codes)

        # L3 静态指标验证 (volume_shrink/volatility/vol_ratio/解禁/利空等)
        from screening.layer3_technical import screen_l3_technical
        l3_passed = screen_l3_technical(
            contexts, self.preloader, self.funnel.config.l3
        )
        self.funnel.stats['l3_count'] = len(l3_passed)
        self._write_stage_snapshot(
            stage="S3",
            iteration=1,
            contexts=contexts,
            input_codes=codes,
            passed_contexts=l3_passed,
        )
        logger.info(f"S3 L3静态验证: {len(contexts)} → {len(l3_passed)} "
                    f"({len(l3_passed) / max(len(contexts), 1) * 100:.1f}%)")
        self._log_low_count(l3_passed, "S3 L3静态")

        # 北向资金情绪 — 数据自2024-08起全行业断供
        self.northbound_sentiment = {"available": False}
        if self.config.enable_northbound:
            nb = get_northbound_sentiment()
            if nb:
                self.northbound_sentiment = nb
        if self.northbound_sentiment.get("available"):
            logger.info(
                f"昨日北向资金: 净买入 {self.northbound_sentiment['today_net_yi']}亿, "
                f"趋势分 {self.northbound_sentiment['trend_score']:.0f}"
            )
        else:
            logger.info("北向资金: 数据不可用 (日度净买额自2024-08起行业断供)")

        self.tracker.stage_end("S3_均线验证", len(l3_passed))
        return l3_passed

    # ============================================================
    # S4: L4多轮评分 + LLM (每10秒循环刷新分时指标, 14:55-14:58)
    # ============================================================

    def _run_stage4(self, l3_passed: list[StockContext]) -> list[StockContext]:
        """S4: L4多轮评分 (刷新分时价格+5min指标) + 末轮LLM分析 + 融合排序"""
        self.tracker.stage_start("S4_评分冲刺", len(l3_passed))

        loop_interval = 10  # 每10秒刷新L4评分
        iteration = 0
        scored: list[StockContext] = []

        sector_codes: dict[str, list[str]] = {}
        for code, sec in self._s0_sector_map.items():
            sector_codes.setdefault(sec, []).append(code)

        from screening.layer4_scoring import score_l4, set_northbound_sentiment, set_concept_analyzer

        # 候选池≤3只时，K线/资金/概念等维度均为静态，多轮循环无意义
        single_round = len(l3_passed) <= 3
        if single_round:
            logger.info(f"S4 候选池仅{len(l3_passed)}只，单轮评分 (跳过多轮循环)")

        while True:
            iteration += 1
            logger.info(f"S4 第{iteration}轮评分...")

            # 刷新分时价格 (腾讯API)
            codes = [c.code for c in l3_passed]
            l3_passed = self._fetch_and_convert(codes)

            # 5分钟K线尾盘指标 — 每轮刷新 (14:55后新bar持续生成)
            if self._kline_provider and codes:
                min5_cache = self._kline_provider.load_5min_batch(codes, bars=48)
                for code, df in min5_cache.items():
                    if not df.empty:
                        df = self._strip_incomplete_5min_bar(df)
                        if not df.empty:
                            self._5min_metrics[code] = KlineProvider.compute_late_metrics(df)

            # Context富化 + 龙头效应
            self._enrich_contexts(l3_passed)
            self._enrich_leader_strength(l3_passed, sector_codes)

            # L4 量化评分 (每轮刷新, 价格敏感的D/E维度随之变化)
            set_northbound_sentiment(self.northbound_sentiment)
            set_concept_analyzer(self.concept_analyzer)
            scored = score_l4(l3_passed, self.funnel.config.l4, self.capital_data_date)
            self.funnel.stats['l4_count'] = len(scored)
            self._write_stage_snapshot(
                stage="S4",
                iteration=iteration,
                contexts=scored,
                input_codes=codes,
                passed_contexts=scored,
                decision_time=self.config.s3_window_end,
            )

            if single_round:
                break

            if not self._sleep_or_break(self.config.s3_window_end, loop_interval):
                break

        # 取最后一轮评分最高的30只
        top30 = self.funnel.get_top(scored, 30)
        logger.info(f"S4 最终L4评分 top30: {len(top30)} 只")

        # LLM并行分析 (单次, 在最后评分上执行)
        llm_results = {}
        if self.llm_runner:
            self.tracker.llm_total = len(top30)
            llm_results = self.llm_runner.analyze_batch(top30)
            self.tracker.llm_success = sum(
                1 for r in llm_results.values()
                if not r.get('fallback', False)
            )
            self.tracker.llm_buy_signals = sum(
                1 for r in llm_results.values()
                if r.get('decision') == 'buy'
            )

            # 对失败的标的重试一次LLM分析
            failed_codes = [code for code, r in llm_results.items() if r.get('fallback', False)]
            if failed_codes:
                failed_ctxs = [ctx for ctx in top30 if ctx.code in failed_codes]
                logger.info(f"LLM重试: {len(failed_ctxs)} 只失败标的 ({', '.join(failed_codes)})")
                retry_results = self.llm_runner.analyze_batch(failed_ctxs)
                llm_results.update(retry_results)
                self.tracker.llm_success = sum(
                    1 for r in llm_results.values()
                    if not r.get('fallback', False)
                )
                self.tracker.llm_buy_signals = sum(
                    1 for r in llm_results.values()
                    if r.get('decision') == 'buy'
                )

            self._llm_results = llm_results

        capital_ok = (self.capital_data_date == 'today')
        llm_ok = (self.llm_runner is not None
                  and any(not r.get('fallback', False) for r in llm_results.values()))

        rule_cfg = getattr(self.funnel.config, 'rule_scorer', None)

        # 推荐阈值: 从config读取regime感知基准值, 按数据缺失场景打折
        base_sb = self.config._regime_value("l4_high_threshold", self.regime)
        base_buy = self.config._regime_value("l4_buy_threshold", self.regime)
        base_watch = self.config._regime_value("l4_medium_threshold", self.regime)

        if capital_ok and llm_ok:
            offset = 0
        elif capital_ok and not llm_ok:
            offset = -7
        elif not capital_ok and llm_ok:
            offset = -10
        else:
            offset = -17

        logger.info(
            f"S4 推荐阈值 (regime={self.regime}): "
            f"strong_buy≥{base_sb + offset:.0f} buy≥{base_buy + offset:.0f} watch≥{base_watch + offset:.0f}"
        )

        top30 = merge_and_rank(top30, llm_results,
            strong_buy_threshold=base_sb + offset,
            buy_threshold=base_buy + offset,
            watch_threshold=base_watch + offset,
            rule_scorer_cfg=rule_cfg)

        self.tracker.stage_end("S4_评分冲刺", len(top30))
        return top30

    # ============================================================
    # 大盘概况
    # ============================================================

    def _fetch_market_overview(self) -> dict:
        """拉取大盘概况: 三大指数 + 涨跌家数 + 热点板块 (S4结束后调用，越晚越准)"""
        result = {
            "indices": [],
            "breadth": {"up": 0, "down": 0, "flat": 0, "ratio": 0, "bias": ""},
            "hot_sectors": [],
        }

        # 1. 三大指数 (mootdx category=4 日线)
        try:
            from mootdx.quotes import Quotes
            index_targets = [
                ("000001", "上证指数"),
                ("399001", "深证成指"),
                ("399006", "创业板指"),
            ]
            for code, name in index_targets:
                try:
                    df = self._kline_provider._client.index_bars(symbol=code, frequency=9, offset=1)
                    if df is not None and not df.empty:
                        row = df.iloc[-1]
                        close = float(row.get("close", 0))
                        last_close = float(row.get("last_close", 0)) or float(row.get("open", 0))
                        change_pct = (close - last_close) / last_close * 100 if last_close else 0
                        change_amt = close - last_close
                        result["indices"].append({
                            "name": name, "close": close,
                            "change_pct": change_pct, "change_amt": change_amt,
                        })
                except Exception as e:
                    logger.warning(f"获取指数 {name}({code}) 失败: {e}")
        except Exception as e:
            logger.warning(f"指数数据获取失败: {e}")

        # 2. 涨跌家数 (全市场扫描)
        try:
            import akshare as ak
            df = ak.stock_zh_a_spot()
            if df is not None and not df.empty:
                chg_col = "涨跌幅" if "涨跌幅" in df.columns else None
                if chg_col:
                    up = int((df[chg_col] > 0).sum())
                    down = int((df[chg_col] < 0).sum())
                    flat = len(df) - up - down
                    result["breadth"]["up"] = up
                    result["breadth"]["down"] = down
                    result["breadth"]["flat"] = flat
                    result["breadth"]["ratio"] = round(up / max(down, 1), 2)
                    if result["breadth"]["ratio"] >= 1.5:
                        result["breadth"]["bias"] = "偏多"
                    elif result["breadth"]["ratio"] >= 1.0:
                        result["breadth"]["bias"] = "中性偏多"
                    elif result["breadth"]["ratio"] >= 0.67:
                        result["breadth"]["bias"] = "中性偏空"
                    else:
                        result["breadth"]["bias"] = "偏空"
                    logger.info(f"市场宽度: 涨{up} 跌{down} 平{flat} 比{result['breadth']['ratio']}:1 ({result['breadth']['bias']})")
        except Exception as e:
            logger.warning(f"涨跌家数获取失败: {e}")

        # 3. 热点板块 Top5 (从 preloader 缓存读取)
        try:
            if self.preloader and self.preloader.sector_performance:
                sectors = sorted(self.preloader.sector_performance.items(),
                                 key=lambda x: x[1], reverse=True)
                result["hot_sectors"] = sectors[:5]
        except Exception as e:
            logger.warning(f"热点板块获取失败: {e}")

        return result

    # ============================================================
    # 报告生成
    # ============================================================

    def _generate_report(self, top30: list) -> str:
        """生成报告"""
        strong_buy = [c for c in top30 if c.recommendation == 'strong_buy']
        buy_stocks = [c for c in top30 if c.recommendation == 'buy']
        watch_stocks = [c for c in top30 if c.recommendation == 'watch']

        actionable = strong_buy + buy_stocks
        if len(actionable) < 10:
            logger.info(f"最终推荐: strong_buy={len(strong_buy)} buy={len(buy_stocks)} ->")
            for ctx in actionable:
                logger.info(f"  {ctx.code} {ctx.name} ({ctx.recommendation})")

        # 汇总统计
        llm_fallback_count = sum(1 for c in top30 if c.llm_fallback)
        nb_data = self.northbound_sentiment or {}
        concept_dist = self.concept_analyzer.get_concept_distribution() if self.concept_analyzer.is_analyzed else {}
        s0_count = self.funnel.stats.get('s0_count', 0)
        s1_kline = self.funnel.stats.get('s1_kline_count', 0)
        l1_count = self.funnel.stats.get('l1_count', 0)
        l2_count = self.funnel.stats.get('l2_count', 0)
        l3_count = self.funnel.stats.get('l3_count', 0)
        stats = {
            'initial': 5000,
            's0': s0_count,
            's1_l1': l1_count,
            's1_kline': s1_kline,
            'l2': l2_count,
            'l3': l3_count,
            'l4': self.funnel.stats.get('l4_count', len(top30)),
            's0_ratio': s0_count / 5000 * 100,
            's1_ratio': s1_kline / max(s0_count, 1) * 100 if s0_count else 0,
            'l2_ratio': l2_count / max(s1_kline, 1) * 100 if s1_kline else 0,
            'l3_ratio': l3_count / max(l2_count, 1) * 100 if l2_count else 0,
            'elapsed': self.tracker.pipeline_end - self.tracker.pipeline_start,
            'llm_api_success': self.tracker.llm_success,
            'llm_total': self.tracker.llm_total,
            'llm_fallback_count': llm_fallback_count,
            'llm_buy_signals': getattr(self.tracker, 'llm_buy_signals', 0),
            'llm_disabled': self.llm_client is None,
            'capital_data_available': any(c.big_order_net != 0 for c in top30),
            'capital_data_date': self.capital_data_date,
            'northbound_available': nb_data.get("available", False),
            'northbound_today_net': nb_data.get("today_net_yi", 0),
            'northbound_trend': nb_data.get("trend_score", 50),
            'northbound_label': nb_data.get("trend_label", "N/A"),
            'concept_total': concept_dist.get("total_concepts", 0),
            'concept_top5': concept_dist.get("top10", [])[:5],
            'concept_occurrences': concept_dist.get("total_occurrences", 0),
            # 数据质量统计
            'dq_daily_kline': sum(1 for c in top30 if c.data_quality_flags.get('daily_kline')),
            'dq_5min_kline': sum(1 for c in top30 if c.data_quality_flags.get('5min_kline')),
            'dq_fund_flow': sum(1 for c in top30 if c.data_quality_flags.get('fund_flow')),
            'dq_late_metrics': sum(1 for c in top30 if c.data_quality_flags.get('late_metrics_calculated')),
            'dq_ma_calculated': sum(1 for c in top30 if c.data_quality_flags.get('ma_calculated')),
        }

        md = render_report(
            strong_buy=strong_buy,
            buy_stocks=buy_stocks,
            watch_stocks=watch_stocks,
            stats=stats,
            data_source=self.fetcher_mgr.get_active_name(),
            market_overview=getattr(self, 'market_overview', None),
        )

        path = save_report(md, self.config.report_output_dir)

        if self.config.enable_md_to_image:
            img_path = md_to_image(md, self.config.report_output_dir)
            if img_path:
                path = img_path

        return path
