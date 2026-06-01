"""akshare 数据源 — 备选降级数据源"""
import time
import logging
import pandas as pd
from data_provider.base import BaseFetcher, RealtimeQuote
from data_provider.circuit_breaker import CircuitBreaker, CircuitOpenError

logger = logging.getLogger(__name__)


class AkshareFetcher(BaseFetcher):
    """Akshare 数据源，efinance 不可用时的备选"""

    def __init__(self):
        self._breaker = CircuitBreaker("akshare", failure_threshold=2, cooldown_sec=300)

    @property
    def name(self) -> str:
        return "akshare"

    @property
    def priority(self) -> int:
        return 3

    def is_available(self) -> bool:
        if self._breaker.is_open:
            return False
        try:
            import akshare as ak
            return hasattr(ak, 'stock_zh_a_spot_em')
        except ImportError:
            return False

    def fetch_snapshot(self) -> list[RealtimeQuote]:
        if self._breaker.is_open:
            raise CircuitOpenError(f"[akshare] 熔断中，跳过")

        import akshare as ak
        t0 = time.time()
        try:
            df = self._breaker.call(ak.stock_zh_a_spot_em)
        except CircuitOpenError:
            raise
        except Exception as e:
            elapsed = time.time() - t0
            logger.error(f"akshare API调用失败 ({elapsed:.1f}s): {type(e).__name__}: {e}")
            raise
        elapsed = time.time() - t0
        if df is None:
            logger.error(f"akshare 返回 None ({elapsed:.1f}s)")
            raise ValueError("akshare 返回空数据")
        if df.empty:
            logger.error(f"akshare 返回空DataFrame ({elapsed:.1f}s)")
            raise ValueError("akshare 返回空数据")
        logger.info(f"akshare API返回: {len(df)}行, {len(df.columns)}列 ({elapsed:.1f}s)")
        logger.info(f"akshare 列名: {list(df.columns)[:12]}")
        logger.info(f"akshare 首行样例: {df.iloc[0].to_dict()}")
        return self._parse_dataframe(df)

    def fetch_depth(self, codes: list[str]) -> dict[str, dict]:
        """akshare 可获取盘口数据，但需逐个查询，MVP阶段暂不启用"""
        return {}

    def _parse_dataframe(self, df: pd.DataFrame) -> list[RealtimeQuote]:
        results = []
        col_map = {
            'code': '代码', 'name': '名称', 'price': '最新价',
            'change_pct': '涨跌幅', 'turnover': '成交额',
            'turnover_rate': '换手率', 'volume': '成交量',
            'high': '最高', 'low': '最低', 'open': '今开', 'pre_close': '昨收',
        }

        for _, row in df.iterrows():
            try:
                code = str(row.get(col_map['code'], ''))
                if not code:
                    continue

                price = _sf(row.get(col_map['price'], 0))
                pre_close = _sf(row.get(col_map['pre_close'], 0))
                change_pct = _sf(row.get(col_map['change_pct'], 0))

                quote = RealtimeQuote(
                    code=code,
                    name=str(row.get(col_map['name'], '')),
                    price=price,
                    change_pct=change_pct,
                    turnover=_sf(row.get(col_map['turnover'], 0)),
                    turnover_rate=_sf(row.get(col_map['turnover_rate'], 0)),
                    volume=_sf(row.get(col_map['volume'], 0)),
                    high=_sf(row.get(col_map['high'], 0)),
                    low=_sf(row.get(col_map['low'], 0)),
                    open=_sf(row.get(col_map['open'], 0)) or price,
                    pre_close=pre_close,
                    limit_up=round(pre_close * 1.1, 2) if pre_close > 0 else 0,
                    limit_down=round(pre_close * 0.9, 2) if pre_close > 0 else 0,
                )
                quote.is_st = 'ST' in quote.name or '*ST' in quote.name
                results.append(quote)
            except Exception as e:
                logger.debug(f"解析股票数据失败: {e}")
                continue

        logger.info(f"akshare 拉取 {len(results)} 只股票")
        return results


def _sf(val) -> float:
    try:
        if val is None or val == '' or val == '-':
            return 0.0
        return float(val)
    except (ValueError, TypeError):
        return 0.0
