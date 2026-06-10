from __future__ import annotations
import json, math, time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
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
        # Last fetched OHLCV bars for the chart — shared with /api/bars as a cache
        self.last_bars: list[dict] = []
        self.last_bars_ts: float = 0.0
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
        eq     = [e for _, e in self.equity_curve]
        closed = [t for t in self.trades if t.pnl is not None]

        # Max drawdown from equity curve
        peak = eq[0] if eq else float(self.initial_equity)
        mdd  = 0.0
        for e in eq:
            if e > peak:
                peak = e
            mdd = max(mdd, (peak - e) / peak if peak > 0 else 0.0)

        current_equity = eq[-1] if eq else float(self.initial_equity)
        current_dd_pct = round(max(0.0, (peak - current_equity) / peak) * 100, 2) if peak > 0 else 0.0

        wins       = [t for t in closed if (t.pnl or 0) > 0]
        losses     = [t for t in closed if (t.pnl or 0) < 0]
        gross_win  = sum(t.pnl for t in wins)
        gross_loss = abs(sum(t.pnl for t in losses))
        n          = len(closed)

        profit_factor  = round(gross_win / gross_loss, 3) if gross_loss > 0 else None
        expectancy_usd = round(sum(t.pnl for t in closed) / n, 2) if n > 0 else None

        # Sharpe — only meaningful at n ≥ 100 trades; annualise per trade-frequency
        # (not per equity-point which gives spurious precision on small samples)
        sharpe = None
        if n >= 100:
            pnls = [t.pnl for t in closed]
            mu_pnl  = sum(pnls) / n
            var_pnl = sum((p - mu_pnl) ** 2 for p in pnls) / (n - 1)
            if var_pnl > 0:
                sharpe = round(mu_pnl / math.sqrt(var_pnl) * math.sqrt(n), 3)

        today_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0)
        week_start = today_start - timedelta(days=today_start.weekday())
        daily_pnl = weekly_pnl = 0.0
        for t in closed:
            if t.closed_at:
                try:
                    ct = datetime.fromisoformat(t.closed_at.replace("Z", "+00:00"))
                    if ct.tzinfo is None:
                        ct = ct.replace(tzinfo=timezone.utc)
                    pnl = t.pnl or 0.0
                    if ct >= today_start:
                        daily_pnl  += pnl
                    if ct >= week_start:
                        weekly_pnl += pnl
                except (ValueError, AttributeError):
                    pass

        return {
            # Primary metrics — profit factor and expectancy are honest for
            # a low-win-rate trend follower; never show Sharpe at n < 100
            "profit_factor":       profit_factor,
            "expectancy_usd":      expectancy_usd,
            "win_rate":            round(len(wins) / n * 100, 1) if n > 0 else 0,
            "n_trades":            n,
            "n_wins":              len(wins),
            "n_losses":            len(losses),
            "total_pnl":           round(sum(t.pnl or 0 for t in closed), 2),
            "gross_win":           round(gross_win, 2),
            "gross_loss":          round(gross_loss, 2),
            # Sharpe shown only at n ≥ 100; None means "insufficient sample"
            "sharpe":              sharpe,
            "sharpe_sample_note":  f"n={n}" if n < 100 else None,
            "max_drawdown":        round(mdd * 100, 2),
            "current_drawdown_pct": current_dd_pct,
            "peak_equity":         round(peak, 2),
            "current_equity":      round(current_equity, 2),
            "daily_pnl":           round(daily_pnl, 2),
            "weekly_pnl":          round(weekly_pnl, 2),
            "uptime_s":            round(time.time() - self.started_at),
            "data_source":         "live_paper",
        }
