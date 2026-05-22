"""S1 K线形态预筛选 — 波动率/节奏/K线质量过滤

实现尾盘策略0514.txt全部要求:

Round 1 (14:30) 基础过滤 — 不合格直接淘汰:
  1. ATR/Close in 2.5%~8.5% (波动率适中)
  2. 连涨≤5天 (不过热)
  3. 近9天涨≤6天 (节奏健康)
  4. 近4天≥3阳 (阳线质量)
  5. 连续收盘涨 (近期有持续上升动力)
  6. 单日涨幅<6.5% (非暴涨)
  7. 单日涨幅<2倍ATR (涨幅相对波幅合理)

Round 2 (14:33) 深度验证 — 不合格标记但暂不淘汰 (L4降权):
  8. 涨幅不骤降 (后一天≥前一天的50%)
  9. 涨幅不连续递减
  10. 阳线实体不连续缩小
  11. 今日最高>前3天最高
  12. 今日收盘>前3天开盘
  13. 不能长上影线
"""
import logging
import os
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from screening.context import StockContext

logger = logging.getLogger(__name__)

# 调试: 跟踪每个R1检查的失败计数
_debug_fail_counts = defaultdict(int)
_debug_fail_details: list[dict] = []
_debug_max_details = int(os.getenv("KL_DEBUG_DETAILS", "3"))


@dataclass
class KlineConfig:
    """K线形态预筛选阈值"""
    # Round 1 阈值
    min_atr_pct: float = 2.0           # ATR/Close 最低(%) — 过滤无波动大盘股
    max_atr_pct: float = 8.5           # ATR/Close 最高(%) — 过滤过度投机
    max_consecutive_up: int = 5         # 最多连涨天数
    max_up_in_9days: int = 6           # 近9天最多涨几天
    min_yang_ratio_4d: float = 0.75   # 近4天阳线占比最低 (3/4)
    min_consecutive_close_rise: int = 3  # 至少连续N天收盘上涨
    min_close_rise_pct: float = 0.3      # 连续上涨每天最低涨幅(%)
    max_single_day_pct: float = 6.5    # 单日涨幅上限(%)
    max_atr_multiple: float = 2.0      # 单日涨幅 ≤ N倍ATR

    # Round 2 阈值
    max_drop_ratio: float = 0.5        # 涨幅骤降判定: 后一天/前一天 < 0.5
    max_consecutive_decline: int = 3   # 连续递减天数阈值
    max_consecutive_body_shrink: int = 3  # 阳线实体连续缩小天数阈值
    max_upper_shadow_ratio: float = 0.6  # 上影线占实体比上限

    # 允许跳过的检查 (数据不足时)
    skip_on_missing_data: bool = False    # 无日线数据的股票直接淘汰

    # Round 2 不合格标记 (不淘汰，仅记录)
    r2_flags: set[str] = field(default_factory=set)


def screen_kline(
    contexts: list[StockContext],
    config: KlineConfig,
    daily_cache: dict = None,
) -> list[StockContext]:
    """K线形态预筛选 — S1阶段

    Args:
        contexts: 候选股票列表 (已完成 L1 基础准入)
        config: K线筛选阈值
        daily_cache: {code: DataFrame} 日线数据 (mootdx格式, 按时间升序)

    Returns:
        通过K线形态筛选的股票列表
    """
    global _debug_fail_counts, _debug_fail_details
    _debug_fail_counts = defaultdict(int)
    _debug_fail_details = []

    daily_cache = daily_cache or {}
    passed: list[StockContext] = []
    r1_fail = 0
    r1_missing = 0
    r2_warn = 0

    for ctx in contexts:
        df = daily_cache.get(ctx.code)

        # Round 1: 基础过滤 — 不合格直接淘汰
        if not _round1_basic_filter(ctx, config, df):
            ctx.kline_passed = False
            if df is None or df.empty:
                r1_missing += 1
            else:
                r1_fail += 1
            continue

        # Round 2: 深度验证 — 标记结果，不淘汰
        if not _round2_deep_verify(ctx, config, df):
            r2_warn += 1

        ctx.kline_passed = True
        passed.append(ctx)

    # 调试: 打印失败分布
    if r1_fail > 0 or r1_missing > 0:
        parts = []
        for check_name in ["missing_data", "atr_range", "consecutive_up", "up_frequency",
                           "yang_ratio", "close_momentum", "single_day_pct", "pct_vs_atr"]:
            c = _debug_fail_counts.get(check_name, 0)
            if c > 0:
                parts.append(f"{check_name}={c}")
        logger.info(
            f"K线预筛选: {len(passed)}/{len(contexts)} 通过 "
            f"(R1淘汰: {r1_fail}形态+{r1_missing}缺数据, R2警告: {r2_warn})"
        )
        if parts:
            logger.info(f"K线失败分布: {' | '.join(parts)}")
        for d in _debug_fail_details:
            logger.debug(
                f"K线失败详情: code={d['code']} check={d['check']} "
                f"chg={d['change_pct']}% price={d['price']} "
                f"atr={d['atr_pct']}% rows={d['df_rows']} "
                f"close[-3:]={d['close_last3']}"
            )
    else:
        logger.info(
            f"K线预筛选: {len(passed)}/{len(contexts)} 通过 "
            f"(R1淘汰: {r1_fail}形态+{r1_missing}缺数据, R2警告: {r2_warn})"
        )

    return passed


# ================================================================
# Round 1: 基础过滤
# ================================================================

def _round1_basic_filter(ctx: StockContext, cfg: KlineConfig, df: Optional[pd.DataFrame]) -> bool:
    """Round 1 七项基础检查，任一项不通过即淘汰"""
    if df is None or df.empty:
        _debug_fail_counts["missing_data"] += 1
        return cfg.skip_on_missing_data  # False → 无日线数据则淘汰

    # 1. ATR/Close 在合理范围
    if not _check_atr_range(ctx, cfg, df):
        _debug_fail_counts["atr_range"] += 1
        _record_debug(ctx, "atr_range", df)
        return False

    # 2. 连涨天数检查
    if not _check_consecutive_up(ctx, cfg, df):
        _debug_fail_counts["consecutive_up"] += 1
        _record_debug(ctx, "consecutive_up", df)
        return False

    # 3. 近9天上涨频率
    if not _check_up_frequency(ctx, cfg, df):
        _debug_fail_counts["up_frequency"] += 1
        _record_debug(ctx, "up_frequency", df)
        return False

    # 4. 阳线占比 + 今日阳线
    if not _check_yang_ratio(ctx, cfg, df):
        _debug_fail_counts["yang_ratio"] += 1
        _record_debug(ctx, "yang_ratio", df)
        return False

    # 5. 近期收盘持续上涨
    if not _check_close_momentum(ctx, cfg, df):
        _debug_fail_counts["close_momentum"] += 1
        _record_debug(ctx, "close_momentum", df)
        return False

    # 6. 单日涨幅上限
    if not _check_single_day_pct(ctx, cfg):
        _debug_fail_counts["single_day_pct"] += 1
        _record_debug(ctx, "single_day_pct", df)
        return False

    # 7. 单日涨幅 vs ATR
    if not _check_pct_vs_atr(ctx, cfg, df):
        _debug_fail_counts["pct_vs_atr"] += 1
        _record_debug(ctx, "pct_vs_atr", df)
        return False

    return True


def _record_debug(ctx, check: str, df: pd.DataFrame):
    """记录前N条失败详情"""
    if len(_debug_fail_details) >= _debug_max_details:
        return
    close = pd.to_numeric(df["close"], errors="coerce")
    atr_val = _safe_atr(df)
    atr_pct = (atr_val / float(close.iloc[-1]) * 100) if atr_val > 0 and len(close) > 0 else 0
    _debug_fail_details.append({
        "code": ctx.code,
        "check": check,
        "change_pct": round(ctx.change_pct, 4),
        "price": round(ctx.price, 2),
        "atr_pct": round(atr_pct, 2),
        "df_rows": len(df),
        "close_last3": [round(float(x), 2) for x in close.iloc[-3:].tolist()] if len(close) >= 3 else [],
    })


def _safe_atr(df: pd.DataFrame) -> float:
    try:
        from data_provider.kline_provider import KlineProvider
        return KlineProvider.compute_atr(df)
    except Exception:
        return 0.0


def _check_atr_range(ctx: StockContext, cfg: KlineConfig, df: pd.DataFrame) -> bool:
    """ATR/Close 在 2.5%~8.5% 之间"""
    from data_provider.kline_provider import KlineProvider
    atr = KlineProvider.compute_atr(df)
    close = float(df["close"].iloc[-1])
    if atr <= 0 or close <= 0:
        return cfg.skip_on_missing_data
    atr_pct = atr / close * 100
    return cfg.min_atr_pct <= atr_pct <= cfg.max_atr_pct


def _check_consecutive_up(ctx: StockContext, cfg: KlineConfig, df: pd.DataFrame) -> bool:
    """连涨天数 ≤ 5天 (从最近一天向前数)"""
    close = pd.to_numeric(df["close"], errors="coerce").dropna()
    if len(close) < 2:
        return True
    consecutive = 0
    for i in range(len(close) - 1, 0, -1):
        if close.iloc[i] > close.iloc[i - 1]:
            consecutive += 1
        else:
            break
    return consecutive <= cfg.max_consecutive_up


def _check_up_frequency(ctx: StockContext, cfg: KlineConfig, df: pd.DataFrame) -> bool:
    """近9天最多涨6天"""
    close = pd.to_numeric(df["close"], errors="coerce").dropna()
    if len(close) < 5:
        return True
    recent = close.iloc[-9:]
    up_days = sum(1 for i in range(1, len(recent)) if recent.iloc[i] > recent.iloc[i - 1])
    return up_days <= cfg.max_up_in_9days


def _check_yang_ratio(ctx: StockContext, cfg: KlineConfig, df: pd.DataFrame) -> bool:
    """近4天至少3天阳线"""
    open_p = pd.to_numeric(df["open"], errors="coerce")
    close = pd.to_numeric(df["close"], errors="coerce")
    if len(close) < 5:
        return True

    # 最近4个已完成的交易日
    recent_open = open_p.iloc[-5:-1]
    recent_close = close.iloc[-5:-1]
    yang_days = sum(1 for o, c in zip(recent_open, recent_close) if c > o)
    min_yang = int(cfg.min_yang_ratio_4d * 4)
    return yang_days >= min_yang


def _check_close_momentum(ctx: StockContext, cfg: KlineConfig, df: pd.DataFrame) -> bool:
    """近期存在连续N天收盘上涨 (在最近M天内扫描，不要求最近一天必涨)"""
    if cfg.min_consecutive_close_rise <= 0:
        return True
    close = pd.to_numeric(df["close"], errors="coerce").dropna()
    if len(close) < cfg.min_consecutive_close_rise + 1:
        return True

    # 最近 M 天 (不包括今天), 扫描任意连续N天上涨
    lookback = cfg.min_consecutive_close_rise + 4
    recent = close.iloc[-(lookback + 1):-1]
    if len(recent) < cfg.min_consecutive_close_rise + 1:
        return True

    max_streak = 0
    current_streak = 0
    for i in range(1, len(recent)):
        prev = recent.iloc[i - 1]
        curr = recent.iloc[i]
        rise_pct = (curr - prev) / prev * 100
        if rise_pct >= cfg.min_close_rise_pct:
            current_streak += 1
            max_streak = max(max_streak, current_streak)
        else:
            current_streak = 0

    return max_streak >= cfg.min_consecutive_close_rise


def _check_single_day_pct(ctx: StockContext, cfg: KlineConfig) -> bool:
    """单日涨幅 < 6.5%"""
    return abs(ctx.change_pct) < cfg.max_single_day_pct


def _check_pct_vs_atr(ctx: StockContext, cfg: KlineConfig, df: pd.DataFrame) -> bool:
    """单日涨幅 < 2倍ATR (涨幅相对波幅合理)"""
    from data_provider.kline_provider import KlineProvider
    atr = KlineProvider.compute_atr(df)
    close = float(df["close"].iloc[-1])
    if atr <= 0 or close <= 0:
        return cfg.skip_on_missing_data
    atr_pct = atr / close * 100
    return abs(ctx.change_pct) < cfg.max_atr_multiple * atr_pct


# ================================================================
# Round 2: 深度验证 (不淘汰，标记结果供L4降权)
# ================================================================

def _round2_deep_verify(ctx: StockContext, cfg: KlineConfig, df: Optional[pd.DataFrame]) -> bool:
    """Round 2 六项深度验证，有任一项不通过标记警告"""
    if df is None or df.empty:
        return True  # 无数据不判失败

    all_ok = True

    if not _check_no_sharp_drop(ctx, cfg, df):
        all_ok = False

    if not _check_no_continuous_decline(ctx, cfg, df):
        all_ok = False

    if not _check_no_body_shrink(ctx, cfg, df):
        all_ok = False

    if not _check_high_break(ctx, cfg, df):
        all_ok = False

    if not _check_close_vs_open(ctx, cfg, df):
        all_ok = False

    if not _check_upper_shadow(ctx, cfg, df):
        all_ok = False

    return all_ok


def _check_no_sharp_drop(ctx: StockContext, cfg: KlineConfig, df: pd.DataFrame) -> bool:
    """涨幅不骤降: 最近每天涨幅 ≥ 前一天的50%"""
    close = pd.to_numeric(df["close"], errors="coerce").dropna()
    if len(close) < 4:
        return True

    # 计算最近3天的日收益率
    recent = close.iloc[-4:]
    returns = [(recent.iloc[i] - recent.iloc[i-1]) / recent.iloc[i-1] * 100
               for i in range(1, len(recent))]

    for i in range(1, len(returns)):
        prev = returns[i - 1]
        curr = returns[i]
        if prev > 0 and curr < prev * cfg.max_drop_ratio:
            return False

    return True


def _check_no_continuous_decline(ctx: StockContext, cfg: KlineConfig, df: pd.DataFrame) -> bool:
    """涨幅不连续递减 (连续3天涨幅递减)"""
    close = pd.to_numeric(df["close"], errors="coerce").dropna()
    if len(close) < 6:
        return True

    recent = close.iloc[-6:]
    returns = [(recent.iloc[i] - recent.iloc[i-1]) / recent.iloc[i-1] * 100
               for i in range(1, len(recent))]

    # 检查最近3天是否连续递减
    if len(returns) >= 3:
        last3 = returns[-3:]
        if all(last3[i] > last3[i + 1] for i in range(len(last3) - 1)):
            return False

    return True


def _check_no_body_shrink(ctx: StockContext, cfg: KlineConfig, df: pd.DataFrame) -> bool:
    """阳线实体不连续缩小 (最近3根阳线)"""
    open_p = pd.to_numeric(df["open"], errors="coerce")
    close = pd.to_numeric(df["close"], errors="coerce")
    if len(close) < 4:
        return True

    bodies = [abs(close.iloc[i] - open_p.iloc[i]) for i in range(len(close))
              if close.iloc[i] > open_p.iloc[i]]  # 只要阳线

    if len(bodies) < 3:
        return True

    last3_bodies = bodies[-3:]
    if all(last3_bodies[i] > last3_bodies[i + 1] for i in range(len(last3_bodies) - 1)):
        return False

    return True


def _check_high_break(ctx: StockContext, cfg: KlineConfig, df: pd.DataFrame) -> bool:
    """今日最高 > 前3天最高"""
    high = pd.to_numeric(df["high"], errors="coerce").dropna()
    if len(high) < 4:
        return True

    prev_3_high = high.iloc[-4:-1].max()  # 前3天最高
    today_high = ctx.high if ctx.high > 0 else high.iloc[-1]

    return today_high > prev_3_high


def _check_close_vs_open(ctx: StockContext, cfg: KlineConfig, df: pd.DataFrame) -> bool:
    """今日收盘 > 前3天开盘"""
    open_p = pd.to_numeric(df["open"], errors="coerce").dropna()
    if len(open_p) < 4:
        return True

    prev_3_open = open_p.iloc[-4:-1]  # 前3天开盘
    today_close = ctx.price if ctx.price > 0 else float(df["close"].iloc[-1])

    return all(today_close > o for o in prev_3_open)


def _check_upper_shadow(ctx: StockContext, cfg: KlineConfig, df: pd.DataFrame) -> bool:
    """不能长上影线: 上影线/实体 ≤ 60%"""
    open_p = pd.to_numeric(df["open"], errors="coerce")
    close = pd.to_numeric(df["close"], errors="coerce")
    high = pd.to_numeric(df["high"], errors="coerce")
    if len(close) < 2:
        return True

    # 检查最近一根日线的上影线
    o = open_p.iloc[-1]
    c = close.iloc[-1]
    h = high.iloc[-1]

    body_high = max(o, c)
    upper_shadow = h - body_high
    body = abs(c - o)

    if body <= 0:
        # 十字星或接近十字星 — 用全振幅 (high-low) 判断上影是否过长
        l = pd.to_numeric(df["low"], errors="coerce").iloc[-1]
        full_range = h - l
        if full_range <= 0:
            return True
        return upper_shadow / full_range <= cfg.max_upper_shadow_ratio

    return (upper_shadow / body) <= cfg.max_upper_shadow_ratio
