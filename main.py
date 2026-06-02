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
from data_provider.kline_provider import KlineProvider
from data_provider.northbound_fetcher import get_northbound_sentiment

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
    parser.add_argument(
        '--regime', type=str, default='auto',
        choices=['auto', 'bull', 'bear', 'neutral'],
        help='市场状态: auto(自动判定) / bull / bear / neutral (默认: auto)',
    )
    return parser.parse_args()


def check_trading_time() -> bool:
    """检查是否在交易时段内 (14:25-15:05)"""
    now = datetime.now()
    hour, minute = now.hour, now.minute
    # 周一到周五
    if now.weekday() >= 5:
        logger.warning("今日非交易日(周末)")
        return False
    # 14:25-15:05 视为有效时段
    if hour == 14 and minute >= 25:
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

    if args.regime != 'auto':
        config.regime_mode = args.regime
        logger.info(f">>> 手动市场状态: {args.regime} <<<")

    # 解析阶段 (--stages 0,1,2,3,4 逗号分隔)
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

    pipeline = LateSessionPipeline(config, test_mode=is_test)

    try:
        if args.dry_run:
            _run_dry_run(pipeline)
            return

        path = pipeline.run(stages=stages)
        logger.info(f"报告路径: {path}")

        # 打印摘要
        print("\n" + "=" * 60)
        print("尾盘分析完成!")
        print(f"报告: {path}")
        print("=" * 60)

    except RuntimeError as e:
        logger.error(f"运行失败: {e}")
        _print_troubleshooting_guide()
        sys.exit(1)


def _run_dry_run(pipeline):
    """盘前数据源连通性诊断 — 逐项测试所有实盘依赖的数据接口

    覆盖: 行情快照 / 预加载(行业+解禁) / K线(mootdx+Sina) /
          东财资金流(门控+熔断) / 新浪资金流 / 公告API / 北向状态
    """
    import time
    results: dict[str, dict] = {}  # source → {ok, detail, elapsed}

    def _record(name: str, ok: bool, detail: str, t0: float):
        results[name] = {"ok": ok, "detail": detail, "elapsed": time.time() - t0}

    print("\n" + "=" * 64)
    print("  盘前数据源连通性诊断")
    print("=" * 64)

    # ── 1. 行情快照 ───────────────────────────────────────────
    print("\n[1/7] 行情快照 (Tencent API)...")
    t0 = time.time()
    try:
        quotes = pipeline.fetcher_mgr.fetch_snapshot()
        active = [q for q in quotes if q.price > 0 and not q.is_suspended]
        _record("行情快照", True,
                f"{len(quotes)}只 (有效 {len(active)}只, 数据源: {pipeline.fetcher_mgr.get_active_name()})", t0)
    except RuntimeError as e:
        _record("行情快照", False, str(e)[:100], t0)

    # ── 2. 预加载数据 ─────────────────────────────────────────
    print("[2/7] 预加载 (同花顺行业行情 + 限售解禁 + 概念标签)...")
    t0 = time.time()
    try:
        pipeline.preloader.load_all()
        n_sectors = len(pipeline.preloader.sector_performance or {})
        n_unlock = len(pipeline.preloader.unlock_stocks or set())
        n_concept = len(pipeline.preloader.hot_concepts or {})
        _record("预加载", True,
                f"{n_sectors}行业行情, {n_unlock}条解禁, {n_concept}概念股", t0)
    except Exception as e:
        _record("预加载", False, str(e)[:100], t0)

    # ── 3. K线 (mootdx + Sina回退) ─────────────────────────────
    print("[3/7] K线 (mootdx TCP → Sina HTTP 回退)...")
    t0 = time.time()
    try:
        kp = KlineProvider()
        # 用贵州茅台(600519)和宁德时代(300750)测试，覆盖沪深两市
        klines = kp.load_daily_batch(["600519", "300750"], bars=5)
        n_ok = sum(1 for df in klines.values() if df is not None and len(df) > 0)
        _record("K线", n_ok >= 1,
                f"{n_ok}/2只成功 (mootdx→Sina回退)", t0)
    except Exception as e:
        _record("K线", False, str(e)[:100], t0)

    # ── 4. 东财资金流 (push2his, 走 em_get 门控 + 熔断) ─────────
    print("[4/7] 东财 push2his 资金流 (em_get 门控 + 熔断器)...")
    t0 = time.time()
    try:
        from data_provider.eastmoney_flow_fetcher import EastmoneyFlowFetcher
        ef = EastmoneyFlowFetcher(timeout=10.0, max_workers=1)
        test_codes = ["600519"]  # 仅测1只，减少API调用
        flow_data = ef.enrich_batch(test_codes)
        if flow_data:
            d = flow_data.get("600519", {})
            _record("东财资金流", True,
                    f"主力{d.get('mainForce', 0):.0f}万, 主动买入{d.get('active_buy_ratio', 0):.1f}%", t0)
        else:
            breaker_state = ef._breaker.state.value if hasattr(ef, '_breaker') else 'N/A'
            _record("东财资金流", False, f"返回空 (熔断状态: {breaker_state})", t0)
    except Exception as e:
        _record("东财资金流", False, str(e)[:100], t0)

    # ── 5. 东财分钟资金流 (push2, 走 em_urlopen 门控 + 熔断) ────
    print("[5/7] 东财 push2 分钟资金流 (em_urlopen 门控 + 熔断器)...")
    t0 = time.time()
    try:
        from data_provider.eastmoney_minute_flow import EastmoneyMinuteFlowFetcher
        mf = EastmoneyMinuteFlowFetcher(timeout=8.0, max_workers=1)
        minute_data = mf.enrich_batch(["600519"])
        if minute_data:
            d = minute_data.get("600519", {})
            _record("东财分钟流", True,
                    f"主力{d.get('mainForce', 0):.0f}万, 超大单{d.get('super', 0):.0f}万", t0)
        else:
            breaker_state = mf._breaker.state.value if hasattr(mf, '_breaker') else 'N/A'
            _record("东财分钟流", False, f"返回空 (熔断状态: {breaker_state})", t0)
    except Exception as e:
        _record("东财分钟流", False, str(e)[:100], t0)

    # ── 6. 新浪资金流 ─────────────────────────────────────────
    print("[6/7] 新浪资金流 (active_buy_ratio + 备选主力)...")
    t0 = time.time()
    try:
        from data_provider.sina_fund_flow import SinaFundFlowFetcher
        sf = SinaFundFlowFetcher(timeout=8.0, max_workers=1)
        sina_data = sf.enrich_batch(["600519"])
        if sina_data:
            d = sina_data.get("600519", {})
            _record("新浪资金流", True,
                    f"主力{d.get('mainForce', 0):.0f}万, 主动买入{d.get('active_buy_ratio', 0):.1f}%", t0)
        else:
            _record("新浪资金流", False, "返回空", t0)
    except Exception as e:
        _record("新浪资金流", False, str(e)[:100], t0)

    # ── 7. 东财公告API + 北向状态 ──────────────────────────────
    print("[7/7] 东财公告API + 北向资金状态...")
    t0 = time.time()
    try:
        nh = pipeline.news_fetcher.health_check()
        nb = get_northbound_sentiment()
        detail_parts = [f"公告采样{nh.get('sample_count', 0)}条"]
        detail_parts.append(f"北向={'可用' if nb.get('available') else '断供'}")
        _record("公告+北向", nh.get("ok", False),
                ", ".join(detail_parts), t0)
    except Exception as e:
        _record("公告+北向", False, str(e)[:100], t0)

    # ── 汇总 ──────────────────────────────────────────────────
    print("\n" + "=" * 64)
    print("  诊断结果")
    print("=" * 64)
    all_ok = True
    for name, r in results.items():
        status = "PASS" if r["ok"] else "FAIL"
        print(f"  [{status}] {name:<12s} ({r['elapsed']:.1f}s)  {r['detail']}")
        if not r["ok"]:
            all_ok = False

    print("-" * 64)
    total_elapsed = sum(r["elapsed"] for r in results.values())
    print(f"  总计: {sum(1 for r in results.values() if r['ok'])}/{len(results)} 通过 ({total_elapsed:.1f}s)")

    if all_ok:
        print("\n  所有数据源正常，可以执行实盘。")
    else:
        failed = [n for n, r in results.items() if not r["ok"]]
        print(f"\n  [!!] 以下数据源异常: {', '.join(failed)}")
        print("  管线将自动降级，但建议排查后重试。")
    print()


def _print_troubleshooting_guide():
    """打印问题排查指南"""
    print("""
╔══════════════════════════════════════════════════════════╗
║  数据源连接失败 — 排查指南                               ║
╠══════════════════════════════════════════════════════════╣
║  降级链: sector_based → sina → efinance → akshare        ║
║  - sector_based: 按板块拉取(eastmoney),盘中优先           ║
║  - sina: 全市场(新浪财经),独立于eastmoney,24/7可用        ║
║  - efinance/akshare: eastmoney备选                       ║
║                                                          ║
║  1. 交易时段: eastmoney实时API仅在9:30-15:00响应          ║
║     盘后测试会自动切换到sina源(收盘数据)                  ║
║                                                          ║
║  2. 网络连通性: sina使用新浪财经,不受eastmoney限制        ║
║     如果sina也失败,检查是否能正常访问外网                  ║
║                                                          ║
║  3. 调整优先级: 在 .env 中设置                            ║
║     DATA_PROVIDER_PRIORITY=sector_based,sina,efinance,... ║
║     盘中想跳过板块直接用sina: DATA_PROVIDER_PRIORITY=sina ║
║                                                          ║
║  4. 测试数据拉取: 用 --dry-run 仅测试连接                 ║
║     python main.py --test --dry-run                      ║
╚══════════════════════════════════════════════════════════╝
""")



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
        pipeline = LateSessionPipeline(config, test_mode=False)
        path = pipeline.run(stages=stages)
        logger.info(f"报告: {path}")
    except Exception as e:
        logger.error(f"调度执行失败: {e}", exc_info=True)


if __name__ == '__main__':
    main()
