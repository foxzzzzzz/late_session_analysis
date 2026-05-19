"""构建 baostock 行业→股票映射缓存，用于 S0 板块预筛选的降级数据源

用法: python tools/build_sector_cache.py
输出: data/sector_constituents_cache.json
刷新: 每日一次，非交易时段运行最佳
"""
import json
import os
import sys
import time
import logging

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

CACHE_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'sector_constituents_cache.json')


def build_cache():
    try:
        import baostock as bs
    except ImportError:
        logger.error("baostock not installed. Run: pip install baostock")
        return

    lg = bs.login()
    if lg.error_code != '0':
        logger.error(f"baostock login failed: {lg.error_msg}")
        return

    logger.info("Querying baostock industry classification...")
    rs = bs.query_stock_industry()
    if rs.error_code != '0':
        logger.error(f"query_stock_industry failed: {rs.error_msg}")
        bs.logout()
        return

    # Build industry → codes mapping
    industries: dict[str, list[str]] = {}
    stock_count = 0
    while rs.next():
        row = rs.get_row_data()
        # row: [date, 'sh.xxxxxx' or 'sz.xxxxxx', name, industry, classification_system]
        code_full = row[1]
        if code_full.startswith('sh.'):
            code = code_full[3:]
        elif code_full.startswith('sz.'):
            code = code_full[3:]
        else:
            code = code_full
        industry = row[3].strip() if row[3] else ''
        if not industry:
            continue
        industries.setdefault(industry, []).append(code)
        stock_count += 1

    bs.logout()

    # Sort by stock count for easy inspection
    sorted_industries = dict(
        sorted(industries.items(), key=lambda x: -len(x[1]))
    )

    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, 'w', encoding='utf-8') as f:
        json.dump({
            'updated': time.strftime('%Y-%m-%d %H:%M:%S'),
            'total_stocks': stock_count,
            'total_industries': len(sorted_industries),
            'industries': sorted_industries,
        }, f, ensure_ascii=False, indent=2)

    logger.info(f"Cache saved: {stock_count} stocks in {len(sorted_industries)} industries → {CACHE_PATH}")
    for ind, codes in list(sorted_industries.items())[:10]:
        logger.info(f"  {ind}: {len(codes)} stocks")


if __name__ == '__main__':
    build_cache()
