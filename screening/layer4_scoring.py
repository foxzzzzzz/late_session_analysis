"""L4 融合量化评分 — 对齐 尾盘策略0514.txt 5维模型

100分制，各维度子分直接累加:
  A: 尾盘强度 (max 30): 尾盘涨幅(8) + 放量倍数(8) + 大单占比(8) + 封单强度(6)
  B: K线形态 (max 25): 连续阳线质量(8) + 涨幅稳定性(7) + 实体放大(5) + 突破前高(5)
  C: 资金面 (max 20): 主力净流入(10) + 北向/机构动向(10)
  D: 均线系统 (max 15): 多头排列(8) + MA5加速(4) + 收盘站稳MA5(3)
  E: 市场环境 (max 10): 板块强度(6) + 概念热度(4)

资金面不可用时: C置0, A→37/B→31/D→19/E→13 (等比放大)

等级阈值:
  > 85: strong_buy (超强信号)
  75-85: buy (强信号)
  60-75: watch (中等信号)
  < 60: skip (弱信号)
"""
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# 模块级外部依赖 (由 pipeline 注入)
_northbound_sentiment: Optional[dict] = None
_concept_analyzer = None


def set_northbound_sentiment(data: Optional[dict]):
    global _northbound_sentiment
    _northbound_sentiment = data


def set_concept_analyzer(analyzer):
    global _concept_analyzer
    _concept_analyzer = analyzer


@dataclass
class L4Config:
    """L4 量化评分配置 — 所有阶梯值均可从外部覆盖"""
    # 分数阈值
    high_attention_threshold: float = 85.0
    medium_attention_threshold: float = 60.0
    buy_threshold: float = 75.0
    max_high_attention: int = 15
    max_total_output: int = 30

    # A维度: 尾盘强度
    late_price_tiers: list = None       # [[threshold, score], ...]
    vol_ratio_tiers: list = None
    last5min_bonus_tiers: list = None
    big_order_tiers: list = None
    big_order_net_score: float = 2.0
    bid_ask_tiers: list = None

    # B维度: K线形态
    yang_days_tiers: list = None
    close_rise_tiers: list = None
    volatility_penalty_tiers: list = None
    body_amplifying_score: float = 5.0
    yang_no_amplify_score: float = 2.0
    broke_high_score: float = 5.0
    breakout_score: float = 3.0

    # C维度: 资金面
    flow_net_tiers: list = None         # [[万元, score], ...]
    flow_net_positive_score: float = 2.0
    flow_ratio_score: float = 5.0
    active_buy_tiers: list = None
    late_active_buy_tiers: list = None    # 尾盘实时active_buy_ratio阶梯分
    northbound_tiers: list = None

    # D维度: 均线系统
    ma_alignment_scores: dict = None    # {alignment: score}
    ma5_accel_score: float = 4.0
    price_ma5_tiers: list = None
    ma5_low_floor_ratio: float = 0.98  # 最低价/MA5 下限, 跌破扣分
    ma5_low_floor_penalty: float = 3.0 # 跌穿MA5支撑扣分

    # E维度: 市场环境
    sector_perf_tiers: list = None
    concept_weight: float = 0.4
    concept_max: float = 4.0
    hot_concept_per_item: float = 1.5
    hot_concept_max: float = 4.0
    leader_bonus: float = 1.0

    def __post_init__(self):
        if self.late_price_tiers is None:
            self.late_price_tiers = [[4.0, 8], [2.0, 6], [1.0, 4], [0.0, 2]]
        if self.vol_ratio_tiers is None:
            self.vol_ratio_tiers = [[3.0, 8], [2.0, 6], [1.5, 4], [1.0, 2]]
        if self.last5min_bonus_tiers is None:
            self.last5min_bonus_tiers = [[15.0, 1.0], [10.0, 0.5]]
        if self.big_order_tiers is None:
            self.big_order_tiers = [[0.3, 8], [0.2, 6], [0.1, 4]]
        if self.bid_ask_tiers is None:
            self.bid_ask_tiers = [[2.0, 6], [1.5, 4], [1.0, 2]]
        if self.yang_days_tiers is None:
            self.yang_days_tiers = [[4, 8], [3, 6], [2, 4], [1, 2]]
        if self.close_rise_tiers is None:
            self.close_rise_tiers = [[4, 7], [3, 5], [2, 3], [1, 1]]
        if self.volatility_penalty_tiers is None:
            self.volatility_penalty_tiers = [[0.40, -2], [0.30, -1]]
        if self.flow_net_tiers is None:
            self.flow_net_tiers = [[1000, 10], [500, 7], [100, 4]]
        if self.active_buy_tiers is None:
            self.active_buy_tiers = [[60, 5], [55, 3]]
        if self.late_active_buy_tiers is None:
            self.late_active_buy_tiers = [[70, 8], [60, 5], [50, 3]]
        if self.northbound_tiers is None:
            self.northbound_tiers = [[70, 3], [55, 1]]
        if self.ma_alignment_scores is None:
            self.ma_alignment_scores = {'bullish': 8, 'above_ma5': 5, 'bottom_area': 3, 'low_above_ma5': 2}
        if self.price_ma5_tiers is None:
            self.price_ma5_tiers = [[1.01, 3], [1.005, 2], [1.0, 1]]
        if self.sector_perf_tiers is None:
            self.sector_perf_tiers = [[3, 6], [1, 4], [0, 2], [-1, 1]]


def score_l4(
    contexts: list,
    config: Optional[L4Config] = None,
    capital_data_date: str = "none",
) -> list:
    """L4 量化评分 (5维直接累加, 满分100)

    Returns:
        按 total_score 降序排列, 设置 recommendation 字段
    """
    if config is None:
        config = L4Config()

    # 始终使用标准5维权重, 满分100
    # 资金流不可用时C维度各项子分自然为0, 降级阈值由 merge_and_rank 处理
    max_a, max_b, max_c, max_d, max_e = 30, 25, 20, 15, 10

    for ctx in contexts:
        s_a = _score_tail_strength(ctx, max_a, config)
        s_b = _score_kline_form(ctx, max_b, config)
        s_c = _score_capital_flow(ctx, max_c, config)
        s_d = _score_ma_system(ctx, max_d, config)
        s_e = _score_market_env(ctx, max_e, config)

        ctx.score_tail_strength = s_a
        ctx.score_technical = s_b       # 兼容旧字段名
        ctx.score_capital = s_c
        ctx.score_ma_system = s_d
        ctx.score_market_env = s_e
        ctx.score_history = s_d         # 兼容旧字段名

        ctx.total_score = s_a + s_b + s_c + s_d + s_e

        # 等级判定
        if ctx.total_score > config.high_attention_threshold:
            ctx.recommendation = 'strong_buy'
        elif ctx.total_score >= config.buy_threshold:
            ctx.recommendation = 'buy'
        elif ctx.total_score >= config.medium_attention_threshold:
            ctx.recommendation = 'watch'
        else:
            ctx.recommendation = 'skip'

    contexts.sort(key=lambda c: c.total_score, reverse=True)

    # 评分分布 (实际推荐阈值由 merge_and_rank 根据数据可用性动态决定)
    scores = [c.total_score for c in contexts]
    n = len(scores)
    if n > 0:
        p50 = scores[n // 2] if n >= 2 else scores[0]
        p75 = scores[n * 3 // 4] if n >= 4 else scores[-1]
        p90 = scores[n * 9 // 10] if n >= 10 else scores[-1]
        logger.info(
            f"L4 规则评分 [{capital_data_date}资金流]: {n} 只, "
            f"max={max(scores):.0f}, p90={p90:.0f}, p75={p75:.0f}, p50={p50:.0f}, "
            f"min={min(scores):.0f}"
        )

    return contexts


def _tier_score(value: float, tiers: list) -> float:
    """阶梯查分: tiers=[(threshold, score), ...] 从高到低匹配"""
    for threshold, score in tiers:
        if value >= threshold:
            return score
    return 0.0


# ================================================================
# A: 尾盘强度 (max 30, 无资金流时 max 37)
# ================================================================

def _score_tail_strength(ctx, max_a: int, cfg) -> float:
    """尾盘涨幅(8) + 放量倍数(8) + 大单占比(8) + 封单强度(6)

    无资金流时子分等比放大: 实际分 * max_a/30
    """
    score = 0.0
    scale = max_a / 30

    # 尾盘涨幅
    score += _tier_score(ctx.late_price_change, cfg.late_price_tiers)

    # 尾盘放量倍数
    score += _tier_score(ctx.late_volume_ratio, cfg.vol_ratio_tiers)

    # 最后5分钟量占比加成
    score += _tier_score(ctx.last_5min_volume_pct, cfg.last5min_bonus_tiers)

    # 大单占比
    score += _tier_score(ctx.big_order_ratio, cfg.big_order_tiers)
    if ctx.big_order_net > 0:
        score += cfg.big_order_net_score

    # 封单强度
    if ctx.bid_vol > 0 and ctx.ask_vol > 0:
        ratio = ctx.bid_vol / ctx.ask_vol
        score += _tier_score(ratio, cfg.bid_ask_tiers)

    return min(score * scale, max_a)


# ================================================================
# B: K线形态 (max 25, 无资金流时 max 31)
# ================================================================

def _score_kline_form(ctx, max_b: int, cfg) -> float:
    """连续阳线质量(8) + 涨幅稳定性(7) + 实体放大趋势(5) + 突破前高确认(5)"""
    score = 0.0
    scale = max_b / 25

    # 连续阳线质量: 近4天阳线天数
    yd = ctx.yang_days_4
    score += _tier_score(yd, cfg.yang_days_tiers)

    # 涨幅稳定性: 连续收盘上涨天数
    score += _tier_score(ctx.consecutive_close_rise, cfg.close_rise_tiers)

    # 波动率惩罚
    for threshold, penalty in cfg.volatility_penalty_tiers:
        if ctx.volatility > threshold:
            score += penalty  # penalty is negative
            break

    # 实体放大趋势
    if ctx.body_amplifying:
        score += cfg.body_amplifying_score
    elif yd >= 3:
        score += cfg.yang_no_amplify_score

    # 突破前高确认
    if ctx.broke_high:
        score += cfg.broke_high_score
    elif ctx.anomaly_type == 'breakout':
        score += cfg.breakout_score

    return max(0, min(score * scale, max_b))


# ================================================================
# C: 资金面 (max 20, 无资金流时 max 0)
# ================================================================

def _score_capital_flow(ctx, max_c: int, cfg) -> float:
    """主力净流入(10) + 北向/机构动向(10)"""
    if max_c == 0:
        return 0.0

    score = 0.0
    scale = max_c / 20

    # 主力净流入 (0-10): tiers 单位为万元
    matched = False
    for threshold, s in cfg.flow_net_tiers:
        if ctx.big_order_net > threshold * 10000:
            score += s
            matched = True
            break
    if not matched and ctx.big_order_net > 0:
        score += cfg.flow_net_positive_score

    # 机构动向: 大单占比 + 主动买入占比 + 尾盘实时资金
    inst = 0.0
    if ctx.big_order_ratio >= 0.2:
        inst += cfg.flow_ratio_score
    inst += _tier_score(ctx.active_buy_ratio, cfg.active_buy_tiers)
    if ctx.late_active_buy_ratio > 0:
        inst += _tier_score(ctx.late_active_buy_ratio, cfg.late_active_buy_tiers)

    # 北向资金加成
    if _northbound_sentiment and _northbound_sentiment.get("available"):
        trend = _northbound_sentiment["trend_score"]
        inst += _tier_score(trend, cfg.northbound_tiers)

    score += min(inst, 10)

    return min(score * scale, max_c)


# ================================================================
# D: 均线系统 (max 15, 无资金流时 max 19)
# ================================================================

def _score_ma_system(ctx, max_d: int, cfg) -> float:
    """多头排列(8) + MA5加速(4) + 收盘站稳MA5(3)"""
    score = 0.0
    scale = max_d / 15

    # 多头排列 (层级匹配: bullish > above_ma5* > bottom_area > low_above_ma5)
    if ctx.ma_alignment == 'bullish':
        score += cfg.ma_alignment_scores.get('bullish', 8)
    elif 'above_ma5' in ctx.ma_alignment:
        score += cfg.ma_alignment_scores.get('above_ma5', 5)
    elif ctx.ma_alignment == 'bottom_area':
        score += cfg.ma_alignment_scores.get('bottom_area', 3)
    elif ctx.ma_alignment == 'low_above_ma5':
        score += cfg.ma_alignment_scores.get('low_above_ma5', 2)

    # MA5 渐进加速
    if ctx.ma5_accelerating:
        score += cfg.ma5_accel_score

    # 收盘站稳MA5: 分层比率
    if ctx.ma5 > 0 and ctx.price > 0:
        ratio = ctx.price / ctx.ma5
        score += _tier_score(ratio, cfg.price_ma5_tiers)

    # 最低价不破MA5支撑 — 跌破则扣分 (从L3移至L4随分时价格刷新)
    if ctx.ma5 > 0 and ctx.low > 0 and ctx.low < ctx.ma5 * cfg.ma5_low_floor_ratio:
        score -= cfg.ma5_low_floor_penalty

    return min(score * scale, max_d)


# ================================================================
# E: 市场环境 (max 10, 无资金流时 max 13)
# ================================================================

def _score_market_env(ctx, max_e: int, cfg) -> float:
    """板块强度(6) + 概念热度(4)"""
    score = 0.0
    scale = max_e / 10

    # 板块强度
    score += _tier_score(ctx.sector_performance, cfg.sector_perf_tiers)

    # 概念热度 — 加权热度
    if _concept_analyzer and _concept_analyzer.is_analyzed and ctx.hot_concepts:
        concept_score = _concept_analyzer.get_concept_score(ctx.hot_concepts)
        score += min(concept_score * cfg.concept_weight, cfg.concept_max)
    else:
        score += min(len(ctx.hot_concepts) * cfg.hot_concept_per_item, cfg.hot_concept_max)

    # 龙头效应附加分
    if ctx.leader_strength:
        score += cfg.leader_bonus

    return min(score * scale, max_e)
