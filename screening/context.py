"""股票数据上下文 — 贯穿筛选漏斗各层的统一数据结构"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class StockContext:
    """单只股票的完整数据上下文，各筛选层逐步填充"""

    # === 基础标识 ===
    code: str
    name: str

    # === L1: 实时行情 (来自DataFetcher) ===
    price: float = 0.0
    change_pct: float = 0.0
    turnover: float = 0.0
    turnover_rate: float = 0.0
    volume: float = 0.0
    high: float = 0.0
    low: float = 0.0
    open: float = 0.0
    pre_close: float = 0.0
    limit_up: float = 0.0
    limit_down: float = 0.0
    is_st: bool = False
    is_suspended: bool = False
    sector: str = ""
    market_cap: float = 0.0
    pe_ttm: float = 0.0        # 市盈率(TTM)
    pb: float = 0.0            # 市净率
    vol_ratio: float = 0.0     # 量比
    amplitude: float = 0.0     # 振幅%

    # === L1: 流动性过滤(计算值) ===
    afternoon_volume: float = 0.0       # 14:30后成交量
    morning_volume: float = 0.0         # 上午成交量
    last_5min_volume: float = 0.0       # 最后5分钟成交量
    avg_period_volume: float = 0.0      # 全天时段均量
    afternoon_volume_ratio: float = 0.0 # 午后量比 (afternoon/morning)
    last_5min_volume_pct: float = 0.0   # 最后5分钟占比
    l1_passed: Optional[bool] = None    # L1是否通过

    # === L2: 尾盘异动 ===
    late_price_change: float = 0.0      # 14:30后价格变动%
    price_at_1430: float = 0.0          # 14:30时刻价格
    intraday_high: float = 0.0          # 日内最高(截止当前)
    broke_high: bool = False            # 是否突破日内高点
    big_order_net: float = 0.0          # 大单净流入
    big_order_ratio: float = 0.0        # 大单占比
    daily_avg_big_order_ratio: float = 0.0
    active_buy_ratio: float = 0.0       # 主动买入占比
    bid_vol: float = 0.0                # 买盘挂单总量
    ask_vol: float = 0.0                # 卖盘挂单总量
    cancel_rate: float = 0.0            # 尾盘撤单率
    anomaly_type: str = ""              # 异动类型: rally/steady/breakout
    l2_passed: Optional[bool] = None

    # === S1 K线形态 ===
    kline_passed: Optional[bool] = None

    # === L3: 技术面 ===
    ma5: float = 0.0
    ma10: float = 0.0
    ma20: float = 0.0
    ma30: float = 0.0
    ma60: float = 0.0
    ma_alignment: str = ""              # bullish/bearish/neutral
    ma5_accelerating: bool = False      # MA5 渐进加速
    volume_shrinking: bool = False      # 连续3天缩量>10%
    position_20d: float = 0.0           # 近20日价格百分位
    near_key_level: bool = False        # 是否接近关键位置
    sector_performance: float = 0.0     # 板块涨跌幅
    sector_rank_pct: float = 100.0     # 板块排名百分位(越小越好)
    hot_concepts: list[str] = field(default_factory=list)
    leader_strength: bool = False
    volatility: float = 0.0             # 近期波动率
    consecutive_limit_ups: int = 0      # 连续涨停天数
    has_bad_news: bool = False          # 近3日有无利空
    is_unlock_date: bool = False        # 是否解禁日
    history_win_rate: float = 0.0       # 近5日相似形态次日胜率
    l3_passed: Optional[bool] = None

    # === L4: 评分 ===
    score_tail_strength: float = 0.0    # 尾盘强度(35%)
    score_technical: float = 0.0        # 技术面(25%)
    score_capital: float = 0.0          # 资金面(20%)
    score_market_env: float = 0.0       # 市场环境(15%)
    score_history: float = 0.0          # 历史胜率(5%)
    total_score: float = 0.0            # 综合评分

    # === LLM分析结果 ===
    llm_confidence: str = ""            # A/B/C
    llm_decision: str = ""              # buy/hold/skip
    llm_reason: str = ""                # 简要理由
    llm_fallback: bool = False          # 是否降级到规则评分

    # === 数据质量标记 ===
    data_quality_flags: dict[str, bool] = field(default_factory=lambda: {
        'daily_kline': False,           # 日线是否加载成功
        '5min_kline': False,            # 5分钟线是否加载成功
        'fund_flow': False,             # 资金流向是否获取到
        'ma_calculated': False,         # MA是否真实计算
        'volatility_calculated': False, # 波动率是否真实计算
        'late_metrics_calculated': False,  # 尾盘指标是否真实计算
    })

    # === 最终决策 ===
    final_score: float = 0.0
    final_rank: int = 0
    recommendation: str = ""            # strong_buy/buy/watch/skip

    def _get_effective_limit_up(self) -> float:
        """获取有效涨停价，limit_up=0时从pre_close+代码推断"""
        if self.limit_up > 0:
            return self.limit_up
        if self.pre_close > 0:
            from data_provider.board_utils import get_limit_pct
            pct = get_limit_pct(self.code, self.is_st)
            return round(self.pre_close * (1 + pct / 100), 2)
        return 0.0

    def _get_effective_limit_down(self) -> float:
        """获取有效跌停价"""
        if self.limit_down > 0:
            return self.limit_down
        if self.pre_close > 0:
            from data_provider.board_utils import get_limit_pct
            pct = get_limit_pct(self.code, self.is_st)
            return round(self.pre_close * (1 - pct / 100), 2)
        return 0.0

    @property
    def is_limit_up(self) -> bool:
        """是否涨停"""
        lu = self._get_effective_limit_up()
        if lu <= 0:
            return False
        return abs(self.price - lu) < 0.005

    @property
    def is_limit_down(self) -> bool:
        """是否跌停"""
        ld = self._get_effective_limit_down()
        if ld <= 0:
            return False
        return abs(self.price - ld) < 0.005

    @property
    def is_one_word_limit(self) -> bool:
        """是否一字板 (开盘即涨停/跌停)"""
        lu = self._get_effective_limit_up()
        if lu > 0 and abs(self.open - lu) < 0.005:
            return True
        ld = self._get_effective_limit_down()
        if ld > 0 and abs(self.open - ld) < 0.005:
            return True
        return False
