"""资金流向数据源独立测试工具

用途: 在开盘后任意时间单独测试东财资金流向API是否可达，无需跑完整管线。
用法: python tools/test_fund_flow.py [--codes 600519,000858,300750]

数据源优先级:
  push2his (当日实时) → push2 (昨日降级兜底)

输出解读:
  - mainForce : 主力净流入(万元)，正=流入，负=流出
  - active_buy_ratio : 主动买入占比(%)，>55 偏多
  - data_date : 数据日期，今天=实时，昨天=降级数据
"""
import argparse
import logging
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S',
)

from data_provider.eastmoney_flow_fetcher import EastmoneyFlowFetcher

DEFAULT_CODES = ['600519', '000858', '601678', '300750', '000001']


def main():
    parser = argparse.ArgumentParser(description='资金流向数据源独立测试')
    parser.add_argument(
        '--codes', type=str, default=','.join(DEFAULT_CODES),
        help=f'测试股票代码，逗号分隔 (默认: {",".join(DEFAULT_CODES)})',
    )
    parser.add_argument(
        '--no-batch', action='store_true',
        help='仅做健康检查，不跑批量获取',
    )
    args = parser.parse_args()

    codes = [c.strip() for c in args.codes.split(',') if c.strip()]

    f = EastmoneyFlowFetcher()

    # === 1. 健康检查 ===
    print('=' * 60)
    print('  1. 健康检查 (600519 贵州茅台)')
    print('=' * 60)
    hc = f.health_check()
    print(f'  push2his (当日实时): {"[OK] 可达" if hc["push2his"] else "[FAIL] 不可达"}')
    print(f'  综合: {"[OK] push2his通道可用" if hc["ok"] else "[FAIL] push2his不可达"}')

    if not hc['ok']:
        print('\n  结论: 资金流向API完全不可达，无需继续测试。')
        print('  可能原因: 东财API限流 / 网络问题 / 非交易时段。')
        return

    if args.no_batch:
        return

    # === 2. 单只详细测试 (第一只) ===
    print(f'\n{"=" * 60}')
    print(f'  2. 单只详细测试 ({codes[0]})')
    print(f'{"=" * 60}')
    single = f.fetch_capital_flow(codes[0])
    if single:
        print(f'  主力净流入: {single.get("mainForce", 0):.0f} 万')
        print(f'  超大单净流入: {single.get("super", 0):.0f} 万')
        print(f'  大单净流入: {single.get("large", 0):.0f} 万')
        print(f'  中单净流入: {single.get("mid", 0):.0f} 万')
        print(f'  散户净流入: {single.get("retail", 0):.0f} 万')
        print(f'  主动买入占比: {single.get("active_buy_ratio", 0):.1f}%')
        print(f'  数据日期: {single.get("data_date", "?")}')
    else:
        print(f'  [FAIL] 获取失败 (无数据)')

    # === 3. 批量获取 ===
    print(f'\n{"=" * 60}')
    print(f'  3. 批量获取 ({len(codes)}只)')
    print(f'{"=" * 60}')

    results = f.enrich_batch(codes)
    success = 0
    today = 0
    yesterday = 0

    for code in codes:
        data = results.get(code)
        if data:
            success += 1
            dd = data.get('data_date', '?')
            if dd == 'today':
                today += 1
            elif dd:
                yesterday += 1
            tag = '实时' if dd == 'today' else ('降级' if dd else '?')
            print(f'  {code}: 主力={data.get("mainForce", 0):.0f}万, '
                  f'主动买入={data.get("active_buy_ratio", 0):.1f}%, '
                  f'日期={dd}({tag})')
        else:
            print(f'  {code}: [FAIL] 无数据')

    print(f'\n  有效: {success}/{len(codes)} (今日实时: {today}, 昨日降级: {yesterday})')

    print(f'\n{"=" * 60}')
    print(f'  结论')
    print(f'{"=" * 60}')
    if success == 0:
        print('  [FAIL] 批量获取完全失败，资金流向不可用')
        print('  建议: 检查网络 / 确认交易时段 / 等待后重试')
    elif today > 0:
        print(f'  [OK] 获取到今日实时数据 ({today}/{len(codes)}只)')
    elif yesterday > 0:
        print(f'  [WARN] 仅获取到昨日降级数据 ({yesterday}/{len(codes)}只)')
        print('  降级数据可能与今日实际资金流向相反，谨慎使用')


if __name__ == '__main__':
    main()
