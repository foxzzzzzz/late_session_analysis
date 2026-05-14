"""百度股市通资金流向 — 分钟级主力/散户/超大单/大单净流入"""
import time
import logging
import requests
from datetime import datetime

logger = logging.getLogger(__name__)

_BAIDU_PAE_HEADERS = {
    "Host": "finance.pae.baidu.com",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/117.0.0.0",
    "Accept": "application/vnd.finance-web.v1+json",
    "Origin": "https://gushitong.baidu.com",
    "Referer": "https://gushitong.baidu.com/",
}


class BaiduFlowFetcher:
    """百度股市通个股资金流向 — 分钟级实时数据"""

    def __init__(self, timeout: float = 10.0):
        self.timeout = timeout

    def fetch_capital_flow(self, code: str) -> dict:
        """获取单只股票当日分钟级资金流向

        返回: {mainForce: 主力净流入(万), retail: 散户净流入(万),
               super: 超大单净流入(万), large: 大单净流入(万),
               active_buy_ratio: 主动买入占比(%)}
        """
        date_str = datetime.now().strftime("%Y%m%d")
        url = (
            f"https://finance.pae.baidu.com/vapi/v1/fundflow"
            f"?code={code}&market=ab&date={date_str}"
            f"&finClientType=pc"
        )
        try:
            r = requests.get(url, headers=_BAIDU_PAE_HEADERS, timeout=self.timeout)
            d = r.json()
            if str(d.get("ResultCode", -1)) != "0":
                return {}

            raw = d.get("Result", {}).get("update_data", "")
            if not raw:
                return {}

            result = {"mainForce": 0.0, "retail": 0.0, "super": 0.0, "large": 0.0}
            total_main = 0.0
            total_all = 0.0

            for segment in raw.split(";"):
                parts = segment.split(",")
                if len(parts) >= 9:
                    main = _sf(parts[2])
                    retail = _sf(parts[3])
                    super_ = _sf(parts[4])
                    large_ = _sf(parts[5])
                    result["mainForce"] += main
                    result["retail"] += retail
                    result["super"] += super_
                    result["large"] += large_
                    total_main += main
                    total_all += abs(main) + abs(retail)

            # 主动买入占比 = 主力净流入 / 总成交额(用资金流绝对值近似)
            if total_all > 0 and total_main > 0:
                result["active_buy_ratio"] = round(total_main / total_all * 100, 1)
            else:
                result["active_buy_ratio"] = 50.0 if total_main >= 0 else 40.0

            return result
        except Exception as e:
            logger.debug(f"百度资金流向 [{code}] 失败: {e}")
            return {}

    def enrich_batch(self, codes: list[str], delay: float = 0.15) -> dict[str, dict]:
        """批量获取资金流向数据

        Returns: {code: {mainForce, retail, super, large, active_buy_ratio}}
        """
        results: dict[str, dict] = {}
        total = len(codes)
        for i, code in enumerate(codes):
            if i > 0:
                time.sleep(delay)
            flow = self.fetch_capital_flow(code)
            if flow:
                results[code] = flow
            if (i + 1) % 50 == 0 or (i + 1) == total:
                logger.info(f"百度资金流向: {i + 1}/{total}, 有效 {len(results)}")
        return results


def _sf(val) -> float:
    try:
        if val is None or val == '' or val == '-':
            return 0.0
        return float(val)
    except (ValueError, TypeError):
        return 0.0
