"""Daily recommendations tracking page."""

from datetime import date, timedelta
from flask import Blueprint, render_template, request, jsonify
from web.models import db, PipelineRun, DailyRecommendation, RecommendationTracking

bp = Blueprint("recommendations", __name__)


@bp.route("/")
def index():
    """Show recommendations by date."""
    selected_date = request.args.get("date", date.today().isoformat())

    # Available dates (that have recommendations)
    available_dates = [
        row[0].isoformat()
        for row in (
            db.session.query(DailyRecommendation.recommendation_date)
            .distinct()
            .order_by(DailyRecommendation.recommendation_date.desc())
            .limit(60)
            .all()
        )
    ]

    # Recommendations for selected date
    recs = (
        DailyRecommendation.query
        .filter_by(recommendation_date=selected_date)
        .order_by(DailyRecommendation.final_score.desc())
        .all()
    )

    # Tracking data for each recommendation
    from collections import defaultdict
    tracking: dict[int, list[RecommendationTracking]] = defaultdict(list)
    track_dates: set[str] = set()
    for rec in recs:
        records = (
            RecommendationTracking.query
            .filter_by(recommendation_id=rec.id)
            .order_by(RecommendationTracking.track_date)
            .all()
        )
        tracking[rec.id] = records
        for t in records:
            track_dates.add(t.track_date.isoformat())

    sorted_track_dates = sorted(track_dates)

    # Summary
    tracked_recs = [r for r in recs if tracking[r.id]]
    positive = sum(1 for r in tracked_recs if tracking[r.id][-1].cumulative_return_pct > 0)
    avg_return = (
        round(sum(tracking[r.id][-1].cumulative_return_pct for r in tracked_recs) / len(tracked_recs), 2)
        if tracked_recs
        else 0
    )

    summary = {
        "total": len(recs),
        "tracked": len(tracked_recs),
        "positive": positive,
        "win_rate": round(positive / len(tracked_recs) * 100, 1) if tracked_recs else 0,
        "avg_return": avg_return,
    }

    return render_template(
        "recommendations.html",
        selected_date=selected_date,
        available_dates=available_dates,
        recommendations=recs,
        tracking=tracking,
        track_dates=sorted_track_dates,
        summary=summary,
    )


@bp.route("/track/<int:rec_id>", methods=["POST"])
def update_tracking(rec_id: int):
    """Manually trigger tracking update for a recommendation (API)."""
    rec = db.session.get(DailyRecommendation, rec_id)
    if not rec:
        return jsonify({"status": "error", "message": "Not found"}), 404

    # Get latest price from data provider
    try:
        from data_provider.kline_provider import KlineProvider
        kline = KlineProvider()
        daily = kline.load_daily_batch([rec.code], bars=5)
        bars = daily.get(rec.code)
        if bars is not None and not bars.empty:
            latest = bars.iloc[-1]
            close = float(latest["close"])
            return_pct = round((close - rec.entry_price) / rec.entry_price * 100, 2)

            # Save tracking record
            today = date.today()
            existing = RecommendationTracking.query.filter_by(
                recommendation_id=rec.id, track_date=today
            ).first()
            if not existing:
                track = RecommendationTracking(
                    recommendation_id=rec.id,
                    track_date=today,
                    close_price=close,
                    cumulative_return_pct=return_pct,
                    days_held=(today - rec.recommendation_date).days,
                )
                db.session.add(track)
                db.session.commit()

            return jsonify({
                "status": "ok",
                "close_price": close,
                "return_pct": return_pct,
            })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

    return jsonify({"status": "error", "message": "No data available"}), 404
