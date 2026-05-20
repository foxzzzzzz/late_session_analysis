"""7阶段主控流水线

时间线:
  S0 14:25-14:30 板块预筛选 (1次)
  S1 14:30-14:50 初始扫描 (每3分钟,候选池,L1+K线)
  S2 14:50-14:55 加速监控 (每1分钟,候选池,L2+资金流向)
  S3 14:55-14:57 最后验证 (每30秒,精选池,L3+均线)
  S4 14:57-14:58 决策冲刺 (每10秒,Top30,L4评分)
  S5-S7 后续实现
"""
import time
import logging
from datetime import datetime, timedelta
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

        # 初始化数据源
        self.fetcher_mgr = self._init_fetchers()

        # 初始化漏斗
        screening_configs = config.get_screening_configs()
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

        # 资金流向数据日期 (today/yesterday/none)
        self.capital_data_date: str = "none"

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
            self.llm_runner = ParallelLLMRunner(self.llm_client)

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

            # === S3: 均线验证 ===
            if 3 in stages:
                contexts, top30 = self._run_stage3(contexts)

            # === S4: 融合评分 + LLM ===
            if 4 in stages:
                top30 = self._run_stage4(top30)

            self.tracker.finish()

            # 生成报告
            return self._generate_report(top30)

        except Exception as e:
            logger.error(f"流水线异常: {e}", exc_info=True)
            raise

    # ============================================================
    # 数据获取与转换
    # ============================================================

    def _fetch_and_convert(self, codes: list[str] = None) -> list[StockContext]:
        """拉取数据并转换为StockContext

        Args:
            codes: 指定股票代码列表。None 表示拉取全市场。
        """
        if codes:
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
            # === 日线指标: MA5/MA10/MA20, 波动率 ===
            dm = self._daily_metrics.get(ctx.code)
            if dm:
                ctx.ma5 = dm['ma5']
                ctx.ma10 = dm['ma10']
                ctx.ma20 = dm['ma20']
                ctx.ma30 = dm.get('ma30', 0.0)
                ctx.ma60 = dm.get('ma60', 0.0)
                ctx.volatility = dm['volatility']
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
                ctx.afternoon_volume_ratio = fm['afternoon_volume_ratio']
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

            # === 近10日胜率 (收盘>开盘) ===
            if ctx.code in self._daily_cache:
                df = self._daily_cache[ctx.code]
                if not df.empty and len(df) >= 5:
                    recent = df.tail(10)
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

            # === S3资金流回填 (从S2缓存恢复) ===
            if self._fund_flow_data:
                fd = self._fund_flow_data.get(ctx.code, {})
                if fd:
                    ctx.big_order_net = fd.get("mainForce", 0) * 10000
                    ctx.big_order_ratio = (
                        abs(ctx.big_order_net) / max(ctx.turnover, 1)
                        if ctx.turnover > 0 else 0
                    )
                    ctx.active_buy_ratio = fd.get("active_buy_ratio", 50.0)
                    ctx.data_quality_flags['fund_flow'] = True

    # ============================================================
    # S0: 板块预筛选
    # ============================================================

    def _run_stage0(self) -> list[str]:
        """S0: 板块预筛选 → 候选股票代码列表"""
        self.tracker.stage_start("S0_板块预筛选", 5000)

        from data_provider.sector_filter import SectorFilter
        sf = SectorFilter(self.preloader, self.config)
        codes, sector_map = sf.filter()
        self._s0_sector_map = sector_map

        self.tracker.stage_end("S0_板块预筛选", len(codes))
        logger.info(f"S0 完成: {len(codes)} 只候选股票")
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

        # K线形态预筛选
        if self.kline_config:
            from screening.layer_kline import screen_kline
            contexts = screen_kline(contexts, self.kline_config, self._daily_cache)
            logger.info(f"S1 K线形态通过: {len(contexts)} 只")

        self.tracker.stage_end("S1_K线扫描", len(contexts))
        return contexts

    # ============================================================
    # S2: 尾盘异常 + 资金流向 (每1分钟循环, 14:50-14:55)
    # ============================================================

    def _run_stage2(self, contexts: list[StockContext]) -> list[StockContext]:
        """S2: 尾盘异常检测 + 资金流向富化 + 5分钟线尾盘指标"""
        self.tracker.stage_start("S2_尾盘异常", len(contexts))

        loop_interval = self.config.s2_loop_interval
        iteration = 0
        codes = [c.code for c in contexts]

        while True:
            iteration += 1
            logger.info(f"S2 第{iteration}轮扫描...")

            # 刷新量价数据
            contexts = self._fetch_and_convert(codes)
            self._enrich_contexts(contexts)

            # === 首轮: 加载5分钟线 + 资金流向 ===
            if iteration == 1:
                # 5分钟K线 → 尾盘指标
                # 注意: _fetch_and_convert 创建的是新 StockContext, l1_passed 为默认值 None
                # 但所有进入 S2 的股票都已通过 S1 筛选, 直接使用全部 codes
                if self._kline_provider and codes:
                    logger.info(f"加载 {len(codes)} 只的5分钟K线...")
                    min5_cache = self._kline_provider.load_5min_batch(codes, bars=48)
                    for code, df in min5_cache.items():
                        if not df.empty:
                            self._5min_metrics[code] = KlineProvider.compute_late_metrics(df)
                    logger.info(f"5分钟线指标计算完成: {len(self._5min_metrics)} 只")
                    # 用5分钟线数据重新增强contexts
                    self._enrich_contexts(contexts)

                # 资金流向 (双源并发: 分钟线 + 新浪)
                if self._flow_minute or self._flow_sina or self._flow_push2his:
                    self._enrich_fund_flow(contexts)

            # L2 尾盘异常检测
            from screening.layer2_anomaly import screen_l2_anomaly, L2Config
            has_capital = self._fund_flow_fetched
            l2_candidates = list(contexts)  # 保留全量，后续可能需要放宽重筛
            contexts = screen_l2_anomaly(
                l2_candidates, self.funnel.config.l2,
                has_depth_data=False,
                has_capital_data=has_capital,
            )

            # 最低保障: 通过数不足 min_pass 时，放宽资金条件重筛
            min_pass = self.config.l2_min_pass
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
                )
                logger.info(f"L2 放宽后: {len(contexts)} 只通过")

            self.funnel.stats['l2_count'] = len(contexts)
            codes = [c.code for c in contexts]

            logger.info(
                f"S2 L2通过: {len(contexts)} 只 "
                f"(资金流向: {'已获取' if self._fund_flow_fetched else '未获取'})"
            )

            if not self._sleep_or_break("14:55", loop_interval):
                break

        self.tracker.stage_end("S2_尾盘异常", len(contexts))
        return contexts

    def _enrich_fund_flow(self, contexts: list[StockContext]):
        """资金流向富化 — 双源并发 + 字段合并 (仅 S2 首轮调用)

        并发拉取:
          A. 分钟线 (push2 fflow/kline klt=1) → 实时 mainForce/super/large/retail
          B. 新浪 (MoneyFlow) → active_buy_ratio + 备选 mainForce

        合并: mainForce优先A, active_buy_ratio始终用B; B失败降级到push2his
        """
        if self._fund_flow_fetched:
            return

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

        # 保存原始数据供 S3 回填
        self._fund_flow_data = minute_data or sina_data or push2his_data

        today_count = 0
        today_str = datetime.now().strftime("%Y-%m-%d")

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
                main_force = hd.get("mainForce")
                flow_detail = hd
                ctx.data_quality_flags['fund_flow_source'] = 'push2his'

            if main_force is None:
                continue

            ctx.data_quality_flags['fund_flow'] = True
            ctx.big_order_net = main_force * 10000
            ctx.big_order_ratio = (
                abs(ctx.big_order_net) / max(ctx.turnover, 1)
                if ctx.turnover > 0 else 0
            )

            # active_buy_ratio: 始终从新浪取 (已修正为 r0_in/(r0_in+r0_out)*100)
            # 新浪失败 → push2his 降级
            ab_source = sd or hd
            ctx.active_buy_ratio = ab_source.get("active_buy_ratio", 50.0)

            # 日期验证: 分钟线或新浪有当日日期即算有效
            dates = [d.get("data_date", "") for d in (md, sd, hd) if d]
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
        self._fund_flow_fetched = True

    # ============================================================
    # S3: 均线验证 (每30秒循环, 14:55-14:57)
    # ============================================================

    def _run_stage3(self, contexts: list[StockContext]) -> tuple:
        """S3: 均线验证 + 北向情绪 + L4评分"""
        self.tracker.stage_start("S3_均线验证", len(contexts))

        loop_interval = self.config.s3_loop_interval
        iteration = 0
        scored: list[StockContext] = []

        while True:
            iteration += 1
            logger.info(f"S3 第{iteration}轮扫描...")

            # 刷新量价数据
            codes = [c.code for c in contexts]
            contexts = self._fetch_and_convert(codes)
            self._enrich_contexts(contexts)  # Tencent 近似在此处执行

            # L3 技术面验证
            from screening.layer3_technical import screen_l3_technical
            l3_passed = screen_l3_technical(
                contexts, self.preloader, self.funnel.config.l3
            )
            self.funnel.stats['l3_count'] = len(l3_passed)
            logger.info(f"S3 L3通过: {len(l3_passed)} 只")

            # 北向资金情绪 (首轮获取)
            if iteration == 1:
                self.northbound_sentiment = {"available": False}
                if self.config.enable_northbound:
                    nb = get_northbound_sentiment()
                    if nb:
                        self.northbound_sentiment = nb
                if self.northbound_sentiment.get("available"):
                    logger.info(
                        f"北向资金: 净买入 {self.northbound_sentiment['today_net_yi']}亿, "
                        f"趋势分 {self.northbound_sentiment['trend_score']:.0f}"
                    )

            # L4 评分
            from screening.layer4_scoring import score_l4, set_northbound_sentiment, set_concept_analyzer
            set_northbound_sentiment(self.northbound_sentiment)
            set_concept_analyzer(self.concept_analyzer)
            scored = score_l4(l3_passed, self.funnel.config.l4, self.capital_data_date)
            self.funnel.stats['l4_count'] = len(scored)

            if not self._sleep_or_break("14:57", loop_interval):
                break

        top30 = self.funnel.get_top(scored, 30)
        self.tracker.stage_end("S3_均线验证", len(scored))
        return scored, top30

    # ============================================================
    # S4: 融合评分 + LLM (每10秒循环, 14:57-14:58)
    # ============================================================

    def _run_stage4(self, top30: list[StockContext]) -> list[StockContext]:
        """S4: LLM分析 + 融合排序 (纯计算，无新数据拉取)"""
        self.tracker.stage_start("S4_评分冲刺", len(top30))

        loop_interval = self.config.s4_loop_interval

        while True:
            # LLM并行分析 (仅首轮，避免重复调用)
            if self.llm_runner and not hasattr(self, '_llm_done'):
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
                self._llm_results = llm_results
                self._llm_done = True

            llm_results = getattr(self, '_llm_results', {})
            top30 = merge_and_rank(top30, llm_results)

            if not self._sleep_or_break("14:58", loop_interval):
                break

        self.tracker.stage_end("S4_评分冲刺", len(top30))
        return top30

    # ============================================================
    # 报告生成
    # ============================================================

    def _generate_report(self, top30: list) -> str:
        """生成报告"""
        strong_buy = [c for c in top30 if c.recommendation == 'strong_buy']
        buy_stocks = [c for c in top30 if c.recommendation == 'buy']
        watch_stocks = [c for c in top30 if c.recommendation == 'watch']

        # 汇总统计
        llm_fallback_count = sum(1 for c in top30 if c.llm_fallback)
        nb_data = self.northbound_sentiment or {}
        concept_dist = self.concept_analyzer.get_concept_distribution() if self.concept_analyzer.is_analyzed else {}
        stats = {
            'initial': 5000,
            'l1': self.funnel.stats.get('l1_count', 0),
            'l2': self.funnel.stats.get('l2_count', 0),
            'l3': self.funnel.stats.get('l3_count', 0),
            'l4': self.funnel.stats.get('l4_count', len(top30)),
            'l1_ratio': self.funnel.stats.get('l1_count', 0) / 5000 * 100,
            'l2_ratio': self.funnel.stats.get('l2_count', 0) / max(self.funnel.stats.get('l1_count', 1), 1) * 100 if self.funnel.stats.get('l1_count') else 0,
            'l3_ratio': self.funnel.stats.get('l3_count', 0) / max(self.funnel.stats.get('l2_count', 1), 1) * 100 if self.funnel.stats.get('l2_count') else 0,
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
        )

        path = save_report(md, self.config.report_output_dir)

        if self.config.enable_md_to_image:
            img_path = md_to_image(md, self.config.report_output_dir)
            if img_path:
                path = img_path

        return path
