"""Simulated trading engine — tracks positions, exit conditions, and P&L."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

logger = logging.getLogger(__name__)

STOP_LOSS_PCT = -5.0
TAKE_PROFIT_PCT = 5.0
MAX_HOLD_DAYS = 10  # Auto-close after 10 trading days


def _get_stop_loss(trade) -> float:
    """Return per-trade stop_loss_pct or global default."""
    val = getattr(trade, "stop_loss_pct", None)
    return float(val) if val is not None else STOP_LOSS_PCT


def _get_take_profit(trade) -> float:
    """Return per-trade take_profit_pct or global default."""
    val = getattr(trade, "take_profit_pct", None)
    return float(val) if val is not None else TAKE_PROFIT_PCT


class SimulationService:
    """Manages simulated trades: price updates, exit checks, P&L calculation."""

    @staticmethod
    def update_all_open_positions() -> dict[str, int]:
        """Update current prices for all open positions.

        Returns {"updated": N, "stopped_out": N, "take_profit": N}.
        """
        from web.models import db, SimulatedTrade

        open_trades = SimulatedTrade.query.filter_by(status="open").all()
        if not open_trades:
            return {"updated": 0, "stopped_out": 0, "take_profit": 0}

        today = date.today()

        # Collect unique codes (handle manual trades without recommendation)
        codes = list({
            t.recommendation.code if t.recommendation_id else t.code
            for t in open_trades
            if t.recommendation_id or t.code
        })

        # Fetch daily bars for all codes
        prices = SimulationService._fetch_latest_prices(codes)

        updated = 0
        stopped = 0
        took_profit = 0

        for trade in open_trades:
            code = trade.recommendation.code if trade.recommendation_id else trade.code
            if not code or code not in prices or prices.get(code, 0) <= 0:
                continue

            current_price = prices[code]
            return_pct = round((current_price - trade.entry_price) / trade.entry_price * 100, 2)
            trade.return_pct = return_pct  # type: ignore[attr-defined]
            updated += 1

            # Check exit conditions (per-trade thresholds)
            days_held = (today - trade.entry_date).days
            stop = _get_stop_loss(trade)
            profit = _get_take_profit(trade)

            if return_pct <= stop:
                trade.status = "closed"  # type: ignore[attr-defined]
                trade.exit_date = today  # type: ignore[attr-defined]
                trade.exit_price = current_price  # type: ignore[attr-defined]
                trade.exit_reason = "stop_loss"  # type: ignore[attr-defined]
                stopped += 1
                logger.info(f"Stop-loss: {code} at {return_pct}% (threshold={stop}%)")

            elif return_pct >= profit:
                trade.status = "closed"  # type: ignore[attr-defined]
                trade.exit_date = today  # type: ignore[attr-defined]
                trade.exit_price = current_price  # type: ignore[attr-defined]
                trade.exit_reason = "take_profit"  # type: ignore[attr-defined]
                took_profit += 1
                logger.info(f"Take-profit: {code} at {return_pct}% (threshold={profit}%)")

            elif days_held >= MAX_HOLD_DAYS:
                trade.status = "closed"  # type: ignore[attr-defined]
                trade.exit_date = today  # type: ignore[attr-defined]
                trade.exit_price = current_price  # type: ignore[attr-defined]
                trade.exit_reason = "expired"  # type: ignore[attr-defined]
                logger.info(f"Expired: {code} after {days_held} days at {return_pct}%")

        db.session.commit()
        return {"updated": updated, "stopped_out": stopped, "take_profit": took_profit}

    @staticmethod
    def update_recommendation_tracking() -> int:
        """Update price tracking for all recommendations not yet stopped/take-profit.

        Returns number of tracking records created.
        """
        from web.models import db, DailyRecommendation, RecommendationTracking

        today = date.today()

        # Get all recommendations that have simulated trades (open or recently closed)
        from web.models import SimulatedTrade
        trade_rec_ids = {
            row[0] for row in
            db.session.query(SimulatedTrade.recommendation_id).distinct().all()
            if row[0] is not None  # skip manual trades (recommendation_id=None)
        }

        if not trade_rec_ids:
            return 0

        recs = DailyRecommendation.query.filter(
            DailyRecommendation.id.in_(trade_rec_ids)
        ).all()

        # Fetch prices
        codes = list({r.code for r in recs})
        prices = SimulationService._fetch_latest_prices(codes)

        count = 0
        for rec in recs:
            if rec.code not in prices or prices[rec.code] <= 0:
                continue

            current_price = prices[rec.code]
            return_pct = round((current_price - rec.entry_price) / rec.entry_price * 100, 2)
            days_held = (today - rec.recommendation_date).days

            # Don't create duplicate entries for the same day
            existing = RecommendationTracking.query.filter_by(
                recommendation_id=rec.id, track_date=today
            ).first()
            if existing:
                existing.close_price = current_price  # type: ignore[attr-defined]
                existing.cumulative_return_pct = return_pct  # type: ignore[attr-defined]
                existing.days_held = days_held  # type: ignore[attr-defined]
                existing.is_stopped_out = return_pct <= STOP_LOSS_PCT  # type: ignore[attr-defined]
                existing.is_take_profit = return_pct >= TAKE_PROFIT_PCT  # type: ignore[attr-defined]
            else:
                track = RecommendationTracking(
                    recommendation_id=rec.id,
                    track_date=today,
                    close_price=current_price,
                    cumulative_return_pct=return_pct,
                    days_held=days_held,
                    is_stopped_out=return_pct <= STOP_LOSS_PCT,
                    is_take_profit=return_pct >= TAKE_PROFIT_PCT,
                )
                db.session.add(track)
                count += 1

        db.session.commit()
        return count

    @staticmethod
    def _fetch_latest_prices(codes: list[str]) -> dict[str, float]:
        """Fetch latest close prices for a list of stock codes.

        Uses mootdx daily batch, falls back to tencent for real-time.
        """
        result: dict[str, float] = {}
        if not codes:
            return result

        # Try mootdx kline batch first
        try:
            from data_provider.kline_provider import KlineProvider
            kp = KlineProvider()
            daily = kp.load_daily_batch(list(codes), bars=5)
            for code, df in daily.items():
                if df is not None and not df.empty:
                    result[code] = float(df.iloc[-1]["close"])
        except Exception:
            pass

        # Fallback: try tencent API for remaining
        remaining = [c for c in codes if c not in result]
        if remaining:
            try:
                from data_provider.tencent_fetcher import TencentFetcher
                tf = TencentFetcher()
                quotes = tf.fetch_codes(remaining)
                for q in quotes:
                    if q.price > 0:
                        result[q.code] = float(q.price)
            except Exception:
                pass

        return result

    @staticmethod
    def create_manual_trade(
        code: str, name: str, entry_price: float,
        notional: float = 10000.0,
        stop_loss_pct: float | None = None,
        take_profit_pct: float | None = None,
    ) -> dict:
        """Create a manually-added simulated trade (no recommendation_id)."""
        from datetime import date as _date
        from web.models import db, SimulatedTrade

        trade = SimulatedTrade(
            recommendation_id=None,
            code=str(code).zfill(6),
            name=name or code,
            entry_date=_date.today(),
            entry_price=float(entry_price),
            status="open",
            notional=float(notional),
            shares=int(float(notional) / float(entry_price)) if float(entry_price) > 0 else 0,
            stop_loss_pct=float(stop_loss_pct) if stop_loss_pct is not None else None,
            take_profit_pct=float(take_profit_pct) if take_profit_pct is not None else None,
            source="manual",
        )
        db.session.add(trade)
        db.session.commit()
        return {"status": "ok", "trade_id": trade.id}

    @staticmethod
    def update_single_price(trade) -> dict:
        """Refresh current price for a single trade and check exit conditions."""
        from datetime import date as _date
        from web.models import db

        code = (trade.recommendation.code if trade.recommendation_id
                else trade.code)
        if not code:
            return {"status": "error", "message": "No code"}

        prices = SimulationService._fetch_latest_prices([code])
        if code not in prices or prices[code] <= 0:
            return {"status": "error", "message": "Price not available"}

        current_price = prices[code]
        return_pct = round((current_price - trade.entry_price) / trade.entry_price * 100, 2)
        trade.return_pct = return_pct

        today = _date.today()
        stop = _get_stop_loss(trade)
        profit = _get_take_profit(trade)

        if return_pct <= stop:
            trade.status = "closed"
            trade.exit_date = today
            trade.exit_price = current_price
            trade.exit_reason = "stop_loss"
        elif return_pct >= profit:
            trade.status = "closed"
            trade.exit_date = today
            trade.exit_price = current_price
            trade.exit_reason = "take_profit"

        db.session.commit()
        return {"status": "ok", "price": current_price, "return_pct": return_pct}
