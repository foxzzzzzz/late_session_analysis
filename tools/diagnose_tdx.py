"""通达信 K 线接口诊断 — 区分库问题 vs 服务器限制

用法: python tools/diagnose_tdx.py
"""
import sys


def sep(title):
    print(f"\n{'='*50}")
    print(f"  {title}")
    print('='*50)


def test_mootdx_raw():
    """直连底层 API, 看各接口返回"""
    sep("1. mootdx 底层 API")
    from mootdx.quotes import Quotes
    q = Quotes.factory()
    print(f"Server: {q.server}")

    # 连接
    try:
        q.client.connect(*q.server)
        print("[OK] connect")
    except Exception as e:
        print(f"[FAIL] connect: {e}")
        return

    # 证券数量 (已知可用)
    try:
        print(f"[OK] 深市证券数: {q.client.get_security_count(0)}")
        print(f"[OK] 沪市证券数: {q.client.get_security_count(1)}")
    except Exception as e:
        print(f"[FAIL] get_security_count: {e}")

    # 证券列表
    try:
        lst = q.client.get_security_list(0, 0)
        print(f"[{'OK' if lst else 'EMPTY'}] get_security_list: {len(lst) if lst else 0} 条")
    except Exception as e:
        print(f"[FAIL] get_security_list: {e}")

    # 实时行情 —— 关键测试点
    try:
        quotes = q.client.get_security_quotes([(0, '000001')])
        print(f"[{'OK' if quotes else 'EMPTY'}] get_security_quotes: {quotes}")
    except Exception as e:
        print(f"[FAIL] get_security_quotes: {e}")

    # K线 —— 当前失败的接口
    for cat, name in [(4, '日线'), (8, '5分钟'), (0, '5分钟(旧)'), (9, '日线(旧)')]:
        try:
            r = q.client.get_security_bars(cat, 0, '000001', 0, 5)
            status = f"{len(r)} 条" if r else "EMPTY/None"
            print(f"[{cat}] {name}: {status}")
        except Exception as e:
            print(f"[FAIL] {cat} {name}: {e}")


def test_mootdx_highlevel():
    """mootdx 高层接口"""
    sep("2. mootdx 高层接口 bars()")
    from mootdx.quotes import Quotes
    q = Quotes.factory()
    for cat, name in [(4, '日线'), (8, '5分钟线')]:
        try:
            d = q.bars(symbol='000001', category=cat, offset=5)
            n = len(d) if d is not None else 0
            print(f"[{cat}] {name}: {n} 行")
        except Exception as e:
            print(f"[FAIL] {cat}: {e}")


def test_pytdx():
    """pytdx 作为对照库"""
    sep("3. pytdx 对照测试")
    try:
        from pytdx.hq import TdxHq_API
    except ImportError:
        print("[SKIP] pytdx 未安装")
        return

    servers = [
        ('115.238.56.198', 7709),
        ('218.6.170.47', 7709),
        ('123.125.108.14', 7709),
        ('180.153.18.170', 7709),
    ]
    for ip, port in servers:
        api = TdxHq_API()
        try:
            with api.connect(ip, port, time_out=5):
                cnt = api.get_security_count(0)
                bars = api.get_security_bars(9, 0, '000001', 0, 5)
                n = len(bars) if bars else 0
                print(f"  {ip}:{port} 证券数={cnt} K线={n}条")
        except Exception as e:
            print(f"  {ip}:{port} FAIL: {type(e).__name__}: {e}")


def test_sina_fallback():
    """新浪 HTTP 回退是否可用"""
    sep("4. 新浪 HTTP K线 (回退方案)")
    import urllib.request
    url = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "CN_MarketData.getKLineData?symbol=sz000001&scale=240&ma=no&datalen=5")
    try:
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "Mozilla/5.0")
        r = urllib.request.urlopen(req, timeout=10)
        body = r.read().decode('utf-8', errors='ignore')
        print(f"[OK] 日线: {len(body)} bytes, 前120字符: {body[:120]}")
    except Exception as e:
        print(f"[FAIL] 日线: {e}")

    # 5分钟线
    url5 = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            "CN_MarketData.getKLineData?symbol=sz000001&scale=5&ma=no&datalen=5")
    try:
        req = urllib.request.Request(url5)
        req.add_header("User-Agent", "Mozilla/5.0")
        r = urllib.request.urlopen(req, timeout=10)
        body = r.read().decode('utf-8', errors='ignore')
        print(f"[OK] 5分钟线: {len(body)} bytes, 前120字符: {body[:120]}")
    except Exception as e:
        print(f"[FAIL] 5分钟线: {e}")


if __name__ == '__main__':
    print("通达信 K 线接口诊断")
    print(f"Python: {sys.version.split()[0]}")

    for fn in [test_mootdx_raw, test_mootdx_highlevel, test_pytdx, test_sina_fallback]:
        try:
            fn()
        except Exception as e:
            print(f"\n[ERROR] {fn.__name__}: {e}")

    print("\n" + "="*50)
    print("  诊断完成")
    print("="*50)
