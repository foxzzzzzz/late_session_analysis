"""规则评分 + LLM结论 融合排序

融合策略：
- 规则评分权重: 70%
- LLM置信度加权: 30%
- 最终排序: 综合分降序
"""
import logging

logger = logging.getLogger(__name__)

# LLM决策→分数映射
DECISION_SCORE = {'buy': 90, 'hold': 50, 'skip': 0}

# LLM置信度→权重因子
CONFIDENCE_WEIGHT = {'A': 1.0, 'B': 0.7, 'C': 0.4}


def merge_and_rank(
    contexts: list,
    llm_results: dict[str, dict],
    rule_weight: float = 0.7,
    strong_buy_threshold: float = 75.0,
    buy_threshold: float = 60.0,
    watch_threshold: float = 45.0,
    rule_scorer_cfg=None,
) -> list:
    """融合规则评分和LLM分析结果，重新排序

    Args:
        contexts: StockContext列表(已通过L4评分)
        llm_results: {code: {decision, confidence, reason}}
        rule_weight: 规则评分权重 (默认0.7, LLM占0.3)
        strong_buy_threshold: 强烈买入阈值 (实盘75, 回测可调低)
        buy_threshold: 买入阈值 (实盘60)
        watch_threshold: 观察阈值 (实盘45)
        rule_scorer_cfg: RuleScorerConfig, 规则评分参数

    Returns:
        按final_score降序排列的列表
    """
    for ctx in contexts:
        llm = llm_results.get(ctx.code)
        if llm is None:
            # 没有LLM结果，纯规则兜底
            llm = _rule_fallback(ctx, rule_scorer_cfg)
            ctx.llm_fallback = True
        elif llm.get('fallback', False):
            # LLM调用超时或失败，已降级
            ctx.llm_fallback = True
        else:
            ctx.llm_fallback = False

        # 记录LLM结果
        ctx.llm_decision = llm.get('decision', 'skip')
        ctx.llm_confidence = llm.get('confidence', 'C')
        ctx.llm_reason = llm.get('reason', '')

        # 融合计算
        rule_score = ctx.total_score  # 0-100

        llm_decision_score = DECISION_SCORE.get(ctx.llm_decision, 0)
        llm_weight = CONFIDENCE_WEIGHT.get(ctx.llm_confidence, 0.4)
        llm_score = llm_decision_score * llm_weight  # 0-90

        ctx.final_score = rule_score * rule_weight + llm_score * (1 - rule_weight)

    # 排序并分配排名
    contexts.sort(key=lambda c: c.final_score, reverse=True)
    for i, ctx in enumerate(contexts):
        ctx.final_rank = i + 1

        # 最终建议
        if ctx.final_score >= strong_buy_threshold:
            ctx.recommendation = 'strong_buy'
        elif ctx.final_score >= buy_threshold:
            ctx.recommendation = 'buy'
        elif ctx.final_score >= watch_threshold:
            ctx.recommendation = 'watch'
        else:
            ctx.recommendation = 'skip'

    strong_buy = sum(1 for c in contexts if c.recommendation == 'strong_buy')
    buy = sum(1 for c in contexts if c.recommendation == 'buy')

    logger.info(f"融合排序完成: strong_buy={strong_buy}, buy={buy}, "
                f"total={len(contexts)}")

    return contexts


def _rule_fallback(ctx, rule_scorer_cfg=None) -> dict:
    """LLM无结果时的规则兜底"""
    from analysis.rule_scorer import rule_based_recommendation
    result = rule_based_recommendation(ctx, rule_scorer_cfg)
    result['fallback'] = True
    return result
