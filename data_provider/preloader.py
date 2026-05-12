"""基础数据预加载 — 在14:30前加载日K线、板块映射等静态数据"""
import logging
import pandas as pd
from typing import Optional

logger = logging.getLogger(__name__)


class DataPreloader:
    """14:30前预加载静态数据，减少盘中等待时间"""

    def __init__(self):
        self.daily_kline: Optional[pd.DataFrame] = None   # 日K线(近30日)
        self.sector_map: dict[str, str] = {}               # 股票→板块映射
        self.sector_performance: dict[str, float] = {}     # 板块→当日涨跌幅
        self.bad_news_stocks: set[str] = set()             # 近3日利空公告
        self.unlock_stocks: set[str] = set()               # 当日解禁股
        self._loaded = False

    def load_all(self):
        """加载所有静态数据"""
        logger.info("开始预加载静态数据...")
        self._load_sector_map()
        self._load_sector_performance()
        self._load_daily_kline()
        self._loaded = True
        logger.info(f"预加载完成: {len(self.sector_map)} 板块映射, "
                     f"{len(self.sector_performance)} 板块行情")

    def _load_sector_map(self):
        """加载股票→板块映射"""
        try:
            import akshare as ak
            df = ak.stock_board_concept_name_em()
            if df is not None and not df.empty:
                for _, row in df.iterrows():
                    # 简化处理，记录板块涨跌幅
                    name = str(row.get('板块名称', row.get('name', '')))
                    pct = float(row.get('涨跌幅', row.get('change_pct', 0)))
                    if name:
                        self.sector_performance[name] = pct
            logger.info(f"加载 {len(self.sector_performance)} 个概念板块")
        except Exception as e:
            logger.warning(f"板块数据加载失败: {e}")

    def _load_sector_performance(self):
        """加载行业板块表现"""
        try:
            import akshare as ak
            df = ak.stock_board_industry_name_ema()
            if df is not None and not df.empty:
                for _, row in df.iterrows():
                    name = str(row.get('板块名称', row.get('name', '')))
                    pct = float(row.get('涨跌幅', row.get('change_pct', 0)))
                    if name:
                        self.sector_performance[name] = pct
        except Exception as e:
            logger.warning(f"行业板块数据加载失败: {e}")

    def _load_daily_kline(self):
        """预加载近30日日K线 (全市场) — MVP阶段按需延迟加载"""
        self.daily_kline = pd.DataFrame()

    def get_sector_for_stock(self, code: str) -> str:
        """获取股票所属板块"""
        return self.sector_map.get(code, "")

    def get_sector_performance(self, sector_name: str) -> float:
        """获取板块涨跌幅"""
        return self.sector_performance.get(sector_name, 0.0)

    def is_loaded(self) -> bool:
        return self._loaded
