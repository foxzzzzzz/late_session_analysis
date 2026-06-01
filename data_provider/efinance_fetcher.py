"""efinance 数据源 — 主数据源，全市场实时快照"""
import time
import logging
import pandas as pd
from data_provider.base import BaseFetcher, RealtimeQuote
from data_provider.circuit_breaker import CircuitBreaker, CircuitOpenError

logger = logging.getLogger(__name__)


class EfinanceFetcher(BaseFetcher):
    """东方财富数据源 (efinance)，优先级最高"""

    def __init__(self):
        self._breaker = CircuitBreaker("efinance", failure_threshold=3, cooldown_sec=300)

    @property
    def name(self) -> str:
        return "efinance"

    @property
    def priority(self) -> int:
        return 2

    def is_available(self) -> bool:
        if self._breaker.is_open:
            return False
        try:
            import efinance as ef
            return hasattr(ef, 'stock')
        except ImportError:
            return False

    def fetch_snapshot(self) -> list[RealtimeQuote]:
        if self._breaker.is_open:
            raise CircuitOpenError(f"[efinance] 熔断中，跳过")

        import efinance as ef
        t0 = time.time()
        try:
            df = self._breaker.call(ef.stock.get_realtime_quotes)
        except CircuitOpenError:
            raise
        except Exception as e:
            elapsed = time.time() - t0
            logger.error(f"efinance API调用失败 ({elapsed:.1f}s): {type(e).__name__}: {e}")
            raise
        elapsed = time.time() - t0
        if df is None:
            logger.error(f"efinance 返回 None ({elapsed:.1f}s) — API可能不可用")
            raise ValueError("efinance 返回空数据 (None)")
        if df.empty:
            logger.error(f"efinance 返回空DataFrame ({elapsed:.1f}s) — 可能非交易时段")
            raise ValueError("efinance 返回空数据 (空DataFrame)")
        logger.info(f"efinance API返回: {len(df)}行, {len(df.columns)}列 ({elapsed:.1f}s)")
        logger.info(f"efinance 列名: {list(df.columns)[:12]}")
        logger.info(f"efinance 首行样例: {df.iloc[0].to_dict()}")
        results = self._parse_dataframe(df)
        if not results:
            col_map = self._get_column_mapping(df)
            # 诊断: 检查有多少行的code为空
            code_col = col_map.get('code', '代码')
            empty_code_count = sum(1 for _, row in df.iterrows()
                                   if not str(row.get(code_col, '')).strip())
            logger.error(
                f"efinance 解析失败: {len(df)}行DataFrame → 0只股票. "
                f"code列为空的行数: {empty_code_count}/{len(df)}. "
                f"列映射: {col_map}"
            )
            raise ValueError(
                f"efinance 解析失败: DataFrame有{len(df)}行但无法提取有效数据, "
                f"可能原因: 非交易时段(9:30-15:00)/API格式变更/IP被限 "
                f"(检测到列: {list(df.columns)[:8]})"
            )
        return results

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
            '代码': ['代码', 'code', 'symbol', '股票代码'],
            '名称': ['名称', 'name', 'stock_name', '股票名称'],
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
