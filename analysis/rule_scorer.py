"""规则评分引擎 — LLM的兜底方案

当LLM超时或调用失败时，直接用规则评分决定建议
保证14:57:50前一定能出结果
"""
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class RuleScorerConfig:
    """规则评分引擎配置 — 所有阈值可从外部覆盖"""
    # 尾盘涨幅信号 [threshold, weight]
    late_price_tiers: list = None
    # 量比信号 [threshold, weight]
    vol_ratio_tiers: list = None
    # 大单信号 [threshold, weight] (ratio>=threshold且net>0→weight分)
    big_order_tiers: list = None
    big_order_net_score: float = 1.0
    # 技术面
    ma_bullish_score: float = 2.0
    ma_good_score: float = 1.0
    # 决策阶梯 [min_score, decision, confidence]
    decision_tiers: list = None
    # 默认决策 [decision, confidence]
    default_decision: list = None

    def __post_init__(self):
        if self.late_price_tiers is None:
            self.late_price_tiers = [[4, 3], [2, 2], [1, 1]]
        if self.vol_ratio_tiers is None:
            self.vol_ratio_tiers = [[2.5, 3], [1.5, 2], [1.0, 1]]
        if self.big_order_tiers is None:
            self.big_order_tiers = [[0.3, 3]]
        if self.decision_tiers is None:
            self.decision_tiers = [[8, 'buy', 'A'], [5, 'buy', 'B'], [3, 'hold', 'B']]
        if self.default_decision is None:
            self.default_decision = ['skip', 'C']


def _tier_score(value: float, tiers: list) -> float:
    """阶梯查分: tiers=[(threshold, score), ...] 从高到低匹配"""
    for threshold, score in tiers:
        if value >= threshold:
            return score
    return 0.0


def rule_based_recommendation(ctx, cfg: Optional[RuleScorerConfig] = None) -> dict:
    """基于纯规则的买卖建议

    返回与LLM一致的格式: {decision, confidence, reason}
    """
    if cfg is None:
        cfg = RuleScorerConfig()

    signals = []

    # 尾盘涨幅信号
    w = _tier_score(ctx.late_price_change, cfg.late_price_tiers)
    if w > 0:
        signals.append(('尾盘强势拉升' if ctx.late_price_change >= 4 else
                        '尾盘拉升' if ctx.late_price_change >= 2 else
                        '尾盘温和上涨', w))

    # 量能信号
    w = _tier_score(ctx.late_volume_ratio, cfg.vol_ratio_tiers)
    if w > 0:
        signals.append(('大幅放量' if ctx.late_volume_ratio >= 2.5 else
                        '放量' if ctx.late_volume_ratio >= 1.5 else
                        '量能正常', w))

    # 大单信号
    for threshold, score in cfg.big_order_tiers:
        if ctx.big_order_ratio >= threshold and ctx.big_order_net > 0:
            signals.append(('大单强势流入', score))
            break
    else:
        if ctx.big_order_net > 0:
            signals.append(('大单净流入', cfg.big_order_net_score))

    # 技术面信号
    if ctx.ma_alignment == 'bullish':
        signals.append(('多头排列', cfg.ma_bullish_score))
    elif ctx.ma_alignment in ('above_ma5', 'bottom_area'):
        signals.append(('技术面良好', cfg.ma_good_score))

    # 计算总信号分
    total = sum(w for _, w in signals)

    # 决策阶梯
    for min_score, decision, confidence in cfg.decision_tiers:
        if total >= min_score:
            reason = '; '.join(s for s, _ in signals[:3]) if signals else '无明确信号'
            return {'decision': decision, 'confidence': confidence, 'reason': reason}

    default_dec, default_conf = cfg.default_decision
    reason = '; '.join(s for s, _ in signals[:3]) if signals else '无明确信号'
    return {'decision': default_dec, 'confidence': default_conf, 'reason': reason}
