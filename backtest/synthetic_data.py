"""回测合成数据估算 — 从5分钟K线推算缺失的资金流向/盘口/概念数据

回测环境缺失的数据:
  - big_order_net/ratio, active_buy_ratio (baostock无资金流向)
  - bid_vol/ask_vol (baostock无盘口)
  - hot_concepts (历史概念标签)

估算策略: 从5分钟K线的量价方向推断, 量级与实盘对齐。
"""
import logging
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


def estimate_capital_flow(df_5min: pd.DataFrame) -> dict:
    """从5分钟线估算资金流向指标

    Returns:
        dict with: big_order_net(万元), big_order_ratio(0-1), active_buy_ratio(0-100)
    """
    result = {"big_order_net": 0.0, "big_order_ratio": 0.0, "active_buy_ratio": 0.0}

    if df_5min is None or df_5min.empty:
        return result

    # 列名标准化
    time_col = "time" if "time" in df_5min.columns else df_5min.columns[0]
    close_col = "close" if "close" in df_5min.columns else df_5min.columns[3]
    open_col = "open" if "open" in df_5min.columns else df_5min.columns[2]
    vol_col = "volume" if "volume" in df_5min.columns else df_5min.columns[7]

    df = df_5min.copy()
    df["_t"] = pd.to_datetime(df[time_col])

    # 尾盘时段 (14:30-14:59)
    late_mask = (df["_t"].dt.hour == 14) & (df["_t"].dt.minute >= 30)
    late = df[late_mask]
    if late.empty:
        return result

    net_flow_yuan = 0.0
    buy_volume = 0.0
    total_late_volume = 0.0

    for _, bar in late.iterrows():
        o = float(bar[open_col])
        c = float(bar[close_col])
        vol = float(bar.get(vol_col, 0))

        # 用成交额估算资金净流向: bar涨幅 × 成交额(元)
        turnover_col_candidates = ["turnover", "成交额", "amount"]
        amt = 0.0
        for tc in turnover_col_candidates:
            if tc in bar.index:
                amt = float(bar[tc])
                break
        # fallback: volume * average price
        if amt == 0.0 and vol > 0 and o > 0:
            amt = vol * (o + c) / 2

        if o > 0 and amt > 0:
            direction = (c - o) / o  # bar内涨跌幅
            net_flow_yuan += direction * amt

        if c > o:
            buy_volume += vol
        total_late_volume += vol

    # big_order_net: 元→万元
    result["big_order_net"] = round(net_flow_yuan / 10000, 2)

    # active_buy_ratio: 尾盘阳线量占比 (0-100)
    if total_late_volume > 0:
        result["active_buy_ratio"] = round(buy_volume / total_late_volume * 100, 2)

    # big_order_ratio: 尾盘量/全天量 (尾盘放量≈大资金参与)
    all_volume = float(df[vol_col].sum()) if vol_col in df.columns else total_late_volume
    if all_volume > 0:
        result["big_order_ratio"] = round(total_late_volume / all_volume, 4)

    return result


def estimate_order_book(df_5min: pd.DataFrame) -> tuple:
    """从最后一根5分钟K线估算挂单强度

    收盘在K线高位 → 买方强势 → bid/ask > 1
    收盘在K线低位 → 卖方强势 → bid/ask < 1

    Returns:
        (bid_vol, ask_vol) 单位: 股
    """
    if df_5min is None or df_5min.empty:
        return 0.0, 0.0

    close_col = "close" if "close" in df_5min.columns else df_5min.columns[3]
    open_col = "open" if "open" in df_5min.columns else df_5min.columns[2]
    high_col = "high" if "high" in df_5min.columns else df_5min.columns[4]
    low_col = "low" if "low" in df_5min.columns else df_5min.columns[5]
    vol_col = "volume" if "volume" in df_5min.columns else df_5min.columns[7]

    last = df_5min.iloc[-1]
    c = float(last[close_col])
    h = float(last[high_col])
    l = float(last[low_col])
    vol = float(last.get(vol_col, 0))

    if h <= l or vol <= 0:
        return 0.0, 0.0

    # 收盘在K线中的位置: 0(最低) ~ 1(最高)
    close_pos = (c - l) / (h - l)

    # 映射到 bid/ask ratio: 0.5 ~ 3.0
    bid_ask_ratio = 0.5 + close_pos * 2.5

    # 构造绝对量: 假设总成交量中卖盘占30%, 买盘按ratio推算
    ask_vol = vol * 0.3
    bid_vol = ask_vol * bid_ask_ratio

    return round(bid_vol, 2), round(ask_vol, 2)


def estimate_concepts(code: str, sector: str = "") -> list[str]:
    """估算股票概念标签

    回测中概念缓存不可用时, 用板块名作为单个标签。
    概念数据是辅助性的 (E维度最多4分), 不影响核心信号。

    Returns:
        概念标签列表, 至少包含板块名
    """
    concepts = []
    if sector:
        concepts.append(sector)
    return concepts
