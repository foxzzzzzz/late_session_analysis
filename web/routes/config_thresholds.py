"""Threshold configuration page."""

from flask import Blueprint, render_template, request, jsonify
from web.models import db, SystemConfig as DbConfig

bp = Blueprint("config", __name__)


def _hide_shadowed_legacy(rows: list[DbConfig], scope: str) -> list[DbConfig]:
    """Hide old unscoped keys when scoped replacements exist."""
    keys = {row.key for row in rows}
    visible = []
    for row in rows:
        if row.key.startswith(f"{scope}."):
            visible.append(row)
            continue
        if f"{scope}.{row.key}" in keys:
            continue
        if row.key.startswith("regime_") and scope == "live":
            parts = row.key.split("_", 2)
            if len(parts) == 3 and f"live.regime.{parts[1]}.{parts[2]}" in keys:
                continue
        visible.append(row)
    return visible


@bp.route("/")
def index():
    """Show threshold configuration page."""
    # Fetch all config entries grouped by category
    live_thresholds = _hide_shadowed_legacy(
        DbConfig.query
        .filter(DbConfig.category.like("threshold_live%"))
        .order_by(DbConfig.key)
        .all(),
        "live",
    )
    backtest_thresholds = _hide_shadowed_legacy(
        DbConfig.query
        .filter(DbConfig.category.like("threshold_backtest%"))
        .order_by(DbConfig.key)
        .all(),
        "backtest",
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
