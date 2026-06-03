"""规则评分 + LLM结论 融合排序

融合策略 (方案B — 置信度加权逐只动态权重):
- LLM权重 = 0.40 × confidence_factor (A→0.40, B→0.30, C→0.20)
- 规则权重 = 1.0 - LLM权重
- LLM回退时规则权重=1.0
- final_score = rule_score × rule_weight + llm_score × llm_weight
"""
import logging

logger = logging.getLogger(__name__)

# LLM置信度→权重因子 (用于计算LLM权重)
CONFIDENCE_FACTOR = {'A': 1.0, 'B': 0.75, 'C': 0.5}

# LLM最大权重
LLM_MAX_WEIGHT = 0.40


def _compute_weights(confidence: str, is_fallback: bool) -> tuple[float, float]:
    """计算逐只动态权重

    Returns:
        (rule_weight, llm_weight)
    """
    if is_fallback or confidence not in CONFIDENCE_FACTOR:
        return (1.0, 0.0)

    llm_weight = LLM_MAX_WEIGHT * CONFIDENCE_FACTOR[confidence]
    rule_weight = 1.0 - llm_weight
    return (rule_weight, llm_weight)


def merge_and_rank(
    contexts: list,
    llm_results: dict[str, dict],
    strong_buy_threshold: float = 75.0,
    buy_threshold: float = 60.0,
    watch_threshold: float = 45.0,
    rule_scorer_cfg=None,
) -> list:
    """融合规则评分和LLM分析结果，重新排序

    每只股票独立计算LLM权重: A→40%, B→30%, C→20%, 回退→0%

    Args:
        contexts: StockContext列表(已通过L4评分)
        llm_results: {code: {decision, confidence, llm_score, risk_flags, key_factors, reason}}
        strong_buy_threshold: 强烈买入阈值
        buy_threshold: 买入阈值
        watch_threshold: 观察阈值
        rule_scorer_cfg: RuleScorerConfig, 规则评分参数

    Returns:
        按final_score降序排列的列表
    """
    for ctx in contexts:
        llm = llm_results.get(ctx.code)
        if llm is None:
            llm = _rule_fallback(ctx, rule_scorer_cfg)
            ctx.llm_fallback = True
        elif llm.get('fallback', False):
            ctx.llm_fallback = True
        else:
            ctx.llm_fallback = False

        # 记录LLM结果
        ctx.llm_decision = llm.get('decision', 'skip')
        ctx.llm_confidence = llm.get('confidence', 'C')
        ctx.llm_reason = llm.get('reason', '')
        llm_score_raw = llm.get('llm_score')
        if llm_score_raw is not None:
            ctx.llm_score = float(llm_score_raw)
        else:
            # 向后兼容: 旧格式无llm_score, 从decision映射
            _decision_map = {'buy': 85, 'hold': 55, 'skip': 15}
            ctx.llm_score = float(_decision_map.get(ctx.llm_decision, 0))
        ctx.llm_risk_flags = llm.get('risk_flags', [])
        ctx.llm_key_factors = llm.get('key_factors', [])

        # 逐只动态权重融合
        rule_weight, llm_weight = _compute_weights(ctx.llm_confidence, ctx.llm_fallback)
        rule_score = ctx.total_score  # 0-100

        ctx.final_score = rule_score * rule_weight + ctx.llm_score * llm_weight

    # 排序并分配排名
    contexts.sort(key=lambda c: c.final_score, reverse=True)
    for i, ctx in enumerate(contexts):
        ctx.final_rank = i + 1

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

    # 逐只明细
    for ctx in contexts:
        dim_scores = (
            f"A尾盘={ctx.score_tail_strength:.0f} B形态={ctx.score_technical:.0f} "
            f"C资金={ctx.score_capital:.0f} D均线={ctx.score_ma_system:.0f} "
            f"E环境={ctx.score_market_env:.0f}"
        )
        _, llm_w = _compute_weights(ctx.llm_confidence, ctx.llm_fallback)
        logger.info(
            f"  {ctx.code} {ctx.name} | 规则={ctx.total_score:.0f}({dim_scores}) | "
            f"LLM={ctx.llm_score:.0f}({ctx.llm_confidence},w={llm_w:.2f}) | "
            f"融合={ctx.final_score:.0f} → {ctx.recommendation}"
        )
        if ctx.llm_risk_flags:
            logger.info(f"    ⚠ 风险: {', '.join(ctx.llm_risk_flags)}")

    return contexts


def _rule_fallback(ctx, rule_scorer_cfg=None) -> dict:
    """LLM无结果时的规则兜底，返回新格式字段"""
    from analysis.rule_scorer import rule_based_recommendation
    result = rule_based_recommendation(ctx, rule_scorer_cfg)
    result['fallback'] = True
    result.setdefault('llm_score', 0.0)
    result.setdefault('risk_flags', [])
    result.setdefault('key_factors', [])
    return result
