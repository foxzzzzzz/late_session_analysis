#!/usr/bin/env python3
"""尾盘策略回测系统 — CLI入口

基于历史数据 + 5分钟K线精确计算的策略回测验证系统。复用实盘管线 S1→S2→S3→S4 筛选逻辑，
用固定板块股票池替代全市场扫描，T+1 开盘卖出模拟。

用法:
  python main_backtest.py                                    # 默认日期区间 (.env)
  python main_backtest.py --start 20260401 --end 20260515    # 指定区间
  python main_backtest.py --sectors 半导体,电子元件           # 自定义板块
  python main_backtest.py --no-cache --no-5min               # 强制刷新+近似模式
  python main_backtest.py -v                                 # 详细日志(DEBUG)

环境变量覆盖 (所有 BT_* 变量均可独立于实盘管线调参):
  BT_START_DATE / BT_END_DATE        回测日期区间
  BT_L2_VOLUME_RATIO                 尾盘量比 (默认 1.5)
  BT_L2_LAST5MIN_VOL_PCT             最后5分钟量占比% (默认 6.0)
  BT_L2_LATE_RALLY_PCT               尾盘拉升% (默认 1.5)
  BT_L2_MIN_PASS                     L2最低通过数 (默认 10)
  BT_KLINE_MIN_YANG_RATIO_4D         近4天阳线占比 (回测默认 0.25, 实盘 0.75)
  BT_KLINE_MIN_CONSECUTIVE_CLOSE_RISE 连续收盘上涨天数 (回测默认 0=禁用, 实盘 4)
  BT_KLINE_MIN_CLOSE_RISE_PCT        每天最低涨幅% (回测默认 0=禁用, 实盘 0.5)
  BT_L4_STRONG_BUY / BT_L4_BUY / BT_L4_WATCH  推荐阈值 (35/25/15)

数据源:
  日线: baostock (24/7可用, 无PE/PB/市值字段)
  5分钟线: baostock (24/7可用, 用于S2尾盘指标精算)
  板块成分股: akshare → baostock降级
  资金流向: 不可用 (push2his/akshare/baostock均无法获取历史资金流)
  北向资金: akshare历史日度汇总 (中性分50)
  LLM: 不可用 (rule_weight=1.0, 纯规则评分)
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
  python main_backtest.py                                         # 默认区间
  python main_backtest.py --start 20260401 --end 20260515         # 指定区间
  python main_backtest.py --start 20260501 --end 20260515 --no-cache  # 强制刷新
  python main_backtest.py --sectors 半导体,电子元件                # 自定义板块

环境变量 (BT_* 可独立于实盘管线调参):
  BT_L2_VOLUME_RATIO, BT_L2_LAST5MIN_VOL_PCT, BT_L2_LATE_RALLY_PCT   L2尾盘阈值
  BT_KLINE_MIN_YANG_RATIO_4D, BT_KLINE_MIN_CONSECUTIVE_CLOSE_RISE    K线形态阈值
  BT_L4_STRONG_BUY, BT_L4_BUY, BT_L4_WATCH                          L4推荐阈值
  详见 README.md "回测专用阈值" 章节
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
                        choices=['none', 'proxy', 'replay'],
                        help='资金流模式: none=停用, proxy=5分钟量价估算, replay=实盘快照回放')
    parser.add_argument('--backtest-type', type=str, default='',
                        choices=['historical', 'live_replay'],
                        help='回测口径: historical=历史近似, live_replay=实盘快照回放')
    parser.add_argument('--decision-time', type=str, default='',
                        help='实盘可见性截断时间 HH:MM, 例如 14:57')
    parser.add_argument('--live-snapshot-dir', type=str, default='',
                        help='live_replay 使用的实盘快照目录')
    parser.add_argument('--sectors', type=str, default='',
                        help='覆盖 TARGET_SECTORS，逗号分隔，如: 半导体,电子元件')
    parser.add_argument('--max-positions', type=int, default=0,
                        help='每日最大持仓数 (默认 5)')
    parser.add_argument('--slippage', type=float, default=0,
                        help='滑点 bps (默认 5.0)')
    parser.add_argument('--regime', type=str, default='auto',
                        choices=['auto', 'bull', 'bear', 'neutral'],
                        help='市场状态: auto(逐日判定) / bull / bear / neutral (默认: auto)')
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
    if args.backtest_type:
        config.backtest_type = args.backtest_type
    if args.decision_time:
        config.decision_time = args.decision_time
    if args.live_snapshot_dir:
        config.live_snapshot_dir = args.live_snapshot_dir
    if args.sectors:
        config.target_sectors = [s.strip() for s in args.sectors.split(',') if s.strip()]
    if args.max_positions > 0:
        config.max_positions = args.max_positions
    if args.slippage > 0:
        config.slippage_bps = args.slippage
    if args.regime != 'auto':
        config.regime_mode = args.regime
        logger.info(f">>> 回测市场状态: {args.regime} <<<")

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
    print(f"  数据源: baostock (日线+5分钟线)")
    print(f"  市场状态: {config.regime_mode}")
    print(f"  5分钟线: {'启用' if config.use_5min_data else '禁用(近似)'}")
    print(f"  回测口径: {'live_replay_backtest' if config.backtest_type == 'live_replay' else 'historical_backtest'}")
    print(f"  决策截断: {config.decision_time}")
    print(f"  资金流: {config.capital_flow_mode} (历史数据不可用)")
    print(f"  LLM: 不可用 (纯规则评分)")
    print(f"  滑点: {config.slippage_bps}bps  佣金: {config.commission_rate*100:.3f}%")
    print(f"  最大持仓: {config.max_positions}只/天")
    print(f"  缓存目录: {config.cache_dir}")
    print(f"  输出目录: {config.output_dir}")
    print(f"  --- 回测专用阈值 ---")
    print(f"  L2: 量比≥{config.l2_volume_ratio}  尾盘拉升≥{config.l2_late_rally_pct}%  量占比≥{config.l2_last5min_vol_pct}%")
    print(f"  K线: 阳线占比≥{config.kline_min_yang_ratio_4d}  连涨≥{config.kline_min_consecutive_close_rise}天  ATR {config.kline_min_atr_pct}-{config.kline_max_atr_pct}%")
    print(f"  L4: strong_buy≥{config.l4_high_threshold}  buy≥{config.l4_buy_threshold}  watch≥{config.l4_medium_threshold}")
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
