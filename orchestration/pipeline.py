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
            if name == "efinance":
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
            )
            contexts.append(ctx)
        return contexts

    def _enrich_contexts(self, contexts: list[StockContext]):
        """用盘口数据增强StockContext (如果有pytdx可用)"""
        # MVP阶段：efinance/akshare不提供盘口，暂跳过
        pass

    def _run_stage1(self) -> tuple:
        """S1: 初始扫描 L1+L2"""
        self.tracker.stage_start("S1_扫描", 5000)

        contexts = self._fetch_and_convert()
        contexts = self.funnel.run(contexts, stage=1)
        top30 = self.funnel.get_top(contexts, 30)

        self.tracker.stage_end("S1_扫描", len(contexts))
        return contexts, top30

    def _run_stage2(self, all_contexts: list, top30: list) -> tuple:
        """S2: 加速监控 L2+L3"""
        self.tracker.stage_start("S2_加速", len(all_contexts))

        # 刷新数据后重新过L2+L3 (继承S1的L1缓存结果)
        contexts = self._fetch_and_convert()
        contexts = self.funnel.run(contexts, stage=2)
        top30 = self.funnel.get_top(contexts, 30)

        self.tracker.stage_end("S2_加速", len(contexts))
        return contexts, top30

    def _run_stage3(self, all_contexts: list, top30: list) -> tuple:
        """S3: 最后验证 L4评分"""
        self.tracker.stage_start("S3_验证", len(all_contexts))

        # 只对通过L2的继续过L3+L4
        l2_passed = [c for c in all_contexts if c.l2_passed]
        scored = self.funnel.run(l2_passed, stage=3)
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
            self.tracker.llm_success = sum(
                1 for r in llm_results.values()
                if r.get('confidence', 'C') != 'C' or r.get('decision') != 'skip'
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
            'llm_success': self.tracker.llm_success,
            'llm_total': self.tracker.llm_total,
            'llm_fallback_count': llm_fallback_count,
            'llm_disabled': self.llm_client is None,
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
