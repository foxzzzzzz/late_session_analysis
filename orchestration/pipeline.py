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

        # 初始化资金流向 (东方财富)
        self.baidu_flow: Optional[EastmoneyFlowFetcher] = None
        if config.enable_capital_flow:
            self.baidu_flow = EastmoneyFlowFetcher(timeout=15.0, max_workers=2)

        # 初始化题材分析器
        self.concept_analyzer = get_concept_analyzer()

        # 北向资金情绪 (S3阶段获取)
        self.northbound_sentiment: Optional[dict] = None

        # 资金流向数据日期 (today/yesterday/none)
        self.capital_data_date: str = "none"

        # S0 板块映射 (股票代码 → 板块名)
        self._s0_sector_map: dict[str, str] = {}

        # S2→S3 基线值 (用于 Tencent 量价近似资金流向)
        self._s2_baselines: dict[str, dict] = {}

        # 资金流向是否已拉取 (S2仅首次)
        self._fund_flow_fetched: bool = False

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
        """用快照/预加载数据增强StockContext

        注意: 近似值必须保守，避免在活跃交易时段让过多股票通过 L2。
        主要数据源(Tencent)提供的是全天数据，不能当作尾盘数据使用。
        """
        for ctx in contexts:
            # 午后量比: 仅当全天量比 >= 3.0 时才近似，且午后通常低于全天(×0.7)
            if ctx.vol_ratio >= 3.0 and ctx.afternoon_volume_ratio == 0:
                ctx.afternoon_volume_ratio = ctx.vol_ratio * 0.7

            # 全天涨幅的约35%归因到尾盘 (保守估计，避免L2/L4过度宽松)
            if ctx.late_price_change == 0 and ctx.change_pct != 0:
                ctx.late_price_change = abs(ctx.change_pct) * 0.35

            # 接近日内新高视为突破 (1%以内)
            if ctx.high > 0 and ctx.price >= ctx.high * 0.99:
                ctx.broke_high = True

            # 尾盘量占比: 仅当量比足够高时才近似
            if ctx.vol_ratio >= 3.0 and ctx.last_5min_volume_pct == 0:
                ctx.last_5min_volume_pct = min(ctx.vol_ratio * 4, 15.0)

            # 用振幅近似波动率
            if ctx.volatility == 0 and ctx.amplitude > 0:
                ctx.volatility = ctx.amplitude * 5

            # 用昨收近似MA5 (K线数据不可用时)
            if ctx.ma5 == 0 and ctx.pre_close > 0:
                ctx.ma5 = ctx.pre_close * 0.98
            if ctx.ma10 == 0 and ctx.pre_close > 0:
                ctx.ma10 = ctx.pre_close * 0.97

            # 解禁检查 (来自预加载)
            if self.preloader and ctx.code in self.preloader.unlock_stocks:
                ctx.is_unlock_date = True

            # 热点题材 (来自预加载)
            if self.preloader and ctx.code in self.preloader.hot_concepts:
                ctx.hot_concepts = self.preloader.hot_concepts[ctx.code]
                if ctx.hot_concepts:
                    ctx.leader_strength = True

            # === 修复: 回填板块信息 (S0建立映射 → TencentFetcher无板块字段) ===
            if not ctx.sector and ctx.code in self._s0_sector_map:
                ctx.sector = self._s0_sector_map[ctx.code]

            # === 修复: 写入板块涨跌幅 (之前从未被设置，L4永远为0) ===
            if ctx.sector and self.preloader:
                ctx.sector_performance = self.preloader.get_sector_performance(ctx.sector)

            # === S3: Tencent 量价近似替代资金流向趋势 ===
            if ctx.code in self._s2_baselines:
                baseline = self._s2_baselines[ctx.code]
                # 量比变化 → 量能持续性 (正值表示尾盘量能在加速)
                vol_ratio_delta = ctx.vol_ratio - baseline.get('vol_ratio', ctx.vol_ratio)
                # 价格变化 → 尾盘加速程度
                price_delta_pct = (
                    (ctx.price - baseline.get('price', ctx.price))
                    / max(baseline.get('price', ctx.price), 0.01) * 100
                )
                # 换手率增量
                turnover_delta = ctx.turnover_rate - baseline.get('turnover_rate', ctx.turnover_rate)

                # 综合判断: 量价齐升 → 资金持续流入信号
                if vol_ratio_delta > 0 and price_delta_pct > 0:
                    if ctx.big_order_net == 0:
                        ctx.big_order_net = ctx.turnover * 0.02  # 近似2%为大单
                    if ctx.active_buy_ratio == 0:
                        ctx.active_buy_ratio = min(55.0 + vol_ratio_delta * 2, 70.0)

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
        """S1: K线形态预筛选 + L1基础准入"""
        self.tracker.stage_start("S1_K线扫描", len(s0_codes) if s0_codes else 5000)

        loop_interval = self.config.s1_loop_interval
        iteration = 0

        while True:
            iteration += 1
            logger.info(f"S1 第{iteration}轮扫描...")

            # 拉取量价数据
            contexts = self._fetch_and_convert(s0_codes)
            self._enrich_contexts(contexts)

            # L1 基础准入
            from screening.layer1_access import screen_l1_access
            contexts = screen_l1_access(contexts, self.funnel.config.l1)
            self.funnel.stats['l1_count'] = len(contexts)
            logger.info(f"S1 L1通过: {len(contexts)} 只")

            # K线形态预筛选
            if self.kline_config:
                from screening.layer_kline import screen_kline
                contexts = screen_kline(contexts, self.kline_config)

            if not self._sleep_or_break("14:50", loop_interval):
                break

        self.tracker.stage_end("S1_K线扫描", len(contexts))
        return contexts

    # ============================================================
    # S2: 尾盘异常 + 资金流向 (每1分钟循环, 14:50-14:55)
    # ============================================================

    def _run_stage2(self, contexts: list[StockContext]) -> list[StockContext]:
        """S2: 尾盘异常检测 + 资金流向富化（仅首轮拉取）"""
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

            # === 资金流向: 仅首轮拉取 ===
            if iteration == 1 and self.baidu_flow and contexts:
                self._enrich_fund_flow(contexts)

            # 保存 S2 基线值 (供 S3 Tencent 近似用)
            for ctx in contexts:
                if ctx.l2_passed or ctx.l2_passed is None:
                    self._s2_baselines[ctx.code] = {
                        'vol_ratio': ctx.vol_ratio,
                        'price': ctx.price,
                        'turnover_rate': ctx.turnover_rate,
                    }

            # L2 尾盘异常检测
            from screening.layer2_anomaly import screen_l2_anomaly
            has_capital = self._fund_flow_fetched
            contexts = screen_l2_anomaly(
                contexts, self.funnel.config.l2,
                has_depth_data=False,
                has_capital_data=has_capital,
            )
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
        """资金流向富化 (东财 push2his，仅 S2 首轮调用)"""
        if self._fund_flow_fetched:
            return

        l2_candidates = [c for c in contexts if c.l1_passed]
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

        logger.info(f"资金流向: 富化 {len(enrich_candidates)} 只...")
        codes = [c.code for c in enrich_candidates]
        flow_data = self.baidu_flow.enrich_batch(codes)

        today_count = 0
        yesterday_count = 0
        today_str = datetime.now().strftime("%Y-%m-%d")

        for ctx in enrich_candidates:
            fd = flow_data.get(ctx.code, {})
            if fd:
                ctx.big_order_net = fd.get("mainForce", 0) * 10000
                ctx.big_order_ratio = (
                    abs(ctx.big_order_net) / max(ctx.turnover, 1)
                    if ctx.turnover > 0 else 0
                )
                ctx.active_buy_ratio = fd.get("active_buy_ratio", 50.0)
                dd = fd.get("data_date", "")
                if dd == today_str:
                    today_count += 1
                elif dd:
                    yesterday_count += 1

        self.capital_data_date = "today" if today_count > 0 else (
            "yesterday" if yesterday_count > 0 else "none"
        )
        date_label = (
            f"今日 {today_count} 只" if today_count
            else f"昨日(降级) {yesterday_count} 只" if yesterday_count
            else "无数据"
        )
        logger.info(f"资金流向: 获得 {len(flow_data)} 只有效数据 ({date_label})")
        self._fund_flow_fetched = True

    # ============================================================
    # S3: 均线验证 (每30秒循环, 14:55-14:57)
    # ============================================================

    def _run_stage3(self, contexts: list[StockContext]) -> tuple:
        """S3: 均线验证 + 北向情绪 (不拉取 push2，用 S2 基线做 Tencent 近似)"""
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
            scored = score_l4(l3_passed, self.funnel.config.l4)
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
