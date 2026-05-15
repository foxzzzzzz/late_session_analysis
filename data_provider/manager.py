"""数据源管理器 — 多源优先级降级，参考DSA DataFetcherManager"""
import logging
from typing import Optional
from data_provider.base import BaseFetcher, RealtimeQuote

logger = logging.getLogger(__name__)


class DataFetcherManager:
    """按优先级管理多个数据源，自动降级"""

    def __init__(self, fetchers: list[BaseFetcher]):
        self.fetchers = sorted(fetchers, key=lambda f: f.priority)
        self.active_fetcher: Optional[BaseFetcher] = None
        self._detect_best_source()

    def _detect_best_source(self):
        """检测可用的最高优先级数据源"""
        for fetcher in self.fetchers:
            try:
                if fetcher.is_available():
                    self.active_fetcher = fetcher
                    logger.info(f"数据源: {fetcher.name} (优先级 {fetcher.priority})")
                    return
            except Exception as e:
                logger.warning(f"数据源 {fetcher.name} 不可用: {e}")
        raise RuntimeError("没有可用的数据源")

    def fetch_snapshot(self) -> list[RealtimeQuote]:
        """拉取全市场快照，失败自动降级到下一源"""
        for fetcher in self.fetchers:
            try:
                quotes = fetcher.fetch_snapshot()
                if quotes:
                    if fetcher != self.active_fetcher:
                        logger.warning(f"数据源降级: {self.active_fetcher.name} -> {fetcher.name}")
                        self.active_fetcher = fetcher
                    return quotes
            except Exception as e:
                logger.warning(f"数据源 {fetcher.name} 拉取失败: {e}")
                continue
        raise RuntimeError("所有数据源均拉取失败")

    def fetch_codes(self, codes: list[str]) -> list[RealtimeQuote]:
        """按指定代码列表拉取行情，不支持时降级到全量拉取+本地过滤"""
        if hasattr(self.active_fetcher, 'fetch_codes'):
            try:
                quotes = self.active_fetcher.fetch_codes(codes)
                if quotes:
                    return quotes
            except Exception as e:
                logger.warning(f"fetch_codes 失败: {e}")
        all_quotes = self.fetch_snapshot()
        code_set = set(str(c).zfill(6) for c in codes)
        return [q for q in all_quotes if q.code in code_set]

    def fetch_depth(self, codes: list[str]) -> dict[str, dict]:
        """拉取盘口深度，失败返回空"""
        try:
            return self.active_fetcher.fetch_depth(codes)
        except Exception as e:
            logger.warning(f"盘口数据拉取失败: {e}")
            return {}

    def get_active_name(self) -> str:
        return self.active_fetcher.name if self.active_fetcher else "none"
