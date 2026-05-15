"""4阶段主控流水线

时间线:
  S1 14:30-14:50 初始扫描 (每3分钟,全市场,L1+L2)
  S2 14:50-14:55 加速监控 (每1分钟,候选池,L2+L3)
  S3 14:55-14:57 最后验证 (每30秒,精选池,L3+L4)
  S4 14:57-14:58 决策冲刺 (每10秒,Top30,L4+LLM)
"""
import time
import logging
from datetime import datetime
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
    """尾盘分析4阶段流水线"""

    def __init__(self, config: SystemConfig):
        self.config = config
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

        # 初始化资金流向 (东方财富替代百度)
        self.baidu_flow: Optional[EastmoneyFlowFetcher] = None
        if config.enable_capital_flow:
            self.baidu_flow = EastmoneyFlowFetcher(timeout=15.0, max_workers=2)

        # 初始化题材分析器
        self.concept_analyzer = get_concept_analyzer()

        # 北向资金情绪 (S3阶段获取)
        self.northbound_sentiment: Optional[dict] = None

        # 资金流向数据日期 (today/yesterday/none)
        self.capital_data_date: str = "none"

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

    def run(self, stages: list[int] = None) -> str:
        """运行流水线

        Args:
            stages: 要运行的阶段列表，默认全部 [1,2,3,4]
                    1=仅L1+L2, 2=含L3, 3=含L4, 4=含LLM

        Returns:
            报告文件路径
        """
        if stages is None:
            stages = [1, 2, 3, 4]

        self.tracker.start()

        # 预加载
        if self.preloader and not self.preloader.is_loaded():
            self.preloader.load_all()

        # 题材热度分析 (基于预加载的热点数据)
        if self.preloader and self.preloader.hot_concepts:
            self.concept_analyzer.analyze(self.preloader.hot_concepts)

        try:
            all_contexts = []
            top30 = []

            # === Stage 1 ===
            if 1 in stages:
                all_contexts, top30 = self._run_stage1()

            # === Stage 2 ===
            if 2 in stages:
                all_contexts, top30 = self._run_stage2(all_contexts, top30)

            # === Stage 3 ===
            if 3 in stages:
                all_contexts, top30 = self._run_stage3(all_contexts, top30)

            # === Stage 4 ===
            if 4 in stages:
                top30 = self._run_stage4(top30)

            # 生成报告
            return self._generate_report(top30)

        except Exception as e:
            logger.error(f"流水线异常: {e}", exc_info=True)
            raise

    def _fetch_and_convert(self) -> list[StockContext]:
        """拉取全市场数据并转换为StockContext"""
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

    def _run_stage1(self) -> tuple:
        """S1: 初始扫描 L1+L2 (不依赖资金流向,快速过滤)"""
        self.tracker.stage_start("S1_扫描", 5000)

        contexts = self._fetch_and_convert()
        self._enrich_contexts(contexts)
        contexts = self.funnel.run(contexts, stage=1)
        top30 = self.funnel.get_top(contexts, 30)

        self.tracker.stage_end("S1_扫描", len(contexts))
        return contexts, top30

    def _run_stage2(self, all_contexts: list, top30: list) -> tuple:
        """S2: 加速监控 L2+L3"""
        self.tracker.stage_start("S2_加速", len(all_contexts))

        # 刷新数据后重新过L2+L3 (继承S1的L1缓存结果)
        contexts = self._fetch_and_convert()
        self._enrich_contexts(contexts)
        contexts = self.funnel.run(contexts, stage=2)
        top30 = self.funnel.get_top(contexts, 30)

        self.tracker.stage_end("S2_加速", len(contexts))
        return contexts, top30

    def _run_stage3(self, all_contexts: list, top30: list) -> tuple:
        """S3: L3技术验证 → 百度资金富化 → L4评分"""
        self.tracker.stage_start("S3_验证", len(all_contexts))

        l2_passed = [c for c in all_contexts if c.l2_passed]

        # L3 技术面验证
        from screening.layer3_technical import screen_l3_technical
        l3_passed = screen_l3_technical(l2_passed, self.preloader, self.funnel.config.l3)
        self.funnel.stats['l3_count'] = len(l3_passed)
        logger.info(f"L3 通过: {len(l3_passed)} 只")

        # 资金流向富化 (东方财富: push2his今日 → push2昨日降级)
        if self.baidu_flow and l3_passed:
            # 按活跃度排序，仅对前N只做资金流向富化 (控制API调用量)
            enrich_limit = self.config.max_capital_enrich
            l3_passed.sort(
                key=lambda c: abs(c.change_pct) * c.vol_ratio * c.turnover,
                reverse=True,
            )
            enrich_candidates = l3_passed[:enrich_limit]
            if len(l3_passed) > enrich_limit:
                logger.info(
                    f"资金流向: L3通过 {len(l3_passed)} 只, "
                    f"仅对前 {enrich_limit} 只做资金富化 (按活跃度排序)"
                )
            logger.info(f"资金流向: 富化 {len(enrich_candidates)} 只 L3 通过股...")
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

        # 北向资金情绪 (S3阶段获取，非交易时段降级为空)
        self.northbound_sentiment = {"available": False}
        if self.config.enable_northbound:
            nb = get_northbound_sentiment()
            if nb:
                self.northbound_sentiment = nb
        if self.northbound_sentiment.get("available"):
            logger.info(
                f"北向资金: 净买入 {self.northbound_sentiment['today_net_yi']}亿, "
                f"趋势分 {self.northbound_sentiment['trend_score']:.0f}, "
                f"情绪: {self.northbound_sentiment['trend_label']}"
            )

        # L4 评分 (注入北向情绪 + 题材分析)
        from screening.layer4_scoring import score_l4, set_northbound_sentiment, set_concept_analyzer
        set_northbound_sentiment(self.northbound_sentiment)
        set_concept_analyzer(self.concept_analyzer)
        scored = score_l4(l3_passed, self.funnel.config.l4)
        self.funnel.stats['l4_count'] = len(scored)
        top30 = self.funnel.get_top(scored, 30)

        self.tracker.stage_end("S3_验证", len(scored))
        return scored, top30

    def _run_stage4(self, top30: list) -> list:
        """S4: 决策冲刺 LLM分析 + 融合排序"""
        self.tracker.stage_start("S4_冲刺", len(top30))

        # LLM并行分析
        if self.llm_runner:
            self.tracker.llm_total = len(top30)
            llm_results = self.llm_runner.analyze_batch(top30)
            # API调用成功(非fallback) vs API调用失败(fallback)
            self.tracker.llm_success = sum(
                1 for r in llm_results.values()
                if not r.get('fallback', False)
            )
            self.tracker.llm_buy_signals = sum(
                1 for r in llm_results.values()
                if r.get('decision') == 'buy'
            )
        else:
            llm_results = {}

        # 融合规则评分 + LLM结果
        top30 = merge_and_rank(top30, llm_results)

        self.tracker.stage_end("S4_冲刺", len(top30))
        return top30

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
            'elapsed': self.funnel.stats.get('elapsed', 0),
            'llm_api_success': self.tracker.llm_success,
            'llm_total': self.tracker.llm_total,
            'llm_fallback_count': llm_fallback_count,
            'llm_buy_signals': getattr(self.tracker, 'llm_buy_signals', 0),
            'llm_disabled': self.llm_client is None,
            'capital_data_available': any(c.big_order_net != 0 for c in top30),
            'capital_data_date': self.capital_data_date,  # today/yesterday/none
            # P2: 北向资金 + 题材热度
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
