"""涨跌停板工具函数 — 根据股票代码/ST状态返回正确的涨跌停幅度

主板: 10%  (60xxxx, 00xxxx, 30xxxx)
科创板: 20% (688xxx)
北交所: 30% (4xxxxx, 8xxxxx 除 688)
ST: 5%    (名称含 ST)
"""


def get_limit_pct(code: str, is_st: bool = False) -> float:
    """根据股票代码判断涨跌停幅度

    Args:
        code: 6位股票代码
        is_st: 是否为ST股票

    Returns:
        涨跌停幅度百分比 (如 10.0, 20.0, 30.0, 5.0)
    """
    if is_st:
        return 5.0
    code = str(code).zfill(6)
    if code.startswith("68"):       # 科创板 688xxx
        return 20.0
    if code.startswith(("4", "8")):  # 北交所 4xxxxx, 8xxxxx(非688)
        return 30.0
    return 10.0  # 主板(60xxxx, 00xxxx) + 创业板(30xxxx)


def calc_limit_prices(pre_close: float, code: str = "", is_st: bool = False,
                      limit_pct: float = 0.0) -> tuple[float, float]:
    """计算涨停价和跌停价

    Args:
        pre_close: 昨收价
        code: 股票代码 (与 limit_pct 二选一)
        is_st: 是否ST
        limit_pct: 涨跌停幅度 (0 表示自动从 code 推断)

    Returns:
        (limit_up, limit_down) 四舍五入到2位小数
    """
    if limit_pct <= 0:
        limit_pct = get_limit_pct(code, is_st)
    ratio = limit_pct / 100.0
    limit_up = round(pre_close * (1 + ratio), 2)
    limit_down = round(pre_close * (1 - ratio), 2)
    return limit_up, limit_down
