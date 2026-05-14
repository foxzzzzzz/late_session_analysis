"""东方财富个股资金流向 — 实时主力/散户/超大单/大单净流入

替代已失效的百度股市通API。使用东方财富 push2his 日K线资金流接口，
低并发 + 重试机制避免触发反爬。

数据字段 (push2his kline -> StockContext):
  - f52 (主力净流入, 元) / 10000 -> mainForce (万元)
  - f53 (小单净流入, 元) / 10000 -> retail (万元)
  - f56 (超大单净流入, 元) / 10000 -> super (万元)
  - f55 (大单净流入, 元) / 10000   -> large (万元)
  - f57 (主力净流入占比, %)        -> active_buy_ratio (%)
"""
import time
import logging
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

logger = logging.getLogger(__name__)

_EM_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "Chrome/117.0.0.0 Safari/537.36"
    ),
    "Referer": "https://data.eastmoney.com/",
}

_HIS_API = (
    "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
    "?secid={secid}&fields1=f1,f2,f3,f7"
    "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
    "&lmt=1&klt=101"
)

_RETRY_DELAYS = [0.5, 1.5, 3.0]


class EastmoneyFlowFetcher:
    """东方财富个股资金流向获取器"""

    def __init__(self, timeout: float = 10.0, max_workers: int = 3):
        self.timeout = timeout
        self.max_workers = max_workers
        self._session = requests.Session()
        self._session.headers.update(_EM_HEADERS)

    def _to_secid(self, code: str) -> str:
        if code.startswith("6"):
            return f"1.{code}"
        return f"0.{code}"

    def fetch_capital_flow(self, code: str) -> Optional[dict]:
        result = self.enrich_batch([code])
        return result.get(code)

    def enrich_batch(
        self,
        codes: list[str],
        delay: float = 0.4,
    ) -> dict[str, dict]:
        """批量获取资金流向 — 并发拉取个股日K线资金流

        Returns:
            {code: {mainForce, retail, super, large, active_buy_ratio}}
            金额单位为万元（与百度原接口保持一致）
        """
        if not codes:
            return {}

        results: dict[str, dict] = {}
        total = len(codes)
        batch_size = self.max_workers

        # 分批执行，批次间加延迟避免触发反爬
        for i in range(0, total, batch_size):
            batch = codes[i:i + batch_size]
            with ThreadPoolExecutor(max_workers=len(batch)) as pool:
                futures = {
                    pool.submit(self._fetch_one, code): code
                    for code in batch
                }
                for future in as_completed(futures):
                    code = futures[future]
                    try:
                        data = future.result()
                        if data:
                            results[code] = data
                    except Exception as e:
                        logger.debug(f"东方财富资金流向 [{code}] 异常: {e}")

            # 批次间延迟
            if i + batch_size < total:
                time.sleep(delay)

            # 每50只汇报一次
            done = i + len(batch)
            if done % 50 <= len(batch) and done <= total:
                logger.info(
                    f"东方财富资金流向: {min(done, total)}/{total}, 有效 {len(results)}"
                )

        logger.info(
            f"东方财富资金流向: {len(results)}/{total}"
        )
        return results

    def _fetch_one(self, code: str) -> Optional[dict]:
        secid = self._to_secid(code)
        url = _HIS_API.format(secid=secid)
        last_error = None

        for attempt, backoff in enumerate(_RETRY_DELAYS):
            try:
                r = self._session.get(url, timeout=self.timeout)
                d = r.json()
                klines = (
                    d.get("data", {}).get("klines", [])
                    if d.get("data") else []
                )
                if not klines:
                    return None

                parts = klines[-1].split(",")
                if len(parts) < 6:
                    return None

                return {
                    "mainForce": _safe_float(parts[1]) / 10000.0,
                    "retail": _safe_float(parts[2]) / 10000.0,
                    "mid": _safe_float(parts[3]) / 10000.0,
                    "large": _safe_float(parts[4]) / 10000.0,
                    "super": _safe_float(parts[5]) / 10000.0,
                    "active_buy_ratio": (
                        _safe_float(parts[6])
                        if len(parts) >= 7
                        else 50.0
                    ),
                }
            except Exception as e:
                last_error = e
                if attempt < len(_RETRY_DELAYS) - 1:
                    time.sleep(backoff)

        logger.debug(f"东方财富资金流向 [{code}] 重试耗尽: {last_error}")
        return None


def _safe_float(val) -> float:
    try:
        if val is None or val == "" or val == "-":
            return 0.0
        return float(val)
    except (ValueError, TypeError):
        return 0.0
