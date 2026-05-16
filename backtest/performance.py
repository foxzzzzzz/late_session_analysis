"""绩效计算 — 胜率/夏普/最大回撤/Calmar/月度统计"""
import numpy as np
import pandas as pd
from typing import Optional

from backtest.trade_log import Trade


class PerformanceCalculator:
    @staticmethod
    def calculate(trades: list[Trade]) -> dict:
        closed = [t for t in trades if t.return_pct is not None]
        if not closed:
            return {"total_trades": 0, "message": "无有效交易记录"}

        returns = np.array([t.return_pct for t in closed])

        # 基础统计
        win_rate = sum(1 for r in returns if r > 0) / len(returns) * 100
        avg_return = float(np.mean(returns))
        median_return = float(np.median(returns))
        std_return = float(np.std(returns, ddof=1)) if len(returns) > 1 else 0.0
        total_return = float(np.sum(returns))

        # 盈亏比
        winners = [r for r in returns if r > 0]
        losers = [r for r in returns if r <= 0]
        avg_win = float(np.mean(winners)) if winners else 0.0
        avg_loss = float(np.mean(losers)) if losers else 0.0
        profit_factor = abs(sum(winners) / sum(losers)) if losers and sum(losers) != 0 else float("inf")
        win_loss_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else float("inf")

        # 累计收益与回撤
        cumulative = np.cumprod(1 + returns / 100)
        running_max = np.maximum.accumulate(cumulative)
        drawdowns = (cumulative - running_max) / running_max * 100
        max_drawdown = float(abs(min(drawdowns)))

        # 夏普比率 (每笔交易=1天持有期)
        sharpe = float(np.mean(returns) / np.std(returns, ddof=1) * np.sqrt(252)) if std_return > 0 else 0.0

        # Calmar 比率
        calmar = float(avg_return / max_drawdown * 252) if max_drawdown > 0 else 0.0

        # 胜率序列 (连续盈利/亏损)
        max_consecutive_wins = PerformanceCalculator._max_consecutive(returns, lambda r: r > 0)
        max_consecutive_losses = PerformanceCalculator._max_consecutive(returns, lambda r: r <= 0)

        return {
            "total_trades": len(closed),
            "win_rate": round(win_rate, 2),
            "avg_return_pct": round(avg_return, 4),
            "median_return_pct": round(median_return, 4),
            "std_return_pct": round(std_return, 4),
            "total_return_pct": round(total_return, 2),
            "avg_win_pct": round(avg_win, 4),
            "avg_loss_pct": round(avg_loss, 4),
            "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else "∞",
            "win_loss_ratio": round(win_loss_ratio, 2) if win_loss_ratio != float("inf") else "∞",
            "max_drawdown_pct": round(max_drawdown, 2),
            "sharpe_ratio": round(sharpe, 2),
            "calmar_ratio": round(calmar, 2),
            "max_consecutive_wins": max_consecutive_wins,
            "max_consecutive_losses": max_consecutive_losses,
        }

    @staticmethod
    def monthly_breakdown(trades: list[Trade]) -> pd.DataFrame:
        closed = [t for t in trades if t.return_pct is not None]
        if not closed:
            return pd.DataFrame()

        df = pd.DataFrame([{
            "date": t.date,
            "month": t.date[:6],
            "return_pct": t.return_pct,
            "recommendation": t.recommendation,
        } for t in closed])

        monthly = df.groupby("month").agg(
            trade_count=("return_pct", "count"),
            win_rate=("return_pct", lambda x: (x > 0).sum() / len(x) * 100),
            avg_return=("return_pct", "mean"),
            total_return=("return_pct", "sum"),
        ).round(2)
        return monthly

    @staticmethod
    def score_stratified(trades: list[Trade]) -> dict:
        closed = [t for t in trades if t.return_pct is not None]
        result = {}
        for level in ["strong_buy", "buy", "watch"]:
            subset = [t for t in closed if t.recommendation == level]
            if subset:
                returns = [t.return_pct for t in subset]
                result[level] = {
                    "count": len(subset),
                    "win_rate": round(sum(1 for r in returns if r > 0) / len(returns) * 100, 2),
                    "avg_return": round(float(np.mean(returns)), 4),
                    "total_return": round(float(np.sum(returns)), 2),
                }
        return result

    @staticmethod
    def daily_equity_curve(trades: list[Trade]) -> list[float]:
        closed = [t for t in trades if t.return_pct is not None]
        cumulative = 1.0
        curve = [cumulative]
        for t in closed:
            cumulative *= (1 + t.return_pct / 100)
            curve.append(round(cumulative, 6))
        return curve

    @staticmethod
    def _max_consecutive(arr, condition) -> int:
        max_streak = current = 0
        for v in arr:
            if condition(v):
                current += 1
                max_streak = max(max_streak, current)
            else:
                current = 0
        return max_streak
