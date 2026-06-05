"""Dashboard home page."""

from datetime import date
from flask import Blueprint, render_template
from web.models import db, PipelineRun, DailyRecommendation, SimulatedTrade, BacktestRun
from web.models import SystemConfig as DbConfig

bp = Blueprint("dashboard", __name__)


def _get_config(key: str, default: str = "") -> str:
    row = DbConfig.query.filter_by(key=key).first()
    return row.value if row else default


@bp.route("/")
def index():
    # ── today's status ──────────────────────────────────────
    today = date.today()
    today_run = PipelineRun.query.filter_by(trading_date=today).first()

    # last run
    last_run = (
        PipelineRun.query
        .filter(PipelineRun.status != "running")
        .order_by(PipelineRun.trading_date.desc())
        .first()
    )

    # today's recommendations
    if today_run:
        today_recs = DailyRecommendation.query.filter_by(run_id=today_run.id).all()
    else:
        today_recs = []

    strong_buy = [r for r in today_recs if r.level == "strong_buy"]
    buy = [r for r in today_recs if r.level == "buy"]
    watch = [r for r in today_recs if r.level == "watch"]

    # cumulative stats from simulated trades
    all_trades = SimulatedTrade.query.filter_by(status="closed").all()
    closed_count = len(all_trades)
    win_count = sum(1 for t in all_trades if (t.return_pct or 0) > 0)
    win_rate = round(win_count / closed_count * 100, 1) if closed_count > 0 else 0
    total_return = round(sum(t.return_pct or 0 for t in all_trades), 2)

    # open positions
    open_trades = SimulatedTrade.query.filter_by(status="open").all()
    open_pnl = sum(
        (t.return_pct or 0) * t.notional / 100 for t in open_trades
    )

    # live snapshot stats
    from pathlib import Path
    snapshot_root = Path(_get_config("live_snapshot_dir", "./live_snapshots"))
    snapshot_days = 0
    snapshot_files = 0
    if snapshot_root.exists():
        snapshot_days = len([d for d in snapshot_root.iterdir() if d.is_dir()])
        snapshot_files = sum(1 for _ in snapshot_root.rglob("*.jsonl"))

    # last 5 runs summary
    recent_runs = (
        PipelineRun.query
        .order_by(PipelineRun.trading_date.desc())
        .limit(5)
        .all()
    )

    # market regime
    regime = "–"
    if today_run and today_run.regime:
        regime = today_run.regime
    elif last_run:
        regime = last_run.regime

    # combine stats for template
    stats = {
        "today_date": today.isoformat(),
        "regime": regime,
        "last_run_status": last_run.status if last_run else "none",
        "last_run_date": last_run.trading_date.isoformat() if last_run else "–",
        "strong_buy_count": len(strong_buy),
        "buy_count": len(buy),
        "watch_count": len(watch),
        "today_total": len(today_recs),
        "win_rate": win_rate,
        "cumulative_return": total_return,
        "closed_trades": closed_count,
        "open_positions": len(open_trades),
        "open_pnl": round(open_pnl, 2),
        "snapshot_days": snapshot_days,
        "snapshot_files": snapshot_files,
    }

    return render_template("dashboard.html", stats=stats, recent_runs=recent_runs)
