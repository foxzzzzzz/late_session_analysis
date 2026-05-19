"""新浪财经个股资金流向 — 免费零认证，JSON格式，独立于东方财富

替代交易时段不可达的东方财富API和已下线的腾讯ff_ API。

API: vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow.ssi_ssfx_flzjtj
参数: daima=sh600519 (市场前缀+代码)

返回JSON字段:
  r0_in/r0_out:  主力(超大单+大单) 流入/流出 (元)
  r1_in/r1_out:  中单 流入/流出 (元)
  r2_in/r2_out:  小单 流入/流出 (元)
  r3_in/r3_out:  散单 流入/流出 (元)
  r0x_ratio:     主力占比 (%)
  netamount:     净流入总额 (元)
  name/trade/changeratio/volume/turnover/curr_capital

映射到本项目:
  mainForce = (r0_in - r0_out) / 10000  (万元)
  mid = (r1_in - r1_out) / 10000
  retail = (r2_in - r2_out + r3_in - r3_out) / 10000
  super = 0  (新浪不拆分超大单/大单)
  large = 0
  active_buy_ratio = r0x_ratio
"""
import json
import logging
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

_SINA_API = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/"
    "json_v2.php/MoneyFlow.ssi_ssfx_flzjtj?daima={market}{code}"
)


class SinaFundFlowFetcher:
    """新浪财经资金流向获取器 — 单股JSON API，并发拉取"""

    def __init__(self, timeout: float = 8.0, max_workers: int = 8):
        self.timeout = timeout
        self.max_workers = max_workers

    def enrich_batch(self, codes: list[str]) -> dict[str, dict]:
        """批量获取资金流向，并发拉取

        Returns:
            {code: {mainForce(万元), retail(万元), mid(万元), large(0), super(0),
                    active_buy_ratio(%), data_date}}
        """
        if not codes:
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
                    logger.debug(f"新浪资金流 [{code}] 异常: {e}")

        today_str = datetime.now().strftime("%Y-%m-%d")
        today_count = sum(
            1 for d in results.values()
            if d.get("data_date", "") == today_str
        )
        parts = [f"{len(results)}/{len(unique_codes)}"]
        if today_count:
            parts.append(f"当日 {today_count}")
        logger.info(f"新浪资金流向: {', '.join(parts)}")
        return results

    def _fetch_one(self, code: str) -> Optional[dict]:
        """获取单只股票资金流向"""
        market = _get_sina_prefix(code)
        url = _SINA_API.format(market=market, code=code)
        try:
            req = urllib.request.Request(url)
            req.add_header("User-Agent", "Mozilla/5.0")
            resp = urllib.request.urlopen(req, timeout=self.timeout)
            raw = json.loads(resp.read().decode("gbk"))
        except Exception as e:
            logger.debug(f"新浪资金流 [{code}] 请求失败: {e}")
            return None

        if not raw or not isinstance(raw, dict):
            return None

        try:
            r0_in = _sf(raw.get("r0_in"))
            r0_out = _sf(raw.get("r0_out"))
            r1_in = _sf(raw.get("r1_in"))
            r1_out = _sf(raw.get("r1_out"))
            r2_in = _sf(raw.get("r2_in"))
            r2_out = _sf(raw.get("r2_out"))
            r3_in = _sf(raw.get("r3_in"))
            r3_out = _sf(raw.get("r3_out"))

            main_force = (r0_in - r0_out) / 10000.0
            mid = (r1_in - r1_out) / 10000.0
            retail = (r2_in - r2_out + r3_in - r3_out) / 10000.0
            active_buy = _sf(raw.get("r0x_ratio"))

            return {
                "mainForce": main_force,
                "retail": retail,
                "mid": mid,
                "large": 0.0,   # 新浪不拆分超大单/大单
                "super": 0.0,
                "active_buy_ratio": active_buy,
                "data_date": datetime.now().strftime("%Y-%m-%d"),
            }
        except Exception as e:
            logger.debug(f"新浪资金流 [{code}] 解析失败: {e}")
            return None

    def health_check(self) -> dict:
        """飞行前检查 — 用贵州茅台测试API可达性"""
        try:
            result = self._fetch_one("600519")
            return {"ok": result is not None}
        except Exception:
            return {"ok": False}


def _get_sina_prefix(code: str) -> str:
    """根据6位代码返回新浪市场前缀 sh/sz"""
    code = str(code).zfill(6)
    if code.startswith(("6", "9")):
        return "sh"
    else:
        return "sz"  # 00x, 30x, 8x (科创在京?)


def _sf(val) -> float:
    try:
        if val is None or val == "" or val == "-":
            return 0.0
        return float(val)
    except (ValueError, TypeError):
        return 0.0
