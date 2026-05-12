"""指标缓存层 — 避免逐轮重复计算 L1/L2/L3 指标"""
import time
import logging
from typing import Optional
from screening.context import StockContext

logger = logging.getLogger(__name__)


class StockMetricsCache:
    """股票指标缓存

    缓存键 = 股票代码
    版本控制 = 数据时间戳变化时自动刷新
    用途: 多轮扫描中，同一只股票已通过的层级不用重新计算
    """

    def __init__(self):
        self._cache: dict[str, dict] = {}

    def get(self, code: str) -> Optional[dict]:
        """获取缓存"""
        entry = self._cache.get(code)
        if entry is None:
            return None
        return entry

    def set_layer_result(self, code: str, layer: int, passed: bool, context: StockContext):
        """缓存某层的计算结果"""
        if code not in self._cache:
            self._cache[code] = {}
        self._cache[code][f'L{layer}'] = {
            'passed': passed,
            'timestamp': time.time(),
            'score': context.total_score if layer == 4 else None,
        }

    def is_layer_cached(self, code: str, layer: int) -> bool:
        """检查某层是否已有缓存结果"""
        entry = self._cache.get(code)
        if entry is None:
            return False
        return f'L{layer}' in entry

    def get_layer_result(self, code: str, layer: int) -> Optional[bool]:
        """获取某层缓存的通过/失败结果"""
        entry = self._cache.get(code)
        if entry is None:
            return None
        layer_data = entry.get(f'L{layer}')
        if layer_data is None:
            return None
        return layer_data['passed']

    def clear_expired(self, max_age_seconds: float = 60.0):
        """清除过期缓存 (超过max_age_seconds未更新的条目)"""
        now = time.time()
        expired = []
        for code, layers in self._cache.items():
            newest = max(
                (v['timestamp'] for v in layers.values()), default=0
            )
            if now - newest > max_age_seconds:
                expired.append(code)
        for code in expired:
            del self._cache[code]
        if expired:
            logger.debug(f"缓存清理: {len(expired)} 条过期")

    def invalidate(self, code: str):
        """主动使某只股票的缓存失效"""
        self._cache.pop(code, None)

    def __len__(self):
        return len(self._cache)
