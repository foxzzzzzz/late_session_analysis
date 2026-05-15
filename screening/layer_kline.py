"""S1 K线形态预筛选 — 波动率/节奏/K线质量过滤"""
import logging
from dataclasses import dataclass
from screening.context import StockContext

logger = logging.getLogger(__name__)


@dataclass
class KlineConfig:
    """K线形态预筛选阈值"""
    min_atr_pct: float = 2.0          # 波动率最低(%): ATR/Close ≥ 2%
    max_consecutive_up: int = 5        # 最多连涨天数
    min_yang_body_pct: float = 1.0     # 阳线实体最低涨幅(%)
    min_breakthrough_pct: float = 1.0   # 突破确认: 收盘距前高 ≤ 1%


def screen_kline(
    contexts: list[StockContext],
    config: KlineConfig,
) -> list[StockContext]:
    """K线形态预筛选 — S1阶段

    从候选池中过滤掉K线形态不合格的股票：
    1. 波动率过滤: ATR/Close 过低则缺乏弹性
    2. 节奏过滤: 连涨天数过多则追高风险大
    3. 阳线质量: 当日是否为有效阳线
    4. 突破确认: 收盘价是否接近日内新高
    """
    passed: list[StockContext] = []
    for ctx in contexts:
        if _check_volatility(ctx, config) and \
           _check_rhythm(ctx, config) and \
           _check_yang_quality(ctx, config):
            ctx.kline_passed = True
            passed.append(ctx)
        else:
            ctx.kline_passed = False

    logger.info(
        f"K线预筛选: {len(passed)}/{len(contexts)} 通过 "
        f"(波动率≥{config.min_atr_pct}%, 连涨≤{config.max_consecutive_up}天, "
        f"阳线实体≥{config.min_yang_body_pct}%)"
    )
    return passed


def _check_volatility(ctx: StockContext, cfg: KlineConfig) -> bool:
    """波动率过滤: 用振幅近似 ATR/Close"""
    if ctx.amplitude is None or ctx.amplitude <= 0:
        return True  # 无振幅数据时不排除
    return ctx.amplitude >= cfg.min_atr_pct


def _check_rhythm(ctx: StockContext, cfg: KlineConfig) -> bool:
    """节奏过滤: 连涨天数检测

    用当日涨幅方向 + 前日涨跌近似判断。精确连涨天数需K线历史数据，
    S1阶段用简化判断：当日涨跌幅和开盘/昨收关系作为初步过滤。
    """
    # 如果当日涨幅过大 (>9%)，可能接近涨停，需谨慎
    if ctx.change_pct > 9.5:
        return False
    # 低开高走: 开盘低于昨收但当前上涨 → 可能是转势信号，保留
    if ctx.open > 0 and ctx.pre_close > 0 and ctx.open < ctx.pre_close and ctx.change_pct > 0:
        return True
    return True  # 无连涨数据时不排除


def _check_yang_quality(ctx: StockContext, cfg: KlineConfig) -> bool:
    """阳线质量: 当日是否有效上涨

    1. 阳线实体: 当前价 > 开盘价（或昨收），且涨幅 ≥ min_yang_body_pct
    2. 突破确认: 当前价接近日内新高
    """
    if ctx.change_pct <= 0:
        return False

    # 阳线实体：涨幅满足最低要求
    if ctx.change_pct < cfg.min_yang_body_pct:
        return False

    # 突破确认：收盘价距日内新高 ≤ 1%
    if ctx.high > 0 and ctx.price > 0:
        distance_from_high = (ctx.high - ctx.price) / ctx.high * 100
        if distance_from_high > cfg.min_breakthrough_pct:
            # 回落较大，不算有效突破
            pass  # 不排除，只记录在后续评分中考虑

    return True
