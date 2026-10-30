"""独立全量诊断TDX访问，比较两种客户端；不修改运行缓存或交易状态。"""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import importlib
from importlib.metadata import version, PackageNotFoundError
import json
from pathlib import Path
import socket
import struct
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from data_provider.tdx_server_pool import _collect_servers


def inspect_query(api, parser_class, args, hex_bytes):
    result = {}

    class CapturingParser(parser_class):
        def parseResponse(self, body):
            result.update(body_bytes=len(body), body_hex_prefix=bytes(body[:hex_bytes]).hex())
            if len(body) >= 2:
                result['declared_count'] = struct.unpack('<H', body[:2])[0]
            return super().parseResponse(body)

    try:
        parser = CapturingParser(api.client)
        parser.setParams(*args)
        rows = parser.call_api()
        result.update(status='ok' if rows else 'empty', rows=len(rows or []))
        if rows:
            result['latest'] = rows[-1].get('datetime')
    except Exception as exc:
        result.update(status='query_error', error=f'{type(exc).__name__}: {exc!r}')
    return result


def probe_backend(server, factory, parsers, queries, timeout, hex_bytes):
    results = []
    for spec in queries:
        api = None
        result = {'query': spec['name']}
        try:
            api = factory(auto_retry=False, raise_exception=True)
            if not api.connect(*server, time_out=timeout):
                raise ConnectionError('TDX connect returned False')
            result.update(inspect_query(api, parsers[spec['parser']], spec['args'], hex_bytes))
        except Exception as exc:
            original = getattr(exc, 'original_exception', None)
            result.update(status='connect_error', error=f'{type(exc).__name__}: {exc!r}',
                          original_error=repr(original))
        finally:
            if api is not None:
                try:
                    api.disconnect()
                except Exception as exc:
                    result['close_error'] = repr(exc)
        results.append(result)
    return results


def probe_server(server, config):
    result = {'host': server[0], 'port': server[1], 'backends': {}}
    try:
        with socket.create_connection(server, timeout=config['timeout_seconds']):
            result['tcp'] = 'ok'
    except Exception as exc:
        result.update(tcp='error', error=f'{type(exc).__name__}: {exc!r}')
        return result
    for backend in ('pytdx', 'tdxpy'):
        try:
            hq = importlib.import_module(f'{backend}.hq')
            factory = hq.TdxHq_API
            parsers = {
                'security': hq.GetSecurityBarsCmd,
                'index': hq.GetIndexBarsCmd,
            }
            result['backends'][backend] = probe_backend(
                server, factory, parsers, config['queries'],
                config['timeout_seconds'], config['hex_prefix_bytes'])
        except ImportError as exc:
            result['backends'][backend] = [{'status': 'import_error', 'error': repr(exc)}]
    return result


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--config', type=Path, default=Path(__file__).with_name('tdx_diagnostic_config.json'))
    cli.add_argument('--output', type=Path, default=Path('tmp/tdx-access-diagnostic.json'))
    cli.add_argument('--hosts', nargs='+', help='可选：只测指定IP:端口；默认扫描完整名单')
    args = cli.parse_args()
    config = json.loads(args.config.read_text(encoding='utf-8'))
    servers = (list(dict.fromkeys((host.rsplit(':', 1)[0], int(host.rsplit(':', 1)[1]))
                                for host in args.hosts)) if args.hosts else _collect_servers())
    versions = {}
    for package in ('pytdx', 'tdxpy', 'mootdx'):
        try:
            versions[package] = version(package)
        except PackageNotFoundError:
            versions[package] = None
    report = {'started_at': datetime.now().astimezone().isoformat(), 'versions': versions,
              'config': config, 'server_count': len(servers), 'results': []}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    print(f'诊断 {len(servers)} 台节点；结果写入 {args.output}', flush=True)
    with ThreadPoolExecutor(max_workers=config['workers']) as pool:
        for result in pool.map(lambda server: probe_server(server, config), servers):
            report['results'].append(result)
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            print(f"[{len(report['results'])}/{len(servers)}] {result['host']} TCP={result['tcp']}", flush=True)
    report['finished_at'] = datetime.now().astimezone().isoformat()
    report['summary'] = {
        'tcp': dict(Counter(r['tcp'] for r in report['results'])),
        'queries': dict(Counter(f"{backend}:{q['status']}" for r in report['results']
                               for backend, queries in r['backends'].items() for q in queries)),
    }
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report['summary'], ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    main()
