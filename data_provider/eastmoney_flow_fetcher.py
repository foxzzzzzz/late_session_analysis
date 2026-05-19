"""东方财富个股资金流向 — 实时主力/散户/超大单/大单净流入

双通道策略 (仅保留当日数据):
  1. push2his 日K线资金流 → 今日盘中实时数据
  2. (已删除 push2 降级 — 只返回昨日数据，对尾盘决策无价值)

数据字段 (返回 dict):
  - mainForce (万元), retail (万元), super (万元), large (万元)
  - active_buy_ratio (%)
  - data_date: 当日日期
"""
import time
import random
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

_RETRY_DELAYS = [0.5, 2.0]  # 快速失败，交易时段push2his大概率不可达


class EastmoneyFlowFetcher:
    """东方财富个股资金流向获取器 (仅 push2his 当日数据)"""

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

    def health_check(self) -> dict:
        """飞行前检查 — 用贵州茅台测试 push2his 可达性

        Returns:
            {"push2his": bool, "ok": bool}
        """
        secid = self._to_secid("600519")
        his_ok = self._try_push2his(secid, "600519") is not None
        return {"push2his": his_ok, "ok": his_ok}

    def enrich_batch(
        self,
        codes: list[str],
        delay: float = 0.4,
    ) -> dict[str, dict]:
        """批量获取资金流向 — 并发拉取个股资金流

        Returns:
            {code: {mainForce, retail, super, large, active_buy_ratio, data_date}}
            金额单位为万元（与百度原接口保持一致）
        """
        if not codes:
            return {}

        # 飞行前检查: 双通道都不可达则立即返回，避免浪费 8+ 分钟
        health = self.health_check()
        if not health["ok"]:
            logger.warning(
                "东方财富资金流向双通道均不可达 (push2his/push2 IP可能被封禁)，跳过富化"
            )
            return {}

        results: dict[str, dict] = {}
        total = len(codes)
        batch_size = self.max_workers

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

            if i + batch_size < total:
                time.sleep(delay)

            done = i + len(batch)
            if done % 50 <= len(batch) and done <= total:
                today_count = sum(
                    1 for d in results.values()
                    if d.get("data_date", "") != _yesterday_str()
                )
                yesterday_count = len(results) - today_count
                parts = [f"{min(done, total)}/{total}, 有效 {len(results)}"]
                if today_count:
                    parts.append(f"今日 {today_count}")
                if yesterday_count:
                    parts.append(f"昨日 {yesterday_count}")
                logger.info(f"东方财富资金流向: {', '.join(parts)}")

        today_count = sum(
            1 for d in results.values()
            if d.get("data_date", "") != _yesterday_str()
        )
        yesterday_count = len(results) - today_count
        summary_parts = [f"{len(results)}/{total}"]
        if today_count:
            summary_parts.append(f"今日 {today_count}")
        if yesterday_count:
            summary_parts.append(f"昨日(降级) {yesterday_count}")
        if not results:
            summary_parts.append("全部失败")
        logger.info(f"东方财富资金流向: {', '.join(summary_parts)}")
        return results

    def _fetch_one(self, code: str) -> Optional[dict]:
        """获取单只股票资金流向 — 仅 push2his (当日数据)"""
        secid = self._to_secid(code)
        return self._try_push2his(secid, code)

    def _try_push2his(self, secid: str, code: str) -> Optional[dict]:
        """push2his 日K线资金流 — 今日数据"""
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
                    "data_date": parts[0],  # 日期在kline第一列
                }
            except Exception as e:
                last_error = e
                if attempt < len(_RETRY_DELAYS) - 1:
                    time.sleep(backoff + random.uniform(0, 1.0))

        logger.debug(
            f"东方财富 push2his [{code}] 不可达: {last_error}"
        )
        return None

def _yesterday_str() -> str:
    """返回昨日日期字符串，用于日志判断"""
    from datetime import datetime, timedelta
    return (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")


def _safe_float(val) -> float:
    try:
        if val is None or val == "" or val == "-":
            return 0.0
        return float(val)
    except (ValueError, TypeError):
        return 0.0
