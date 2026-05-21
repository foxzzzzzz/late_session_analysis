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
    # 分数阈值 (对齐策略: 85/75/60)
    high_attention_threshold: float = 85.0   # strong_buy (超强信号)
    medium_attention_threshold: float = 60.0  # watch 下限
    buy_threshold: float = 75.0              # buy (强信号)
    max_high_attention: int = 15
    max_total_output: int = 30


def score_l4(
    contexts: list,
    config: Optional[L4Config] = None,
    capital_data_date: str = "none",
) -> list:
    """L4 量化评分 (5维直接累加, 满分100)

    Args:
        capital_data_date: "today"=当日资金流可用, 否则不可用

    Returns:
        按 total_score 降序排列, 设置 recommendation 字段
    """
    if config is None:
        config = L4Config()

    has_capital = (
        capital_data_date == "today"
        and any(
            ctx.big_order_net != 0 or ctx.big_order_ratio > 0
            for ctx in contexts
        )
    )

    # 评分模式: 有资金流→标准5维, 无资金流→C归零其余等比放大
    if has_capital:
        max_a, max_b, max_c, max_d, max_e = 30, 25, 20, 15, 10
    else:
        max_a, max_b, max_c, max_d, max_e = 37, 31, 0, 19, 13

    for ctx in contexts:
        s_a = _score_tail_strength(ctx, max_a)
        s_b = _score_kline_form(ctx, max_b)
        s_c = _score_capital_flow(ctx, max_c)
        s_d = _score_ma_system(ctx, max_d)
        s_e = _score_market_env(ctx, max_e)

        ctx.score_tail_strength = s_a
        ctx.score_technical = s_b       # 兼容旧字段名
        ctx.score_capital = s_c
        ctx.score_market_env = s_e
        ctx.score_history = _score_ma_system(ctx, max_d)  # 兼容

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

    strong_buy = sum(1 for c in contexts if c.recommendation == 'strong_buy')
    buy = sum(1 for c in contexts if c.recommendation == 'buy')
    watch = sum(1 for c in contexts if c.recommendation == 'watch')

    logger.info(
        f"L4 评分 [{capital_data_date}资金流]: {len(contexts)} 只, "
        f"strong_buy(>{config.high_attention_threshold}): {strong_buy}, "
        f"buy({config.buy_threshold}-{config.high_attention_threshold}): {buy}, "
        f"watch({config.medium_attention_threshold}-{config.buy_threshold}): {watch}"
    )

    return contexts


# ================================================================
# A: 尾盘强度 (max 30, 无资金流时 max 37)
# ================================================================

def _score_tail_strength(ctx, max_a: int) -> float:
    """尾盘涨幅(8) + 放量倍数(8) + 大单占比(8) + 封单强度(6)

    无资金流时子分等比放大: 实际分 * max_a/30
    """
    score = 0.0
    scale = max_a / 30

    # 尾盘涨幅 (0-8)
    if ctx.late_price_change >= 4:
        score += 8
    elif ctx.late_price_change >= 2:
        score += 6
    elif ctx.late_price_change >= 1:
        score += 4
    elif ctx.late_price_change > 0:
        score += 2

    # 尾盘放量倍数 (0-8)
    if ctx.late_volume_ratio >= 3:
        score += 8
    elif ctx.late_volume_ratio >= 2:
        score += 6
    elif ctx.late_volume_ratio >= 1.5:
        score += 4
    elif ctx.late_volume_ratio >= 1.0:
        score += 2
    # 最后5分钟量占比加成
    if ctx.last_5min_volume_pct >= 15:
        score += 1
    elif ctx.last_5min_volume_pct >= 10:
        score += 0.5

    # 大单占比 (0-8)
    if ctx.big_order_ratio >= 0.3:
        score += 8
    elif ctx.big_order_ratio >= 0.2:
        score += 6
    elif ctx.big_order_ratio >= 0.1:
        score += 4
    elif ctx.big_order_net > 0:
        score += 2

    # 封单强度 (0-6)
    if ctx.bid_vol > 0 and ctx.ask_vol > 0:
        ratio = ctx.bid_vol / ctx.ask_vol
        if ratio >= 2:
            score += 6
        elif ratio >= 1.5:
            score += 4
        elif ratio >= 1.0:
            score += 2

    return min(score * scale, max_a)


# ================================================================
# B: K线形态 (max 25, 无资金流时 max 31)
# ================================================================

def _score_kline_form(ctx, max_b: int) -> float:
    """连续阳线质量(8) + 涨幅稳定性(7) + 实体放大趋势(5) + 突破前高确认(5)"""
    score = 0.0
    scale = max_b / 25

    # 连续阳线质量 (0-8): 近4天阳线天数
    yd = ctx.yang_days_4
    if yd >= 4:
        score += 8
    elif yd >= 3:
        score += 6
    elif yd >= 2:
        score += 4
    elif yd >= 1:
        score += 2

    # 涨幅稳定性 (0-7): 连续收盘上涨天数 + 低波动率
    cr = ctx.consecutive_close_rise
    if cr >= 4:
        score += 7
    elif cr >= 3:
        score += 5
    elif cr >= 2:
        score += 3
    elif cr >= 1:
        score += 1

    # 波动率惩罚 (涨得不稳扣分)
    if ctx.volatility > 40:
        score -= 2
    elif ctx.volatility > 30:
        score -= 1

    # 实体放大趋势 (0-5)
    if ctx.body_amplifying:
        score += 5
    elif yd >= 3:
        score += 2  # 阳线多但未放大 → 中等

    # 突破前高确认 (0-5)
    if ctx.broke_high:
        score += 5
    elif ctx.anomaly_type == 'breakout':
        score += 3

    return max(0, min(score * scale, max_b))


# ================================================================
# C: 资金面 (max 20, 无资金流时 max 0)
# ================================================================

def _score_capital_flow(ctx, max_c: int) -> float:
    """主力净流入(10) + 北向/机构动向(10)"""
    if max_c == 0:
        return 0.0

    score = 0.0
    scale = max_c / 20

    # 主力净流入 (0-10)
    if ctx.big_order_net > 10_000_000:
        score += 10
    elif ctx.big_order_net > 5_000_000:
        score += 7
    elif ctx.big_order_net > 1_000_000:
        score += 4
    elif ctx.big_order_net > 0:
        score += 2

    # 北向/机构动向 (0-10)
    inst = 0.0
    if ctx.big_order_ratio >= 0.2:
        inst += 5
    if ctx.active_buy_ratio >= 60:
        inst += 5
    elif ctx.active_buy_ratio >= 55:
        inst += 3

    # 北向资金加成
    if _northbound_sentiment and _northbound_sentiment.get("available"):
        trend = _northbound_sentiment["trend_score"]
        if trend >= 70:
            inst += 3
        elif trend >= 55:
            inst += 1

    score += min(inst, 10)

    return min(score * scale, max_c)


# ================================================================
# D: 均线系统 (max 15, 无资金流时 max 19)
# ================================================================

def _score_ma_system(ctx, max_d: int) -> float:
    """多头排列(8) + MA5加速(4) + 收盘站稳MA5(3)"""
    score = 0.0
    scale = max_d / 15

    # 多头排列 (0-8)
    if ctx.ma_alignment == 'bullish':
        score += 8
    elif 'above_ma5' in ctx.ma_alignment:
        score += 5
    elif ctx.ma_alignment == 'bottom_area':
        score += 3
    elif ctx.ma_alignment == 'low_above_ma5':
        score += 2

    # MA5 渐进加速 (0-4)
    if ctx.ma5_accelerating:
        score += 4

    # 收盘站稳MA5 (0-3): 分层比率
    if ctx.ma5 > 0 and ctx.price > 0:
        ratio = ctx.price / ctx.ma5
        if ratio >= 1.01:
            score += 3
        elif ratio >= 1.005:
            score += 2
        elif ratio >= 1.0:
            score += 1

    return min(score * scale, max_d)


# ================================================================
# E: 市场环境 (max 10, 无资金流时 max 13)
# ================================================================

def _score_market_env(ctx, max_e: int) -> float:
    """板块强度(6) + 概念热度(4)"""
    score = 0.0
    scale = max_e / 10

    # 板块强度 (0-6)
    sp = ctx.sector_performance
    if sp >= 3:
        score += 6
    elif sp >= 1:
        score += 4
    elif sp >= 0:
        score += 2
    elif sp >= -1:
        score += 1

    # 概念热度 (0-4) — 加权热度
    if _concept_analyzer and _concept_analyzer.is_analyzed and ctx.hot_concepts:
        concept_score = _concept_analyzer.get_concept_score(ctx.hot_concepts)
        score += min(concept_score * 0.4, 4)
    else:
        score += min(len(ctx.hot_concepts) * 1.5, 4)

    # 龙头效应附加分 (不在策略维度E中，但在L4层面合理)
    if ctx.leader_strength:
        score += 1

    return min(score * scale, max_e)
