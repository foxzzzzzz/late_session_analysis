"""规则评分引擎 — LLM的兜底方案

当LLM超时或调用失败时，直接用规则评分决定建议
保证14:57:50前一定能出结果
"""
import logging

logger = logging.getLogger(__name__)


def rule_based_recommendation(ctx) -> dict:
    """基于纯规则的买卖建议

    返回与LLM一致的格式: {decision, confidence, reason}
    """
    signals = []
    weights = []

    # 尾盘涨幅信号
    if ctx.late_price_change >= 4:
        signals.append(('尾盘强势拉升', 3))
    elif ctx.late_price_change >= 2:
        signals.append(('尾盘拉升', 2))
    elif ctx.late_price_change >= 1:
        signals.append(('尾盘温和上涨', 1))

    # 量能信号
    if ctx.late_volume_ratio >= 2.5:
        signals.append(('大幅放量', 3))
    elif ctx.late_volume_ratio >= 1.5:
        signals.append(('放量', 2))
    elif ctx.late_volume_ratio >= 1.0:
        signals.append(('量能正常', 1))

    # 大单信号
    if ctx.big_order_ratio >= 0.3 and ctx.big_order_net > 0:
        signals.append(('大单强势流入', 3))
    elif ctx.big_order_net > 0:
        signals.append(('大单净流入', 1))

    # 技术面信号
    if ctx.ma_alignment == 'bullish':
        signals.append(('多头排列', 2))
    elif ctx.ma_alignment in ('above_ma5', 'bottom_area'):
        signals.append(('技术面良好', 1))

    # 计算总信号分
    total = sum(w for _, w in signals)

    if total >= 8:
        decision, confidence = 'buy', 'A'
    elif total >= 5:
        decision, confidence = 'buy', 'B'
    elif total >= 3:
        decision, confidence = 'hold', 'B'
    else:
        decision, confidence = 'skip', 'C'

    reason = '; '.join(s for s, _ in signals[:3]) if signals else '无明确信号'
    return {'decision': decision, 'confidence': confidence, 'reason': reason}
