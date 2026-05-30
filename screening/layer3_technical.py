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
    require_above_ma: bool = True         # 有真实日线MA数据，强制站上均线
    position_20d_bottom_pct: float = 50.0 # 近20日位置低于此百分位视为底部
    ma5_close_ratio_min: float = 1.0      # 收盘/MA5 最低比率 (1.01→1.005→1.0 分层)
    ma5_low_ratio_min: float = 0.98       # 最低价/MA5 最低比率 (不破MA5)
    vol_ratio_min: float = 1.1            # 量比下限
    vol_ratio_max: float = 2.0            # 量比上限
    # 市场情绪
    sector_rank_top_pct: float = 30.0     # 板块排名前30%
    # 历史表现
    min_history_win_rate: float = 40.0    # 近5日收阳率(%)
    max_volatility: float = 0.60          # 最大波动率(小数,年化, 0.60=60%)
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
    fail_reasons: dict[str, int] = {}
    for ctx in contexts:
        ok, reason = _check_l3(ctx, config, preloader)
        ctx.l3_passed = ok
        if ok:
            passed.append(ctx)
        else:
            fail_reasons[reason] = fail_reasons.get(reason, 0) + 1

    total = len(contexts)
    pass_cnt = len(passed)
    fail_cnt = total - pass_cnt
    logger.info(f"L3 技术: {total} → {pass_cnt} "
                f"({pass_cnt / max(total, 1) * 100:.1f}%)")
    if fail_reasons:
        items = sorted(fail_reasons.items(), key=lambda x: -x[1])
        detail = ", ".join(f"{k}={v}({v/max(fail_cnt,1)*100:.0f}%)" for k, v in items)
        logger.info(f"L3 淘汰原因: {detail}")
    return passed


def _check_l3(ctx, cfg: L3Config, preloader: Optional[DataPreloader]) -> tuple:
    """检查单只股票是否通过L3。返回 (passed: bool, reason: str)"""
    # 1. 历史胜率 (有数据时才检查)
    if ctx.history_win_rate > 0 and ctx.history_win_rate < cfg.min_history_win_rate:
        return False, "win_rate"

    # 2. 波动率适中 (有数据时才检查)
    if ctx.volatility > 0 and ctx.volatility > cfg.max_volatility:
        return False, "volatility"

    # 3. 非连续涨停板 (避免情绪过热)
    if ctx.consecutive_limit_ups > cfg.max_consecutive_limits:
        return False, "consecutive_limits"

    # 4. 风险排查
    if ctx.has_bad_news:
        return False, "bad_news"
    if ctx.is_unlock_date:
        return False, "unlock_date"

    # 4b. 连续缩量 > 10% (策略要求: 不允许)
    if ctx.volume_shrinking:
        return False, "volume_shrink"

    # 4c. 量比范围 (有数据时检查, Tencent实时量比)
    if ctx.vol_ratio > 0:
        if ctx.vol_ratio < cfg.vol_ratio_min or ctx.vol_ratio > cfg.vol_ratio_max:
            return False, "vol_ratio"

    # 5. 板块强度 (有数据则检查)
    if preloader is not None and ctx.sector:
        sector_perf = preloader.get_sector_performance(ctx.sector)
        if sector_perf <= 0:
            # 板块下跌，需要更谨慎
            pass  # MVP阶段不直接排除，但后续评分会加权

    # 6. 价格敏感检查(close/MA5, low/MA5) → 已移至L4 D维度随分时价格刷新
    #    此处不做淘汰，L3仅保留静态指标过滤

    return True, ""
