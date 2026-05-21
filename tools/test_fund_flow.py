"""资金流向数据源独立测试工具 — 覆盖三层通道

用途: 在开盘后任意时间单独测试资金流向API是否可达，无需跑完整管线。
用法: python tools/test_fund_flow.py [--codes 600519,000858,300750]

数据源优先级 (与管线一致):
  1. 分钟线 (push2 fflow/kline klt=1) → 盘中实时 mainForce (无 active_buy_ratio)
  2. 新浪   (MoneyFlow API)           → 备选 mainForce + active_buy_ratio
  3. push2his (日K线 klt=101)         → 兜底，盘中仅返回昨日数据 (正常现象)

输出解读:
  - mainForce       : 主力净流入(万元)，正=流入，负=流出
  - active_buy_ratio: 主动买入占比(%)，>55 偏多
  - data_date       : 数据日期 — 今天=实时，昨天=降级 (仅 push2his 有此区分)
"""
import argparse
import logging
import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S',
)

from data_provider.eastmoney_flow_fetcher import EastmoneyFlowFetcher
from data_provider.eastmoney_minute_flow import EastmoneyMinuteFlowFetcher
from data_provider.sina_fund_flow import SinaFundFlowFetcher

DEFAULT_CODES = ['600519', '000858', '601678', '300750', '000001']

TODAY = datetime.now().strftime("%Y-%m-%d")


def _label(date_str: str) -> str:
    """根据日期标记实时/降级/未知"""
    if date_str == TODAY:
        return "实时"
    if date_str and date_str != TODAY:
        return "降级"
    return "?"


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

    # 使用与管线一致的参数
    f_minute = EastmoneyMinuteFlowFetcher(timeout=8.0, max_workers=4)
    f_sina = SinaFundFlowFetcher(timeout=8.0, max_workers=8)
    f_push2his = EastmoneyFlowFetcher(timeout=15.0, max_workers=2)

    # ================================================================
    #  1. 健康检查 — 三通道各自探测
    # ================================================================
    print('=' * 60)
    print('  1. 健康检查 (600519 贵州茅台)')
    print('=' * 60)

    hc_minute = f_minute.health_check()
    hc_sina = f_sina.health_check()
    hc_push2his = f_push2his.health_check()

    ok_minute = hc_minute.get("ok", False)
    ok_sina = hc_sina.get("ok", False)
    ok_push2his = hc_push2his.get("ok", False)

    print(f'  分钟线 (push2, L1主通道):    {"[OK] 可达" if ok_minute else "[FAIL] 不可达"}')
    print(f'  新浪   (MoneyFlow, L2备选):  {"[OK] 可达" if ok_sina else "[FAIL] 不可达"}')
    print(f'  push2his (日K线, L3兜底):    {"[OK] 可达" if ok_push2his else "[FAIL] 不可达"}')

    any_ok = ok_minute or ok_sina or ok_push2his
    if any_ok:
        channels = []
        if ok_minute:
            channels.append("分钟线")
        if ok_sina:
            channels.append("新浪")
        if ok_push2his:
            channels.append("push2his")
        print(f'  综合: [OK] 可用通道: {", ".join(channels)}')
    else:
        print('\n  结论: 三通道全部不可达，资金流向完全不可用。')
        print('  可能原因: 网络问题 / 东财限流 / 非交易时段。')
        return

    if args.no_batch:
        return

    # ================================================================
    #  2. 单只详细测试 (第一只) — 三通道对比
    # ================================================================
    probe = codes[0]
    print(f'\n{"=" * 60}')
    print(f'  2. 单只详细测试 ({probe})')
    print(f'{"=" * 60}')

    # 逐个拉取，展示每个通道的原始返回
    md = f_minute.enrich_batch([probe]).get(probe, {})
    sd = f_sina.enrich_batch([probe]).get(probe, {})
    hd = f_push2his.enrich_batch([probe]).get(probe, {})

    # 分钟线
    if md:
        print(f'\n  [L1 分钟线] 日期={md.get("data_date","?")} ({_label(md.get("data_date",""))})')
        print(f'    主力净流入:  {md.get("mainForce", 0):.0f} 万')
        print(f'    超大单:      {md.get("super", 0):.0f} 万')
        print(f'    大单:        {md.get("large", 0):.0f} 万')
        print(f'    中单:        {md.get("mid", 0):.0f} 万')
        print(f'    散户:        {md.get("retail", 0):.0f} 万')
        print(f'    主动买入占比: (分钟线不提供)')
    else:
        print(f'\n  [L1 分钟线] [FAIL] 无数据')

    # 新浪
    if sd:
        print(f'\n  [L2 新浪]   日期={sd.get("data_date","?")} ({_label(sd.get("data_date",""))})')
        print(f'    主力净流入:  {sd.get("mainForce", 0):.0f} 万')
        print(f'    超大单:      (新浪不拆分)')
        print(f'    大单:        (新浪不拆分)')
        print(f'    中单:        {sd.get("mid", 0):.0f} 万')
        print(f'    散户:        {sd.get("retail", 0):.0f} 万')
        print(f'    主动买入占比: {sd.get("active_buy_ratio", 0):.1f}%')
    else:
        print(f'\n  [L2 新浪]   [FAIL] 无数据')

    # push2his
    if hd:
        dd = hd.get("data_date", "?")
        print(f'\n  [L3 push2his] 日期={dd} ({_label(dd)})')
        print(f'    主力净流入:  {hd.get("mainForce", 0):.0f} 万')
        print(f'    超大单:      {hd.get("super", 0):.0f} 万')
        print(f'    大单:        {hd.get("large", 0):.0f} 万')
        print(f'    中单:        {hd.get("mid", 0):.0f} 万')
        print(f'    散户:        {hd.get("retail", 0):.0f} 万')
        print(f'    主动买入占比: {hd.get("active_buy_ratio", 0):.1f}%')
    else:
        print(f'\n  [L3 push2his] [FAIL] 无数据')

    # 合并结果 (模拟管线逻辑: mainForce优先分钟线, active_buy_ratio优先新浪)
    print(f'\n  {"-" * 50}')
    print(f'  合并结果 (管线逻辑)')
    merged_main = md.get("mainForce") if md else (sd.get("mainForce") if sd else hd.get("mainForce"))
    merged_ab = sd.get("active_buy_ratio") if sd else hd.get("active_buy_ratio")
    source_main = "分钟线" if md else ("新浪" if sd else "push2his")
    source_ab = "新浪" if sd else ("push2his" if hd else "无")
    print(f'    mainForce       = {merged_main:.0f} 万   (来源: {source_main})')
    print(f'    active_buy_ratio = {merged_ab:.1f}%      (来源: {source_ab})')
    print(f'    数据日期判定: 分钟线/新浪有数据 → "today", 仅push2his → "{hd.get("data_date","?") if hd else "无"}"')

    # ================================================================
    #  3. 批量获取 — 三通道对比
    # ================================================================
    print(f'\n{"=" * 60}')
    print(f'  3. 批量获取 ({len(codes)}只)')
    print(f'{"=" * 60}')

    batch_minute = f_minute.enrich_batch(codes)
    batch_sina = f_sina.enrich_batch(codes)
    batch_push2his = f_push2his.enrich_batch(codes)

    # 各通道实际返回数
    n_minute_ret = len(batch_minute)
    n_sina_ret = len(batch_sina)
    n_ph_ret = len(batch_push2his)

    # 主力来源归属
    n_src_minute = 0
    n_src_sina = 0
    n_src_ph = 0
    n_ph_today = 0
    for code in codes:
        md = batch_minute.get(code, {})
        sd = batch_sina.get(code, {})
        hd = batch_push2his.get(code, {})

        if md:
            src = "分钟线"
            mf = md.get("mainForce", 0)
            ab = sd.get("active_buy_ratio") if sd else hd.get("active_buy_ratio")
            dd = md.get("data_date", "?")
            n_src_minute += 1
        elif sd:
            src = "新浪"
            mf = sd.get("mainForce", 0)
            ab = sd.get("active_buy_ratio")
            dd = sd.get("data_date", "?")
            n_src_sina += 1
        elif hd:
            src = "push2his"
            mf = hd.get("mainForce", 0)
            ab = hd.get("active_buy_ratio")
            dd = hd.get("data_date", "?")
            n_src_ph += 1
            if dd == TODAY:
                n_ph_today += 1
        else:
            src = "无"
            mf = 0
            ab = None
            dd = "?"

        ab_str = f"{ab:.1f}%" if ab is not None else "-"
        print(f'  {code}: 主力={mf:.0f}万, 主动买入={ab_str}, '
              f'日期={dd}({_label(dd)}), 来源={src}')

    n_total = n_src_minute + n_src_sina + n_src_ph

    print(f'\n  通道返回: 分钟线={n_minute_ret}/{len(codes)}  '
          f'新浪={n_sina_ret}/{len(codes)}  '
          f'push2his={n_ph_ret}/{len(codes)}')
    print(f'  主力来源: 分钟线={n_src_minute}  新浪={n_src_sina}  push2his={n_src_ph}')
    print(f'  有效: {n_total}/{len(codes)} (含今日数据: {n_src_minute+n_src_sina+n_ph_today}, 降级: {n_src_ph-n_ph_today})')

    # ================================================================
    #  4. 结论
    # ================================================================
    print(f'\n{"=" * 60}')
    print(f'  结论')
    print(f'{"=" * 60}')

    if not ok_minute and not ok_sina and not ok_push2his:
        print('  [FAIL] 三通道全部不可达，资金流向不可用')
        return

    issues = []
    if ok_minute and n_minute_ret == 0:
        issues.append('分钟线健康检查通过但批量返回空，可能间歇性限流')
    if ok_sina and n_sina_ret == 0:
        issues.append('新浪健康检查通过但批量返回空，可能限流')
    if not ok_minute and not ok_sina:
        issues.append('L1/L2 主通道均不可达，仅剩 push2his 兜底 (盘中数据为昨日)')

    n_today = n_src_minute + n_src_sina + n_ph_today
    if n_today >= len(codes):
        print(f'  [OK] 全部 {len(codes)} 只获取到今日实时数据')
    elif n_today > 0:
        print(f'  [OK] {n_today}/{len(codes)} 只获取到今日实时数据')
    elif n_src_ph > 0:
        is_market_hours = 9 <= datetime.now().hour < 15
        if is_market_hours:
            print(f'  [WARN] 仅获取到昨日降级数据 ({n_src_ph}/{len(codes)}只)')
            print('  盘中 push2his 返回昨日数据为预期行为，但 L1/L2 主通道也未返回今日数据')
            print('  建议: 检查分钟线/新浪日志，确认是否网络或限流导致')
        else:
            print(f'  [INFO] 非交易时段，分钟线/新浪不可达，push2his 返回昨日数据 ({n_src_ph}/{len(codes)}只)')
    else:
        print(f'  [FAIL] {n_total}/{len(codes)} 只有效，且无今日数据')

    if issues:
        print()
        for issue in issues:
            print(f'  [!] {issue}')


if __name__ == '__main__':
    main()
