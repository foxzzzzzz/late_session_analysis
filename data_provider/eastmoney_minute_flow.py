"""东方财富分钟级资金流向 — 盘中实时主力/散户/超大单/大单净流入

push2 fflow/kline klt=1 是唯一已知的盘中实时资金流数据源，分钟级粒度。
与 push2his 日线(klt=101)不同，分钟线在交易时段返回今日实时数据。

API: push2.eastmoney.com/api/qt/stock/fflow/kline/get
参数: secid={market}.{code}, klt=1 (1分钟), lmt=1 (最新1条),
      fields1=f1,f2,f3,f7, fields2=f51..f61 (必须显式指定，否则返回 rc=102)

返回字段 (逗号分隔): 时间戳, 主力净额(f52), 散户净额(f53), 中单净额(f54),
                    大单净额(f55), 超大单净额(f56)
字段映射: parts[4]=large(f55), parts[5]=super(f56) — 与 push2his 日线一致
注意: 无 active_buy_ratio 字段 (f57 分钟线返回空，需从新浪获取)

东财请求统一走 rate_limiter.em_urlopen() 门控 (push2 需 urllib，QPS≤1)。
"""
import json
import logging
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Optional

from data_provider.rate_limiter import em_urlopen
from data_provider.circuit_breaker import CircuitBreaker, CircuitOpenError

logger = logging.getLogger(__name__)

_PUSH2_FFLOW_API = (
    "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get"
    "?secid={secid}&klt=1&lmt=5"
    "&fields1=f1,f2,f3,f7"
    "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
)

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/117.0.0.0 Safari/537.36"

_RETRY_DELAYS = [0.5, 1.5, 3.0]  # urllib 重试间隔(秒)


class EastmoneyMinuteFlowFetcher:
    """东方财富分钟级资金流向获取器 — push2 fflow/kline klt=1"""

    def __init__(self, timeout: float = 8.0, max_workers: int = 4):
        self.timeout = timeout
        self.max_workers = max_workers
        self._last_success: dict[str, dict] = {}
        self._stagger_range = (0.3, 0.6)
        self._breaker = CircuitBreaker("eastmoney_push2", failure_threshold=3, cooldown_sec=300)

    def enrich_batch(self, codes: list[str]) -> dict[str, dict]:
        """批量获取分钟级资金流向

        Returns:
            {code: {mainForce(万元), retail, mid, super, large, data_date}}
            无 active_buy_ratio 字段
        """
        if not codes:
            return {}

        if self._breaker.is_open:
            logger.warning("东方财富 push2 已熔断，跳过分钟资金流富化")
            return {}

        unique_codes = list(dict.fromkeys([str(c).zfill(6) for c in codes]))
        results: dict[str, dict] = {}

        with ThreadPoolExecutor(max_workers=min(self.max_workers, len(unique_codes))) as pool:
            futures = {
                pool.submit(self._fetch_one, code): code
                for code in unique_codes
            }
            for future in as_completed(futures):
                code = futures[future]
                try:
                    data = future.result()
                    if data:
                        results[code] = data
                except Exception as e:
                    logger.debug(f"东财分钟资金流 [{code}] 异常: {e}")

        today_str = datetime.now().strftime("%Y-%m-%d")
        summary = f"{len(results)}/{len(unique_codes)}"
        logger.info(f"东财分钟资金流: {summary}, 日期={today_str}")
        return results

    def _fetch_one(self, code: str) -> Optional[dict]:
        """获取单只股票分钟级资金流"""
        if self._breaker.is_open:
            return None

        time.sleep(random.uniform(*self._stagger_range))

        secid = _to_secid(code)
        url = _PUSH2_FFLOW_API.format(secid=secid)

        last_error = None
        for attempt, backoff in enumerate(_RETRY_DELAYS):
            try:
                headers = {
                    "User-Agent": _UA,
                    "Referer": "https://data.eastmoney.com/",
                }
                resp = self._breaker.call(em_urlopen, url, timeout=self.timeout, headers=headers)
                raw = json.loads(resp.read().decode("utf-8"))
            except CircuitOpenError:
                return None
            except Exception as e:
                last_error = e
                if attempt < len(_RETRY_DELAYS) - 1:
                    time.sleep(backoff + random.uniform(0, 0.5))
                continue

            klines = (raw.get("data") or {}).get("klines") or []
            if not klines:
                return None

            parts = klines[-1].split(",")
            if len(parts) < 6:
                return None

            result = {
                "mainForce": _sf(parts[1]) / 10000.0,
                "retail": _sf(parts[2]) / 10000.0,
                "mid": _sf(parts[3]) / 10000.0,
                "large": _sf(parts[4]) / 10000.0,
                "super": _sf(parts[5]) / 10000.0,
                "data_date": datetime.now().strftime("%Y-%m-%d"),
            }
            self._last_success[code] = result
            return result

        # 全部重试失败 → 返回上次成功缓存
        cached = self._last_success.get(code)
        if cached:
            logger.debug(f"东财分钟资金流 [{code}] 刷新失败，使用缓存 (日期={cached.get('data_date')})")
            return cached

        logger.debug(f"东财分钟资金流 [{code}] 不可达: {last_error}")
        return None

    def health_check(self) -> dict:
        """飞行前检查 — 用贵州茅台测试 push2 fflow API 可达性"""
        try:
            result = self._fetch_one("600519")
            return {"ok": result is not None}
        except Exception:
            return {"ok": False}


def _to_secid(code: str) -> str:
    """股票代码 → 东财 secid"""
    code = str(code).zfill(6)
    if code.startswith("6"):
        return f"1.{code}"
    return f"0.{code}"


def _sf(val) -> float:
    try:
        if val is None or val == "" or val == "-":
            return 0.0
        return float(val)
    except (ValueError, TypeError):
        return 0.0
