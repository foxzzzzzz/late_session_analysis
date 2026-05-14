"""新浪财经数据源 — 不依赖 eastmoney 的独立数据源

使用 ak.stock_zh_a_spot() 拉取全市场实时行情，
数据来源 vip.stock.finance.sina.com.cn，24/7 可用。
"""
import time
import logging
import pandas as pd
from data_provider.base import BaseFetcher, RealtimeQuote

logger = logging.getLogger(__name__)


class SinaFetcher(BaseFetcher):
    """新浪财经数据源 — eastmoney 不可用时的独立备选"""

    @property
    def name(self) -> str:
        return "sina"

    @property
    def priority(self) -> int:
        return 1

    def is_available(self) -> bool:
        try:
            import akshare as ak
            return hasattr(ak, 'stock_zh_a_spot')
        except ImportError:
            return False

    def fetch_snapshot(self) -> list[RealtimeQuote]:
        import akshare as ak
        t0 = time.time()
        try:
            df = ak.stock_zh_a_spot()
        except Exception as e:
            elapsed = time.time() - t0
            logger.error(f"Sina API调用失败 ({elapsed:.1f}s): {type(e).__name__}: {e}")
            raise

        elapsed = time.time() - t0
        if df is None:
            logger.error(f"Sina 返回 None ({elapsed:.1f}s)")
            raise ValueError("Sina 返回空数据")
        if df.empty:
            logger.error(f"Sina 返回空DataFrame ({elapsed:.1f}s)")
            raise ValueError("Sina 返回空数据")

        logger.info(f"Sina API返回: {len(df)}行, {len(df.columns)}列 ({elapsed:.1f}s)")
        logger.info(f"Sina 列名: {list(df.columns)}")
        if len(df) > 0:
            logger.info(f"Sina 首行样例: {df.iloc[0].to_dict()}")

        return self._parse_dataframe(df)

    def fetch_depth(self, codes: list[str]) -> dict[str, dict]:
        return {}

    def _parse_dataframe(self, df: pd.DataFrame) -> list[RealtimeQuote]:
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
                )
                quote.is_st = 'ST' in name or '*ST' in name
                results.append(quote)
            except Exception as e:
                logger.debug(f"解析股票数据失败: {e}")
                continue

        logger.info(f"Sina 拉取 {len(results)} 只股票")
        return results

    @staticmethod
    def _get_column_mapping(df: pd.DataFrame) -> dict:
        cols = set(df.columns)
        mapping = {}

        candidates = {
            'code': ['代码', 'code', 'symbol'],
            'name': ['名称', 'name'],
            'price': ['最新价', 'price', 'trade'],
            'change_pct': ['涨跌幅', 'change_pct', 'changepercent'],
            'turnover': ['成交额', 'turnover', 'amount'],
            'turnover_rate': ['换手率', 'turnover_rate', 'turnoverratio'],
            'volume': ['成交量', 'volume'],
            'high': ['最高', 'high'],
            'low': ['最低', 'low'],
            'open': ['今开', 'open'],
            'pre_close': ['昨收', 'pre_close', 'settlement'],
        }

        for key, candidates_list in candidates.items():
            for c in candidates_list:
                if c in cols:
                    mapping[key] = c
                    break

        return mapping


def _sf(val) -> float:
    try:
        if val is None or val == '' or val == '-':
            return 0.0
        return float(val)
    except (ValueError, TypeError):
        return 0.0
