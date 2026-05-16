"""历史数据加载器 — 日线 + 5分钟线 + 板块成分股 + 北向资金"""
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


class BacktestDataLoader:
    def __init__(self, config):
        self.config = config
        self.cache_dir = config.cache_dir
        os.makedirs(self.cache_dir, exist_ok=True)

    # ============================================================
    # 板块成分股
    # ============================================================

    def load_sector_constituents(self, sectors: list[str]) -> dict[str, list[str]]:
        """获取各板块成分股代码列表，优先从缓存读取"""
        cache_file = os.path.join(self.cache_dir, "sector_constituents.json")
        if not self.config.no_cache and os.path.exists(cache_file):
            with open(cache_file, "r") as f:
                cached = json.load(f)
            if all(s in cached for s in sectors):
                logger.info(f"板块成分股(缓存): {sum(len(v) for v in cached.values())} 只 ({len(cached)} 板块)")
                return cached

        import akshare as ak
        result = {}
        for sector in sectors:
            for attempt in range(3):
                try:
                    df = ak.stock_board_industry_cons_em(symbol=sector)
                    if df is not None and not df.empty:
                        code_col = next((c for c in df.columns if c in ("代码", "code")), df.columns[0])
                        codes = [str(row[code_col]).strip() for _, row in df.iterrows()]
                        result[sector] = codes
                        logger.info(f"板块 {sector}: {len(codes)} 只")
                        break
                except Exception as e:
                    delay = 1.0 * (2 ** attempt)
                    logger.warning(f"板块 {sector} 第{attempt+1}次失败: {e}, {delay:.1f}s后重试")
                    time.sleep(delay)
            else:
                logger.warning(f"板块 {sector}: 3次重试均失败，使用空列表")
                result[sector] = []

        if not self.config.no_cache:
            with open(cache_file, "w") as f:
                json.dump(result, f, ensure_ascii=False)

        total_codes = set()
        for codes in result.values():
            total_codes.update(codes)
        logger.info(f"板块成分股: {sum(len(v) for v in result.values())} 只(含重复), {len(total_codes)} 只(去重)")
        return result

    # ============================================================
    # 日线数据
    # ============================================================

    def load_daily_bars(self, codes: list[str], start_date: str, end_date: str) -> dict[str, pd.DataFrame]:
        """加载指定股票的日线数据，按股票分别缓存为parquet"""
        import akshare as ak

        result = {}
        bars_dir = os.path.join(self.cache_dir, "daily_bars")
        os.makedirs(bars_dir, exist_ok=True)

        codes_to_fetch = []
        for code in codes:
            cache_file = os.path.join(bars_dir, f"{code}.parquet")
            if not self.config.no_cache and os.path.exists(cache_file):
                try:
                    cached = pd.read_parquet(cache_file)
                    # 检查日期范围
                    if not cached.empty:
                        cached_dates = pd.to_datetime(cached.iloc[:, 0])
                        need_start = pd.Timestamp(start_date)
                        need_end = pd.Timestamp(end_date)
                        if cached_dates.min() <= need_start and cached_dates.max() >= need_end:
                            # 缓存覆盖所需范围
                            mask = (cached_dates >= need_start) & (cached_dates <= need_end)
                            result[code] = cached[mask].reset_index(drop=True)
                            continue
                except Exception:
                    pass
            codes_to_fetch.append(code)

        if codes_to_fetch:
            logger.info(f"日线需拉取: {len(codes_to_fetch)} 只股票")
            for i, code in enumerate(codes_to_fetch):
                try:
                    df = ak.stock_zh_a_hist(symbol=code, period="daily", start_date=start_date, end_date=end_date, adjust="")
                    if df is not None and not df.empty:
                        # 缓存整段
                        cache_file = os.path.join(bars_dir, f"{code}.parquet")
                        if not self.config.no_cache:
                            df.to_parquet(cache_file, index=False)
                        result[code] = df
                except Exception as e:
                    logger.debug(f"日线 {code} 失败: {e}")
                if (i + 1) % 100 == 0:
                    logger.info(f"  日线进度: {i+1}/{len(codes_to_fetch)}")
                time.sleep(0.15)

        logger.info(f"日线加载完成: {len(result)} 只")
        return result

    def get_daily_snapshot(self, daily_bars: dict[str, pd.DataFrame], date_str: str) -> pd.DataFrame:
        """从已加载的日线字典中提取某一天的快照"""
        rows = []
        target_date = str(date_str).replace("-", "")[:8]
        for code, df in daily_bars.items():
            if df is None or df.empty:
                continue
            date_col = df.columns[0]
            df_d = df[df[date_col].astype(str).str.replace("-", "").str[:8] == target_date]
            if not df_d.empty:
                row = df_d.iloc[-1].to_dict()
                row["code"] = str(code).zfill(6)
                rows.append(row)
        return pd.DataFrame(rows)

    # ============================================================
    # 5分钟线数据
    # ============================================================

    def load_5min_bars(self, code: str, date_str: str) -> Optional[pd.DataFrame]:
        """加载单只股票单日的5分钟K线数据"""
        import akshare as ak

        cache_dir_5min = os.path.join(self.cache_dir, "5min_bars", str(date_str)[:8])
        os.makedirs(cache_dir_5min, exist_ok=True)
        cache_file = os.path.join(cache_dir_5min, f"{code}.parquet")

        if not self.config.no_cache and os.path.exists(cache_file):
            try:
                return pd.read_parquet(cache_file)
            except Exception:
                pass

        try:
            start = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]} 09:30:00"
            end = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]} 15:00:00"
            df = ak.stock_zh_a_hist_min_em(symbol=code, start_date=start, end_date=end, period="5", adjust="")
            if df is not None and not df.empty:
                # 标准化列名
                cols = ["time", "open", "close", "high", "low", "chg_amt", "chg_pct", "volume", "turnover", "amplitude", "turnover_rate"]
                df.columns = cols[:len(df.columns)]
                df["time"] = pd.to_datetime(df["time"])
                if not self.config.no_cache:
                    df.to_parquet(cache_file, index=False)
                return df
        except Exception as e:
            logger.debug(f"5分钟线 {code} {date_str}: {e}")

        return None

    def load_5min_bars_batch(self, codes: list[str], date_str: str) -> dict[str, pd.DataFrame]:
        """批量加载某日的5分钟线"""
        result = {}
        total = len(codes)
        for i, code in enumerate(codes):
            df = self.load_5min_bars(code, date_str)
            if df is not None:
                result[code] = df
            if (i + 1) % 50 == 0:
                logger.info(f"  5分钟线进度: {i+1}/{total} (成功{len(result)})")
            time.sleep(1.0 / max(self.config.rate_limit_per_sec, 0.5))
        return result

    # ============================================================
    # 北向资金
    # ============================================================

    def load_northbound_history(self, start_date: str, end_date: str) -> dict:
        """加载北向资金历史数据"""
        import akshare as ak

        cache_file = os.path.join(self.cache_dir, "northbound_history.parquet")
        if not self.config.no_cache and os.path.exists(cache_file):
            try:
                df = pd.read_parquet(cache_file)
                logger.info(f"北向历史(缓存): {len(df)} 天")
                return self._parse_northbound(df, start_date, end_date)
            except Exception:
                pass

        try:
            df = ak.stock_hsgt_north_net_flow_in_em(start_date=start_date, end_date=end_date)
            if df is not None and not df.empty:
                if not self.config.no_cache:
                    df.to_parquet(cache_file, index=False)
                logger.info(f"北向历史: {len(df)} 天")
                return self._parse_northbound(df, start_date, end_date)
        except Exception as e:
            logger.warning(f"北向历史获取失败: {e}")

        return {}

    def _parse_northbound(self, df: pd.DataFrame, start_date: str, end_date: str) -> dict:
        """解析北向DataFrame为 {date: {today_net_yi, trend_score, ...}}"""
        result = {}
        for _, row in df.iterrows():
            date_val = str(row.iloc[0])[:10].replace("-", "")
            net = float(row.iloc[1]) if len(row.columns) > 1 else 0
            result[date_val] = {
                "today_net_yi": net,
                "available": True,
            }
        return result

    # ============================================================
    # 交易日历
    # ============================================================

    @staticmethod
    def get_trading_days(start_date: str, end_date: str) -> list[str]:
        """获取交易日列表 (YYYYMMDD)"""
        import akshare as ak
        try:
            df = ak.tool_trade_date_hist_sina()
            if df is not None and not df.empty:
                date_col = df.columns[0]
                dates = df[date_col].astype(str).tolist()
                return [d for d in dates if start_date <= d <= end_date]
        except Exception:
            pass

        # 降级: 生成所有日期, 排除周末
        start = pd.Timestamp(start_date)
        end = pd.Timestamp(end_date)
        all_dates = pd.date_range(start, end, freq="B")  # business days only
        return [d.strftime("%Y%m%d") for d in all_dates]


# ============================================================
# S2 指标精确计算 (从5分钟线)
# ============================================================

def compute_s2_metrics(df_5min: pd.DataFrame) -> dict:
    """从5分钟K线DataFrame计算S2尾盘异动指标

    Args:
        df_5min: 单日5分钟K线，列为 [time, open, close, high, low, ..., volume, turnover, ...]

    Returns:
        dict with: afternoon_volume_ratio, late_price_change, last_5min_vol_pct,
                   broke_high, intraday_high, price_at_1430, morning_vol, afternoon_vol,
                   total_vol, late_volume
    """
    if df_5min is None or df_5min.empty:
        return _empty_s2_metrics()

    # 列名标准化
    t_col = df_5min.columns[0]  # time
    close_col = "close" if "close" in df_5min.columns else df_5min.columns[3]
    high_col = "high" if "high" in df_5min.columns else df_5min.columns[4]
    low_col = "low" if "low" in df_5min.columns else df_5min.columns[5]
    vol_col = "volume" if "volume" in df_5min.columns else df_5min.columns[7]
    turnover_col = "turnover" if "turnover" in df_5min.columns else df_5min.columns[8]

    df = df_5min.copy()
    df["_t"] = pd.to_datetime(df[t_col])

    # 上午 (9:30-11:30) vs 下午 (13:00-15:00)
    morning_mask = df["_t"].dt.hour < 12
    afternoon_mask = df["_t"].dt.hour >= 13
    late_mask = (df["_t"].dt.hour >= 14) & (df["_t"].dt.minute >= 30)

    morning_vol = df.loc[morning_mask, turnover_col].sum() if morning_mask.any() else 0
    afternoon_vol = df.loc[afternoon_mask, turnover_col].sum() if afternoon_mask.any() else 0
    total_vol = df[turnover_col].sum()

    afternoon_volume_ratio = afternoon_vol / max(morning_vol, 1)

    # 14:30 价格
    bar_1430 = df[(df["_t"].dt.hour == 14) & (df["_t"].dt.minute == 30)]
    if not bar_1430.empty:
        price_at_1430 = bar_1430.iloc[0][close_col]
    elif late_mask.any():
        price_at_1430 = df.loc[late_mask].iloc[0][close_col]
    else:
        price_at_1430 = df.iloc[-1][close_col]

    close_1500 = df.iloc[-1][close_col]
    late_price_change = (close_1500 - price_at_1430) / max(price_at_1430, 0.01) * 100

    # 最后5分钟成交量占比
    last_5min_mask = df["_t"] >= (df["_t"].max() - pd.Timedelta(minutes=5))
    last_5min_vol = df.loc[last_5min_mask, turnover_col].sum()
    last_5min_vol_pct = last_5min_vol / max(total_vol, 1) * 100

    # 最后5分钟成交量 (用于L1计算)
    late_volume = df.loc[last_5min_mask, vol_col].sum() if vol_col in df.columns else 0

    # 突破日内高点
    pre_1430 = df[df["_t"].dt.hour < 14]
    after_1430 = df[(df["_t"].dt.hour >= 14) & (df["_t"].dt.minute >= 30)]
    pre_1430_high = pre_1430[high_col].max() if not pre_1430.empty else df[high_col].max()
    after_1430_high = after_1430[high_col].max() if not after_1430.empty else 0
    intraday_high = max(pre_1430_high, after_1430_high)
    broke_high = bool(after_1430_high > pre_1430_high * 0.99) if after_1430_high > 0 else False

    return {
        "afternoon_volume_ratio": round(afternoon_volume_ratio, 4),
        "late_price_change": round(late_price_change, 4),
        "last_5min_vol_pct": round(last_5min_vol_pct, 2),
        "broke_high": broke_high,
        "intraday_high": float(intraday_high),
        "price_at_1430": float(price_at_1430),
        "close_1500": float(close_1500),
        "morning_vol": float(morning_vol),
        "afternoon_vol": float(afternoon_vol),
        "total_vol": float(total_vol),
        "last_5min_vol": float(last_5min_vol),
        "late_volume": float(late_volume),
    }


def _empty_s2_metrics() -> dict:
    return {
        "afternoon_volume_ratio": 0.0,
        "late_price_change": 0.0,
        "last_5min_vol_pct": 0.0,
        "broke_high": False,
        "intraday_high": 0.0,
        "price_at_1430": 0.0,
        "close_1500": 0.0,
        "morning_vol": 0.0,
        "afternoon_vol": 0.0,
        "total_vol": 0.0,
        "last_5min_vol": 0.0,
        "late_volume": 0.0,
    }
