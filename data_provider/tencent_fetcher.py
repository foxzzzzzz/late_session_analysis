"""腾讯财经数据源 — PE/PB/市值/换手率/涨跌停，不封IP"""
import time
import logging
import urllib.request
import os
from dataclasses import replace
from data_provider.base import BaseFetcher, RealtimeQuote

logger = logging.getLogger(__name__)


class TencentFetcher(BaseFetcher):
    """腾讯财经行情 (qt.gtimg.cn)，HTTP GET，GBK编码，~分隔88字段"""

    def __init__(self):
        self.fetch_codes_ttl_seconds = float(
            os.getenv("TENCENT_FETCH_CODES_TTL_SECONDS", "3")
        )
        self._fetch_codes_cache: dict[tuple[str, ...], tuple[float, list[RealtimeQuote]]] = {}

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
                    turnover=_parse_tencent_amount(vals),  # 精确成交额(元), 字段35优先→37兜底
                    turnover_rate=_sf(vals[38]),
                    volume=_normalize_tencent_volume(vals) or 0,  # 交叉校验归一化为股
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
        cache_key = tuple(sorted(all_codes))

        if self.fetch_codes_ttl_seconds > 0:
            cached = self._fetch_codes_cache.get(cache_key)
            if cached:
                cached_at, cached_quotes = cached
                if t0 - cached_at <= self.fetch_codes_ttl_seconds:
                    return [replace(q) for q in cached_quotes]

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
        quotes = list(seen.values())
        if self.fetch_codes_ttl_seconds > 0:
            self._fetch_codes_cache[cache_key] = (time.time(), [replace(q) for q in quotes])
        return quotes

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


def _si(val) -> int | None:
    """Safe int — returns None on empty/invalid, for volume normalization."""
    try:
        if val is None or val == '' or val == '-':
            return None
        return int(val)
    except (ValueError, TypeError):
        return None


def _normalize_tencent_volume(vals: list[str]) -> int | None:
    """
    将腾讯实时行情成交量归一化为股。

    腾讯字段6的返回内容说明和实际返回不完全一致。
    优先用换手率/价格/流通市值交叉校验，在原值和手转股值中选择更准确的一方。
    若无法交叉校验，则兜底为旧的手转股逻辑(*100)。
    """
    if len(vals) <= 6 or not vals[6]:
        return None

    raw_volume = _si(vals[6])
    if raw_volume is None:
        return None

    price = _sf(vals[3]) if len(vals) > 3 else 0
    turnover_rate = _sf(vals[38]) if len(vals) > 38 else 0
    circ_mv_yi = _sf(vals[44]) if len(vals) > 44 and vals[44] else None
    circ_mv = circ_mv_yi * 100_000_000 if circ_mv_yi is not None else None

    if price > 0 and turnover_rate > 0 and circ_mv and circ_mv > 0:
        expected_volume = (circ_mv / price) * (turnover_rate / 100)
        if expected_volume > 0:
            raw_delta = abs(raw_volume - expected_volume)
            hand_to_share = raw_volume * 100
            hand_delta = abs(hand_to_share - expected_volume)
            return raw_volume if raw_delta <= hand_delta else hand_to_share

    return raw_volume * 100


def _parse_tencent_amount(vals: list[str]) -> float:
    """
    解析腾讯实时行情成交额，返回单位为元。

    字段35包含更精确的"价格/成交量/成交额"三元组。
    字段37是旧的"万元"口径兜底字段。
    """
    if len(vals) > 35 and vals[35]:
        parts = vals[35].split("/")
        if len(parts) >= 3:
            precise = _sf(parts[2])
            if precise > 0:
                return precise

    amount_wan = _sf(vals[37]) if len(vals) > 37 and vals[37] else 0
    return amount_wan * 10000 if amount_wan > 0 else 0
