"""Simulated trading page."""

from flask import Blueprint, render_template, request, jsonify
from web.models import db, SimulatedTrade

bp = Blueprint("simulation", __name__)

# Default simulation parameters
DEFAULT_NOTIONAL = 10000.0  # ¥10,000 per trade
STOP_LOSS_PCT = -5.0
TAKE_PROFIT_PCT = 5.0


@bp.route("/")
def index():
    """Simulated trading dashboard."""
    # Open positions
    open_trades = (
        SimulatedTrade.query
        .filter_by(status="open")
        .order_by(SimulatedTrade.entry_date.desc())
        .all()
    )

    # Closed positions
    closed_trades = (
        SimulatedTrade.query
        .filter_by(status="closed")
        .order_by(SimulatedTrade.exit_date.desc())
        .limit(50)
        .all()
    )

    # Portfolio stats
    total_invested = sum(t.notional for t in open_trades) + sum(
        t.notional for t in closed_trades
    )
    open_value = sum(
        t.notional * (1 + (t.return_pct or 0) / 100) for t in open_trades
    )
    realized_pnl = sum(
        (t.return_pct or 0) * t.notional / 100 for t in closed_trades
    )
    unrealized_pnl = sum(
        (t.return_pct or 0) * t.notional / 100 for t in open_trades
    )

    # Win rate
    all_closed = closed_trades
    closed_count = len(all_closed)
    win_count = sum(1 for t in all_closed if (t.return_pct or 0) > 0)
    win_rate = round(win_count / closed_count * 100, 1) if closed_count > 0 else 0

    stats = {
        "open_count": len(open_trades),
        "closed_count": closed_count,
        "total_invested": round(total_invested, 2),
        "open_value": round(open_value, 2),
        "realized_pnl": round(realized_pnl, 2),
        "unrealized_pnl": round(unrealized_pnl, 2),
        "total_pnl": round(realized_pnl + unrealized_pnl, 2),
        "total_return_pct": round((realized_pnl + unrealized_pnl) / total_invested * 100, 2) if total_invested > 0 else 0,
        "win_rate": win_rate,
        "stop_loss_pct": STOP_LOSS_PCT,
        "take_profit_pct": TAKE_PROFIT_PCT,
        "default_notional": DEFAULT_NOTIONAL,
    }

    return render_template(
        "simulation.html",
        stats=stats,
        open_trades=open_trades,
        closed_trades=closed_trades,
    )


@bp.route("/close/<int:trade_id>", methods=["POST"])
def close_trade(trade_id: int):
    """Manually close a simulated trade."""
    trade = db.session.get(SimulatedTrade, trade_id)
    if not trade or trade.status == "closed":
        return jsonify({"status": "error", "message": "Trade not found or already closed"}), 404

    from datetime import date
    trade.status = "closed"
    trade.exit_date = date.today()
    trade.exit_reason = "manual"

    # Try to get current price for exit
    try:
        from data_provider.kline_provider import KlineProvider
        kline = KlineProvider()
        bars = kline.load_daily(trade.recommendation.code)
        if not bars.empty:
            latest = bars.iloc[-1]
            trade.exit_price = float(latest["close"])
            trade.return_pct = round(
                (trade.exit_price - trade.entry_price) / trade.entry_price * 100, 2
            )
    except Exception:
        trade.exit_price = trade.entry_price

    db.session.commit()
    return jsonify({"status": "ok", "return_pct": trade.return_pct})


@bp.route("/update", methods=["POST"])
def daily_update():
    """Trigger daily price update + exit checks for all open positions."""
    from web.services.simulation_service import SimulationService

    try:
        pos_result = SimulationService.update_all_open_positions()
        track_count = SimulationService.update_recommendation_tracking()

        return jsonify({
            "status": "ok",
            "positions_updated": pos_result["updated"],
            "stopped_out": pos_result["stopped_out"],
            "take_profit": pos_result["take_profit"],
            "tracking_records": track_count,
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
