"""K线数据提供器 — mootdx TCP 日线+5分钟线 + 衍生指标计算

替代原有的近似估算，提供真实K线数据用于:
  - MA5/MA10/MA20 计算 (日线)
  - 20日年化波动率 (日线)
  - 14日ATR (日线)
  - 尾盘指标 (5分钟线): 午后量比、尾盘涨跌幅、最后5分钟占比、突破日内高点
"""
import logging
import os
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class KlineProvider:
    """通过 mootdx TCP 提供日线和5分钟K线 + 衍生指标计算

    日线带 parquet 磁盘缓存 (当天有效，盘中不变)。
    5分钟线不缓存 (盘中持续变化)。
    """

    def __init__(self, market: str = "std", cache_dir: str = "data/kline_cache"):
        from mootdx.quotes import Quotes
        self._client = Quotes.factory(market=market)
        self._cache_dir = cache_dir
        os.makedirs(self._cache_dir, exist_ok=True)

    # ================================================================
    # 数据获取
    # ================================================================

    def load_daily_batch(self, codes: list[str], bars: int = 30) -> dict[str, pd.DataFrame]:
        """批量获取日线，每只默认30根，用于MA/波动率/ATR计算。

        带 parquet 磁盘缓存 (当天有效，日线盘中不变)。
        单只失败不阻塞批量。
        """
        today = datetime.now().strftime("%Y%m%d")
        result = {}
        fetched = 0
        cached = 0

        for code in codes:
            code = str(code).zfill(6)
            cache_file = os.path.join(self._cache_dir, f"{today}_{code}.parquet")

            # 检查缓存
            if os.path.exists(cache_file):
                try:
                    df = pd.read_parquet(cache_file)
                    if not df.empty:
                        result[code] = df
                        cached += 1
                        continue
                except Exception:
                    pass

            # 从 mootdx 获取
            try:
                df = self._client.bars(symbol=code, frequency=9, offset=bars)
                if df is not None and not df.empty:
                    df = self._normalize_columns(df)
                    try:
                        df.to_parquet(cache_file, index=False)
                    except Exception:
                        pass
                    result[code] = df
                    fetched += 1
            except Exception as e:
                logger.debug(f"日线获取失败 {code}: {e}")

        if fetched or cached:
            logger.info(
                f"日线加载: {len(result)}/{len(codes)} 只 "
                f"(缓存 {cached}, 新拉取 {fetched})"
            )
        return result

    def load_5min_batch(self, codes: list[str], bars: int = 48) -> dict[str, pd.DataFrame]:
        """批量获取当日5分钟线，每只默认48根 (全天240分钟/5=48)。

        不缓存 — 盘中5分钟线持续变化。
        单只失败不阻塞批量。
        """
        result = {}

        for code in codes:
            code = str(code).zfill(6)
            try:
                df = self._client.bars(symbol=code, frequency=0, offset=bars)
                if df is not None and not df.empty:
                    df = self._normalize_columns(df)
                    result[code] = df
            except Exception as e:
                logger.debug(f"5分钟线获取失败 {code}: {e}")

        if result:
            logger.info(f"5分钟线加载: {len(result)}/{len(codes)} 只")
        return result

    @staticmethod
    def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
        """标准化 mootdx DataFrame 列名，统一添加 turnover 列"""
        df = df.copy()
        # amount 列是成交额 → turnover
        if "amount" in df.columns:
            df["turnover"] = df["amount"]
        # 确保 datetime 列存在 (mootdx 把它作为 index)
        if "datetime" not in df.columns and hasattr(df.index, "name") and df.index.name == "datetime":
            df["datetime"] = df.index
        return df

    # ================================================================
    # 日线衍生指标 (静态方法，可从日线DataFrame独立计算)
    # ================================================================

    @staticmethod
    def compute_ma(df: pd.DataFrame) -> tuple[float, float, float]:
        """从日线DataFrame计算 MA5, MA10, MA20

        Args:
            df: 日线DataFrame，需含 'close' 列，按时间升序排列

        Returns:
            (ma5, ma10, ma20) 或 (0, 0, 0) 数据不足时
        """
        if df is None or df.empty:
            return 0.0, 0.0, 0.0
        close = pd.to_numeric(df["close"], errors="coerce").dropna()
        n = len(close)
        if n < 5:
            return 0.0, 0.0, 0.0
        ma5 = float(close.iloc[-5:].mean())
        ma10 = float(close.iloc[-10:].mean()) if n >= 10 else ma5
        ma20 = float(close.iloc[-20:].mean()) if n >= 20 else ma10
        return ma5, ma10, ma20

    @staticmethod
    def compute_volatility(df: pd.DataFrame, window: int = 20) -> float:
        """从日线close计算年化波动率 = std(returns) * sqrt(252)

        Args:
            df: 日线DataFrame，需含 'close' 列
            window: 计算窗口 (默认20日)

        Returns:
            年化波动率 (小数，如 0.30 = 30%)
        """
        if df is None or df.empty:
            return 0.0
        close = pd.to_numeric(df["close"], errors="coerce").dropna()
        if len(close) < max(window, 5):
            return 0.0
        returns = close.pct_change().dropna().iloc[-window:]
        if len(returns) < 5:
            return 0.0
        return float(returns.std() * np.sqrt(252))

    @staticmethod
    def compute_atr(df: pd.DataFrame, window: int = 14) -> float:
        """从日线计算 ATR (平均真实波幅)

        Args:
            df: 日线DataFrame，需含 'high', 'low', 'close' 列
            window: ATR窗口 (默认14日)

        Returns:
            ATR值 (与价格同单位)
        """
        if df is None or df.empty:
            return 0.0
        high = pd.to_numeric(df["high"], errors="coerce")
        low = pd.to_numeric(df["low"], errors="coerce")
        close = pd.to_numeric(df["close"], errors="coerce")
        if len(close) < 2:
            return 0.0

        prev_close = close.shift(1)
        tr1 = high - low
        tr2 = (high - prev_close).abs()
        tr3 = (low - prev_close).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        tr = tr.dropna()

        if len(tr) < window:
            return float(tr.mean())
        return float(tr.iloc[-window:].mean())

    # ================================================================
    # 5分钟线尾盘指标 (静态方法)
    # ================================================================

    @staticmethod
    def compute_late_metrics(df_5min: pd.DataFrame) -> dict:
        """从5分钟K线计算尾盘异动指标

        Args:
            df_5min: 单日5分钟K线，需含 datetime, close, high, volume, turnover 列

        Returns:
            dict with: price_at_1430, late_price_change, morning_volume,
                       afternoon_volume, afternoon_volume_ratio, last_5min_volume,
                       last_5min_volume_pct, broke_high, intraday_high
        """
        if df_5min is None or df_5min.empty:
            return _empty_late_metrics()

        df = df_5min.copy()

        # 时间列处理
        if "datetime" in df.columns:
            df["_t"] = pd.to_datetime(df["datetime"])
        elif "time" in df.columns:
            df["_t"] = pd.to_datetime(df["time"])
        else:
            # 尝试从 year/month/day/hour/minute 列构造
            if all(c in df.columns for c in ["year", "month", "day", "hour", "minute"]):
                df["_t"] = pd.to_datetime(
                    df[["year", "month", "day", "hour", "minute"]]
                )
            else:
                return _empty_late_metrics()

        # 上午 (9:30-11:30) vs 下午 (13:00-15:00)
        morning_mask = df["_t"].dt.hour < 12
        afternoon_mask = df["_t"].dt.hour >= 13
        late_mask = (df["_t"].dt.hour >= 14) & (df["_t"].dt.minute >= 30)

        # 成交量: 优先 turnover (成交额), 其次 volume
        if "turnover" in df.columns:
            vol_col = "turnover"
        elif "amount" in df.columns:
            vol_col = "amount"
        else:
            vol_col = "volume" if "volume" in df.columns else "vol"

        morning_vol = float(df.loc[morning_mask, vol_col].sum()) if morning_mask.any() else 0.0
        afternoon_vol = float(df.loc[afternoon_mask, vol_col].sum()) if afternoon_mask.any() else 0.0
        total_vol = float(df[vol_col].sum())
        afternoon_volume_ratio = afternoon_vol / max(morning_vol, 1.0)

        # 14:30 价格
        bar_1430 = df[(df["_t"].dt.hour == 14) & (df["_t"].dt.minute == 30)]
        if not bar_1430.empty:
            price_at_1430 = float(bar_1430.iloc[0]["close"])
        elif late_mask.any():
            price_at_1430 = float(df.loc[late_mask].iloc[0]["close"])
        else:
            price_at_1430 = float(df.iloc[-1]["close"])

        # 最新收盘价 (最后一根bar的close)
        close_now = float(df.iloc[-1]["close"])
        late_price_change = (close_now - price_at_1430) / max(price_at_1430, 0.01) * 100

        # 最后5分钟成交量占比
        last_5min_mask = df["_t"] >= (df["_t"].max() - pd.Timedelta(minutes=5))
        last_5min_vol = float(df.loc[last_5min_mask, vol_col].sum())
        last_5min_vol_pct = last_5min_vol / max(total_vol, 1.0) * 100

        # 最后5分钟原始成交量 (用于L1计算)
        raw_vol_col = "volume" if "volume" in df.columns else "vol"
        last_5min_raw_vol = float(df.loc[last_5min_mask, raw_vol_col].sum()) if raw_vol_col in df.columns else 0.0

        # 突破日内高点
        pre_1430 = df[df["_t"].dt.hour < 14]
        after_1430 = df[(df["_t"].dt.hour >= 14) & (df["_t"].dt.minute >= 30)]
        pre_1430_high = float(pre_1430["high"].max()) if not pre_1430.empty else float(df["high"].max())
        after_1430_high = float(after_1430["high"].max()) if not after_1430.empty else 0.0
        intraday_high = max(pre_1430_high, after_1430_high)
        broke_high = bool(after_1430_high > pre_1430_high * 0.99) if after_1430_high > 0 else False

        return {
            "price_at_1430": price_at_1430,
            "late_price_change": round(late_price_change, 4),
            "morning_volume": morning_vol,
            "afternoon_volume": afternoon_vol,
            "afternoon_volume_ratio": round(afternoon_volume_ratio, 4),
            "last_5min_volume": last_5min_raw_vol,
            "last_5min_volume_pct": round(last_5min_vol_pct, 2),
            "broke_high": broke_high,
            "intraday_high": intraday_high,
        }


def _empty_late_metrics() -> dict:
    return {
        "price_at_1430": 0.0,
        "late_price_change": 0.0,
        "morning_volume": 0.0,
        "afternoon_volume": 0.0,
        "afternoon_volume_ratio": 0.0,
        "last_5min_volume": 0.0,
        "last_5min_volume_pct": 0.0,
        "broke_high": False,
        "intraday_high": 0.0,
    }
