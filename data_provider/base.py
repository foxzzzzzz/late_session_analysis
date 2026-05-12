"""数据采集基类 — 参考 DSA data_provider/ 模式"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional
import pandas as pd


@dataclass
class RealtimeQuote:
    """单只股票实时行情快照"""
    code: str
    name: str
    price: float
    change_pct: float          # 涨跌幅 %
    turnover: float            # 成交额 (元)
    turnover_rate: float       # 换手率 %
    volume: float              # 成交量 (股)
    high: float                # 今日最高
    low: float                 # 今日最低
    open: float                # 今日开盘
    pre_close: float           # 昨日收盘
    bid1: float = 0.0          # 买一价
    ask1: float = 0.0          # 卖一价
    bid_vol: float = 0.0       # 买盘总挂单量
    ask_vol: float = 0.0       # 卖盘总挂单量
    big_order_net: float = 0.0 # 大单净流入
    big_order_ratio: float = 0.0  # 大单占比
    active_buy_ratio: float = 0.0 # 主动买入占比
    is_st: bool = False
    is_suspended: bool = False
    limit_up: float = 0.0      # 涨停价
    limit_down: float = 0.0    # 跌停价
    sector: str = ""           # 所属板块
    market_cap: float = 0.0    # 总市值


class BaseFetcher(ABC):
    """数据源抽象基类"""

    @property
    @abstractmethod
    def name(self) -> str:
        """数据源名称"""
        ...

    @abstractmethod
    def fetch_snapshot(self) -> list[RealtimeQuote]:
        """拉取全市场实时快照"""
        ...

    def fetch_depth(self, codes: list[str]) -> dict[str, dict]:
        """拉取盘口深度数据 (可选实现)"""
        return {}

    def is_available(self) -> bool:
        """检查数据源是否可用"""
        return True
