"""L1 基础准入筛选

从全市场5000+筛选到500-800只可交易标的

条件：
- 非ST/退市/停牌
- 当日成交额 > 5000万
- 换手率 > 1%
- 尾盘30分钟成交量 > 全天时段均量
- 5元 < 股价 < 100元 (主板/创业板/科创板价格差异暂忽略)
- 非一字涨停/跌停
"""
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class L1Config:
    min_turnover: float = 50_000_000     # 最低成交额(元)
    min_turnover_rate: float = 1.0       # 最低换手率(%)
    min_price: float = 5.0               # 最低股价(元)
    max_price: float = 100.0             # 最高股价(元)
    exclude_st: bool = True              # 排除ST
    exclude_suspended: bool = True       # 排除停牌
    exclude_one_word_limit: bool = True  # 排除一字板


def screen_l1_access(
    contexts: list,
    config: Optional[L1Config] = None,
) -> list:
    """L1 基础准入筛选

    返回通过L1的 StockContext 列表，同时设置每个 context 的 l1_passed 字段
    """
    if config is None:
        config = L1Config()

    passed = []
    for ctx in contexts:
        ctx.l1_passed = _check_l1(ctx, config)
        if ctx.l1_passed:
            passed.append(ctx)

    logger.info(f"L1 准入: {len(contexts)} → {len(passed)} "
                f"({len(passed) / max(len(contexts), 1) * 100:.1f}%)")
    return passed


def _check_l1(ctx, cfg: L1Config) -> bool:
    """检查单只股票是否通过L1"""
    # ST 过滤
    if cfg.exclude_st and ctx.is_st:
        return False

    # 停牌过滤
    if cfg.exclude_suspended and ctx.is_suspended:
        return False

    # 一字板过滤
    if cfg.exclude_one_word_limit and ctx.is_one_word_limit:
        return False

    # 成交额过滤
    if ctx.turnover < cfg.min_turnover:
        return False

    # 换手率过滤
    if ctx.turnover_rate < cfg.min_turnover_rate:
        return False

    # 价格区间过滤
    if ctx.price < cfg.min_price or ctx.price > cfg.max_price:
        return False

    # 尾盘成交量 > 全天时段均量 (用午后量比近似)
    if ctx.afternoon_volume > 0 and ctx.avg_period_volume > 0:
        if ctx.afternoon_volume <= ctx.avg_period_volume:
            return False

    return True
