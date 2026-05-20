"""测试东方财富 push2his 历史资金流向数据可用性

用法:
    python tools/test_push2his_history.py              # 默认 600519
    python tools/test_push2his_history.py -c 000858    # 指定股票
"""
import sys
import os
import argparse
import json
import time
import urllib.request
import ssl

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

HIS_API = (
    "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
    "?secid={secid}&fields1=f1,f2,f3,f7"
    "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
    "&lmt={lmt}&klt=101"
)


def _to_secid(code: str) -> str:
    code = str(code).zfill(6)
    if code.startswith("6"):
        return f"1.{code}"
    return f"0.{code}"


def test_with_urllib(code: str, lmt: int = 30, timeout: int = 15):
    """使用 urllib (无 sessions 依赖) 测试"""
    secid = _to_secid(code)
    url = HIS_API.format(secid=secid, lmt=lmt)

    print(f"push2his 历史资金流向测试")
    print(f"  股票: {code} (secid={secid})")
    print(f"  lmt={lmt}")
    print()

    # 尝试不同的请求方式
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/117.0.0.0 Safari/537.36",
        "Referer": "https://data.eastmoney.com/",
        "Accept": "application/json, text/plain, */*",
    }

    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=headers)
            ctx = ssl.create_default_context()
            resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)
            raw = resp.read().decode("utf-8")
            d = json.loads(raw)
            break
        except Exception as e:
            if attempt < 2:
                wait = (attempt + 1) * 2
                print(f"  第{attempt+1}次失败: {type(e).__name__}: {e}")
                print(f"  等待 {wait}s 重试...")
                time.sleep(wait)
            else:
                print(f"  3次均失败: {type(e).__name__}: {e}")
                print()
                print("=== 结论 ===")
                print("push2his 当前不可达。非交易时段该接口可能被东方财富限制。")
                print("后续可尝试:")
                print("  1. 在交易日盘后 (15:00-18:00) 再次测试")
                print("  2. 换用新浪资金流API (但新浪也不提供历史数据)")
                print("  3. 使用 akshare 的资金流接口 (ak.stock_individual_fund_flow)")
                return

    data = d.get("data")
    if not data:
        print(f"  响应无 data: {json.dumps(d, ensure_ascii=False)[:200]}")
        return

    klines = data.get("klines", [])
    if not klines:
        print(f"  无 klines 数据, data: {data}")
        return

    print(f"  成功! 获取 {len(klines)} 条记录")
    print()
    print(f"  {'日期':>12} {'主力净额(万)':>14} {'散户净额(万)':>14} {'中单净额(万)':>14} {'超大单(万)':>12} {'大单(万)':>12} {'主动买入%':>10}")
    print("  " + "-" * 103)

    for k in klines[:10]:
        parts = k.split(",")
        if len(parts) >= 6:
            date = parts[0]
            main = float(parts[1]) / 10000 if parts[1] and parts[1] != "-" else 0
            retail = float(parts[2]) / 10000 if parts[2] and parts[2] != "-" else 0
            mid = float(parts[3]) / 10000 if parts[3] and parts[3] != "-" else 0
            large = float(parts[4]) / 10000 if parts[4] and parts[4] != "-" else 0
            super_ = float(parts[5]) / 10000 if parts[5] and parts[5] != "-" else 0
            ab = float(parts[6]) if len(parts) >= 7 and parts[6] and parts[6] != "-" else 0
            print(f"  {date:>12} {main:>14.1f} {retail:>14.1f} {mid:>14.1f} {super_:>12.1f} {large:>12.1f} {ab:>9.1f}%")

    if len(klines) > 10:
        print(f"  ... 省略 {len(klines) - 10} 条")

    print("  " + "-" * 103)
    print(f"  总计: {len(klines)} 条, {klines[-1][:10]} ~ {klines[0][:10]}")
    print()

    # 测试不同 lmt
    print("  测试不同 lmt:")
    for test_lmt in [1, 5, 30, 60, 120]:
        time.sleep(0.5)
        url2 = HIS_API.format(secid=secid, lmt=test_lmt)
        try:
            req2 = urllib.request.Request(url2, headers=headers)
            resp2 = urllib.request.urlopen(req2, timeout=timeout, context=ctx)
            d2 = json.loads(resp2.read().decode("utf-8"))
            k2 = d2.get("data", {}).get("klines", []) if d2.get("data") else []
            first = k2[0][:10] if k2 else "N/A"
            last = k2[-1][:10] if k2 else "N/A"
            print(f"    lmt={test_lmt:>3}: {len(k2):>3} 条, {last} ~ {first}")
        except Exception as e:
            print(f"    lmt={test_lmt:>3}: 失败 {type(e).__name__}")

    print()
    print("=== 结论 ===")
    print("push2his 可返回历史资金流向数据，可用于回测资金流富化。")


def main():
    parser = argparse.ArgumentParser(description="测试 push2his 历史资金流向")
    parser.add_argument("-c", "--code", default="600519", help="股票代码")
    parser.add_argument("-l", "--lmt", type=int, default=30, help="获取条数")
    args = parser.parse_args()

    test_with_urllib(args.code, args.lmt)


if __name__ == "__main__":
    main()
