"""Threshold configuration page."""

from flask import Blueprint, render_template, request, jsonify
from web.models import db, SystemConfig as DbConfig

bp = Blueprint("config", __name__)


@bp.route("/")
def index():
    """Show threshold configuration page."""
    # Fetch all config entries grouped by category
    live_thresholds = (
        DbConfig.query
        .filter(DbConfig.category.like("threshold_live%"))
        .order_by(DbConfig.key)
        .all()
    )
    backtest_thresholds = (
        DbConfig.query
        .filter(DbConfig.category.like("threshold_backtest%"))
        .order_by(DbConfig.key)
        .all()
    )

    # Group by stage for display
    return render_template(
        "config.html",
        live_thresholds=live_thresholds,
        backtest_thresholds=backtest_thresholds,
    )


@bp.route("/save", methods=["POST"])
def save():
    """Save threshold values from form submission."""
    data = request.get_json() or {}
    count = 0
    for key, value in data.items():
        row = DbConfig.query.filter_by(key=key).first()
        if row:
            row.value = str(value)
            count += 1
    db.session.commit()
    return jsonify({"status": "ok", "updated": count})
