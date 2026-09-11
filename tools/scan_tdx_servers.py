"""全量扫描通达信服务器, 找到真正能返回 K 线的

背景: 部分服务器能连、能查证券列表, 但 K 线返回空 (需换台)。
用法: python tools/scan_tdx_servers.py [--write]
      --write 找到可用服务器后写入 ~/.mootdx/config.json
"""
import sys
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed


def collect_servers():
    """汇总所有已知服务器地址"""
    servers = []

    # tdxpy 内置 (104个)
    try:
        from tdxpy.constants import hq_hosts
        servers.extend([(addr, port) for _, addr, port in hq_hosts])
    except ImportError:
        pass

    # mootdx 内置
    try:
        from mootdx.consts import HQ_HOSTS
        servers.extend([(addr, port) for _, addr, port in HQ_HOSTS])
    except ImportError:
        pass

    # 社区推荐的备用服务器 (来自搜索结果/Easy-tdx)
    servers.extend([
        ('119.147.212.81', 7709),
        ('119.147.212.82', 7709),
        ('202.100.166.27', 7709),
        ('218.108.98.244', 7709),
        ('60.191.117.167', 7709),
        ('114.80.149.19', 7709),
        ('114.80.149.22', 7709),
        ('218.75.126.9', 7709),
        ('115.238.90.165', 7709),
        ('115.238.56.198', 7709),
        ('124.160.88.183', 7709),
        ('180.153.39.51', 7709),
        ('218.6.170.47', 7709),
        ('123.125.108.14', 7709),
        ('180.153.18.170', 7709),
        ('180.153.18.171', 7709),
        ('202.108.253.130', 7709),
        ('202.108.253.131', 7709),
        ('60.28.29.69', 7709),
        ('218.60.29.136', 7709),
        ('115.238.90.165', 7709),
        ('218.108.98.244', 7709),
    ])

    # 去重
    return list(dict.fromkeys(servers))


def test_server(addr, port):
    """测试单个服务器: 必须返回真实 K 线数据才算可用"""
    try:
        from pytdx.hq import TdxHq_API
    except ImportError:
        return None

    api = TdxHq_API()
    try:
        with api.connect(addr, port, time_out=5):
            # 先看证券数 (快)
            cnt = api.get_security_count(0)
            if not cnt:
                return {'addr': addr, 'port': port, 'status': 'no_count', 'bars': 0}

            # 关键: 必须能拿到日线
            bars = api.get_security_bars(9, 0, '000001', 0, 5)
            n = len(bars) if bars else 0
            if n > 0:
                return {'addr': addr, 'port': port, 'status': 'OK', 'bars': n, 'count': cnt}
            return {'addr': addr, 'port': port, 'status': 'no_bars', 'bars': 0, 'count': cnt}
    except Exception as e:
        return {'addr': addr, 'port': port, 'status': 'error', 'err': type(e).__name__}


def main():
    servers = collect_servers()
    print(f"扫描 {len(servers)} 个通达信服务器 (并发 20, 每个 5s 超时)...\n")

    ok_servers = []
    no_bars = 0
    errors = 0

    with ThreadPoolExecutor(max_workers=20) as ex:
        futures = {ex.submit(test_server, a, p): (a, p) for a, p in servers}
        done = 0
        for f in as_completed(futures):
            done += 1
            r = f.result()
            if r is None:
                continue
            if r['status'] == 'OK':
                ok_servers.append(r)
                print(f"  [OK] {r['addr']}:{r['port']} — K线 {r['bars']} 条, 证券数 {r['count']}")
            elif r['status'] == 'no_bars':
                no_bars += 1
            else:
                errors += 1
            if done % 30 == 0:
                print(f"  ...进度 {done}/{len(servers)}")

    print(f"\n{'='*50}")
    print(f"  K线可用: {len(ok_servers)}")
    print(f"  能连但K线空: {no_bars}")
    print(f"  连不上: {errors}")
    print('='*50)

    if not ok_servers:
        print("\n[结论] 所有服务器 K 线均返回空 —— 可能是通达信协议变更, 需要升级库")
        return 1

    # 按响应速度排序, 选第一个
    best = ok_servers[0]
    print(f"\n推荐服务器: {best['addr']}:{best['port']}")

    if '--write' in sys.argv:
        cfg_dir = os.path.expanduser('~/.mootdx')
        os.makedirs(cfg_dir, exist_ok=True)
        cfg = {
            'SERVER': {'HQ': [['scan', best['addr'], best['port']]]},
            'BESTIP': {'HQ': [best['addr'], best['port']], 'EX': '', 'GP': ''},
        }
        with open(os.path.join(cfg_dir, 'config.json'), 'w') as fp:
            json.dump(cfg, fp)
        print(f"已写入 {cfg_dir}/config.json")
    else:
        print("(添加 --write 参数可写入配置)")

    return 0


if __name__ == '__main__':
    sys.exit(main())
