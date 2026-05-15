"""东方财富个股资金流向 — 实时主力/散户/超大单/大单净流入

替代已失效的百度股市通API。

双通道策略:
  1. push2his 日K线资金流 → 今日盘中实时数据
  2. push2 单股API f178 → 昨日主力净流入 (push2his 被限流时降级)

数据字段 (返回 dict):
  - mainForce (万元), retail (万元), super (万元), large (万元)
  - active_buy_ratio (%)
  - data_date: "2026-05-14" 表示今日数据, "2026-05-13" 表示昨日降级数据
"""
import time
import json
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

# push2 单股API — 兜底用，f178含近5日主力净流入(昨日为主)
_PUSH2_API = (
    "https://push2.eastmoney.com/api/qt/stock/get"
    "?secid={secid}&fields=f178,f184"
)

_RETRY_DELAYS = [1.0, 3.0, 8.0]  # 更温和的重试间隔，避免触发封禁


class EastmoneyFlowFetcher:
    """东方财富个股资金流向获取器 (双通道: push2his → push2降级)"""

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
        """飞行前检查 — 用贵州茅台测试双通道可达性

        Returns:
            {"push2his": bool, "push2": bool, "ok": bool}
        """
        secid = self._to_secid("600519")
        his_ok = self._try_push2his(secid, "600519") is not None
        fallback_ok = self._try_push2_fallback(secid, "600519") is not None
        return {"push2his": his_ok, "push2": fallback_ok, "ok": his_ok or fallback_ok}

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
        """获取单只股票资金流向 — push2his 优先，失败降级到 push2"""
        secid = self._to_secid(code)

        # 通道1: push2his (今日盘中数据)
        result = self._try_push2his(secid, code)
        if result:
            return result

        # 通道2: push2 单股API (昨日数据兜底)
        result = self._try_push2_fallback(secid, code)
        if result:
            return result

        return None

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
            f"东方财富 push2his [{code}] 不可达, 降级到 push2: {last_error}"
        )
        return None

    def _try_push2_fallback(self, secid: str, code: str) -> Optional[dict]:
        """push2 单股API — 昨日主力净流入兜底"""
        url = _PUSH2_API.format(secid=secid)
        last_error = None

        for attempt, backoff in enumerate(_RETRY_DELAYS):
            try:
                r = self._session.get(url, timeout=self.timeout)
                d = r.json()
                data = d.get("data")
                if not isinstance(data, dict):
                    return None

                f178_raw = data.get("f178")
                if isinstance(f178_raw, str):
                    f178 = json.loads(f178_raw)
                elif isinstance(f178_raw, list):
                    f178 = f178_raw
                else:
                    return None

                if not f178:
                    return None

                yesterday = f178[0]
                date_str = yesterday.get("date", "")
                main_amt = _safe_float(yesterday.get("mainNetAmt"))

                # f184: 实时主力净占比 (可用作 active_buy_ratio 近似)
                active_ratio = _safe_float(data.get("f184"))

                return {
                    "mainForce": main_amt / 10000.0,
                    "retail": 0.0,       # push2单股无散户/超大/大单拆分
                    "mid": 0.0,
                    "large": 0.0,
                    "super": 0.0,
                    "active_buy_ratio": active_ratio,
                    "data_date": date_str,
                }
            except Exception as e:
                last_error = e
                if attempt < len(_RETRY_DELAYS) - 1:
                    time.sleep(backoff + random.uniform(0, 1.0))

        logger.debug(
            f"东方财富 push2降级 [{code}] 也失败: {last_error}"
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
