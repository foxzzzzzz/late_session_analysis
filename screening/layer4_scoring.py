"""L4 量化评分

从L3通过的30-50只中精选5-10只核心标的

100分制评分模型：
- 尾盘强度 35%: 涨幅(10) + 放量(10) + 大单(10) + 封单(5)
- 技术面 25%: 突破(10) + 均线(8) + 量价(7)
- 资金面 20%: 主力净流入(10) + 机构动向(10)
- 市场环境 15%: 板块强度(8) + 概念热度(7)
- 历史胜率 5%: 相似形态历史表现(5)

分数等级：
- > 75分: 重点关注 (深度分析)
- 60-75分: 次重点 (简要分析)
- < 60分: 放弃
"""
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class L4Config:
    # 分数阈值
    high_attention_threshold: float = 75.0
    medium_attention_threshold: float = 60.0
    # 输出数量
    max_high_attention: int = 15
    max_total_output: int = 30


def score_l4(
    contexts: list,
    config: Optional[L4Config] = None,
) -> list:
    """L4 量化评分并排序

    返回按 total_score 降序排列的列表
    """
    if config is None:
        config = L4Config()

    for ctx in contexts:
        ctx.score_tail_strength = _score_tail_strength(ctx)
        ctx.score_technical = _score_technical(ctx)
        ctx.score_capital = _score_capital(ctx)
        ctx.score_market_env = _score_market_env(ctx)
        ctx.score_history = _score_history(ctx)

        ctx.total_score = (
            ctx.score_tail_strength * 0.35 +
            ctx.score_technical * 0.25 +
            ctx.score_capital * 0.20 +
            ctx.score_market_env * 0.15 +
            ctx.score_history * 0.05
        )

    contexts.sort(key=lambda c: c.total_score, reverse=True)

    high = sum(1 for c in contexts if c.total_score > config.high_attention_threshold)
    medium = sum(1 for c in contexts if config.medium_attention_threshold <= c.total_score <= config.high_attention_threshold)

    logger.info(f"L4 评分: {len(contexts)} 只, "
                f"重点关注(>{config.high_attention_threshold}): {high}, "
                f"次重点({config.medium_attention_threshold}-{config.high_attention_threshold}): {medium}")

    return contexts


# === 各维度评分函数 (每题满分100,带权重后合并) ===

def _score_tail_strength(ctx) -> float:
    """尾盘强度评分 0-100"""
    score = 0.0

    # 尾盘涨幅 (10分权重)
    if ctx.late_price_change >= 4:
        score += 10
    elif ctx.late_price_change >= 2:
        score += 8
    elif ctx.late_price_change >= 1:
        score += 5
    elif ctx.late_price_change > 0:
        score += 2

    # 放量倍数 (10分权重 内部满分100)
    vol_sub = 0.0
    if ctx.afternoon_volume_ratio >= 3:
        vol_sub = 10
    elif ctx.afternoon_volume_ratio >= 2:
        vol_sub = 8
    elif ctx.afternoon_volume_ratio >= 1.5:
        vol_sub = 6
    elif ctx.afternoon_volume_ratio >= 1.0:
        vol_sub = 3
    # 最后5分钟量占比加成
    if ctx.last_5min_volume_pct >= 15:
        vol_sub += 2
    elif ctx.last_5min_volume_pct >= 10:
        vol_sub += 1
    score += min(vol_sub, 10)

    # 大单占比 (10分权重 内部满分100)
    big_sub = 0.0
    if ctx.big_order_ratio >= 0.3:
        big_sub = 10
    elif ctx.big_order_ratio >= 0.2:
        big_sub = 8
    elif ctx.big_order_ratio >= 0.1:
        big_sub = 5
    elif ctx.big_order_net > 0:
        big_sub = 3
    score += big_sub

    # 封单强度 (5分权重 内部满分100)
    if ctx.bid_vol > 0 and ctx.ask_vol > 0:
        ratio = ctx.bid_vol / ctx.ask_vol
        if ratio >= 2:
            score += 5
        elif ratio >= 1.5:
            score += 3
        elif ratio >= 1.0:
            score += 1

    return min(score * 100 / 35, 100) if score > 0 else 0.0


def _score_technical(ctx) -> float:
    """技术面评分 0-100"""
    score = 0.0

    # 突破有效性 (10分权重)
    if ctx.anomaly_type == 'breakout':
        score += 10
    elif ctx.anomaly_type == 'rally':
        score += 7
    elif ctx.anomaly_type == 'steady':
        score += 5

    # 均线支撑 (8分权重)
    if ctx.ma_alignment == 'bullish':
        score += 8
    elif ctx.ma_alignment == 'above_ma5':
        score += 5
    elif ctx.ma_alignment == 'bottom_area':
        score += 4

    # 量价配合 (7分权重)
    # 价涨量增 = 最佳
    if ctx.late_price_change > 0 and ctx.afternoon_volume_ratio > 1.5:
        score += 7
    elif ctx.late_price_change > 0 and ctx.afternoon_volume_ratio > 1.0:
        score += 4
    elif ctx.late_price_change > 0:
        score += 2

    return min(score * 100 / 25, 100)


def _score_capital(ctx) -> float:
    """资金面评分 0-100"""
    score = 0.0

    # 主力净流入 (10分权重)
    if ctx.big_order_net > 10_000_000:
        score += 10
    elif ctx.big_order_net > 5_000_000:
        score += 7
    elif ctx.big_order_net > 0:
        score += 4

    # 机构动向 (10分权重) — 用大单占比 + 主动买入推算
    inst_sub = 0.0
    if ctx.big_order_ratio >= 0.2:
        inst_sub += 5
    if ctx.active_buy_ratio >= 60:
        inst_sub += 5
    elif ctx.active_buy_ratio >= 55:
        inst_sub += 3
    score += min(inst_sub, 10)

    return min(score * 100 / 20, 100)


def _score_market_env(ctx) -> float:
    """市场环境评分 0-100"""
    score = 0.0

    # 板块强度 (8分权重)
    sector_pct = ctx.sector_performance
    if sector_pct >= 3:
        score += 8
    elif sector_pct >= 1:
        score += 5
    elif sector_pct >= 0:
        score += 3
    elif sector_pct >= -1:
        score += 1

    # 概念热度 (7分权重)
    concept_score = min(len(ctx.hot_concepts) * 3, 7)
    score += concept_score

    # 龙头效应
    if ctx.leader_strength:
        score += 3

    return min(score * 100 / 18, 100)  # 总分最多18,归一化


def _score_history(ctx) -> float:
    """历史胜率评分 0-100"""
    if ctx.history_win_rate >= 80:
        return 100.0
    elif ctx.history_win_rate >= 70:
        return 80.0
    elif ctx.history_win_rate >= 60:
        return 50.0
    elif ctx.history_win_rate >= 50:
        return 30.0
    else:
        return 10.0
