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
    volume_ratio_min: float = 1.2        # 尾盘量比 (14:30后每bar均量 / 13:00-14:30每bar均量)
    last_5min_vol_pct_min: float = 5.0   # 最后5分钟量占比(%)
    # 价格形态
    late_rally_min: float = 2.0          # 尾盘拉升最低涨幅(%)
    recovery_drop_min: float = 3.0       # 企稳: 14:30前跌幅(%)
    recovery_rise_min: float = 1.5       # 企稳: 14:45后回升(%)
    # 资金流向
    big_order_net_min: float = 0          # 大单净流入下限
    big_order_ratio_mult: float = 1.3     # 尾盘大单占比 / 全天平均
    active_buy_ratio_min: float = 55.0    # 主动买入占比(%) — 全天累计
    late_active_buy_ratio_min: float = 45.0  # 尾盘实时active_buy_ratio(%) — Sina差分
    # 盘口
    cancel_rate_max: float = 30.0        # 最大撤单率(%)
    # 开关
    require_volume: bool = True
    require_price_pattern: bool = True
    require_capital: bool = True         # 新浪资金流已接入，启用资金流向筛选
    require_orderbook: bool = False       # MVP阶段盘口数据来自pytdx(可选)


def screen_l2_anomaly(
    contexts: list,
    config: Optional[L2Config] = None,
    has_depth_data: bool = False,
    has_capital_data: bool = False,
    last_round: bool = False,
) -> list:
    """L2 尾盘异动识别

    一只股票只要命中 放量 + 价格形态 + 资金流向 全部条件即通过
    """
    if config is None:
        config = L2Config()

    passed = []
    # 诊断计数器 — 三个条件必须同时满足
    diag = {"vol_fail": 0, "price_fail": 0, "capital_fail": 0,
            "vol_pass": 0, "price_pass": 0, "capital_pass": 0}
    detail_failures: list[str] = []  # 最终轮详细失败原因
    # 采样值分布
    vol_ratios = []
    last5_pcts = []
    late_changes = []
    active_buy_ratios = []   # 全部股票的 active_buy_ratio (含0值)
    for ctx in contexts:
        ctx.l2_passed, fail_reason = _check_l2(ctx, config, has_depth_data, has_capital_data)
        if fail_reason:
            for reason in fail_reason:
                if reason.startswith("量:"):
                    diag["vol_fail"] += 1
                elif reason.startswith("价:"):
                    diag["price_fail"] += 1
                elif reason.startswith("资金:"):
                    diag["capital_fail"] += 1
            if last_round and fail_reason:
                detail_failures.append(
                    f"  {ctx.code} {ctx.name}(涨{ctx.late_price_change:+.1f}% 量比{ctx.late_volume_ratio:.2f}): {'; '.join(fail_reason)}"
                )
        else:
            diag["vol_pass"] += 1
            diag["price_pass"] += 1
            diag["capital_pass"] += 1
        vol_ratios.append(ctx.late_volume_ratio)
        last5_pcts.append(ctx.last_5min_volume_pct)
        late_changes.append(ctx.late_price_change)
        if ctx.active_buy_ratio > 0:
            active_buy_ratios.append(ctx.active_buy_ratio)
        if ctx.l2_passed:
            _classify_anomaly(ctx, config)
            passed.append(ctx)

    logger.info(f"L2 异动: {len(contexts)} → {len(passed)} "
                f"({len(passed) / max(len(contexts), 1) * 100:.1f}%)")
    logger.info(f"L2 诊断: 量失败={diag.get('vol_fail', 0)}, "
                f"价失败={diag['price_fail']}, "
                f"资金失败={diag['capital_fail']} | "
                f"量通过={diag['vol_pass']}, 价通过={diag['price_pass']}, 资金通过={diag['capital_pass']}")

    # 值分布采样 (非零值)
    def _pctls(vals, pcts):
        clean = sorted([v for v in vals if v > 0])
        if not clean:
            return ["N/A"] * len(pcts)
        return [f"{clean[int(len(clean) * p / 100)]:.2f}" for p in pcts]
    if vol_ratios:
        p = _pctls(vol_ratios, [50, 75, 90, 95])
        logger.info(f"L2 分布 【量】量比(p50/p75/p90/p95): {p[0]}/{p[1]}/{p[2]}/{p[3]}")
        p = _pctls(last5_pcts, [50, 75, 90, 95])
        logger.info(f"L2 分布 【量】尾盘量占比: {p[0]}/{p[1]}/{p[2]}/{p[3]}")
        p = _pctls([abs(v) for v in late_changes], [50, 75, 90, 95])
        logger.info(f"L2 分布 【价】尾盘涨幅|%|: {p[0]}/{p[1]}/{p[2]}/{p[3]}")
    if active_buy_ratios:
        p = _pctls(active_buy_ratios, [25, 50, 75, 90])
        logger.info(f"L2 分布 【资金】active_buy_ratio(p25/p50/p75/p90): {p[0]}/{p[1]}/{p[2]}/{p[3]}")
    # 尾盘实时 active_buy_ratio (Sina差分, 仅首轮后有数据)
    late_ab = sorted([c.late_active_buy_ratio for c in contexts if c.late_active_buy_ratio > 0])
    if late_ab:
        n = len(late_ab)
        logger.info(
            f"L2 分布 【资金】late_active_buy_ratio(Sina差分): n={n} "
            f"p25={late_ab[n//4]:.1f} p50={late_ab[n//2]:.1f} p75={late_ab[n*3//4]:.1f}"
        )

    # 最终轮: 输出所有失败股票的具体原因
    if last_round and detail_failures:
        logger.info(f"L2 最终轮失败详情 ({len(detail_failures)}只):")
        for line in detail_failures:
            logger.info(line)

    return passed


def _check_l2(ctx, cfg: L2Config, has_depth: bool, has_capital: bool) -> tuple[bool, list[str]]:
    """检查单只股票是否通过L2, 返回 (通过, 失败原因列表)

    失败原因粒度:
      vol:  量比<阈值 / 尾盘占比<阈值
      price: 拉升不足 / 企稳不满足 / 未破前高
      capital: 大单净流出 / 大单比不足 / late买入比不足
    """
    failures = []

    # 1. 尾盘放量 (必须满足其中一项)
    vol_ratio_ok = ctx.late_volume_ratio >= cfg.volume_ratio_min
    last5min_ok = ctx.last_5min_volume_pct >= cfg.last_5min_vol_pct_min
    vol_ok = vol_ratio_ok or last5min_ok

    if not vol_ok and cfg.require_volume:
        failures.append(f"量:量比{ctx.late_volume_ratio:.2f}<{cfg.volume_ratio_min}&尾盘占比{ctx.last_5min_volume_pct:.1f}%<{cfg.last_5min_vol_pct_min}%")
        return False, failures

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
        reasons = []
        if ctx.late_price_change < cfg.late_rally_min:
            reasons.append(f"拉升{ctx.late_price_change:.2f}%<{cfg.late_rally_min}%")
        if not ctx.broke_high:
            reasons.append("未破前高")
        failures.append("价:" + "&".join(reasons))
        return False, failures

    # 3. 资金流向 (有数据且要求检查时才查验)
    if has_capital and cfg.require_capital:
        cap_fails = []
        if ctx.big_order_net < cfg.big_order_net_min:
            cap_fails.append(f"大单净流入{ctx.big_order_net/10000:.0f}万<{cfg.big_order_net_min/10000:.0f}万")
        if ctx.daily_avg_big_order_ratio > 0:
            if ctx.big_order_ratio < ctx.daily_avg_big_order_ratio * cfg.big_order_ratio_mult:
                cap_fails.append(f"大单比{ctx.big_order_ratio:.2%}<日均{ctx.daily_avg_big_order_ratio:.2%}×{cfg.big_order_ratio_mult}")
        # 主动买入: 尾盘实时数据优先，无实时数据时不拦 (全天快照不含尾盘，可能误导)
        if ctx.late_active_buy_ratio > 0:
            if ctx.late_active_buy_ratio < cfg.late_active_buy_ratio_min:
                cap_fails.append(f"late买入比{ctx.late_active_buy_ratio:.0f}%<{cfg.late_active_buy_ratio_min}%")
        if cap_fails:
            failures.append("资金:" + "&".join(cap_fails))
            return False, failures
    elif not has_capital:
        pass

    # 4. 盘口 (可选)
    if has_depth and cfg.require_orderbook:
        if ctx.bid_vol <= ctx.ask_vol:
            return False, failures
        if ctx.cancel_rate > cfg.cancel_rate_max:
            return False, failures

    return True, []


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
