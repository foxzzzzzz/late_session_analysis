"""板块聚焦数据源 — 按行业板块分别拉取实时行情

替代全市场 get_realtime_quotes() 的 ~30+ 并发分页请求，
改为按目标板块逐板块拉取 (每次 1-2 页)，板块间加随机延时。
"""
import time
import random
import logging
import threading
import pandas as pd
from data_provider.base import BaseFetcher, RealtimeQuote

logger = logging.getLogger(__name__)

DEFAULT_SECTORS = [
    "半导体", "软件开发", "消费电子", "通信设备",
    "光伏设备", "证券", "汽车零部件", "计算机设备",
]


class SectorBasedFetcher(BaseFetcher):
    """板块聚焦数据源 — 最高优先级，避免全市场并发分页"""

    def __init__(self, sectors: list[str] = None,
                 min_sleep: float = 1.5, max_sleep: float = 3.0,
                 circuit_breaker: int = 2):
        self._sectors = sectors or DEFAULT_SECTORS
        self._min_sleep = min_sleep
        self._max_sleep = max_sleep
        self._circuit_breaker = circuit_breaker
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return "sector_based"

    @property
    def priority(self) -> int:
        return 0

    def is_available(self) -> bool:
        try:
            import akshare as ak
            return hasattr(ak, 'stock_board_industry_cons_em')
        except ImportError:
            return False

    def fetch_snapshot(self) -> list[RealtimeQuote]:
        import akshare as ak

        with self._lock:
            all_quotes: dict[str, RealtimeQuote] = {}
            failed_sectors = []

            consecutive_failures = 0

            for i, sector in enumerate(self._sectors):
                if i > 0:
                    self._enforce_rate_limit()

                t0 = time.time()
                try:
                    df = ak.stock_board_industry_cons_em(symbol=sector)
                except Exception as e:
                    elapsed = time.time() - t0
                    logger.warning(
                        f"板块 [{sector}] 拉取失败 ({elapsed:.1f}s): "
                        f"{type(e).__name__}: {e}"
                    )
                    failed_sectors.append(sector)
                    consecutive_failures += 1
                    if consecutive_failures >= self._circuit_breaker:
                        logger.warning(
                            f"连续 {consecutive_failures} 个板块失败，触发熔断，"
                            f"跳过剩余 {len(self._sectors) - i - 1} 个板块"
                        )
                        break
                    continue

                elapsed = time.time() - t0
                if df is None or df.empty:
                    logger.warning(f"板块 [{sector}] 返回空数据 ({elapsed:.1f}s)")
                    failed_sectors.append(sector)
                    consecutive_failures += 1
                    if consecutive_failures >= self._circuit_breaker:
                        logger.warning(
                            f"连续 {consecutive_failures} 个板块返回空，触发熔断"
                        )
                        break
                    continue

                consecutive_failures = 0  # 成功后重置
                quotes = self._parse_dataframe(df, sector)
                for q in quotes:
                    if q.code not in all_quotes:
                        all_quotes[q.code] = q

                logger.info(
                    f"板块 [{sector}]: {len(quotes)}只 ({elapsed:.1f}s)"
                )

            if failed_sectors:
                logger.warning(
                    f"{len(failed_sectors)}/{len(self._sectors)} 板块失败: {failed_sectors}"
                )

            if not all_quotes:
                raise ValueError(f"所有 {len(self._sectors)} 个板块均拉取失败")

            result = list(all_quotes.values())
            logger.info(
                f"板块聚焦: {len(self._sectors)}板块 → "
                f"{len(result)}只股票 (去重后)"
            )
            return result

    def _enforce_rate_limit(self):
        delay = random.uniform(self._min_sleep, self._max_sleep)
        logger.debug(f"Rate limit: sleep {delay:.1f}s")
        time.sleep(delay)

    def _parse_dataframe(self, df: pd.DataFrame, sector: str) -> list[RealtimeQuote]:
        results = []
        col_map = self._get_column_mapping(df)

        for _, row in df.iterrows():
            try:
                code = str(row.get(col_map.get('code', '代码'), ''))
                if not code:
                    continue

                price = _sf(row.get(col_map.get('price', '最新价'), 0))
                pre_close = _sf(row.get(col_map.get('pre_close', '昨收'), 0))
                change_pct = _sf(row.get(col_map.get('change_pct', '涨跌幅'), 0))
                name = str(row.get(col_map.get('name', '名称'), ''))

                quote = RealtimeQuote(
                    code=code,
                    name=name,
                    price=price,
                    change_pct=change_pct,
                    turnover=_sf(row.get(col_map.get('turnover', '成交额'), 0)),
                    turnover_rate=_sf(row.get(col_map.get('turnover_rate', '换手率'), 0)),
                    volume=_sf(row.get(col_map.get('volume', '成交量'), 0)),
                    high=_sf(row.get(col_map.get('high', '最高'), 0)),
                    low=_sf(row.get(col_map.get('low', '最低'), 0)),
                    open=_sf(row.get(col_map.get('open', '今开'), 0)) or price,
                    pre_close=pre_close,
                    limit_up=round(pre_close * 1.1, 2) if pre_close > 0 else 0,
                    limit_down=round(pre_close * 0.9, 2) if pre_close > 0 else 0,
                    sector=sector,
                )
                quote.is_st = 'ST' in name or '*ST' in name
                results.append(quote)
            except Exception as e:
                logger.debug(f"解析股票数据失败: {e}")
                continue

        return results

    @staticmethod
    def _get_column_mapping(df: pd.DataFrame) -> dict:
        cols = set(df.columns)
        mapping = {}

        candidates = {
            'code': ['代码', 'code', 'symbol'],
            'name': ['名称', 'name'],
            'price': ['最新价', 'price'],
            'change_pct': ['涨跌幅', 'change_pct', 'change_percent'],
            'turnover': ['成交额', 'turnover'],
            'turnover_rate': ['换手率', 'turnover_rate'],
            'volume': ['成交量', 'volume', 'vol'],
            'high': ['最高', 'high'],
            'low': ['最低', 'low'],
            'open': ['今开', 'open'],
            'pre_close': ['昨收', 'pre_close'],
        }

        for key, candidates_list in candidates.items():
            for c in candidates_list:
                if c in cols:
                    mapping[key] = c
                    break

        return mapping

    def fetch_depth(self, codes: list[str]) -> dict[str, dict]:
        return {}


def _sf(val) -> float:
    try:
        if val is None or val == '' or val == '-':
            return 0.0
        return float(val)
    except (ValueError, TypeError):
        return 0.0
