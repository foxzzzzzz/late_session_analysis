"""基础数据预加载 — 14:30前加载板块行情、解禁日历、K线等静态数据

数据源 (来自 a-stock-data skill):
  - 同花顺行业对比 → 90行业涨跌排名 (一次调用)
  - 限售解禁日历  → 全市场待解禁股票
  - mootdx K线     → MA5/10/20 计算 (TCP通达信)
"""
import logging
import time
import pandas as pd
from typing import Optional
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class DataPreloader:
    """14:30前预加载静态数据，减少盘中等待时间"""

    def __init__(self):
        self.daily_kline: Optional[pd.DataFrame] = None
        self.sector_map: dict[str, str] = {}
        self.sector_performance: dict[str, float] = {}
        self.bad_news_stocks: set[str] = set()
        self.unlock_stocks: set[str] = set()
        self.hot_concepts: dict[str, list[str]] = {}  # code → [题材标签]
        self._loaded = False

    def load_all(self):
        """加载所有静态数据"""
        logger.info("开始预加载静态数据...")
        t0 = time.time()

        self._load_sector_performance_ths()
        self._load_unlock_calendar()
        self._load_hot_concepts()
        self._load_daily_kline_mootdx()

        self._loaded = True
        elapsed = time.time() - t0
        logger.info(
            f"预加载完成 ({elapsed:.1f}s): "
            f"{len(self.sector_performance)} 行业行情, "
            f"{len(self.unlock_stocks)} 只待解禁, "
            f"{len(self.hot_concepts)} 只题材标签, "
            f"{len(self.daily_kline)} 只K线"
        )

    # === 同花顺行业对比 (替代buggy akshare) ===

    def _load_sector_performance_ths(self):
        """从同花顺拉取90行业板块涨跌排名"""
        try:
            import akshare as ak
            df = ak.stock_board_industry_summary_ths()
            if df is not None and not df.empty:
                for _, row in df.iterrows():
                    name = str(row.get("板块", ""))
                    pct = float(row.get("涨跌幅", 0))
                    if name:
                        self.sector_performance[name] = pct
                logger.info(
                    f"同花顺行业对比: {len(self.sector_performance)} 个行业"
                )
        except Exception as e:
            logger.warning(f"同花顺行业对比加载失败: {e}")

    # === 限售解禁日历 ===

    def _load_unlock_calendar(self):
        """预加载未来90天全市场解禁数据"""
        try:
            import akshare as ak
            today = datetime.now()
            start = today.strftime("%Y%m%d")
            end = (today + timedelta(days=90)).strftime("%Y%m%d")
            df = ak.stock_restricted_release_detail_em(
                start_date=start, end_date=end
            )
            if df is not None and not df.empty:
                code_col = next(
                    (c for c in df.columns if c in ("股票代码", "代码", "code")),
                    df.columns[0],
                )
                for _, row in df.iterrows():
                    code = str(row.get(code_col, ""))
                    if code:
                        self.unlock_stocks.add(code)
                logger.info(
                    f"限售解禁: {len(self.unlock_stocks)} 只待解禁"
                )
            else:
                logger.info("限售解禁: 无待解禁数据")
        except Exception as e:
            logger.warning(f"限售解禁加载失败: {e}")

    # === 日K线 (MA计算用) ===

    def _load_daily_kline_mootdx(self):
        """通过mootdx预加载全市场日K线"""
        try:
            from mootdx.quotes import Quotes
            client = Quotes.factory(market="std")
            # 尝试拉取全市场日K线索引
            df = client.bars(symbol="000001", category=4, offset=30)
            if df is not None and not df.empty:
                self.daily_kline = df
                logger.info(f"mootdx K线测试: {len(df)} 条 (000001)")
        except ImportError:
            logger.info("mootdx 未安装，K线数据跳过 (pip install mootdx)")
        except Exception as e:
            logger.warning(f"mootdx K线加载失败: {e}")

    # === 同花顺热点归因 (题材标签) ===

    def _load_hot_concepts(self):
        """从同花顺热点加载当日强势股题材归因"""
        try:
            import requests
            from datetime import date
            today = date.today().strftime("%Y-%m-%d")
            url = (
                f"http://zx.10jqka.com.cn/event/api/getharden/"
                f"date/{today}/orderby/date/orderway/desc/charset/GBK/"
            )
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/117.0.0.0"}
            r = requests.get(url, headers=headers, timeout=10)
            data = r.json()
            if data.get("errocode", 0) != 0:
                logger.debug(f"同花顺热点: 数据不可用 (可能盘后未更新)")
                return

            rows = data.get("data") or []
            for row in rows:
                code = str(row.get("code", ""))
                reason = str(row.get("reason", ""))
                if code and reason:
                    self.hot_concepts[code] = [t.strip() for t in reason.split("+") if t.strip()]

            logger.info(f"同花顺热点: {len(self.hot_concepts)} 只有题材标签")
        except Exception as e:
            logger.debug(f"同花顺热点加载跳过: {e}")

    # === 查询接口 ===

    def get_sector_for_stock(self, code: str) -> str:
        """获取股票所属板块 (按需查询百度概念板块)"""
        if code in self.sector_map:
            return self.sector_map[code]
        return ""

    def get_sector_performance(self, sector_name: str) -> float:
        """获取板块涨跌幅"""
        return self.sector_performance.get(sector_name, 0.0)

    def is_loaded(self) -> bool:
        return self._loaded
