"""Backtest report metadata tests."""
import json
from pathlib import Path

from backtest.config import BacktestConfig
from backtest.report_generator import BacktestReportGenerator
from backtest.trade_log import TradeLogRecorder


def test_report_includes_backtest_mode_metadata(tmp_path):
    config = BacktestConfig()
    config.output_dir = str(tmp_path)
    config.backtest_type = "historical"
    config.decision_time = "14:55"
    config.capital_flow_mode = "proxy"
    recorder = TradeLogRecorder()

    BacktestReportGenerator.generate(recorder, {"total_trades": 0}, config)

    summary_path = next(Path(tmp_path).glob("summary_*.json"))
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["backtest_info"]["backtest_type"] == "historical_backtest"
    assert summary["backtest_info"]["decision_time"] == "14:55"
    assert summary["backtest_info"]["capital_flow_mode"] == "proxy"

    overview_path = next(Path(tmp_path).glob("overview_*.md"))
    overview = overview_path.read_text(encoding="utf-8")
    assert "historical_backtest" in overview
    assert "14:55" in overview
    assert "proxy" in overview


def test_live_replay_report_label(tmp_path):
    config = BacktestConfig()
    config.output_dir = str(tmp_path)
    config.backtest_type = "live_replay"
    recorder = TradeLogRecorder()

    BacktestReportGenerator.generate(recorder, {"total_trades": 0}, config)

    summary_path = next(Path(tmp_path).glob("summary_*.json"))
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["backtest_info"]["backtest_type"] == "live_replay_backtest"
