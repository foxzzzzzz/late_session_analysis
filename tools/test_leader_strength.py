"""龙头效应 (leader_strength) 数据源独立测试工具

用途: 测试能否获取板块成分股实时数据，计算个股在板块内的排名 (市值/涨幅/成交额)。
用法: python tools/test_leader_strength.py [--sectors 电子元件,软件开发,汽车零部件] [--top-n 10]

输出解读:
  - 板块成分股数量、数据覆盖率
  - 指定个股的板块内排名百分位 (top 10%/25%/50%)
  - 排名百分位越小越好 (top 5% 表示龙头/前排)
"""
import argparse
import logging
import sys
import os
import time
import random
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger(__name__)

import pandas as pd

DEFAULT_SECTORS = ['电子元件', '软件开发', '汽车零部件', '化学制药', '通信设备']


def get_sector_constituents(sector: str) -> Optional[pd.DataFrame]:
    """获取板块成分股 (akshare东财 + 缓存兜底)"""
    try:
        import akshare as ak
        df = ak.stock_board_industry_cons_em(symbol=sector)
        if df is not None and not df.empty:
            return df
    except Exception as e:
        logger.warning(f"akshare stock_board_industry_cons_em({sector}) 失败: {e}")

    # 兜底: baostock缓存
    cache_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        'data', 'sector_constituents_cache.json'
    )
    if os.path.exists(cache_path):
        try:
            import json
            with open(cache_path, 'r', encoding='utf-8') as f:
                cache = json.load(f)
            industries = cache.get('industries', {})
            for ind_name, stocks in industries.items():
                if sector in ind_name:
                    logger.info(f"从baostock缓存匹配 [{sector}]: {ind_name} → {len(stocks)} 只")
                    return pd.DataFrame({'代码': stocks})
        except Exception as e:
            logger.warning(f"baostock缓存匹配失败: {e}")

    return None


def get_stock_batch_data(codes: list[str]) -> Optional[pd.DataFrame]:
    """批量获取股票实时行情 (腾讯财经)"""
    try:
        # 腾讯批量接口
        import requests
        code_list = []
        for c in codes:
            if c.startswith('6'):
                code_list.append(f"sh{c}")
            else:
                code_list.append(f"sz{c}")

        url = "http://qt.gtimg.cn/q=" + ",".join(code_list)
        r = requests.get(url, timeout=15)
        r.encoding = 'gbk'

        results = []
        for line in r.text.strip().split('\n'):
            if '="' not in line:
                continue
            try:
                data_str = line.split('="')[1].rstrip('";')
                fields = data_str.split('~')
                if len(fields) < 50:
                    continue
                code_full = fields[2]
                code = code_full[2:] if len(code_full) > 2 else code_full
                name = fields[1]
                price = float(fields[3]) if fields[3] else 0.0
                change_pct = float(fields[32]) if fields[32] else 0.0
                market_cap = float(fields[45]) if len(fields) > 45 and fields[45] else 0.0  # 总市值
                turnover = float(fields[37]) if len(fields) > 37 and fields[37] else 0.0   # 成交额(万)
                pe = float(fields[39]) if len(fields) > 39 and fields[39] else 0.0
                results.append({
                    'code': code, 'name': name, 'price': price,
                    'change_pct': change_pct, 'market_cap': market_cap,
                    'turnover': turnover, 'pe': pe,
                })
            except (ValueError, IndexError) as e:
                continue
        return pd.DataFrame(results)
    except Exception as e:
        logger.warning(f"腾讯批量行情失败: {e}")
        return None


def compute_rankings(
    constituents_df: pd.DataFrame,
    market_df: pd.DataFrame,
    focus_code: str = None,
) -> dict:
    """计算板块内排名"""
    if constituents_df.empty or market_df is None or market_df.empty:
        return {"error": "无数据"}

    # 合并板块成分股与实时行情
    code_col_c = next(
        (c for c in constituents_df.columns if c in ('代码', 'code')),
        constituents_df.columns[0],
    )
    codes_in_sector = constituents_df[code_col_c].astype(str).str.strip().tolist()

    merged = market_df[market_df['code'].isin(codes_in_sector)].copy()
    if merged.empty:
        return {
            "error": f"板块 {len(codes_in_sector)} 只成分股无一可获取实时行情",
            "total_in_sector": len(codes_in_sector),
        }

    total = len(codes_in_sector)
    covered = len(merged)

    result = {
        "total_in_sector": total,
        "covered": covered,
        "coverage_pct": covered / max(total, 1) * 100,
    }

    # 市值排名 (越大越好 = 龙头)
    if merged['market_cap'].gt(0).any():
        merged['cap_rank'] = merged['market_cap'].rank(ascending=False, method='min')
        merged['cap_rank_pct'] = merged['cap_rank'] / len(merged) * 100

        top3_cap = merged.nsmallest(3, 'cap_rank')
        result['top3_by_cap'] = [
            {
                'code': r['code'],
                'name': r['name'],
                'cap_yuan': r['market_cap'] * 1e8,  # 亿→元
                'rank_pct': f"{r['cap_rank_pct']:.1f}%",
            }
            for _, r in top3_cap.iterrows()
        ]

    # 涨幅排名
    merged['change_rank'] = merged['change_pct'].rank(ascending=False, method='min')
    merged['change_rank_pct'] = merged['change_rank'] / len(merged) * 100

    top3_change = merged.nlargest(3, 'change_pct')
    result['top3_by_change'] = [
        {
            'code': r['code'],
            'name': r['name'],
            'pct': f"{r['change_pct']:.2f}%",
            'rank_pct': f"{r['change_rank_pct']:.1f}%",
        }
        for _, r in top3_change.iterrows()
    ]

    # 成交额排名
    if merged['turnover'].gt(0).any():
        merged['turnover_rank'] = merged['turnover'].rank(ascending=False, method='min')
        merged['turnover_rank_pct'] = merged['turnover_rank'] / len(merged) * 100

        top3_turnover = merged.nsmallest(3, 'turnover_rank')
        result['top3_by_turnover'] = [
            {
                'code': r['code'],
                'name': r['name'],
                'amount_wan': f"{r['turnover']:.0f}",
                'rank_pct': f"{r['turnover_rank_pct']:.1f}%",
            }
            for _, r in top3_turnover.iterrows()
        ]

    # 如果指定了关注代码，输出其排名
    if focus_code and focus_code in merged['code'].values:
        row = merged[merged['code'] == focus_code].iloc[0]
        result['focus'] = {
            'code': focus_code,
            'name': row['name'],
            'cap_rank_pct': f"{row.get('cap_rank_pct', 0):.1f}%",
            'change_rank_pct': f"{row.get('change_rank_pct', 0):.1f}%",
            'turnover_rank_pct': f"{row.get('turnover_rank_pct', 0):.1f}%",
            'is_leader': row.get('cap_rank_pct', 100) <= 10
                         or row.get('change_rank_pct', 100) <= 10
                         or row.get('turnover_rank_pct', 100) <= 10,
        }

    return result


def main():
    parser = argparse.ArgumentParser(description='龙头效应数据源测试')
    parser.add_argument(
        '--sectors', type=str, default=','.join(DEFAULT_SECTORS),
        help=f'测试板块名称，逗号分隔 (默认: {",".join(DEFAULT_SECTORS)})',
    )
    parser.add_argument(
        '--top-n', type=int, default=10,
        help='每个板块输出排名前N的龙头 (默认: 10)',
    )
    parser.add_argument(
        '--focus', type=str, default='',
        help='指定关注的股票代码，查其在板块内的排名',
    )
    args = parser.parse_args()

    sectors = [s.strip() for s in args.sectors.split(',') if s.strip()]

    print('=' * 65)
    print(f'  龙头效应 (leader_strength) 数据源测试')
    print(f'  目标板块: {sectors}')
    print(f'  关注股票: {args.focus or "(无)"}')
    print('=' * 65)

    total_components = 0
    total_covered = 0
    all_codes = []

    for sector in sectors:
        print(f'\n{"─" * 65}')
        print(f'  >>> 板块: {sector}')
        print(f'{"─" * 65}')

        # 1. 获取板块成分股
        t0 = time.time()
        df_constituents = get_sector_constituents(sector)
        if df_constituents is None or df_constituents.empty:
            print(f'  [FAIL] 无法获取 {sector} 成分股')
            continue

        code_col = next(
            (c for c in df_constituents.columns if c in ('代码', 'code')),
            df_constituents.columns[0],
        )
        codes_in_sector = df_constituents[code_col].astype(str).str.strip().tolist()
        print(f'  成分股: {len(codes_in_sector)} 只 (耗时 {time.time() - t0:.1f}s)')

        total_components += len(codes_in_sector)
        all_codes.extend(codes_in_sector)

        # 2. 批量获取实时行情
        t1 = time.time()
        # 分批获取 (腾讯单次URL长度限制)
        batch_size = 50
        all_market_data = []
        for i in range(0, len(codes_in_sector), batch_size):
            batch_codes = codes_in_sector[i:i + batch_size]
            df_batch = get_stock_batch_data(batch_codes)
            if df_batch is not None and not df_batch.empty:
                all_market_data.append(df_batch)
            time.sleep(0.3)  # 腾讯接口限流

        if not all_market_data:
            print(f'  [FAIL] 无法获取任何实时行情')
            continue

        market_df = pd.concat(all_market_data, ignore_index=True)
        print(f'  实时行情: 获取到 {len(market_df)}/{len(codes_in_sector)} 只 '
              f'(覆盖率 {len(market_df) / max(len(codes_in_sector), 1) * 100:.1f}%, '
              f'耗时 {time.time() - t1:.1f}s)')

        total_covered += len(market_df)

        # 3. 计算板块内排名
        result = compute_rankings(df_constituents, market_df, args.focus)
        if "error" in result:
            print(f'  [FAIL] {result["error"]}')
            continue

        print(f'\n  数据覆盖率: {result["coverage_pct"]:.1f}% ({result["covered"]}/{result["total_in_sector"]})')

        # 输出龙头
        if 'top3_by_cap' in result:
            print(f'\n  --- 市值龙头 (Top 3) ---')
            for r in result['top3_by_cap']:
                print(f'  {r["code"]} {r["name"]:<8} 市值排名 {r["rank_pct"]}')

        if 'top3_by_change' in result:
            print(f'\n  --- 涨幅龙头 (Top 3) ---')
            for r in result['top3_by_change']:
                print(f'  {r["code"]} {r["name"]:<8} {r["pct"]} 涨幅排名 {r["rank_pct"]}')

        if 'top3_by_turnover' in result:
            print(f'\n  --- 成交额龙头 (Top 3) ---')
            for r in result['top3_by_turnover']:
                print(f'  {r["code"]} {r["name"]:<8} {r["amount_wan"]}万 成交排名 {r["rank_pct"]}')

        # 关注股票排名
        if 'focus' in result:
            f = result['focus']
            print(f'\n  ★ 关注股票 {f["code"]} {f["name"]}:')
            print(f'    市值排名: {f["cap_rank_pct"]} | 涨幅排名: {f["change_rank_pct"]} | '
                  f'成交额排名: {f["turnover_rank_pct"]}')
            print(f'    判定: {"[LEADER] 龙头" if f["is_leader"] else "[NOT LEADER] 非龙头"}')

    # === 汇总 ===
    print(f'\n{"=" * 65}')
    print(f'  汇总')
    print(f'{"=" * 65}')
    print(f'  测试板块: {len(sectors)}')
    print(f'  成分股合计: {total_components} 只')
    print(f'  行情获取: {total_covered}/{total_components} '
          f'({total_covered / max(total_components, 1) * 100:.1f}%)')

    # === 结论 ===
    print(f'\n{"=" * 65}')
    print(f'  结论')
    print(f'{"=" * 65}')

    if total_covered > 0:
        coverage = total_covered / max(total_components, 1) * 100
        if coverage > 50:
            print(f'  [OK] 板块内排名计算可行 (覆盖率 {coverage:.1f}%)')
            print(f'  建议: 在 S3 阶段利用 S0 已有 sector_map 做板块内排名')
            print(f'  实现: 对每只通过S2的股票，查询其板块成分股行情，计算排名百分位')
            print(f'  阈值: cap_rank_pct ≤ 30 或 change_rank_pct ≤ 20 视为 leader')
        else:
            print(f'  [WARN] 数据覆盖率偏低 ({coverage:.1f}%), 龙头判定可能不准')
            print(f'  建议: 扩大数据源范围 或 仅对覆盖率高的板块启用')
    else:
        print(f'  [FAIL] 无法获取任何实时行情数据')
        print(f'  建议: 关注 akshare stock_board_industry_spot_em() 作为替代')
        print(f'        付费方案: 同花顺iFinD / Wind板块成分股行情')


if __name__ == '__main__':
    main()
