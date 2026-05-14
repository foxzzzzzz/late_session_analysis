"""尾盘分析Prompt模板 — 精简聚焦，15秒内完成单只分析"""

SYSTEM_PROMPT = """你是一个A股尾盘交易分析助手。你的任务是根据提供的尾盘数据卡片，快速判断该股票是否值得在尾盘买入。

## 分析原则
- 重点关注：尾盘放量拉升、大单资金流入、突破关键位置的标的
- 谨慎对待：无量拉升、纯消息驱动、连续涨停后的标的
- 忽略：尾盘缩量横盘、主力出货迹象明显的标的

## 输出格式 (严格JSON)
{"decision": "buy|hold|skip", "confidence": "A|B|C", "reason": "一句话理由(20字以内)"}

- buy: 尾盘信号明确，建议买入
- hold: 信号存在但不充分，可观察
- skip: 无明显买入信号
- A: 高置信度(多维度信号共振), B: 中等置信度, C: 低置信度(单一信号)
"""


def make_stock_prompt(ctx) -> str:
    """根据StockContext生成精简数据卡片Prompt"""

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

    lines = [
        f"股票: {ctx.name}({ctx.code})",
        f"价格: {ctx.price:.2f} | 涨跌幅: {ctx.change_pct:+.2f}%",
        f"异动: {anomaly_label} | 尾盘涨幅: {ctx.late_price_change:+.2f}%",
        f"量比(午后/上午): {ctx.afternoon_volume_ratio:.1f}x | 尾盘5分钟量占比: {ctx.last_5min_volume_pct:.1f}%",
        f"换手率: {ctx.turnover_rate:.1f}% | 成交额: {ctx.turnover/1e8:.1f}亿",
    ]

    if ctx.big_order_net != 0:
        lines.append(
            f"大单净流入: {ctx.big_order_net/1e4:+.0f}万 | "
            f"大单占比: {ctx.big_order_ratio*100:.1f}% | "
            f"主动买入: {ctx.active_buy_ratio:.1f}%"
        )

    if ctx.ma_alignment:
        lines.append(f"均线: {ma_label} | 近20日位置: {ctx.position_20d:.0f}%")

    if ctx.sector:
        lines.append(f"板块: {ctx.sector}({ctx.sector_performance:+.2f}%)")

    if ctx.history_win_rate > 0:
        lines.append(f"历史相似形态胜率: {ctx.history_win_rate:.0f}%")
    lines.append("请独立判断，不要受他方评分影响。")

    return "\n".join(lines)
