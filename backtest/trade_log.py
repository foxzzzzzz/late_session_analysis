"""交易记录 — Trade, DayResult, TradeLogRecorder"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Trade:
    date: str
    code: str
    name: str
    entry_price: float
    exit_price: Optional[float] = None
    return_pct: Optional[float] = None
    recommendation: str = ""
    total_score: float = 0.0
    anomaly_type: str = ""
    sector: str = ""
    score_tail: float = 0.0
    score_tech: float = 0.0
    score_capital: float = 0.0
    score_env: float = 0.0
    score_history: float = 0.0
    score_fundamental: float = 0.0
    exit_reason: str = "next_open"  # "stop_loss" | "take_profit" | "next_open"


@dataclass
class DayResult:
    date: str
    trades: list[Trade] = field(default_factory=list)
    total_screened: int = 0
    s1_count: int = 0
    s2_count: int = 0
    s3_count: int = 0
    s4_count: int = 0
    buy_signals: int = 0
    elapsed_seconds: float = 0.0


class TradeLogRecorder:
    def __init__(self):
        self.days: list[DayResult] = []

    def record_day(self, result: DayResult):
        self.days.append(result)

    def all_trades(self) -> list[Trade]:
        trades = []
        for d in self.days:
            trades.extend(d.trades)
        return trades

    def closed_trades(self) -> list[Trade]:
        return [t for t in self.all_trades() if t.return_pct is not None]

    def trades_by_recommendation(self, rec: str) -> list[Trade]:
        return [t for t in self.closed_trades() if t.recommendation == rec]

    def total_days(self) -> int:
        return len(self.days)

    def days_with_signals(self) -> int:
        return sum(1 for d in self.days if d.buy_signals > 0)
