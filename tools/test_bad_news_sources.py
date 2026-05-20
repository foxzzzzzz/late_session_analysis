"""利空公告/新闻数据源独立测试工具

用途: 测试各数据源能否提供个股近期公告和新闻，以便在 L3 阶段检查 has_bad_news。
用法: python tools/test_bad_news_sources.py [--codes 600519,000858,300750] [--days 7]

数据源:
  1. akshare stock_news_em      — 东方财富个股新闻 (当日)
  2. akshare stock_notice_report — 巨潮资讯公告 (按日查询)
  3. akshare stock_individual_notice_report — 巨潮资讯个股公告 (按代码+日期范围)

输出解读:
  - 每个源返回条数、日期范围、是否可解析
  - 手动标注是否有明显利空关键词 (减持/违规/亏损/立案/监管函/退市风险等)
"""
import argparse
import logging
import sys
import os
import io
from datetime import datetime, timedelta

# Fix Windows GBK console encoding
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger(__name__)

import akshare as ak
import pandas as pd

DEFAULT_CODES = ['600519', '000858', '300750', '002230', '000063']

# 利空关键词 — 匹配到任何一个就标记为利空
NEGATIVE_KEYWORDS = [
    '减持', '违规', '亏损', '立案', '监管', '警示', '处罚',
    '退市', '破产', '清算', '诉讼', '仲裁', '冻结', '质押',
    '业绩预亏', '商誉减值', '资产减值', '计提', '大额坏账',
    'ST', '*ST', '终止上市', '暂停上市', '问询函', '关注函',
    '重大不利', '可能导致', '无法表示意见', '保留意见',
    '重组失败', '终止重组', '取消', '下修业绩', '业绩变脸',
]


def test_stock_news_em(code: str) -> pd.DataFrame:
    """测试东方财富个股新闻"""
    print(f"\n  [1] stock_news_em({code})")
    try:
        df = ak.stock_news_em(symbol=code)
        if df is not None and not df.empty:
            print(f"      返回 {len(df)} 行, 列: {list(df.columns)}")
            return df
        else:
            print(f"      返回空数据")
            return pd.DataFrame()
    except Exception as e:
        print(f"      [FAIL] {e}")
        return pd.DataFrame()


def test_stock_notice_report(code: str, date_str: str) -> pd.DataFrame:
    """测试巨潮资讯公告 (按日期查全市场)"""
    print(f"\n  [2] stock_notice_report(date={date_str}) — 全市场公告")
    try:
        df = ak.stock_notice_report(symbol="全部", date=date_str)
        if df is not None and not df.empty:
            # 筛选目标股票
            code_col = None
            for c in df.columns:
                if '代码' in str(c) or 'code' in str(c).lower():
                    code_col = c
                    break
            if code_col:
                matched = df[df[code_col].astype(str).str.contains(code)]
                print(f"      全市场 {len(df)} 条, 匹配到 {code} 的 {len(matched)} 条")
                print(f"      列: {list(df.columns)}")
                return matched
            else:
                print(f"      全市场 {len(df)} 条 (无代码列无法筛选), 列: {list(df.columns)}")
                return df.head(10)  # 返回前10条供查看
        else:
            print(f"      返回空数据")
            return pd.DataFrame()
    except Exception as e:
        print(f"      [FAIL] {e}")
        return pd.DataFrame()


def test_individual_notice(code: str, days: int) -> pd.DataFrame:
    """测试巨潮资讯个股公告 (按代码+日期范围)"""
    begin = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    end = datetime.now().strftime("%Y-%m-%d")
    print(f"\n  [3] stock_individual_notice_report(security={code}, begin={begin}, end={end})")
    try:
        df = ak.stock_individual_notice_report(
            security=code,
            symbol="全部",
            begin_date=begin,
            end_date=end,
        )
        if df is not None and not df.empty:
            print(f"      返回 {len(df)} 行, 列: {list(df.columns)}")
            return df
        else:
            print(f"      返回空数据")
            return pd.DataFrame()
    except Exception as e:
        print(f"      [FAIL] {e}")
        return pd.DataFrame()


def analyze_news_sentiment(df: pd.DataFrame, stock_name: str = "") -> dict:
    """分析新闻/公告DataFrame中的利空信号"""
    if df.empty:
        return {"total": 0, "negative": 0, "positive_mentions": 0, "negative_items": []}

    # 找到标题/内容列
    text_cols = []
    for c in df.columns:
        c_lower = str(c).lower()
        if any(kw in c_lower for kw in ['title', '标题', 'content', '内容', 'name', '名称', 'subject']):
            text_cols.append(c)

    negative_count = 0
    negative_items = []
    for _, row in df.iterrows():
        row_text = " ".join(str(row.get(c, "")) for c in df.columns)
        matched_kw = [kw for kw in NEGATIVE_KEYWORDS if kw in row_text]
        if matched_kw:
            negative_count += 1
            # 提取标题
            title = ""
            for c in text_cols:
                title = str(row.get(c, ""))
                if title:
                    break
            negative_items.append({
                "title": title[:80] if title else "(无标题)",
                "keywords": matched_kw,
            })

    return {
        "total": len(df),
        "negative": negative_count,
        "negative_items": negative_items,
    }


def main():
    parser = argparse.ArgumentParser(description='利空公告/新闻数据源独立测试')
    parser.add_argument(
        '--codes', type=str, default=','.join(DEFAULT_CODES),
        help=f'测试股票代码，逗号分隔 (默认: {",".join(DEFAULT_CODES)})',
    )
    parser.add_argument(
        '--days', type=int, default=7,
        help='查询最近N天的公告/新闻 (默认: 7)',
    )
    parser.add_argument(
        '--source', type=str, default='all',
        choices=['all', 'news_em', 'notice_report', 'individual_notice'],
        help='指定测试数据源 (默认: all)',
    )
    args = parser.parse_args()

    codes = [c.strip() for c in args.codes.split(',') if c.strip()]
    today = datetime.now()
    dates_to_test = [
        (today - timedelta(days=i)).strftime("%Y%m%d")
        for i in range(min(args.days, 7))
    ]

    print('=' * 65)
    print(f'  利空公告/新闻数据源测试')
    print(f'  测试股票: {codes}')
    print(f'  测试天数: {args.days}')
    print(f'  当前日期: {today.strftime("%Y-%m-%d %H:%M")}')
    print('=' * 65)

    summary = {}

    for code in codes:
        print(f'\n{"─" * 65}')
        print(f'  >>> 测试 {code}')
        print(f'{"─" * 65}')

        code_summary = {"news_em": 0, "notice_report": 0, "individual_notice": 0, "total_negative": 0}

        # === 源1: 东方财富个股新闻 ===
        if args.source in ('all', 'news_em'):
            df_news = test_stock_news_em(code)
            if not df_news.empty:
                result = analyze_news_sentiment(df_news, code)
                code_summary["news_em"] = result["total"]
                code_summary["total_negative"] += result["negative"]
                if result["negative"] > 0:
                    print(f"      [!] 发现 {result['negative']}/{result['total']} 条疑似利空:")
                    for item in result["negative_items"][:5]:
                        print(f"        - {item['title']}")
                        print(f"          关键词: {', '.join(item['keywords'])}")
                elif result["total"] > 0:
                    print(f"      [OK] {result['total']} 条新闻, 未检测到利空关键词")
                print(f"      日期范围: {df_news.iloc[0].get(list(df_news.columns)[0], '?')} ~ {df_news.iloc[-1].get(list(df_news.columns)[0], '?')}")
                # 打印前3行样本
                print(f"      --- 前3行样本 ---")
                pd.set_option('display.max_columns', 6)
                pd.set_option('display.width', 120)
                for i, row in df_news.head(3).iterrows():
                    print(f"      [{i}] {' | '.join(str(v)[:60] for v in row.values[:4])}")

        # === 源2: 巨潮资讯公告 (按日期) ===
        if args.source in ('all', 'notice_report'):
            total_matched = 0
            for date_str in dates_to_test[:3]:  # 最多测3天
                df_notice = test_stock_notice_report(code, date_str)
                total_matched += len(df_notice)
            code_summary["notice_report"] = total_matched

        # === 源3: 巨潮资讯个股公告 ===
        if args.source in ('all', 'individual_notice'):
            df_indiv = test_individual_notice(code, args.days)
            if not df_indiv.empty:
                result = analyze_news_sentiment(df_indiv, code)
                code_summary["individual_notice"] = result["total"]
                code_summary["total_negative"] += result["negative"]
                if result["negative"] > 0:
                    print(f"      ⚠ 发现 {result['negative']}/{result['total']} 条疑似利空:")
                    for item in result["negative_items"][:5]:
                        print(f"        - {item['title']}")
                        print(f"          关键词: {', '.join(item['keywords'])}")
                elif result["total"] > 0:
                    print(f"      [OK] {result['total']} 条公告, 未检测到利空")

        summary[code] = code_summary

    # === 汇总 ===
    print(f'\n{"=" * 65}')
    print(f'  数据源可用性汇总')
    print(f'{"=" * 65}')
    print(f'  {"代码":<10} {"东方财富新闻":>12} {"巨潮公告(按日)":>14} {"个股公告":>10} {"利空条目":>10}')
    print(f'  {"─" * 10} {"─" * 12} {"─" * 14} {"─" * 10} {"─" * 10}')
    for code, s in summary.items():
        print(f'  {code:<10} {s["news_em"]:>12} {s["notice_report"]:>14} {s["individual_notice"]:>10} {s["total_negative"]:>10}')

    # === 结论 ===
    print(f'\n{"=" * 65}')
    print(f'  结论')
    print(f'{"=" * 65}')

    total_news = sum(s["news_em"] for s in summary.values())
    total_notice = sum(s["notice_report"] for s in summary.values())
    total_indiv = sum(s["individual_notice"] for s in summary.values())

    if total_news > 0:
        print(f'  [OK] 东方财富个股新闻可用 (合计 {total_news} 条) — 推荐优先使用')
    else:
        print(f'  [FAIL] 东方财富个股新闻不可用')

    if total_indiv > 0:
        print(f'  [OK] 巨潮资讯个股公告可用 (合计 {total_indiv} 条) — 推荐作为补充')
    elif total_notice > 0:
        print(f'  [WARN] 巨潮资讯个股公告不可用，但按日全市场查询可用 (需筛选)')
    else:
        print(f'  [FAIL] 巨潮资讯公告接口均不可用')

    if total_news > 0 or total_indiv > 0:
        print(f'\n  建议: 集成可用数据源到 pipeline._enrich_contexts(), 写入 ctx.has_bad_news')
        print(f'  检测策略: 标题+内容关键词匹配 (减持/违规/亏损/立案/监管/退市等)')
    else:
        print(f'\n  建议: 考虑付费数据源 (Wind/CHOICE/同花顺iFinD) 或爬取交易所公告页面')


if __name__ == '__main__':
    main()
