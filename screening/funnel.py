"""筛选漏斗编排器 — 串联L1→L2→L3→L4"""
import time
import logging
from typing import Optional
from dataclasses import dataclass

from screening.layer1_access import screen_l1_access, L1Config
from screening.layer2_anomaly import screen_l2_anomaly, L2Config
from screening.layer3_technical import screen_l3_technical, L3Config
from screening.layer4_scoring import score_l4, L4Config
from screening.cache import StockMetricsCache
from data_provider.preloader import DataPreloader

logger = logging.getLogger(__name__)


@dataclass
class FunnelConfig:
    l1: L1Config = None
    l2: L2Config = None
    l3: L3Config = None
    l4: L4Config = None
    kline: object = None  # KlineConfig, optional
    rule_scorer: object = None  # RuleScorerConfig, optional

    def __post_init__(self):
        if self.l1 is None:
            self.l1 = L1Config()
        if self.l2 is None:
            self.l2 = L2Config()
        if self.l3 is None:
            self.l3 = L3Config()
        if self.l4 is None:
            self.l4 = L4Config()


class FunnelPipeline:
    """4层筛选漏斗编排器"""

    def __init__(
        self,
        config: Optional[FunnelConfig] = None,
        preloader: Optional[DataPreloader] = None,
        cache: Optional[StockMetricsCache] = None,
    ):
        self.config = config or FunnelConfig()
        self.preloader = preloader
        self.cache = cache or StockMetricsCache()
        self.stats: dict = {}

    def run(self, contexts: list, stage: int, has_depth: bool = False, has_capital: bool = False) -> list:
        """运行漏斗筛选

        Args:
            contexts: 待筛选的StockContext列表
            stage: 当前阶段 (1-4)
            has_depth: 是否有盘口数据
            has_capital: 是否有资金流向数据

        Returns:
            筛选后的StockContext列表
        """
        t0 = time.time()
        initial = len(contexts)

        # S1: L1 + L2
        if stage >= 1:
            contexts = screen_l1_access(contexts, self.config.l1)
            self.stats['l1_count'] = len(contexts)

        if stage >= 1:
            contexts = screen_l2_anomaly(
                contexts, self.config.l2,
                has_depth_data=has_depth,
                has_capital_data=has_capital,
            )
            self.stats['l2_count'] = len(contexts)

        # S2+: L3
        if stage >= 2:
            contexts = screen_l3_technical(contexts, self.preloader, self.config.l3)
            self.stats['l3_count'] = len(contexts)

        # S3+: L4评分
        if stage >= 3:
            contexts = score_l4(contexts, self.config.l4)
            self.stats['l4_count'] = len(contexts)

        elapsed = time.time() - t0
        self.stats['elapsed'] = elapsed
        self.stats['initial'] = initial
        self.stats['final'] = len(contexts)

        logger.info(f"漏斗 Stage{stage}: {initial} → {len(contexts)} ({elapsed:.2f}s)")
        return contexts

    def run_quick(self, contexts: list) -> list:
        """快速通过所有4层 (用于收盘后完整分析或测试)"""
        return self.run(contexts, stage=3, has_depth=False, has_capital=False)

    def get_top(self, contexts: list, n: int = 30) -> list:
        """获取评分最高的N只股票"""
        sorted_ctx = sorted(contexts, key=lambda c: c.total_score, reverse=True)
        return sorted_ctx[:n]

    def get_high_attention(self, contexts: list) -> list:
        """获取重点关注标的(>75分)"""
        return [c for c in contexts if c.total_score > self.config.l4.high_attention_threshold]
