"""历史数据适配器 — 日线 + 5分钟线 → StockContext"""
import logging
from typing import Optional

import pandas as pd

from screening.context import StockContext
from backtest.data_loader import compute_s2_metrics

logger = logging.getLogger(__name__)

# akshare stock_zh_a_hist 日线列名 → StockContext 字段映射
DAILY_COL_MAP = {
    "日期": "date",
    "开盘": "open",
    "收盘": "close",
    "最高": "high",
    "最低": "low",
    "成交量": "volume",
    "成交额": "turnover_raw",
    "振幅": "amplitude",
    "涨跌幅": "change_pct",
    "涨跌额": "change_amt",
    "换手率": "turnover_rate",
}


class HistoricalDataAdapter:
    """将历史日线 + 5分钟线数据转换为 StockContext 列表"""

    def __init__(self, config, sector_map: dict[str, str] = None,
                 sector_perf: dict[str, float] = None,
                 northbound: dict = None):
        self.config = config
        self.sector_map = sector_map or {}
        self.sector_perf = sector_perf or {}
        self.northbound = northbound or {}

    def adapt_single_day(
        self,
        daily_snapshot: pd.DataFrame,
        date_str: str,
        bars_5min: dict[str, pd.DataFrame] = None,
        pre_close_map: dict[str, float] = None,
        daily_bars: dict[str, pd.DataFrame] = None,
    ) -> list[StockContext]:
        """将某日的日线快照 + 可选5分钟线转换为 StockContext 列表

        Args:
            daily_snapshot: 日线快照 DataFrame, 每行一只股票
            date_str: 日期 YYYYMMDD
            bars_5min: {code: 5分钟线DataFrame} 可选
            pre_close_map: {code: pre_close} 可选 (用于计算涨停价等)
            daily_bars: {code: 完整日线DataFrame} 可选 (用于MA/波动率精确计算)
        """
        bars_5min = bars_5min or {}
        pre_close_map = pre_close_map or {}
        daily_bars = daily_bars or {}
        contexts = []

        for _, row in daily_snapshot.iterrows():
            ctx = self._row_to_context(row, date_str, bars_5min, pre_close_map, daily_bars)
            if ctx is not None:
                contexts.append(ctx)

        logger.info(f"适配完成: {len(contexts)} 只 StockContext (日期={date_str})")
        return contexts

    def _row_to_context(
        self,
        row: pd.Series,
        date_str: str,
        bars_5min: dict[str, pd.DataFrame],
        pre_close_map: dict[str, float],
        daily_bars: dict[str, pd.DataFrame] = None,
    ) -> Optional[StockContext]:
        """单行日线数据 → StockContext"""
        code = str(row.get("code", "")).zfill(6)
        if not code or len(code) < 6:
            return None

        # 提取日线字段 (兼容中英文列名)
        name = str(row.get("name", row.get("名称", "")))
        open_p = self._f(row, "open", "开盘")
        close = self._f(row, "close", "收盘")
        high = self._f(row, "high", "最高")
        low = self._f(row, "low", "最低")
        pre_close = pre_close_map.get(code, self._f(row, "pre_close"))
        if pre_close <= 0:
            pre_close = close / (1 + self._f(row, "change_pct", "涨跌幅") / 100) if self._f(row, "change_pct", "涨跌幅") != 0 else close

        change_pct = self._f(row, "change_pct", "涨跌幅")
        volume = self._f(row, "volume", "成交量")
        turnover = self._f(row, "turnover", "成交额")
        turnover_rate = self._f(row, "turnover_rate", "换手率")
        amplitude = self._f(row, "amplitude", "振幅")
        vol_ratio = self._f(row, "vol_ratio")

        # 市值/PE/PB (日线数据可能没有这些字段，从 spot 获取)
        market_cap = self._f(row, "market_cap", "总市值")
        pe_ttm = self._f(row, "pe_ttm", "市盈率")
        pb = self._f(row, "pb", "市净率")

        # 基础 StockContext
        ctx = StockContext(
            code=code,
            name=name,
            price=close,
            change_pct=change_pct,
            turnover=turnover,
            turnover_rate=turnover_rate,
            volume=volume,
            high=high,
            low=low,
            open=open_p,
            pre_close=pre_close,
            limit_up=round(pre_close * (1 + _get_limit_pct(code) / 100), 2),
            limit_down=round(pre_close * (1 - _get_limit_pct(code) / 100), 2),
            vol_ratio=vol_ratio,
            amplitude=amplitude,
            market_cap=market_cap,
            pe_ttm=pe_ttm,
            pb=pb,
            sector=self.sector_map.get(code, ""),
            sector_performance=self.sector_perf.get(self.sector_map.get(code, ""), 0.0),
        )

        # === S2 指标: 优先使用5分钟线精确计算 ===
        df_5min = bars_5min.get(code)
        if df_5min is not None and not df_5min.empty:
            s2 = compute_s2_metrics(df_5min)
            ctx.afternoon_volume_ratio = s2["afternoon_volume_ratio"]
            ctx.late_price_change = s2["late_price_change"]
            ctx.last_5min_volume_pct = s2["last_5min_vol_pct"]
            ctx.broke_high = s2["broke_high"]
            ctx.intraday_high = s2["intraday_high"]
            ctx.price_at_1430 = s2["price_at_1430"]
            ctx.afternoon_volume = s2["afternoon_vol"]
            ctx.morning_volume = s2["morning_vol"]
            ctx.last_5min_volume = s2["last_5min_vol"]
            # 全天时段均量 (用于L1午后量>时段均量检查)
            total_bars = len(df_5min)
            if total_bars > 0:
                ctx.avg_period_volume = s2["total_vol"] / total_bars
        else:
            # 无5分钟线: S2指标留空(0)，不生成虚假信号
            pass

        # === L3 技术面: 从日线历史精确计算 ===
        df_daily = daily_bars.get(code) if daily_bars else None
        if df_daily is not None and not df_daily.empty:
            from data_provider.kline_provider import KlineProvider
            ma5, ma10, ma20, ma30, ma60 = KlineProvider.compute_ma(df_daily)
            ctx.ma5 = ma5
            ctx.ma10 = ma10
            ctx.ma20 = ma20
            ctx.ma30 = ma30
            ctx.ma60 = ma60
            ctx.volatility = KlineProvider.compute_volatility(df_daily)
        # 无日线数据时 MA/波动率保持为 0 (L3 会将其标记为数据缺失)

        return ctx

    @staticmethod
    def _f(row: pd.Series, *col_names: str) -> float:
        """从行中取字段值，尝试多个列名"""
        for c in col_names:
            val = row.get(c)
            if val is not None and pd.notna(val):
                try:
                    return float(val)
                except (ValueError, TypeError):
                    pass
        return 0.0


def _get_limit_pct(code: str) -> float:
    """根据股票代码判断涨跌停幅度: 主板10%, 科创板20%, 北交所30%"""
    code = str(code).zfill(6)
    if code.startswith("68"):   # 科创板
        return 20.0
    if code.startswith(("4", "8")):  # 北交所
        return 30.0
    return 10.0  # 主板/创业板
