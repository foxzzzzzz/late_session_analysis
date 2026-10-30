"""纯规则排序；LLM结果仅附加为旁路分析，不参与任何交易决策。"""
import logging

logger = logging.getLogger(__name__)


def merge_and_rank(
    contexts: list,
    llm_results: dict[str, dict],
    strong_buy_threshold: float = 75.0,
    buy_threshold: float = 60.0,
    watch_threshold: float = 45.0,
    rule_scorer_cfg=None,
) -> list:
    """保留调用接口，正式分数恒等于total_score；旧LLM评分亦不采纳。"""
    for ctx in contexts:
        llm = llm_results.get(ctx.code) or {
            'fallback': True, 'status': 'not_evaluated', 'summary': '未评估',
            'events': [], 'conflicts': [], 'unknowns': ['LLM未启用或无结果'],
        }
        ctx.llm_analysis = llm
        ctx.llm_fallback = llm.get('fallback', False)
        ctx.llm_reason = llm.get('summary', '未评估')
        # 旧字段仅为历史存储兼容，不再生成分数、买卖建议和置信度。
        ctx.llm_score = None
        ctx.llm_decision = ''
        ctx.llm_confidence = ''
        ctx.llm_risk_flags = []
        ctx.llm_key_factors = []
        ctx.final_score = ctx.total_score

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
        logger.info(
            f"  {ctx.code} {ctx.name} | 正式评分={ctx.final_score:.0f}(规则) "
            f"→ {ctx.recommendation} | LLM旁路={ctx.llm_analysis.get('status', 'completed')}"
        )
    return contexts
