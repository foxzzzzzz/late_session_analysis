"""验证真实HTTP降级路径，不运行选股/交易、不修改TDX池或市场防抖状态。"""
import argparse
import json
import logging
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
from data_provider.http_kline import _now, fetch_index_daily
from data_provider.kline_provider import KlineProvider


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--codes', nargs='+', required=True, help='验收股票的6位代码')
    cli.add_argument('--output', type=Path, default=Path('tmp/http-fallback-acceptance.json'))
    args = cli.parse_args()
    load_dotenv(Path(__file__).resolve().parents[1] / '.env')
    logging.basicConfig(level=logging.INFO)
    index = fetch_index_daily()
    # 只在本次独立进程中模拟TDX不可用，验收实际load_5min_batch降级路径。
    with patch.object(KlineProvider, '_create_client', return_value=None):
        provider = KlineProvider(cache_dir='tmp/http-acceptance-cache')
        frames = provider.load_5min_batch(args.codes)
    report = {'tested_at': str(_now()), 'tdx_forced_offline': True,
              'index': None, 'minutes': {}, 'requested': args.codes}
    if index is not None:
        report['index'] = {'rows': len(index), 'latest': str(index.iloc[-1]['datetime']),
                           'close': float(index.iloc[-1]['close']), 'metadata': index.attrs}
    for code, df in frames.items():
        report['minutes'][code] = {
            'rows': len(df), 'first': str(df.iloc[0]['datetime']),
            'latest': str(df.iloc[-1]['datetime']), 'metadata': df.attrs,
            'metrics': provider.compute_late_metrics(df),
        }
    report['status'] = ('PASS' if index is not None and all(str(c).zfill(6) in frames for c in args.codes)
                        else 'FAIL')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    print(f"{report['status']}: 指数={index is not None}, 5分钟线={len(frames)}/{len(set(args.codes))}; {args.output}")
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    sys.exit(main())
