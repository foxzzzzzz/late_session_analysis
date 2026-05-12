"""efinance 数据源 — 主数据源，全市场实时快照"""
import logging
import pandas as pd
from data_provider.base import BaseFetcher, RealtimeQuote

logger = logging.getLogger(__name__)


class EfinanceFetcher(BaseFetcher):
    """东方财富数据源 (efinance)，优先级最高"""

    @property
    def name(self) -> str:
        return "efinance"

    @property
    def priority(self) -> int:
        return 0

    def is_available(self) -> bool:
        try:
            import efinance as ef
            return hasattr(ef, 'stock')
        except ImportError:
            return False

    def fetch_snapshot(self) -> list[RealtimeQuote]:
        import efinance as ef
        df = ef.stock.get_realtime_quotes()
        if df is None or df.empty:
            raise ValueError("efinance 返回空数据")
        return self._parse_dataframe(df)

    def fetch_depth(self, codes: list[str]) -> dict[str, dict]:
        """efinance 不提供盘口深度"""
        return {}

    def _parse_dataframe(self, df: pd.DataFrame) -> list[RealtimeQuote]:
        results = []
        col_map = self._get_column_mapping(df)

        for _, row in df.iterrows():
            try:
                code = str(row.get(col_map.get('code', '代码'), ''))
                if not code:
                    continue

                price = self._safe_float(row.get(col_map.get('price', '最新价'), 0))
                pre_close = self._safe_float(row.get(col_map.get('pre_close', '昨收'), 0))
                change_pct = self._safe_float(row.get(col_map.get('change_pct', '涨跌幅'), 0))

                quote = RealtimeQuote(
                    code=code,
                    name=str(row.get(col_map.get('name', '名称'), '')),
                    price=price,
                    change_pct=change_pct,
                    turnover=self._safe_float(row.get(col_map.get('turnover', '成交额'), 0)),
                    turnover_rate=self._safe_float(row.get(col_map.get('turnover_rate', '换手率'), 0)),
                    volume=self._safe_float(row.get(col_map.get('volume', '成交量'), 0)),
                    high=self._safe_float(row.get(col_map.get('high', '最高'), 0)),
                    low=self._safe_float(row.get(col_map.get('low', '最低'), 0)),
                    open=self._safe_float(row.get(col_map.get('open', '今开'), 0)) or price,
                    pre_close=pre_close,
                    limit_up=round(pre_close * 1.1, 2) if pre_close > 0 else 0,
                    limit_down=round(pre_close * 0.9, 2) if pre_close > 0 else 0,
                )
                # 标记ST
                quote.is_st = 'ST' in quote.name or '*ST' in quote.name
                results.append(quote)
            except Exception as e:
                logger.debug(f"解析股票数据失败: {e}")
                continue

        logger.info(f"efinance 拉取 {len(results)} 只股票")
        return results

    @staticmethod
    def _get_column_mapping(df: pd.DataFrame) -> dict:
        """自动检测列名映射 (中英文兼容)"""
        cols = set(df.columns)
        mapping = {}

        candidates = {
            '代码': ['代码', 'code', 'symbol'],
            '名称': ['名称', 'name', 'stock_name'],
            '最新价': ['最新价', 'price', '最新价格'],
            '涨跌幅': ['涨跌幅', 'change_pct', 'change_percent', '涨跌%'],
            '成交额': ['成交额', 'turnover', '成交金额'],
            '换手率': ['换手率', 'turnover_rate', '换手%'],
            '成交量': ['成交量', 'volume', 'vol'],
            '最高': ['最高', 'high'],
            '最低': ['最低', 'low'],
            '今开': ['今开', 'open'],
            '昨收': ['昨收', 'pre_close', '昨日收盘'],
        }

        for key, candidates_list in candidates.items():
            for c in candidates_list:
                if c in cols:
                    mapping[key] = c
                    break

        return mapping

    @staticmethod
    def _safe_float(val) -> float:
        try:
            if val is None or val == '' or val == '-':
                return 0.0
            return float(val)
        except (ValueError, TypeError):
            return 0.0
