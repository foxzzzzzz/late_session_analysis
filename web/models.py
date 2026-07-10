"""SQLAlchemy models for the web dashboard."""

from datetime import datetime, date
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import Text, Date, DateTime, Float, Integer, String, Boolean, ForeignKey, UniqueConstraint

db = SQLAlchemy()


# ── helpers ──────────────────────────────────────────────────


def _utcnow() -> datetime:
    return datetime.now()


# ── tables ───────────────────────────────────────────────────


class SystemConfig(db.Model):  # type: ignore
    """Key-value store for all thresholds, API keys, schedule settings."""

    __tablename__ = "system_config"

    key = db.Column(String(128), primary_key=True)
    value = db.Column(Text, nullable=False, default="")
    value_type = db.Column(String(16), nullable=False, default="str")
    category = db.Column(String(64), nullable=False, default="threshold")
    description = db.Column(String(256), nullable=False, default="")
    updated_at = db.Column(DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)


class PipelineRun(db.Model):  # type: ignore
    """One row per daily pipeline execution."""

    __tablename__ = "pipeline_runs"

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    trading_date = db.Column(Date, unique=True, nullable=False)
    regime = db.Column(String(16), nullable=False, default="auto")
    status = db.Column(String(16), nullable=False, default="running")  # running | completed | failed
    started_at = db.Column(DateTime, nullable=False, default=_utcnow)
    finished_at = db.Column(DateTime, nullable=True)
    stages_json = db.Column(Text, nullable=True)  # {"S0":5, "S1":342, ...}
    snapshot_dir = db.Column(String(256), nullable=True)


class DailyRecommendation(db.Model):  # type: ignore
    """Stocks recommended at strong_buy / buy / watch level each day."""

    __tablename__ = "daily_recommendations"

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    run_id = db.Column(Integer, ForeignKey("pipeline_runs.id"), nullable=False)
    code = db.Column(String(12), nullable=False)
    name = db.Column(String(32), nullable=False)
    level = db.Column(String(16), nullable=False)  # strong_buy | buy | watch
    final_score = db.Column(Float, nullable=False, default=0.0)
    rule_score = db.Column(Float, nullable=False, default=0.0)
    llm_score = db.Column(Float, nullable=False, default=0.0)
    sector = db.Column(String(64), nullable=True)
    entry_price = db.Column(Float, nullable=False, default=0.0)
    recommendation_date = db.Column(Date, nullable=False)

    run = db.relationship("PipelineRun", backref="recommendations")


class RecommendationTracking(db.Model):  # type: ignore
    """Daily price tracking for each recommendation."""

    __tablename__ = "recommendation_tracking"

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    recommendation_id = db.Column(
        Integer, ForeignKey("daily_recommendations.id"), nullable=False
    )
    track_date = db.Column(Date, nullable=False)
    close_price = db.Column(Float, nullable=False, default=0.0)
    cumulative_return_pct = db.Column(Float, nullable=False, default=0.0)
    days_held = db.Column(Integer, nullable=False, default=0)
    is_stopped_out = db.Column(Boolean, nullable=False, default=False)
    is_take_profit = db.Column(Boolean, nullable=False, default=False)

    __table_args__ = (
        UniqueConstraint("recommendation_id", "track_date", name="uq_rec_track_date"),
    )

    recommendation = db.relationship("DailyRecommendation", backref="tracking_records")


class SimulatedTrade(db.Model):  # type: ignore
    """Simulated trades created from daily recommendations or added manually."""

    __tablename__ = "simulated_trades"

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    recommendation_id = db.Column(
        Integer, ForeignKey("daily_recommendations.id"), nullable=True  # nullable for manual trades
    )
    # Manual-trade fields (used when recommendation_id is None)
    code = db.Column(String(12), nullable=True)
    name = db.Column(String(32), nullable=True)
    # Entry/exit
    entry_date = db.Column(Date, nullable=False)
    entry_price = db.Column(Float, nullable=False, default=0.0)
    exit_date = db.Column(Date, nullable=True)
    exit_price = db.Column(Float, nullable=True)
    exit_reason = db.Column(String(32), nullable=True)  # stop_loss | take_profit | manual | expired
    return_pct = db.Column(Float, nullable=True)
    status = db.Column(String(16), nullable=False, default="open")  # open | closed
    notional = db.Column(Float, nullable=False, default=10000.0)
    shares = db.Column(Integer, nullable=False, default=0)
    # Custom risk control
    stop_loss_pct = db.Column(Float, nullable=True)   # override default stop_loss
    take_profit_pct = db.Column(Float, nullable=True)  # override default take_profit
    source = db.Column(String(16), nullable=False, default="auto")  # auto | manual

    recommendation = db.relationship("DailyRecommendation", backref="simulated_trade")


class BacktestRun(db.Model):  # type: ignore
    """Backtest execution records."""

    __tablename__ = "backtest_runs"

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    start_date = db.Column(Date, nullable=False)
    end_date = db.Column(Date, nullable=False)
    backtest_type = db.Column(String(32), nullable=False, default="historical")
    capital_flow_mode = db.Column(String(16), nullable=False, default="none")
    regime = db.Column(String(16), nullable=False, default="auto")
    status = db.Column(String(16), nullable=False, default="running")
    started_at = db.Column(DateTime, nullable=False, default=_utcnow)
    finished_at = db.Column(DateTime, nullable=True)
    summary_json = db.Column(Text, nullable=True)
    output_dir = db.Column(String(256), nullable=True)


class PipelineLog(db.Model):  # type: ignore
    """Per-stage pipeline logs for playback."""

    __tablename__ = "pipeline_logs"

    id = db.Column(Integer, primary_key=True, autoincrement=True)
    run_id = db.Column(Integer, ForeignKey("pipeline_runs.id"), nullable=False)
    stage = db.Column(String(8), nullable=False)
    level = db.Column(String(16), nullable=False, default="INFO")
    message = db.Column(Text, nullable=False, default="")
    timestamp = db.Column(DateTime, nullable=False, default=_utcnow)

    run = db.relationship("PipelineRun", backref="logs")
