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
    ) -> list[StockContext]:
        """将某日的日线快照 + 可选5分钟线转换为 StockContext 列表

        Args:
            daily_snapshot: 日线快照 DataFrame, 每行一只股票
            date_str: 日期 YYYYMMDD
            bars_5min: {code: 5分钟线DataFrame} 可选
            pre_close_map: {code: pre_close} 可选 (用于计算涨停价等)
        """
        bars_5min = bars_5min or {}
        pre_close_map = pre_close_map or {}
        contexts = []

        for _, row in daily_snapshot.iterrows():
            ctx = self._row_to_context(row, date_str, bars_5min, pre_close_map)
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
            limit_up=round(pre_close * 1.1, 2),
            limit_down=round(pre_close * 0.9, 2),
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
            # 降级: 使用近似公式
            _apply_approximations(ctx)

        # === L3 技术面近似 ===
        ctx.ma5 = pre_close * 0.98
        ctx.ma10 = pre_close * 0.97
        ctx.volatility = amplitude * 5 if amplitude > 0 else 0.0

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


def _apply_approximations(ctx: StockContext):
    """无5分钟线时的近似公式 (与 pipeline._enrich_contexts 保持一致)"""
    if ctx.vol_ratio >= 3.0:
        if ctx.afternoon_volume_ratio == 0:
            ctx.afternoon_volume_ratio = ctx.vol_ratio * 0.7
        if ctx.last_5min_volume_pct == 0:
            ctx.last_5min_volume_pct = min(ctx.vol_ratio * 4, 15.0)

    if ctx.late_price_change == 0 and ctx.change_pct != 0:
        ctx.late_price_change = abs(ctx.change_pct) * 0.35

    ctx.broke_high = ctx.price >= ctx.high * 0.99
    ctx.intraday_high = ctx.high
    ctx.price_at_1430 = ctx.open
