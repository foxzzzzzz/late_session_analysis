"""L2 尾盘异动识别

从L1通过的500-800只中筛选到100-200只

核心指标：
- 尾盘放量特征
- 价格形态(拉升/企稳/突破)
- 资金流向(大单/主动买入)
- 盘口特征(买卖挂单/撤单率)
"""
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class L2Config:
    # 尾盘放量
    volume_ratio_min: float = 1.5        # 14:30后量 / 上午量
    last_5min_vol_pct_min: float = 8.0   # 最后5分钟量占比(%)
    # 价格形态
    late_rally_min: float = 2.0          # 尾盘拉升最低涨幅(%)
    recovery_drop_min: float = 3.0       # 企稳: 14:30前跌幅(%)
    recovery_rise_min: float = 1.5       # 企稳: 14:45后回升(%)
    # 资金流向
    big_order_net_min: float = 0          # 大单净流入下限
    big_order_ratio_mult: float = 1.3     # 尾盘大单占比 / 全天平均
    active_buy_ratio_min: float = 55.0    # 主动买入占比(%)
    # 盘口
    cancel_rate_max: float = 30.0        # 最大撤单率(%)
    # 开关
    require_volume: bool = True
    require_price_pattern: bool = True
    require_capital: bool = False         # 资金流向数据在L4评分阶段注入
    require_orderbook: bool = False       # MVP阶段盘口数据来自pytdx(可选)


def screen_l2_anomaly(
    contexts: list,
    config: Optional[L2Config] = None,
    has_depth_data: bool = False,
    has_capital_data: bool = False,
) -> list:
    """L2 尾盘异动识别

    一只股票只要命中 放量 + (价格形态 OR 资金流向 OR 盘口) 任一组合即通过
    """
    if config is None:
        config = L2Config()

    passed = []
    for ctx in contexts:
        ctx.l2_passed = _check_l2(ctx, config, has_depth_data, has_capital_data)
        if ctx.l2_passed:
            # 标记异动类型
            _classify_anomaly(ctx, config)
            passed.append(ctx)

    logger.info(f"L2 异动: {len(contexts)} → {len(passed)} "
                f"({len(passed) / max(len(contexts), 1) * 100:.1f}%)")
    return passed


def _check_l2(ctx, cfg: L2Config, has_depth: bool, has_capital: bool) -> bool:
    """检查单只股票是否通过L2"""
    # 1. 尾盘放量 (必须满足其中一项)
    vol_ok = False
    if ctx.afternoon_volume_ratio >= cfg.volume_ratio_min:
        vol_ok = True
    if ctx.last_5min_volume_pct >= cfg.last_5min_vol_pct_min:
        vol_ok = True

    if not vol_ok and cfg.require_volume:
        return False

    # 2. 价格形态 (至少满足一项)
    price_ok = False

    # 尾盘拉升
    if ctx.late_price_change >= cfg.late_rally_min:
        price_ok = True
        ctx.anomaly_type = 'rally'

    # 尾盘企稳: 14:30前跌超3% + 14:45后回升超1.5%
    if not price_ok and ctx.price_at_1430 > 0 and ctx.open > 0:
        drop_before_1430 = (ctx.price_at_1430 - ctx.open) / ctx.open * 100
        recovery = ctx.late_price_change - drop_before_1430
        if drop_before_1430 <= -cfg.recovery_drop_min and recovery >= cfg.recovery_rise_min:
            price_ok = True
            ctx.anomaly_type = 'steady'

    # 突破日内高点
    if not price_ok and ctx.broke_high:
        price_ok = True
        ctx.anomaly_type = 'breakout'

    if not price_ok and cfg.require_price_pattern:
        return False

    # 3. 资金流向 (可选,有数据才检查)
    if has_capital and cfg.require_capital:
        capital_ok = True
        if ctx.big_order_net < cfg.big_order_net_min:
            capital_ok = False
        if ctx.daily_avg_big_order_ratio > 0:
            if ctx.big_order_ratio < ctx.daily_avg_big_order_ratio * cfg.big_order_ratio_mult:
                capital_ok = False
        if ctx.active_buy_ratio < cfg.active_buy_ratio_min:
            capital_ok = False
        if not capital_ok:
            return False

    # 4. 盘口 (可选)
    if has_depth and cfg.require_orderbook:
        if ctx.bid_vol <= ctx.ask_vol:
            return False
        if ctx.cancel_rate > cfg.cancel_rate_max:
            return False

    return True


def _classify_anomaly(ctx, cfg: L2Config):
    """标记异动类型 (取最强信号)"""
    scores = []

    if ctx.late_price_change >= cfg.late_rally_min:
        scores.append(('rally', ctx.late_price_change))
    if ctx.broke_high:
        scores.append(('breakout', 1.0))
    if ctx.anomaly_type == 'steady':
        recovery = ctx.late_price_change - (ctx.price_at_1430 - ctx.open) / ctx.open * 100
        scores.append(('steady', recovery))

    if scores:
        scores.sort(key=lambda x: x[1], reverse=True)
        ctx.anomaly_type = scores[0][0]
    elif not ctx.anomaly_type:
        ctx.anomaly_type = 'volume_only'
