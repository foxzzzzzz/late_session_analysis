"""资金流向交叉验证 — 分钟线 vs 新浪 vs 东财 push2his

诊断三个数据源对同一股票返回的资金流向是否一致。

数据源能力:
  - 分钟线 (push2 fflow/kline klt=1): 实时 mainForce, 无 active_buy_ratio
  - 新浪 (Sina MoneyFlow): mainForce + active_buy_ratio (r0_in/(r0_in+r0_out)*100)
  - push2his (daykline klt=101): mainForce + active_buy_ratio (收盘后有效)

用法:
    python tools/verify_fund_flow.py                        # 默认 3 只
    python tools/verify_fund_flow.py --codes 600519,000858  # 指定股票

一致性判断:
    - 主力净额同向(同正/同负)且偏差 < 30% → 一致
    - 主动买入比偏差 < 10pp → 一致 (仅新浪 vs push2his, 分钟线无此字段)
"""
import sys
import os
import argparse
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

logging.basicConfig(level=logging.WARNING)

from data_provider.sina_fund_flow import SinaFundFlowFetcher
from data_provider.eastmoney_flow_fetcher import EastmoneyFlowFetcher
from data_provider.eastmoney_minute_flow import EastmoneyMinuteFlowFetcher

DEFAULT_CODES = ["600519", "000858", "300750"]


def _format_amount(wan: float) -> str:
    if abs(wan) >= 10000:
        return f"{wan / 10000:.2f}亿"
    return f"{wan:.1f}万"


def _compare_mainforce(*values: float) -> str:
    """比较主力净额一致性 (多个源)"""
    non_zero = [v for v in values if abs(v) >= 1]
    if len(non_zero) < 2:
        return "OK(无对比)"
    same_dir = all((v >= 0) == (non_zero[0] >= 0) for v in non_zero)
    denom = max(abs(non_zero[0]) + abs(non_zero[-1]), 1)
    dev = abs(non_zero[0] - non_zero[-1]) / (denom / 2) * 100
    if same_dir and dev < 30:
        return "一致"
    elif not same_dir:
        return "偏离(方向)"
    else:
        return f"偏离({dev:.0f}%)"


def _compare_ab(ab_a: float, ab_b: float) -> str:
    """比较主动买入比一致性"""
    if abs(ab_a - ab_b) < 10:
        return "一致"
    return f"偏离({ab_a:.1f} vs {ab_b:.1f})"


def main():
    parser = argparse.ArgumentParser(description="资金流向交叉验证 — 三源对比")
    parser.add_argument(
        "--codes", type=str, default=",".join(DEFAULT_CODES),
        help=f"股票代码，逗号分隔 (默认: {','.join(DEFAULT_CODES)})",
    )
    args = parser.parse_args()
    codes = [c.strip().zfill(6) for c in args.codes.split(",") if c.strip()]

    now = datetime.now()
    in_session = (9, 30) <= (now.hour, now.minute) <= (15, 0)
    timeframe = "交易时段" if in_session else "收盘后" if now.hour >= 15 else "盘前/非交易"
    print(f"资金流向三源验证 | {now.strftime('%Y-%m-%d %H:%M')} | {timeframe}")
    print(f"股票: {', '.join(codes)}")
    print()

    # 并发拉取三个源
    with ThreadPoolExecutor(max_workers=3) as pool:
        f_min = pool.submit(EastmoneyMinuteFlowFetcher(timeout=8.0, max_workers=4).enrich_batch, codes)
        f_sina = pool.submit(SinaFundFlowFetcher(timeout=8.0, max_workers=4).enrich_batch, codes)
        f_his = pool.submit(EastmoneyFlowFetcher(timeout=10.0, max_workers=3).enrich_batch, codes)
        minute_data = f_min.result() or {}
        sina_data = f_sina.result() or {}
        his_data = f_his.result() or {}

    n_min = len(minute_data)
    n_sina = len(sina_data)
    n_his = len(his_data)
    print(f"数据获取: 分钟线={n_min}只  新浪={n_sina}只  push2his={n_his}只")
    if not in_session and n_min == 0:
        print("注意: 非交易时段，分钟线 API 通常不可达 (正常现象)")
    print()

    # 对比表格
    header = (
        f"{'股票':<10} {'分钟_主力':>10} {'新浪_主力':>10} {'push2his_主力':>10} "
        f"{'新浪_买入%':>9} {'his_买入%':>9} {'his日期':>8}  一致性"
    )
    print(header)
    print("-" * len(header))

    for code in codes:
        md = minute_data.get(code, {})
        sd = sina_data.get(code, {})
        hd = his_data.get(code, {})

        mf_min = md.get("mainForce", 0) if md else 0
        mf_sina = sd.get("mainForce", 0) if sd else 0
        mf_his = hd.get("mainForce", 0) if hd else 0
        ab_sina = sd.get("active_buy_ratio", 50) if sd else 50
        ab_his = hd.get("active_buy_ratio", 50) if hd else 50
        his_date = hd.get("data_date", "?")[-5:] if hd else "?"

        # 主力净额一致性: 分钟线 vs 新浪
        if md and sd:
            mf_verdict = _compare_mainforce(mf_min, mf_sina)
        elif sd and hd:
            mf_verdict = _compare_mainforce(mf_sina, mf_his)
        else:
            mf_verdict = "无对比"

        # 主动买入比一致性: 新浪 vs push2his
        if sd and hd:
            ab_verdict = _compare_ab(ab_sina, ab_his)
        else:
            ab_verdict = "无对比"

        print(
            f"{code:<10} {_format_amount(mf_min):>10} {_format_amount(mf_sina):>10} "
            f"{_format_amount(mf_his):>10} {ab_sina:>8.1f}% {ab_his:>8.1f}% "
            f"{his_date:>8}  MF:{mf_verdict} AB:{ab_verdict}"
        )

    print("-" * len(header))

    # 日期检查
    today_str = now.strftime("%Y-%m-%d")
    if hd := next((d for d in his_data.values() if d), {}):
        his_sample = hd.get("data_date", "")
        if his_sample and his_sample != today_str:
            print(f"\n注意: push2his 返回日期为 {his_sample}，非今日({today_str})。"
                  f"当前处于非交易时段，分钟线/新浪也可能是昨日缓存。建议在交易时段重新验证。")

    # 新浪 active_buy_ratio 合理性检查
    print(f"\n新浪 active_buy_ratio 合理性 (应为 0-100%, 通常 40-60%):")
    for code in codes:
        sd = sina_data.get(code, {})
        if sd:
            ab = sd.get("active_buy_ratio", 0)
            flag = "OK" if 30 <= ab <= 70 else "WARN" if ab > 0 else "?"
            print(f"  {code}: {ab:.1f}% {flag}")


if __name__ == "__main__":
    main()
