"""新浪财经个股资金流向 — 免费零认证，JSON格式，独立于东方财富

替代交易时段不可达的东方财富API和已下线的腾讯ff_ API。

API: vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow.ssi_ssfx_flzjtj
参数: daima=sh600519 (市场前缀+代码)

返回JSON字段:
  r0_in/r0_out:  主力(超大单+大单) 流入/流出 (元)
  r1_in/r1_out:  中单 流入/流出 (元)
  r2_in/r2_out:  小单 流入/流出 (元)
  r3_in/r3_out:  散单 流入/流出 (元)
  r0x_ratio:     主力净方向指标 (±95%，非主动买入比)
  netamount:     净流入总额 (元)
  name/trade/changeratio/volume/turnover/curr_capital

映射到本项目:
  mainForce = (r0_in - r0_out) / 10000  (万元)
  mid = (r1_in - r1_out) / 10000
  retail = (r2_in - r2_out + r3_in - r3_out) / 10000
  super = 0  (新浪不拆分超大单/大单)
  large = 0
  active_buy_ratio = r0_in / (r0_in + r0_out) * 100  (主力主动买入占比)
"""
import json
import logging
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

_SINA_API = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/"
    "json_v2.php/MoneyFlow.ssi_ssfx_flzjtj?daima={market}{code}"
)


class SinaFundFlowFetcher:
    """新浪财经资金流向获取器 — 单股JSON API，分批并发拉取

    Sina API 高频请求会触发 HTTP 456 封禁 (~60s)。
    使用分批延迟 + 降并发 + 456检测来规避。
    """

    def __init__(self, timeout: float = 8.0, max_workers: int = 4):
        self.timeout = timeout
        self.max_workers = max_workers
        self._batch_delay = 0.5  # 批次间延迟(秒), 避免触发456
        self._blocked_until: float = 0.0  # 456封禁到期时间戳

    def enrich_batch(self, codes: list[str]) -> dict[str, dict]:
        """批量获取资金流向，分批并发拉取

        Returns:
            {code: {mainForce(万元), retail(万元), mid(万元), large(0), super(0),
                    active_buy_ratio(%), data_date}}
        """
        if not codes:
            return {}

        unique_codes = list(dict.fromkeys([str(c).zfill(6) for c in codes]))
        results: dict[str, dict] = {}

        batch_size = self.max_workers
        for i in range(0, len(unique_codes), batch_size):
            batch = unique_codes[i:i + batch_size]

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
                        logger.debug(f"新浪资金流 [{code}] 异常: {e}")

            # 批次间延迟 + 检查是否被456封禁
            remaining = i + batch_size
            if remaining < len(unique_codes):
                # 如果前一批全部失败，检查是否触发了456封禁
                batch_codes = set(batch)
                batch_success = sum(1 for c in batch_codes if c in results)
                if batch_success == 0 and batch:
                    logger.warning(
                        f"新浪资金流: 连续批次全部失败 ({i+1}-{min(remaining, len(unique_codes))}), "
                        f"可能已触发HTTP 456封禁，跳过后面的请求"
                    )
                    break
                time.sleep(self._batch_delay)

        today_str = datetime.now().strftime("%Y-%m-%d")
        today_count = sum(
            1 for d in results.values()
            if d.get("data_date", "") == today_str
        )
        parts = [f"{len(results)}/{len(unique_codes)}"]
        if today_count:
            parts.append(f"当日 {today_count}")
        if len(results) == 0 and len(unique_codes) > 0:
            parts.append("(全失败, 可能IP封禁)")
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
        except urllib.error.HTTPError as e:
            if e.code == 456:
                logger.warning(
                    f"新浪资金流 HTTP 456 — IP已被Sina临时封禁，"
                    f"后续请求将跳过 (~60s后恢复)"
                )
            else:
                logger.debug(f"新浪资金流 [{code}] HTTP {e.code}: {e.reason}")
            return None
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
            # 主动买入比 = 主力主动买入 / (主力主动买入 + 主力主动卖出)
            # r0x_ratio 是主力净方向指标(±95%)，不是主动买入比，不能直接用
            r0_total = r0_in + r0_out
            active_buy = (r0_in / r0_total * 100) if r0_total > 0 else 50.0

            return {
                "mainForce": main_force,
                "retail": retail,
                "mid": mid,
                "large": 0.0,   # 新浪不拆分超大单/大单
                "super": 0.0,
                "active_buy_ratio": active_buy,
                "r0_in": r0_in,
                "r0_out": r0_out,
                "netamount": _sf(raw.get("netamount")) / 10000.0,  # 净流入总额(万元)
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
