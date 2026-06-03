"""尾盘分析Prompt模板 — 信息丰富的正交验证者

LLM接收与规则引擎同等的多维数据，独立输出0-100评分+结构化风险提示，
作为规则评分的正交验证通道。
"""

SYSTEM_PROMPT = """你是一个A股尾盘交易分析助手。你的任务是根据提供的多维数据卡片，独立评估该股票是否值得在尾盘买入，输出结构化决策仪表盘。

## 市场环境感知
- **牛市(候选充裕)**: 收紧标准，只挑多维度共振的强股，回避蹭热度的边缘标的
- **熊市(候选稀缺)**: 放宽标准，接受单一强信号的标的，珍惜每一个候选机会
- **中性**: 正常标准，综合权衡

## 评分标准 (0-100)
- **80-100**: 多维度共振 — 尾盘放量+大单流入+均线多头+板块领先，强烈看多
- **60-79**: 信号明确但有瑕疵 — 主信号强但某维度偏弱(如板块滞后/波动率偏高)
- **40-59**: 信号存在但不充分 — 单一信号主导，缺乏多维度验证
- **0-39**: 无明显买入信号或存在重大风险(利空/解禁/尾盘跳水)

## 交易纪律 (出现以下情况应降分或skip)
- 追高风险: 当前价高于MA5超过5%，尾盘拉升可能是诱多
- 无量拉升: 尾盘涨幅>2%但量比<1.0x，量价背离
- 接飞刀: 尾盘涨幅>0但全天跌幅>3%，可能是下跌中继
- 消息驱动: 纯题材炒作无成交量配合
- 利空/解禁: 近3日有利空公告或当日为解禁日

## 输出格式 (严格JSON，无markdown包裹)
{"decision": "buy|hold|skip", "confidence": "A|B|C", "llm_score": 0-100, "risk_flags": ["风险1", "风险2"], "key_factors": ["因子1", "因子2"], "reason": "一句话理由(30字以内)"}

- buy: 尾盘信号明确，建议买入
- hold: 信号存在但不充分，可观察
- skip: 无明显买入信号或存在重大风险
- A: 高置信度(多维度信号共振), B: 中等置信度, C: 低置信度(单一信号)
- llm_score: 0-100综合评分，需与decision一致(buy≥60, hold≥40, skip<60)
- risk_flags: 识别到的风险点列表，无风险时为空数组[]
- key_factors: 支撑判断的关键因子列表，最多3个
"""


def make_stock_prompt(ctx) -> str:
    """根据StockContext生成信息丰富的数据卡片Prompt"""

    anomaly_label = {
        'rally': '尾盘拉升',
        'steady': '尾盘企稳',
        'breakout': '尾盘突破',
        'volume_only': '尾盘放量(无明确价格形态)',
    }.get(ctx.anomaly_type, '未识别')

    ma_label = {
        'bullish': '多头排列',
        'above_ma5': '站上5日线',
        'bottom_area': '底部区域',
    }.get(ctx.ma_alignment, '弱势')

    regime_label = {'bull': '牛市(候选充裕→收紧标准)', 'bear': '熊市(候选稀缺→放宽标准)', 'neutral': '中性'}

    lines = [
        f"股票: {ctx.name}({ctx.code})",
        f"价格: {ctx.price:.2f} | 涨跌幅: {ctx.change_pct:+.2f}%",
        f"市场环境: {regime_label.get(ctx.market_regime, '中性')}",
        "",
        f"=== 尾盘异动 (L2) ===",
        f"异动类型: {anomaly_label} | 尾盘涨幅: {ctx.late_price_change:+.2f}%",
        f"尾盘量比(14:30后/13:00-14:30): {ctx.late_volume_ratio:.1f}x | 尾盘5分钟量占比: {ctx.last_5min_volume_pct:.1f}%",
        f"换手率: {ctx.turnover_rate:.1f}% | 成交额: {ctx.turnover/1e8:.1f}亿",
    ]

    # 资金流向
    if ctx.big_order_net != 0 or ctx.active_buy_ratio > 0:
        lines.append("")
        lines.append(f"=== 资金流向 ===")
        lines.append(
            f"大单净流入: {ctx.big_order_net/1e4:+.0f}万 | "
            f"大单占比: {ctx.big_order_ratio*100:.1f}% | "
            f"主动买入比: {ctx.active_buy_ratio:.1f}%"
        )
        if ctx.late_active_buy_ratio > 0:
            lines.append(f"尾盘主动买入比: {ctx.late_active_buy_ratio:.1f}%")

    # 技术面 (L3)
    lines.append("")
    lines.append(f"=== 技术面 (L3) ===")
    lines.append(f"均线: {ma_label} | 近20日位置: {ctx.position_20d:.0f}%")
    lines.append(f"MA5/10/20/60: {ctx.ma5:.2f}/{ctx.ma10:.2f}/{ctx.ma20:.2f}/{ctx.ma60:.2f}")
    lines.append(f"波动率: {ctx.volatility*100:.1f}% | 近5日相似形态胜率: {ctx.history_win_rate:.0f}%")
    lines.append(f"近4日阳线: {ctx.yang_days_4}/4天 | 连续上涨: {ctx.consecutive_close_rise}天")
    if ctx.ma5_accelerating:
        lines.append("MA5渐进加速: 是")
    if ctx.volume_shrinking:
        lines.append("连续缩量: 是(注意量能衰减)")
    if ctx.body_amplifying:
        lines.append("实体逐日放大: 是")

    # 板块 & 题材
    if ctx.sector:
        lines.append("")
        lines.append(f"=== 板块 & 题材 ===")
        lines.append(f"板块: {ctx.sector} | 涨幅: {ctx.sector_performance:+.2f}% | 排名: top{ctx.sector_rank_pct:.0f}%")
    if ctx.hot_concepts:
        lines.append(f"题材标签: {', '.join(ctx.hot_concepts[:5])}")
    if ctx.leader_strength:
        lines.append("板块龙头: 是")

    # 风险标记
    risk_items = []
    if ctx.has_bad_news:
        risk_items.append("近3日有利空公告")
    if ctx.is_unlock_date:
        risk_items.append("当日为限售解禁日")
    if ctx.consecutive_limit_ups > 0:
        risk_items.append(f"连续涨停{ctx.consecutive_limit_ups}天")
    if risk_items:
        lines.append("")
        lines.append(f"=== 风险标记 ===")
        lines.extend(risk_items)

    # 规则引擎子维度评分 (参考)
    lines.append("")
    lines.append(f"=== 规则引擎评分 (参考) ===")
    lines.append(
        f"总分: {ctx.total_score:.0f}/100 | "
        f"A尾盘={ctx.score_tail_strength:.0f} B形态={ctx.score_technical:.0f} "
        f"C资金={ctx.score_capital:.0f} D均线={ctx.score_ma_system:.0f} "
        f"E环境={ctx.score_market_env:.0f}"
    )

    lines.append("")
    lines.append("请基于以上数据独立判断，输出决策仪表盘JSON。不受规则引擎评分约束，但若与规则评分差异>30分，请审视是否有遗漏的关键信息。")

    return "\n".join(lines)
