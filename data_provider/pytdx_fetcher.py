"""pytdx 数据源 — 通达信协议盘口深度数据 (可选增强)"""
import logging
from data_provider.base import BaseFetcher, RealtimeQuote

logger = logging.getLogger(__name__)


class PytdxFetcher(BaseFetcher):
    """通达信数据源 (pytdx)，提供盘口深度，优先级较低作为增强"""

    @property
    def name(self) -> str:
        return "pytdx"

    @property
    def priority(self) -> int:
        return 2

    def is_available(self) -> bool:
        try:
            from pytdx.hq import TdxHq_API
            return True
        except ImportError:
            return False

    def fetch_snapshot(self) -> list[RealtimeQuote]:
        """pytdx 不擅长全市场快照，返回空让主源处理"""
        return []

    def fetch_depth(self, codes: list[str]) -> dict[str, dict]:
        """拉取盘口5档买卖挂单"""
        if not codes:
            return {}
        try:
            from pytdx.hq import TdxHq_API
            api = TdxHq_API()
            results = {}

            # 连接通达信行情服务器
            connected = False
            for ip, port in self._get_server_list():
                try:
                    if api.connect(ip, port):
                        connected = True
                        break
                except Exception:
                    continue

            if not connected:
                logger.warning("pytdx 连接失败")
                return {}

            # 批量查询盘口
            for code in codes:
                try:
                    market = 0 if code.startswith('6') else 1
                    quote = api.get_security_quotes([(market, code)])
                    if quote and len(quote) > 0:
                        q = quote[0]
                        results[code] = {
                            'bid1': q.get('bid1', 0),
                            'bid2': q.get('bid2', 0),
                            'bid3': q.get('bid3', 0),
                            'bid4': q.get('bid4', 0),
                            'bid5': q.get('bid5', 0),
                            'bid_vol1': q.get('bid_vol1', 0),
                            'bid_vol2': q.get('bid_vol2', 0),
                            'bid_vol3': q.get('bid_vol3', 0),
                            'bid_vol4': q.get('bid_vol4', 0),
                            'bid_vol5': q.get('bid_vol5', 0),
                            'ask1': q.get('ask1', 0),
                            'ask2': q.get('ask2', 0),
                            'ask3': q.get('ask3', 0),
                            'ask4': q.get('ask4', 0),
                            'ask5': q.get('ask5', 0),
                            'ask_vol1': q.get('ask_vol1', 0),
                            'ask_vol2': q.get('ask_vol2', 0),
                            'ask_vol3': q.get('ask_vol3', 0),
                            'ask_vol4': q.get('ask_vol4', 0),
                            'ask_vol5': q.get('ask_vol5', 0),
                        }
                except Exception as e:
                    logger.debug(f"pytdx 查询 {code} 失败: {e}")
                    continue

            api.disconnect()
            logger.info(f"pytdx 盘口: {len(results)}/{len(codes)} 只")
            return results

        except Exception as e:
            logger.warning(f"pytdx 盘口拉取异常: {e}")
            return {}

    @staticmethod
    def _get_server_list() -> list[tuple[str, int]]:
        """通达信行情服务器列表"""
        return [
            ('119.147.212.81', 7709),
            ('123.125.108.23', 7709),
            ('123.125.108.24', 7709),
            ('60.12.136.250', 7709),
            ('180.153.18.17', 7709),
            ('180.153.18.18', 7709),
            ('hq.cjis.cn', 7709),
        ]
