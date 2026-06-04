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
        """获取各板块成分股代码列表，优先从缓存读取

        数据源优先级: 缓存JSON → akshare API → baostock近似匹配 → 空列表
        """
        cache_file = os.path.join(self.cache_dir, "sector_constituents.json")
        if not self.config.no_cache and os.path.exists(cache_file):
            with open(cache_file, "r") as f:
                cached = json.load(f)
            if all(s in cached for s in sectors):
                logger.info(f"板块成分股(缓存): {sum(len(v) for v in cached.values())} 只 ({len(cached)} 板块)")
                return cached

        # 尝试 akshare (仅交易时段可用)
        if _is_trading_session():
            result = self._load_sectors_via_akshare(sectors)
        else:
            logger.info("非交易时段，跳过 akshare 板块获取，直接使用 baostock")
            result = {s: [] for s in sectors}
        failed_sectors = [s for s in sectors if not result.get(s)]

        # baostock 降级: 对失败的板块用名称近似匹配
        if failed_sectors:
            logger.info(f"akshare未获取到板块: {failed_sectors}, 尝试baostock降级...")
            bs_result = self._load_sectors_via_baostock(failed_sectors)
            for sector, codes in bs_result.items():
                if codes:
                    result[sector] = codes
                    logger.info(f"板块 {sector}(baostock): {len(codes)} 只")

        # 仍然失败的板块
        still_failed = [s for s in sectors if not result.get(s)]
        if still_failed:
            logger.warning(
                f"以下板块无法获取成分股: {still_failed}。"
                f"建议在交易日运行一次以填充缓存: python -c \"from backtest.data_loader import BacktestDataLoader; "
                f"from backtest.config import BacktestConfig; BacktestDataLoader(BacktestConfig()).load_sector_constituents("
                f"{sectors})\" "
            )
            for s in still_failed:
                if s not in result:
                    result[s] = []

        if not self.config.no_cache and any(result.values()):
            with open(cache_file, "w") as f:
                json.dump(result, f, ensure_ascii=False)

        total_codes = set()
        for codes in result.values():
            total_codes.update(codes)
        logger.info(f"板块成分股: {sum(len(v) for v in result.values())} 只(含重复), {len(total_codes)} 只(去重)")
        return result

    def _load_sectors_via_akshare(self, sectors: list[str]) -> dict[str, list[str]]:
        """通过 akshare 获取板块成分股"""
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
                logger.warning(f"板块 {sector}: akshare 3次重试均失败")
                result[sector] = []
        return result

    def _load_sectors_via_baostock(self, sectors: list[str]) -> dict[str, list[str]]:
        """通过 baostock 股票名称关键词近似匹配板块成分股（慢，一次性降级）"""
        import baostock as bs
        logger.info("baostock降级: 正在获取全量股票列表用于板块名称匹配 (约需30-60秒)...")
        try:
            bs.login()
            rs = bs.query_stock_basic()
            if rs.error_code != "0":
                logger.warning(f"baostock query_stock_basic失败: {rs.error_msg}")
                return {s: [] for s in sectors}

            # 遍历全量股票，按名称关键词匹配板块
            sector_keywords = {
                "半导体": ["半导", "芯片", "集成"],
                "电子元件": ["电子", "元件", "PCB", "电路"],
                "通信设备": ["通信", "通讯", "网络", "电信", "5G"],
                "汽车零部件": ["汽车", "汽配", "车辆", "轮胎", "底盘"],
                "计算机设备": ["计算机", "电脑", "服务器", "信息", "软件", "数据"],
                "软件开发": ["软件", "开发", "程序", "系统集"],
                "消费电子": ["消费电子", "电子"],
                "光伏设备": ["光伏", "太阳能", "硅"],
                "证券": ["证券", "券商"],
            }

            result = {s: [] for s in sectors}
            t0 = time.time()
            count = 0
            while rs.next():
                count += 1
                row = rs.get_row_data()
                if len(row) >= 2:
                    code = row[0].replace("sh.", "").replace("sz.", "").replace("bj.", "")
                    name = row[1] if len(row) > 1 else ""
                    for sector in sectors:
                        keywords = sector_keywords.get(sector, [sector[:2]])
                        if any(kw in name for kw in keywords):
                            result[sector].append(code)

                if count % 1000 == 0:
                    elapsed = time.time() - t0
                    logger.info(f"  baostock扫描: {count}只, 耗时{elapsed:.0f}s")

            elapsed = time.time() - t0
            logger.info(f"baostock降级完成: {count}只扫描, 耗时{elapsed:.0f}s")
            for sector, codes in result.items():
                if codes:
                    logger.info(f"  板块 {sector}: {len(codes)} 只 (名称匹配)")
            return result
        except Exception as e:
            logger.warning(f"baostock降级失败: {e}")
            return {s: [] for s in sectors}
        finally:
            try:
                bs.logout()
            except Exception:
                pass

    # ============================================================
    # 日线数据
    # ============================================================

    def load_daily_bars(self, codes: list[str], start_date: str, end_date: str) -> dict[str, pd.DataFrame]:
        """加载指定股票的日线数据 (baostock数据源, 24/7可用)，按股票分别缓存为parquet

        数据从 start_date 前 60 个自然日开始拉取，确保有足够的历史K线用于
        ATR(14日)、MA20、阳线占比(4日)等指标计算。截断到回测日由引擎的
        _truncate_daily_bars 处理，这里只负责加载充足的历史数据。
        """
        # 历史数据起点: K线指标最长需要20日，取60自然日安全边际
        fetch_start_ts = pd.Timestamp(start_date) - pd.Timedelta(days=60)
        fetch_start = fetch_start_ts.strftime("%Y%m%d")

        result = {}
        bars_dir = os.path.join(self.cache_dir, "daily_bars")
        os.makedirs(bars_dir, exist_ok=True)

        codes_to_fetch = []
        for code in codes:
            cache_file = os.path.join(bars_dir, f"{code}.parquet")
            if not self.config.no_cache and os.path.exists(cache_file):
                try:
                    cached = pd.read_parquet(cache_file)
                    if not cached.empty:
                        cached_dates = pd.to_datetime(cached.iloc[:, 0])
                        need_end = pd.Timestamp(end_date)
                        if cached_dates.min() <= pd.Timestamp(start_date) and cached_dates.max() >= need_end:
                            mask = (cached_dates >= pd.Timestamp(fetch_start)) & (cached_dates <= need_end)
                            result[code] = cached[mask].reset_index(drop=True)
                            continue
                except Exception:
                    pass
            codes_to_fetch.append(code)

        if codes_to_fetch:
            logger.info(f"日线需拉取(baostock): {len(codes_to_fetch)} 只股票 "
                        f"(区间 {fetch_start}→{end_date})")
            query_start = f"{fetch_start[:4]}-{fetch_start[4:6]}-{fetch_start[6:8]}"
            end8 = str(end_date)[:8]
            query_end = f"{end8[:4]}-{end8[4:6]}-{end8[6:8]}"

            import baostock as bs
            bs.login()
            try:
                total = len(codes_to_fetch)
                for i, code in enumerate(codes_to_fetch):
                    try:
                        df = self._fetch_daily_baostock(code, query_start, query_end)
                        if df is not None and not df.empty:
                            cache_file = os.path.join(bars_dir, f"{code}.parquet")
                            if not self.config.no_cache:
                                df.to_parquet(cache_file, index=False)
                            result[code] = df
                    except Exception as e:
                        logger.debug(f"日线 {code} 失败: {e}")
                    if (i + 1) % 100 == 0:
                        logger.info(f"  日线进度: {i+1}/{total}")
            finally:
                try:
                    bs.logout()
                except Exception:
                    pass

        logger.info(f"日线加载完成: {len(result)} 只")
        return result

    def _fetch_daily_baostock(self, code: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
        """使用 baostock 获取日线数据 (调用前需确保 bs.login() 已执行)"""
        import baostock as bs
        bs_code = _code_to_baostock(code)
        rs = bs.query_history_k_data_plus(
            bs_code,
            "date,code,open,high,low,close,volume,amount,turn,pctChg",
            start_date=start_date, end_date=end_date,
            frequency="d", adjustflag="3",
        )
        if rs.error_code != "0":
            logger.debug(f"baostock日线查询失败 {code}: {rs.error_msg}")
            return None

        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
        if not rows:
            return None

        df = pd.DataFrame(rows, columns=["date", "code", "open", "high", "low", "close", "volume", "amount", "turn", "pctChg"])
        for col in ["open", "high", "low", "close", "volume", "amount", "turn", "pctChg"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["change_pct"] = df["pctChg"]
        df["turnover"] = df["amount"]
        df["turnover_rate"] = df["turn"]
        return df

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
        """加载单只股票单日的5分钟K线数据 (baostock数据源, 24/7可用)

        单只调用自动处理 baostock 登录/登出。批量调用请用 load_5min_bars_batch。
        """
        cached = self._load_5min_with_cache(code, date_str)
        if cached is not None:
            return cached

        import baostock as bs
        bs.login()
        try:
            df = self._fetch_5min_baostock(code, date_str)
            if df is not None and not df.empty:
                self._save_5min_cache(code, date_str, df)
                return df
        except Exception as e:
            logger.debug(f"5分钟线 {code} {date_str}: {e}")
        finally:
            try:
                bs.logout()
            except Exception:
                pass

        return None

    def _fetch_5min_baostock(self, code: str, date_str: str) -> Optional[pd.DataFrame]:
        """使用 baostock 获取单日5分钟K线 (调用前需确保 bs.login() 已执行)"""
        import baostock as bs

        bs_code = _code_to_baostock(code)
        date8 = str(date_str)[:8]
        query_date = f"{date8[:4]}-{date8[4:6]}-{date8[6:8]}"

        rs = bs.query_history_k_data_plus(
            bs_code,
            "date,time,code,open,high,low,close,volume,amount",
            start_date=query_date, end_date=query_date,
            frequency="5", adjustflag="3",
        )
        if rs.error_code != "0":
            logger.debug(f"baostock查询失败 {code} {date_str}: {rs.error_msg}")
            return None

        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
        if not rows:
            return None

        df = pd.DataFrame(rows, columns=["date", "time", "code", "open", "high", "low", "close", "volume", "amount"])
        for col in ["open", "high", "low", "close", "volume", "amount"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["time"] = pd.to_datetime(df["time"].astype(str).str[:14], format="%Y%m%d%H%M%S")
        df["turnover"] = df["amount"]
        return df

    def _load_5min_with_cache(self, code: str, date_str: str) -> Optional[pd.DataFrame]:
        """检查5分钟线缓存，命中则返回，未命中返回 None"""
        cache_dir_5min = os.path.join(self.cache_dir, "5min_bars", str(date_str)[:8])
        os.makedirs(cache_dir_5min, exist_ok=True)
        cache_file = os.path.join(cache_dir_5min, f"{code}.parquet")
        if not self.config.no_cache and os.path.exists(cache_file):
            try:
                return pd.read_parquet(cache_file)
            except Exception:
                pass
        return None

    def _save_5min_cache(self, code: str, date_str: str, df: pd.DataFrame):
        if self.config.no_cache:
            return
        cache_dir_5min = os.path.join(self.cache_dir, "5min_bars", str(date_str)[:8])
        os.makedirs(cache_dir_5min, exist_ok=True)
        cache_file = os.path.join(cache_dir_5min, f"{code}.parquet")
        df.to_parquet(cache_file, index=False)

    def load_5min_bars_batch(self, codes: list[str], date_str: str) -> dict[str, pd.DataFrame]:
        """批量加载某日的5分钟线 (baostock单次登录，避免重复login/logout)"""
        import baostock as bs

        # 先检查缓存
        result = {}
        codes_to_fetch = []
        for code in codes:
            cached = self._load_5min_with_cache(code, date_str)
            if cached is not None:
                result[code] = cached
            else:
                codes_to_fetch.append(code)

        if codes_to_fetch:
            bs.login()
            try:
                total = len(codes_to_fetch)
                for i, code in enumerate(codes_to_fetch):
                    try:
                        df = self._fetch_5min_baostock(code, date_str)
                        if df is not None and not df.empty:
                            self._save_5min_cache(code, date_str, df)
                            result[code] = df
                    except Exception as e:
                        logger.debug(f"5分钟线 {code} {date_str}: {e}")
                    if (i + 1) % 50 == 0:
                        logger.info(f"  5分钟线进度: {i+1}/{total} (成功{len(result) - len(codes_to_fetch) + i + 1})")
                    time.sleep(0.3)
            finally:
                try:
                    bs.logout()
                except Exception:
                    pass

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
                "trend_score": 50.0,      # 历史回测固定中性分
                "trend_label": "neutral", # 历史北向趋势标签
            }
        return result

    # ============================================================
    # 交易日历
    # ============================================================

    @staticmethod
    def get_trading_days(start_date: str, end_date: str) -> list[str]:
        """获取交易日列表 (YYYYMMDD)"""
        import akshare as ak

        def _norm(d: str) -> str:
            return str(d).replace("-", "").replace("/", "")[:8]

        try:
            df = ak.tool_trade_date_hist_sina()
            if df is not None and not df.empty:
                date_col = df.columns[0]
                dates = df[date_col].astype(str).tolist()
                result = [d for d in dates if _norm(start_date) <= _norm(d) <= _norm(end_date)]
                if result:
                    return [_norm(d) for d in result]
        except Exception:
            pass

        # 降级: 生成所有日期, 排除周末
        start = pd.Timestamp(start_date)
        end = pd.Timestamp(end_date)
        all_dates = pd.date_range(start, end, freq="B")
        return [d.strftime("%Y%m%d") for d in all_dates]


# ============================================================
# S2 指标精确计算 (从5分钟线)
# ============================================================

def compute_s2_metrics(df_5min: pd.DataFrame) -> dict:
    """从5分钟K线DataFrame计算S2尾盘异动指标

    Args:
        df_5min: 单日5分钟K线，列为 [time, open, close, high, low, ..., volume, turnover, ...]

    Returns:
        dict with: late_volume_ratio, late_price_change, last_5min_vol_pct,
                   broke_high, intraday_high, price_at_1430, morning_vol, afternoon_vol,
                   total_vol, late_volume
    """
    if df_5min is None or df_5min.empty:
        return _empty_s2_metrics()

    # 列名标准化 — 查找时间列 (baostock第1列是date, 第2列是time; akshare第1列是time)
    if "time" in df_5min.columns:
        t_col = "time"
    else:
        t_col = df_5min.columns[0]
    close_col = "close" if "close" in df_5min.columns else df_5min.columns[3]
    high_col = "high" if "high" in df_5min.columns else df_5min.columns[4]
    low_col = "low" if "low" in df_5min.columns else df_5min.columns[5]
    vol_col = "volume" if "volume" in df_5min.columns else df_5min.columns[7]
    turnover_col = "turnover" if "turnover" in df_5min.columns else df_5min.columns[8]

    df = df_5min.copy()
    df["_t"] = pd.to_datetime(df[t_col])

    # 上午 (9:30-11:30) vs 下午 (13:00-15:00)
    minute_of_day = df["_t"].dt.hour * 60 + df["_t"].dt.minute
    morning_mask = minute_of_day < 12 * 60
    afternoon_mask = minute_of_day >= 13 * 60
    late_mask = minute_of_day >= 14 * 60 + 30

    morning_vol = df.loc[morning_mask, turnover_col].sum() if morning_mask.any() else 0
    afternoon_vol = df.loc[afternoon_mask, turnover_col].sum() if afternoon_mask.any() else 0
    pre_late_mask = afternoon_mask & ~late_mask  # 13:00-14:30
    pre_late_vol = df.loc[pre_late_mask, turnover_col].sum() if pre_late_mask.any() else 0
    late_vol = df.loc[late_mask, turnover_col].sum() if late_mask.any() else 0
    total_vol = df[turnover_col].sum()

    # 尾盘量比: 最新尾盘bar / 尾盘前每bar成交额 — 与实盘口径一致
    late_bars = late_mask.sum()
    pre_late_bars = max(pre_late_mask.sum(), 1)
    pre_late_rate = pre_late_vol / pre_late_bars
    if late_bars >= 1:
        latest_late_bar_vol = df.loc[late_mask].iloc[-1][turnover_col]
    else:
        latest_late_bar_vol = 0
    late_volume_ratio = latest_late_bar_vol / max(pre_late_rate, 1.0)

    # 14:30 价格
    bar_1430 = df[minute_of_day == 14 * 60 + 30]
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
    pre_1430 = df[minute_of_day < 14 * 60 + 30]
    after_1430 = df[minute_of_day >= 14 * 60 + 30]
    pre_1430_high = pre_1430[high_col].max() if not pre_1430.empty else df[high_col].max()
    after_1430_high = after_1430[high_col].max() if not after_1430.empty else 0
    intraday_high = max(pre_1430_high, after_1430_high)
    broke_high = bool(after_1430_high > pre_1430_high * 0.99) if after_1430_high > 0 else False

    return {
        "late_volume_ratio": round(late_volume_ratio, 4),
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


def _code_to_baostock(code: str) -> str:
    """将纯数字代码转为 baostock 格式: 000001 → sz.000001, 600001 → sh.600001"""
    c = str(code).zfill(6)
    if c.startswith(("6", "9")):
        return f"sh.{c}"
    elif c.startswith(("4", "8")):
        return f"bj.{c}"
    else:
        return f"sz.{c}"


def _empty_s2_metrics() -> dict:
    return {
        "late_volume_ratio": 0.0,
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


def _is_trading_session() -> bool:
    """判断当前是否处于A股交易时段 (工作日 9:30-15:00)"""
    from datetime import datetime
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.hour * 100 + now.minute
    return 930 <= t <= 1500
