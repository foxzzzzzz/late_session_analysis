"""K线数据提供器 — mootdx TCP 日线+5分钟线 + 衍生指标计算

替代原有的近似估算，提供真实K线数据用于:
  - MA5/MA10/MA20 计算 (日线)
  - 20日年化波动率 (日线)
  - 14日ATR (日线)
  - 尾盘指标 (5分钟线): 午后量比、尾盘涨跌幅、最后5分钟占比、突破日内高点

日线双源回退: mootdx TCP → Sina HTTP (借鉴TradingAgents方案)
"""
import json
import logging
import os
import time
import urllib.request
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Sina 日K线 API (JSON, 无需key, 不封IP)
_SINA_KLINE_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "CN_MarketData.getKLineData"
    "?symbol={prefix}{code}&scale=240&ma=no&datalen={bars}"
)


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
        mootdx TCP 失败 → Sina HTTP 回退。
        """
        today = datetime.now().strftime("%Y%m%d")
        result = {}
        fetched = 0
        cached = 0
        sina_fallback = 0

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
            df = None
            try:
                df = self._client.bars(symbol=code, frequency=9, offset=bars)
                if df is not None and not df.empty:
                    df = self._normalize_columns(df)
            except Exception as e:
                logger.debug(f"日线 mootdx 获取失败 {code}: {e}")

            # Sina HTTP 回退
            if df is None or df.empty:
                df = self._sina_daily(code, bars)
                if df is not None and not df.empty:
                    sina_fallback += 1

            if df is not None and not df.empty:
                try:
                    df.to_parquet(cache_file, index=False)
                except Exception:
                    pass
                result[code] = df
                fetched += 1

        parts = [f"{len(result)}/{len(codes)} 只 (缓存 {cached}, 新拉取 {fetched}"]
        if sina_fallback:
            parts.append(f"Sina回退 {sina_fallback}")
        parts.append(")")
        logger.info(f"日线加载: {', '.join(parts)}")
        return result

    @staticmethod
    def _sina_code_prefix(code: str) -> str:
        """股票代码 → Sina API 前缀"""
        code = str(code).zfill(6)
        if code.startswith("6"):
            return "sh"
        elif code.startswith("8") or code.startswith("9"):
            return "bj"
        return "sz"

    @staticmethod
    def _sina_daily(code: str, bars: int = 30) -> Optional[pd.DataFrame]:
        """Sina HTTP 日K线回退 — 当 mootdx TCP 不可用时的降级方案

        借鉴 TradingAgents-astock 方案。
        Sina API 返回 JSON，不含 amount (成交额)，volume 单位是股。
        """
        prefix = KlineProvider._sina_code_prefix(code)
        url = _SINA_KLINE_URL.format(prefix=prefix, code=code, bars=bars)

        try:
            req = urllib.request.Request(url)
            req.add_header("User-Agent", "Mozilla/5.0")
            resp = urllib.request.urlopen(req, timeout=10)
            raw = resp.read().decode("utf-8")
            data = json.loads(raw)
        except Exception as e:
            logger.debug(f"Sina 日线回退失败 {code}: {e}")
            return None

        if not data or not isinstance(data, list):
            return None

        rows = []
        for bar in data:
            try:
                rows.append({
                    "datetime": bar.get("day", ""),
                    "open": float(bar.get("open", 0)),
                    "high": float(bar.get("high", 0)),
                    "low": float(bar.get("low", 0)),
                    "close": float(bar.get("close", 0)),
                    "volume": float(bar.get("volume", 0)),
                    # Sina 不提供成交额，用 close * volume 估算 (偏差大但可用)
                    "amount": float(bar.get("close", 0)) * float(bar.get("volume", 0)),
                })
            except (ValueError, TypeError):
                continue

        if not rows:
            return None

        df = pd.DataFrame(rows)
        df["turnover"] = df["amount"]
        return df

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
    def compute_ma(df: pd.DataFrame) -> tuple[float, float, float, float, float]:
        """从日线DataFrame计算 MA5, MA10, MA20, MA30, MA60

        Args:
            df: 日线DataFrame，需含 'close' 列，按时间升序排列

        Returns:
            (ma5, ma10, ma20, ma30, ma60)
        """
        if df is None or df.empty:
            return 0.0, 0.0, 0.0, 0.0, 0.0
        close = pd.to_numeric(df["close"], errors="coerce").dropna()
        n = len(close)
        if n < 5:
            return 0.0, 0.0, 0.0, 0.0, 0.0
        ma5 = float(close.iloc[-5:].mean())
        ma10 = float(close.iloc[-10:].mean()) if n >= 10 else ma5
        ma20 = float(close.iloc[-20:].mean()) if n >= 20 else ma10
        ma30 = float(close.iloc[-30:].mean()) if n >= 30 else ma20
        ma60 = float(close.iloc[-60:].mean()) if n >= 60 else ma30
        return ma5, ma10, ma20, ma30, ma60

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

    @staticmethod
    def compute_ma5_acceleration(df: pd.DataFrame) -> bool:
        """检查 MA5 是否渐进加速 — 最近2日 MA5 日变化率均为正且 ≥ 0.1%

        策略要求: MA5渐进加速（0.4%→0.2%→0.1%），即MA5每日增长率逐步提升。
        最低要求: 最近1日 MA5 增长率 ≥ 0.1%。

        Args:
            df: 日线DataFrame，需含 'close' 列，按时间升序排列

        Returns:
            True 如果 MA5 处于加速上升状态
        """
        if df is None or df.empty:
            return False
        close = pd.to_numeric(df["close"], errors="coerce").dropna()
        if len(close) < 8:  # 需要至少 5+3 个数据点
            return False

        # 最近3个交易日的 MA5 值 (每个窗口后移一位)
        ma5_latest = float(close.iloc[-5:].mean())
        ma5_prev1 = float(close.iloc[-6:-1].mean())
        ma5_prev2 = float(close.iloc[-7:-2].mean())

        if ma5_prev1 <= 0 or ma5_prev2 <= 0:
            return False

        # 日变化率 (%)
        rate1 = (ma5_latest - ma5_prev1) / ma5_prev1 * 100  # 最新
        rate2 = (ma5_prev1 - ma5_prev2) / ma5_prev2 * 100  # 前一日

        # 两日均正增长，最新日 ≥ 0.1%
        return rate1 >= 0.1 and rate2 >= 0.05

    @staticmethod
    def check_volume_shrink(df: pd.DataFrame) -> bool:
        """检查最近3个交易日是否连续缩量 (>10% per day)

        策略要求: 不允许连续3天缩量>10%

        Args:
            df: 日线DataFrame，需含 'volume' 列，按时间升序排列

        Returns:
            True 如果连续3天缩量 (不合格)
        """
        if df is None or df.empty:
            return False
        if "volume" not in df.columns and "vol" not in df.columns:
            return False
        vol_col = "volume" if "volume" in df.columns else "vol"
        vol = pd.to_numeric(df[vol_col], errors="coerce").dropna()
        if len(vol) < 4:
            return False
        v1, v2, v3 = float(vol.iloc[-1]), float(vol.iloc[-2]), float(vol.iloc[-3])
        if v1 <= 0 or v2 <= 0 or v3 <= 0:
            return False
        # 每步缩量 > 10%: v2 < v3*0.9 AND v1 < v2*0.9
        return v2 < v3 * 0.9 and v1 < v2 * 0.9

    @staticmethod
    def count_yang_days_4(df: pd.DataFrame) -> int:
        """统计近4个交易日阳线天数 (close > open)

        Returns: 0-4，数据不足时返回0
        """
        if df is None or df.empty:
            return 0
        try:
            open_p = pd.to_numeric(df["open"], errors="coerce")
            close = pd.to_numeric(df["close"], errors="coerce")
            if len(close) < 5:
                return 0
            recent_open = open_p.iloc[-5:-1]
            recent_close = close.iloc[-5:-1]
            return sum(1 for o, c in zip(recent_open, recent_close) if c > o)
        except Exception:
            return 0

    @staticmethod
    def check_body_amplifying(df: pd.DataFrame) -> bool:
        """检查近3个交易日实体是否逐日放大

        实体 = |close - open|，要求每天实体 ≥ 前一天的 1.05 倍
        (允许一定波动，只要趋势是放大的)
        """
        if df is None or df.empty:
            return False
        try:
            open_p = pd.to_numeric(df["open"], errors="coerce")
            close = pd.to_numeric(df["close"], errors="coerce")
            if len(close) < 4:
                return False
            bodies = []
            for i in range(-3, 0):
                bodies.append(abs(close.iloc[i] - open_p.iloc[i]))
            if any(b <= 0 for b in bodies):
                return False
            return bodies[1] >= bodies[0] * 0.95 and bodies[2] >= bodies[1] * 0.95
        except Exception:
            return False

    @staticmethod
    def compute_position_20d(df: pd.DataFrame) -> float:
        """近20日价格百分位 = (close - low_20d) / (high_20d - low_20d) * 100

        Returns 0.0 if data insufficient or range is zero.
        """
        if df is None or df.empty:
            return 0.0
        high = pd.to_numeric(df["high"], errors="coerce").dropna()
        low = pd.to_numeric(df["low"], errors="coerce").dropna()
        close = pd.to_numeric(df["close"], errors="coerce").dropna()
        n = min(len(high), len(low), len(close), 20)
        if n < 5:
            return 0.0
        high_20d = float(high.iloc[-n:].max())
        low_20d = float(low.iloc[-n:].min())
        close_now = float(close.iloc[-1])
        if high_20d <= low_20d:
            return 0.0
        return (close_now - low_20d) / (high_20d - low_20d) * 100

    @staticmethod
    def compute_consecutive_close_rise(df: pd.DataFrame) -> int:
        """计算连续收盘上涨天数 (从最近一天往前数)

        Returns: 连续天数 (0-?), 包含今天
        """
        if df is None or df.empty:
            return 0
        try:
            close = pd.to_numeric(df["close"], errors="coerce")
            if len(close) < 3:
                return 0
            count = 0
            for i in range(len(close) - 1, 0, -1):
                if close.iloc[i] > close.iloc[i - 1]:
                    count += 1
                else:
                    break
            return count
        except Exception:
            return 0

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
                       afternoon_volume, late_volume_ratio, last_5min_volume,
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

        # 上午 (9:30-11:30) vs 尾盘前 (13:00-14:30) vs 尾盘 (14:30-15:00)
        morning_mask = df["_t"].dt.hour < 12
        afternoon_mask = df["_t"].dt.hour >= 13
        late_mask = (df["_t"].dt.hour >= 14) & (df["_t"].dt.minute >= 30)
        pre_late_mask = afternoon_mask & ~late_mask  # 13:00-14:30

        # 成交量: 优先 turnover (成交额), 其次 volume
        if "turnover" in df.columns:
            vol_col = "turnover"
        elif "amount" in df.columns:
            vol_col = "amount"
        else:
            vol_col = "volume" if "volume" in df.columns else "vol"

        morning_vol = float(df.loc[morning_mask, vol_col].sum()) if morning_mask.any() else 0.0
        pre_late_vol = float(df.loc[pre_late_mask, vol_col].sum()) if pre_late_mask.any() else 0.0
        late_vol = float(df.loc[late_mask, vol_col].sum()) if late_mask.any() else 0.0
        total_vol = float(df[vol_col].sum())
        pre_late_bars = max(pre_late_mask.sum(), 1)
        pre_late_rate = pre_late_vol / pre_late_bars  # 13:00-14:30 每bar均量 (固定基线)

        # 尾盘量比: 最新完成bar / 午后前半段每bar均量 (与last_5min_volume_pct对称：固定窗口 + 稳定分母)
        late_bars = late_mask.sum()
        if late_bars >= 1:
            latest_late_bar_vol = float(df.loc[late_mask].iloc[-1][vol_col])  # 最新尾盘bar
        else:
            latest_late_bar_vol = 0.0
        late_volume_ratio = latest_late_bar_vol / max(pre_late_rate, 1.0)

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
            "afternoon_volume": pre_late_vol + late_vol,  # 13:00+ 总量 (兼容旧字段)
            "late_volume_ratio": round(late_volume_ratio, 4),
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
        "late_volume_ratio": 0.0,
        "last_5min_volume": 0.0,
        "last_5min_volume_pct": 0.0,
        "broke_high": False,
        "intraday_high": 0.0,
    }
