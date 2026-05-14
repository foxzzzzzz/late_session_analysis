"""L3 技术面与市场环境验证

从L2通过的100-200只中筛选到30-50只

维度：
- 技术位置 (相对位置、均线、关键位)
- 市场情绪 (板块强度、概念热度、龙头效应)
- 历史表现 (近5日尾盘后次日胜率、波动率)
- 风险排查 (利空公告、解禁、机构减仓)
"""
import logging
from dataclasses import dataclass
from typing import Optional
from data_provider.preloader import DataPreloader

logger = logging.getLogger(__name__)


@dataclass
class L3Config:
    # 技术位置
    require_above_ma: bool = False        # MVP阶段放宽：不强制要求站上均线
    position_20d_bottom_pct: float = 50.0 # 近20日位置低于此百分位视为底部
    # 市场情绪
    sector_rank_top_pct: float = 30.0     # 板块排名前30%
    # 历史表现
    min_history_win_rate: float = 60.0    # 近5日胜率%
    max_volatility: float = 50.0          # 最大波动率(年化%)
    max_consecutive_limits: int = 1       # 最多连续涨停天数


def screen_l3_technical(
    contexts: list,
    preloader: Optional[DataPreloader] = None,
    config: Optional[L3Config] = None,
) -> list:
    """L3 技术面与市场环境验证"""
    if config is None:
        config = L3Config()

    passed = []
    for ctx in contexts:
        ctx.l3_passed = _check_l3(ctx, config, preloader)
        if ctx.l3_passed:
            passed.append(ctx)

    logger.info(f"L3 技术: {len(contexts)} → {len(passed)} "
                f"({len(passed) / max(len(contexts), 1) * 100:.1f}%)")
    return passed


def _check_l3(ctx, cfg: L3Config, preloader: Optional[DataPreloader]) -> bool:
    """检查单只股票是否通过L3"""
    # 1. 历史胜率 (有数据时才检查)
    if ctx.history_win_rate > 0 and ctx.history_win_rate < cfg.min_history_win_rate:
        return False

    # 2. 波动率适中 (有数据时才检查)
    if ctx.volatility > 0 and ctx.volatility > cfg.max_volatility:
        return False

    # 3. 非连续涨停板 (避免情绪过热)
    if ctx.consecutive_limit_ups > cfg.max_consecutive_limits:
        return False

    # 4. 风险排查
    if ctx.has_bad_news:
        return False
    if ctx.is_unlock_date:
        return False

    # 5. 板块强度 (有数据则检查)
    if preloader is not None and ctx.sector:
        sector_perf = preloader.get_sector_performance(ctx.sector)
        if sector_perf <= 0:
            # 板块下跌，需要更谨慎
            pass  # MVP阶段不直接排除，但后续评分会加权

    # 6. 技术位置 (至少满足一项)
    tech_ok = False
    # 站上5/10日均线
    if ctx.ma5 > 0 and ctx.price > ctx.ma5:
        tech_ok = True
        ctx.ma_alignment = 'above_ma5'
    if ctx.ma10 > 0 and ctx.price > ctx.ma10:
        tech_ok = True
        if ctx.ma_alignment == 'above_ma5' and ctx.ma5 > ctx.ma10:
            ctx.ma_alignment = 'bullish'

    # 处于底部区域
    if ctx.position_20d <= cfg.position_20d_bottom_pct:
        tech_ok = True
        if ctx.ma_alignment != 'bullish':
            ctx.ma_alignment = 'bottom_area'

    # 接近关键位置
    if ctx.near_key_level:
        tech_ok = True

    if not tech_ok and cfg.require_above_ma:
        return False

    return True
