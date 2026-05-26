"""腾讯财经数据源 — PE/PB/市值/换手率/涨跌停，不封IP"""
import time
import logging
import urllib.request
from data_provider.base import BaseFetcher, RealtimeQuote

logger = logging.getLogger(__name__)


class TencentFetcher(BaseFetcher):
    """腾讯财经行情 (qt.gtimg.cn)，HTTP GET，GBK编码，~分隔88字段"""

    @property
    def name(self) -> str:
        return "tencent"

    @property
    def priority(self) -> int:
        return 0

    def is_available(self) -> bool:
        return True

    def fetch_snapshot(self) -> list[RealtimeQuote]:
        t0 = time.time()
        try:
            quotes = self._fetch_all()
        except Exception as e:
            elapsed = time.time() - t0
            logger.error(f"腾讯API调用失败 ({elapsed:.1f}s): {type(e).__name__}: {e}")
            raise

        elapsed = time.time() - t0
        if not quotes:
            raise ValueError("腾讯API返回空数据")

        logger.info(f"腾讯API: {len(quotes)}只 ({elapsed:.1f}s)")
        return quotes

    def _fetch_all(self) -> list[RealtimeQuote]:
        """使用新浪源获取股票列表，腾讯API批量拉取行情"""
        import akshare as ak
        import pandas as pd

        # 1. 从新浪源获取全市场股票列表 (24/7可用, 不依赖eastmoney)
        try:
            df = ak.stock_zh_a_spot()
        except Exception as e:
            logger.error(f"获取股票列表失败: {e}")
            raise ValueError(f"无法获取股票列表: {e}")

        if df is None or df.empty:
            raise ValueError("股票列表为空")

        code_col = next((c for c in df.columns if c in ('代码', 'code')), df.columns[0])
        all_codes = [str(c).zfill(6) for c in df[code_col].tolist()]
        all_codes = list(dict.fromkeys(all_codes))  # 去重保序
        logger.info(f"获取 {len(all_codes)} 只股票代码")

        # 2. 分批拉取腾讯行情 (每批50只)
        seen: dict[str, RealtimeQuote] = {}
        batch_size = 50
        total_batches = (len(all_codes) + batch_size - 1) // batch_size

        for i in range(0, len(all_codes), batch_size):
            batch = all_codes[i:i + batch_size]
            batch_quotes = self._fetch_batch(batch)
            for q in batch_quotes:
                if q.code not in seen:
                    seen[q.code] = q
            # 进度日志
            batch_num = i // batch_size + 1
            if batch_num % 20 == 0 or batch_num == total_batches:
                logger.info(f"腾讯API分页: {batch_num}/{total_batches}, 累计 {len(seen)} 只")
            if i + batch_size < len(all_codes):
                time.sleep(0.15)

        return list(seen.values())

    def _fetch_batch(self, codes: list[str]) -> list[RealtimeQuote]:
        prefixed = []
        for raw in codes:
            raw = str(raw).strip()
            # 归一化为纯6位+市场前缀
            pure, prefix = _normalize_code(raw)
            prefixed.append(f"{prefix}{pure}")

        url = "https://qt.gtimg.cn/q=" + ",".join(prefixed)
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "Mozilla/5.0")
        try:
            resp = urllib.request.urlopen(req, timeout=10)
            data = resp.read().decode("gbk")
        except Exception as e:
            logger.warning(f"腾讯API批次拉取失败: {e}")
            return []

        results = []
        for line in data.strip().split(";"):
            if not line.strip() or "=" not in line or '"' not in line:
                continue
            try:
                vals = line.split('"')[1].split("~")
                if len(vals) < 53:
                    continue
                key = line.split("=")[0]
                code = key.replace("v_", "").replace("sh", "").replace("sz", "").replace("bj", "")
                code = code.strip("_").strip()

                price = _sf(vals[3])
                pre_close = _sf(vals[4])
                if price <= 0:
                    continue

                name = vals[1]
                quote = RealtimeQuote(
                    code=code,
                    name=name,
                    price=price,
                    bid_vol=(_sf(vals[10]) + _sf(vals[12]) + _sf(vals[14]) + _sf(vals[16]) + _sf(vals[18])) * 100,  # 买1-5量(手→股)
                    ask_vol=(_sf(vals[20]) + _sf(vals[22]) + _sf(vals[24]) + _sf(vals[26]) + _sf(vals[28])) * 100,  # 卖1-5量(手→股)
                    change_pct=_sf(vals[32]),
                    turnover=_sf(vals[37]) * 10000,  # 万→元
                    turnover_rate=_sf(vals[38]),
                    volume=_sf(vals[6]),
                    high=_sf(vals[33]),
                    low=_sf(vals[34]),
                    open=_sf(vals[5]) or price,
                    pre_close=pre_close,
                    limit_up=_sf(vals[47]),
                    limit_down=_sf(vals[48]),
                    pe_ttm=_sf(vals[39]),
                    pb=_sf(vals[46]),
                    market_cap=_sf(vals[44]),
                    vol_ratio=_sf(vals[49]),
                    amplitude=_sf(vals[43]),
                )
                quote.is_st = 'ST' in name or '*ST' in name
                results.append(quote)
            except Exception:
                continue

        return results

    def fetch_codes(self, codes: list[str]) -> list[RealtimeQuote]:
        """按指定代码列表拉取行情，跳过全市场股票列表获取"""
        t0 = time.time()
        all_codes = [str(c).zfill(6) for c in codes]
        all_codes = list(dict.fromkeys(all_codes))

        seen: dict[str, RealtimeQuote] = {}
        batch_size = 50
        total_batches = (len(all_codes) + batch_size - 1) // batch_size

        for i in range(0, len(all_codes), batch_size):
            batch = all_codes[i:i + batch_size]
            for q in self._fetch_batch(batch):
                if q.code not in seen:
                    seen[q.code] = q
            if i + batch_size < len(all_codes):
                time.sleep(0.15)

        elapsed = time.time() - t0
        logger.info(
            f"腾讯API(fetch_codes): {len(seen)}/{len(codes)}只 ({elapsed:.1f}s)"
        )
        return list(seen.values())

    def fetch_depth(self, codes: list[str]) -> dict[str, dict]:
        return {}


def _normalize_code(raw: str) -> tuple[str, str]:
    """归一化股票代码 → (纯6位, 市场前缀sh/sz/bj)"""
    raw = raw.strip().upper()
    # 检测已有前缀
    known_prefix = ""
    for p in ("SH", "SZ", "BJ"):
        if raw.startswith(p):
            known_prefix = p.lower()
            raw = raw[len(p):]
            break
    pure = raw.zfill(6)
    if known_prefix:
        return pure, known_prefix
    # 无前缀时根据首位推断
    if pure.startswith(("6", "9")):
        return pure, "sh"
    elif pure.startswith("8"):
        return pure, "bj"
    else:
        return pure, "sz"


def _sf(val) -> float:
    try:
        if val is None or val == '' or val == '-':
            return 0.0
        return float(val)
    except (ValueError, TypeError):
        return 0.0
