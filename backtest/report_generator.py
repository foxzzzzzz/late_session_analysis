"""回测报告生成 — CSV + JSON + Markdown"""
import json
import logging
import os
from datetime import datetime

import pandas as pd

from backtest.trade_log import TradeLogRecorder

logger = logging.getLogger(__name__)


class BacktestReportGenerator:
    @staticmethod
    def generate(recorder: TradeLogRecorder, metrics: dict, config) -> str:
        output_dir = config.output_dir
        os.makedirs(output_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")

        # 1. CSV 交易明细
        trades_csv = os.path.join(output_dir, f"trades_{ts}.csv")
        BacktestReportGenerator._save_trades_csv(recorder, trades_csv)

        # 2. JSON 汇总
        summary_json = os.path.join(output_dir, f"summary_{ts}.json")
        BacktestReportGenerator._save_summary_json(recorder, metrics, summary_json)

        # 3. 月度统计
        monthly_csv = os.path.join(output_dir, f"monthly_{ts}.csv")
        BacktestReportGenerator._save_monthly_csv(recorder, monthly_csv)

        # 4. 分层统计
        stratified_csv = os.path.join(output_dir, f"stratified_{ts}.csv")
        BacktestReportGenerator._save_stratified_csv(recorder, stratified_csv)

        # 5. Markdown 总览
        md_path = os.path.join(output_dir, f"overview_{ts}.md")
        BacktestReportGenerator._save_overview_md(recorder, metrics, config, md_path)

        logger.info(f"报告已生成: {output_dir}/ (ts={ts})")
        return output_dir

    @staticmethod
    def _save_trades_csv(recorder: TradeLogRecorder, path: str):
        trades = recorder.all_trades()
        if not trades:
            return
        df = pd.DataFrame([{
            "日期": t.date, "代码": t.code, "名称": t.name,
            "买入价": t.entry_price, "卖出价": t.exit_price,
            "收益率%": t.return_pct, "推荐": t.recommendation,
            "评分": t.total_score, "异动类型": t.anomaly_type,
            "板块": t.sector, "尾盘分": t.score_tail,
            "技术分": t.score_tech, "资金分": t.score_capital,
            "市场分": t.score_env, "历史分": t.score_history,
            "退出方式": t.exit_reason,
        } for t in trades])
        df.to_csv(path, index=False, encoding="utf-8-sig")
        logger.info(f"  交易明细: {path} ({len(df)} 笔)")

    @staticmethod
    def _save_summary_json(recorder: TradeLogRecorder, metrics: dict, path: str):
        summary = {
            "backtest_info": {
                "total_days": recorder.total_days(),
                "days_with_signals": recorder.days_with_signals(),
            },
            "performance": metrics,
            "stratified": BacktestReportGenerator._stratified(recorder),
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        logger.info(f"  汇总JSON: {path}")

    @staticmethod
    def _save_monthly_csv(recorder: TradeLogRecorder, path: str):
        from backtest.performance import PerformanceCalculator
        monthly = PerformanceCalculator.monthly_breakdown(recorder.all_trades())
        if not monthly.empty:
            monthly.to_csv(path, encoding="utf-8-sig")
            logger.info(f"  月度统计: {path}")

    @staticmethod
    def _save_stratified_csv(recorder: TradeLogRecorder, path: str):
        from backtest.performance import PerformanceCalculator
        stratified = PerformanceCalculator.score_stratified(recorder.all_trades())
        if stratified:
            rows = []
            for level, stats in stratified.items():
                stats["level"] = level
                rows.append(stats)
            df = pd.DataFrame(rows)
            df.to_csv(path, index=False, encoding="utf-8-sig")
            logger.info(f"  分层统计: {path}")

    @staticmethod
    def _stratified(recorder: TradeLogRecorder) -> dict:
        from backtest.performance import PerformanceCalculator
        return PerformanceCalculator.score_stratified(recorder.all_trades())

    @staticmethod
    def _save_overview_md(recorder: TradeLogRecorder, metrics: dict, config, path: str):
        lines = [
            "# 尾盘策略回测报告",
            "",
            f"**回测区间**: {config.start_date} → {config.end_date}",
            f"**股票池**: {len(config.target_sectors)} 个固定板块 ({', '.join(config.target_sectors)})",
            f"**候选股数**: ~{recorder.total_days()} 天",
            "",
            "## 绩效汇总",
            "",
            "| 指标 | 数值 |",
            "|------|------|",
        ]
        metric_labels = {
            "total_trades": "总交易笔数",
            "win_rate": "胜率 (%)",
            "avg_return_pct": "平均收益率 (%)",
            "median_return_pct": "中位数收益率 (%)",
            "std_return_pct": "收益率标准差 (%)",
            "total_return_pct": "累计收益率 (%)",
            "avg_win_pct": "平均盈利 (%)",
            "avg_loss_pct": "平均亏损 (%)",
            "profit_factor": "盈亏因子",
            "max_drawdown_pct": "最大回撤 (%)",
            "sharpe_ratio": "夏普比率",
            "calmar_ratio": "Calmar比率",
            "max_consecutive_wins": "最长连胜",
            "max_consecutive_losses": "最长连亏",
        }
        for key, label in metric_labels.items():
            val = metrics.get(key, "-")
            if isinstance(val, float):
                val = f"{val:.2f}"
            lines.append(f"| {label} | {val} |")

        # 分层统计
        stratified = BacktestReportGenerator._stratified(recorder)
        if stratified:
            lines.extend([
                "",
                "## 按推荐等级分层",
                "",
                "| 等级 | 笔数 | 胜率(%) | 平均收益(%) | 累计收益(%) |",
                "|------|------|---------|-------------|-------------|",
            ])
            for level in ["strong_buy", "buy", "watch"]:
                s = stratified.get(level, {})
                if s:
                    lines.append(
                        f"| {level} | {s['count']} | {s['win_rate']} | {s['avg_return']:.4f} | {s['total_return']:.2f} |"
                    )

        lines.extend([
            "",
            f"**报告生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        ])

        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        logger.info(f"  总览Markdown: {path}")
