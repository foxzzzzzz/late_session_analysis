"""题材热度分析 — 同花顺热点题材词频统计 + 热度排名

从 preloader.hot_concepts 中提取所有题材标签，
计算频次、共现关系，输出热度排名，供 L4 市场环境评分使用。
"""
import logging
from collections import Counter
from typing import Optional

logger = logging.getLogger(__name__)


class ConceptAnalyzer:
    """题材热度分析器"""

    def __init__(self):
        self._freq: Counter = Counter()
        self._analyzed = False

    def analyze(self, hot_concepts: dict[str, list[str]]):
        """分析所有股票的题材标签

        Args:
            hot_concepts: {code: [题材标签]}
        """
        self._freq.clear()
        for code, tags in hot_concepts.items():
            for tag in tags:
                self._freq[tag] += 1
        self._analyzed = True
        logger.info(
            f"题材分析: {len(self._freq)} 个独立题材, "
            f"Top5: {self.top_concepts(5)}"
        )

    def top_concepts(self, n: int = 10) -> list[tuple[str, int]]:
        """返回频次最高的 N 个题材"""
        return self._freq.most_common(n)

    def get_concept_score(self, tags: list[str]) -> float:
        """计算某只股票的题材热度得分 0-10

        得分 = sum(min(每个题材频次, 10)) / len(tags) 归一化到0-10
        """
        if not tags or not self._analyzed:
            return 0.0
        total = self._freq.total() or 1
        score = 0.0
        for tag in tags:
            freq = self._freq.get(tag, 1)
            # 频次越高越热，但不超过10
            score += min(freq, 10)
        return min(score, 10)

    def get_concept_rank_pct(self, tags: list[str]) -> float:
        """计算股票题材在全市场中的排名百分位 (越小越热)

        取所有题材中排名最高的那个
        """
        if not tags or not self._analyzed:
            return 100.0

        sorted_concepts = [c for c, _ in self._freq.most_common()]
        total = len(sorted_concepts) or 1

        best_rank = total
        for tag in tags:
            try:
                rank = sorted_concepts.index(tag) + 1
                best_rank = min(best_rank, rank)
            except ValueError:
                pass

        return best_rank / total * 100

    def get_concept_distribution(self) -> dict:
        """返回题材分布统计"""
        if not self._analyzed:
            return {}
        sorted_freq = self._freq.most_common()
        return {
            "total_concepts": len(sorted_freq),
            "total_occurrences": self._freq.total(),
            "top10": sorted_freq[:10],
            "long_tail_count": sum(1 for _, c in sorted_freq if c == 1),
            "avg_per_stock": self._freq.total() / max(len(self._freq), 1),
        }

    @property
    def is_analyzed(self) -> bool:
        return self._analyzed


# 单例
_default_analyzer: Optional[ConceptAnalyzer] = None


def get_concept_analyzer() -> ConceptAnalyzer:
    global _default_analyzer
    if _default_analyzer is None:
        _default_analyzer = ConceptAnalyzer()
    return _default_analyzer
