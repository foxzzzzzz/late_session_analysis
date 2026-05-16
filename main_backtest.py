#!/usr/bin/env python3
"""尾盘策略回测系统 — CLI入口

用法:
  python main_backtest.py                           # 使用默认日期范围
  python main_backtest.py --start 20260401 --end 20260515
  python main_backtest.py --start 20260501 --end 20260515 --no-cache
  python main_backtest.py --sectors 半导体,电子元件 --output-dir ./my_reports
"""
import sys
import os
import argparse
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

from backtest.config import BacktestConfig
from backtest.engine import BacktestEngine

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger("backtest")


def parse_args():
    parser = argparse.ArgumentParser(
        description="尾盘策略回测系统 — 逐日循环 S1→S2→S3→S4，T+1 退出模拟",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python main_backtest.py                                    # 默认区间
  python main_backtest.py --start 20260401 --end 20260515    # 指定区间
  python main_backtest.py --start 20260501 --end 20260515 --no-cache  # 强制刷新缓存
  python main_backtest.py --sectors 半导体,电子元件           # 自定义板块
        """,
    )
    parser.add_argument('--start', type=str, default='',
                        help='回测起始日期 YYYYMMDD (默认从.env/BT_START_DATE读取)')
    parser.add_argument('--end', type=str, default='',
                        help='回测结束日期 YYYYMMDD (默认从.env读取)')
    parser.add_argument('--output-dir', type=str, default='',
                        help='报告输出目录 (默认 ./backtest_reports)')
    parser.add_argument('--cache-dir', type=str, default='',
                        help='缓存目录 (默认 ./backtest_cache)')
    parser.add_argument('--no-cache', action='store_true',
                        help='禁用数据缓存，强制重新拉取')
    parser.add_argument('--no-5min', action='store_true',
                        help='禁用5分钟线精确数据，使用近似公式')
    parser.add_argument('--capital-flow-mode', type=str, default='',
                        choices=['none', 'estimated'],
                        help='资金流模式: none=停用, estimated=估算 (默认 none)')
    parser.add_argument('--sectors', type=str, default='',
                        help='覆盖 TARGET_SECTORS，逗号分隔，如: 半导体,电子元件')
    parser.add_argument('--max-positions', type=int, default=0,
                        help='每日最大持仓数 (默认 5)')
    parser.add_argument('--slippage', type=float, default=0,
                        help='滑点 bps (默认 5.0)')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='详细日志 (DEBUG级别)')
    return parser.parse_args()


def main():
    args = parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
        logging.getLogger("backtest").setLevel(logging.DEBUG)

    # 构建配置
    config = BacktestConfig()

    if args.start:
        config.start_date = args.start
    if args.end:
        config.end_date = args.end
    if args.output_dir:
        config.output_dir = args.output_dir
    if args.cache_dir:
        config.cache_dir = args.cache_dir
    if args.no_cache:
        config.no_cache = True
    if args.no_5min:
        config.use_5min_data = False
    if args.capital_flow_mode:
        config.capital_flow_mode = args.capital_flow_mode
    if args.sectors:
        config.target_sectors = [s.strip() for s in args.sectors.split(',') if s.strip()]
    if args.max_positions > 0:
        config.max_positions = args.max_positions
    if args.slippage > 0:
        config.slippage_bps = args.slippage

    # 验证必要配置
    if not config.target_sectors:
        logger.error("未配置 TARGET_SECTORS，请在 .env 中设置或用 --sectors 指定")
        sys.exit(1)

    # 打印回测配置
    print("\n" + "=" * 60)
    print("  尾盘策略回测系统")
    print("=" * 60)
    print(f"  回测区间: {config.start_date} → {config.end_date}")
    print(f"  目标板块: {', '.join(config.target_sectors)}")
    print(f"  5分钟线: {'启用' if config.use_5min_data else '禁用(近似)'}")
    print(f"  资金流: {config.capital_flow_mode}")
    print(f"  滑点: {config.slippage_bps}bps  佣金: {config.commission_rate*100:.3f}%")
    print(f"  最大持仓: {config.max_positions}只/天")
    print(f"  缓存目录: {config.cache_dir}")
    print(f"  输出目录: {config.output_dir}")
    print("=" * 60 + "\n")

    # 执行回测
    engine = BacktestEngine(config)
    result = engine.run()

    # 打印结果
    metrics = result["metrics"]
    print("\n" + "=" * 60)
    print("  回测结果")
    print("=" * 60)
    print(f"  回测天数: {result['total_days']}")
    print(f"  总交易笔数: {result['total_trades']}")
    print(f"  耗时: {result['elapsed']:.0f}s")
    print(f"  报告目录: {result['report_path']}")
    print()

    if isinstance(metrics, dict) and metrics.get("total_trades", 0) > 0:
        print(f"  胜率:        {metrics.get('win_rate', '-')}%")
        print(f"  平均收益率:  {metrics.get('avg_return_pct', '-')}%")
        print(f"  累计收益率:  {metrics.get('total_return_pct', '-')}%")
        print(f"  最大回撤:    {metrics.get('max_drawdown_pct', '-')}%")
        print(f"  夏普比率:    {metrics.get('sharpe_ratio', '-')}")
        print(f"  Calmar比率:  {metrics.get('calmar_ratio', '-')}")
        print(f"  盈亏因子:    {metrics.get('profit_factor', '-')}")
        print(f"  最长连胜:    {metrics.get('max_consecutive_wins', '-')}")
        print(f"  最长连亏:    {metrics.get('max_consecutive_losses', '-')}")
        print("=" * 60)
    else:
        print("  (无有效交易)")
        print("=" * 60)


if __name__ == '__main__':
    main()
