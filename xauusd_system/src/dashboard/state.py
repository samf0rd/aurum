from __future__ import annotations
import json, math, time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional


@dataclass
class TradeRecord:
    trade_id: str; symbol: str; side: str; size: float
    entry_price: float; exit_price: Optional[float]; pnl: Optional[float]
    opened_at: str; closed_at: Optional[str] = None


@dataclass
class PositionSnapshot:
    symbol: str; side: str; size: float
    entry_px: float; current_px: float; unrealized_pnl: float
    hard_stop: float = 0.0


class SystemState:
    def __init__(self, initial_equity: Decimal):
        self.initial_equity = float(initial_equity)
        self.equity_curve: list[tuple[str, float]] = [
            (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), float(initial_equity))]
        self.trades: list[TradeRecord] = []
        self.position: Optional[PositionSnapshot] = None
        self.regime = "NEUTRAL"; self.adx = 0.0
        self.started_at = time.time()
        # Live price fields (updated by _price_loop every 5 s)
        self.current_price: Optional[float] = None
        self.last_tick_ts: Optional[Any] = None
        self.last_spread: float = 0.0
        self.last_median_spread: float = 0.0
        self.last_indicators: dict = {}
        self.daily_trade_count: int = 0
        # Last signal (updated by process_bar on each H1 close)
        self.last_signal_type: str = "NO_SIGNAL"
        self.last_signal_reason: str = ""
        self.last_signal_ts: Optional[Any] = None
        # Bar evaluation timestamp — used by the UI countdown timer
        self.last_bar_eval_ts: str = ""
        # Live forming H1 candle — updated each price tick for the chart
        self.forming_candle: dict = {}
        Path("logs").mkdir(exist_ok=True)

    def record_equity(self, equity: float) -> None:
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.equity_curve.append((ts, round(equity, 2)))
        with open("logs/equity_curve.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": ts, "equity": equity}) + "\n")

    def record_trade(self, t: TradeRecord) -> None:
        self.trades.append(t)
        with open("logs/paper_trades.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(t.__dict__) + "\n")

    def update_position(self, p: Optional[PositionSnapshot]) -> None:
        self.position = p

    def update_regime(self, regime: str, adx: float) -> None:
        self.regime = regime; self.adx = adx

    def get_stats(self) -> dict[str, Any]:
        eq = [e for _, e in self.equity_curve]
        closed = [t for t in self.trades if t.pnl is not None]
        if len(eq) < 2:
            return {"sharpe": 0, "max_drawdown": 0, "win_rate": 0,
                    "total_trades": 0, "total_pnl": 0,
                    "uptime_s": round(time.time() - self.started_at)}
        rets = [eq[i]/eq[i-1]-1 for i in range(1, len(eq))]
        mu = sum(rets)/len(rets)
        var = sum((r-mu)**2 for r in rets)/max(len(rets)-1, 1)
        sharpe = (mu/math.sqrt(var)*math.sqrt(252)) if var > 0 else 0
        peak = eq[0]; mdd = 0.0
        for e in eq:
            if e > peak: peak = e
            mdd = max(mdd, (peak-e)/peak)
        wins = sum(1 for t in closed if (t.pnl or 0) > 0)
        return {
            "sharpe": round(sharpe, 3),
            "max_drawdown": round(mdd*100, 2),
            "win_rate": round(wins/len(closed)*100, 1) if closed else 0,
            "total_trades": len(closed),
            "total_pnl": round(sum(t.pnl or 0 for t in closed), 2),
            "uptime_s": round(time.time() - self.started_at),
        }
