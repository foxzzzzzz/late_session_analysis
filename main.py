#!/usr/bin/env python3
"""A股尾盘分析系统 — CLI入口

三种运行模式:
  1. 实时模式:    python main.py              (14:30后运行,执行4阶段流水线)
  2. 快速测试:    python main.py --test       (用当前市场数据跑完整流程,随时可用)
  3. 定时调度:    python main.py --schedule   (14:29自动启动)

用法:
  python main.py [--stages STAGES] [--test] [--schedule] [--output-dir DIR]
"""
import sys
import os
import argparse
import logging
from datetime import datetime
from pathlib import Path

# 确保项目根目录在 sys.path
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

from orchestration.config import SystemConfig
from orchestration.pipeline import LateSessionPipeline

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger("late_session")


def parse_args():
    parser = argparse.ArgumentParser(
        description="A股尾盘分析系统 — 实时扫描全市场，识别尾盘交易机会",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python main.py                    # 实时模式
  python main.py --test             # 快速测试(用当前行情验证)
  python main.py --stages 1,2       # 只跑前两个阶段
  python main.py --schedule         # 定时调度模式
  python main.py --output-dir ./my_reports  # 指定报告输出目录
        """,
    )
    parser.add_argument(
        '--test', action='store_true',
        help='快速测试模式：用当前市场数据跑完整4阶段流程(不要求14:30后)',
    )
    parser.add_argument(
        '--stages', type=str, default='',
        help='指定运行阶段: 1,2,3,4 (逗号分隔)',
    )
    parser.add_argument(
        '--schedule', action='store_true',
        help='定时调度模式：14:29自动启动',
    )
    parser.add_argument(
        '--output-dir', type=str, default='',
        help='报告输出目录',
    )
    parser.add_argument(
        '--no-llm', action='store_true',
        help='禁用LLM分析，仅用规则评分',
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='仅拉取数据并显示摘要，不做分析和报告',
    )
    return parser.parse_args()


def check_trading_time() -> bool:
    """检查是否在交易时段内 (14:30-15:00)"""
    now = datetime.now()
    hour, minute = now.hour, now.minute
    # 周一到周五
    if now.weekday() >= 5:
        logger.warning("今日非交易日(周末)")
        return False
    # 14:30-15:05 视为有效时段
    if hour == 14 and minute >= 25:
        return True
    if hour == 14:
        return True
    if hour == 15 and minute <= 5:
        return True
    logger.info(f"当前时间 {hour:02d}:{minute:02d}, 非尾盘时段(14:25-15:05)")
    return False


def main():
    args = parse_args()
    config = SystemConfig()

    if args.output_dir:
        config.report_output_dir = args.output_dir

    if args.no_llm:
        os.environ['LLM_API_KEY'] = ''
        config.llm_api_key = ''

    # 解析阶段
    stages = None
    if args.stages:
        stages = [int(s.strip()) for s in args.stages.split(',')]

    # 调度模式
    if args.schedule:
        run_schedule(config, stages)
        return

    # 测试模式 / 实时模式
    is_test = args.test
    if not is_test and not check_trading_time():
        logger.info("非尾盘时段，使用 --test 可强制运行测试")
        return

    if is_test:
        logger.info(">>> 快速测试模式 <<<")

    pipeline = LateSessionPipeline(config)

    if args.dry_run:
        logger.info("Dry-run: 拉取数据...")
        quotes = pipeline.fetcher_mgr.fetch_snapshot()
        logger.info(f"拉取 {len(quotes)} 只股票")
        # 显示前10只
        for q in quotes[:10]:
            logger.info(f"  {q.code} {q.name}: {q.price:.2f} ({q.change_pct:+.2f}%) "
                        f"成交额{q.turnover/1e8:.2f}亿 换手{q.turnover_rate:.1f}%")
        return

    path = pipeline.run(stages=stages)
    logger.info(f"报告路径: {path}")

    # 打印摘要
    print("\n" + "=" * 60)
    print("尾盘分析完成!")
    print(f"报告: {path}")
    print("=" * 60)


def run_schedule(config: SystemConfig, stages: list[int] = None):
    """定时调度模式"""
    import schedule
    import time

    logger.info(f"定时调度模式启动，每日 {config.schedule_time} 执行")
    logger.info("等待触发时间... 按 Ctrl+C 退出")

    schedule.every().day.at(config.schedule_time).do(
        lambda: _scheduled_run(config, stages)
    )

    while True:
        schedule.run_pending()
        time.sleep(30)


def _scheduled_run(config: SystemConfig, stages: list[int] = None):
    """调度触发的执行"""
    logger.info(">>> 定时触发: 开始尾盘分析 <<<")
    try:
        pipeline = LateSessionPipeline(config)
        path = pipeline.run(stages=stages)
        logger.info(f"报告: {path}")
    except Exception as e:
        logger.error(f"调度执行失败: {e}", exc_info=True)


if __name__ == '__main__':
    main()
