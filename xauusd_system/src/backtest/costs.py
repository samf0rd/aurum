"""
backtest/costs.py — realistic cost model for XAU/USD backtesting.

Models
──────
1. Spread      — session-dependent: tighter during London/NY, wider overnight / Asian.
2. Slippage    — fraction of ATR on entries and stop fills.
3. Gap fill    — if bar opens past a stop, fill is at the open, not the stop.
4. Commission  — zero (CFD/spread-only instrument for now).

All prices are raw floats (not Decimal) for speed inside the hot loop.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class CostModel:
    """
    Parameters for the cost model.

    entry_slippage_frac : fraction of ATR added to entry fill (both sides).
    stop_slippage_frac  : fraction of ATR added to stop-out fill (adverse direction).
    london_ny_spread    : spread during London / NY overlap (07:00–17:00 UTC).
    asian_spread        : spread during Asian / overnight session.
    """
    entry_slippage_frac: float = 0.05   # 5% of ATR
    stop_slippage_frac:  float = 0.10   # 10% of ATR (stops are harder to fill cleanly)
    london_ny_spread:    float = 0.35   # USD — typical XAU/USD in active hours
    asian_spread:        float = 0.70   # USD — wider in thin sessions

    def spread_at(self, bar_open_utc: datetime) -> float:
        """Return modelled spread for the bar based on UTC open time."""
        hour = bar_open_utc.hour
        # London/NY overlap roughly 07:00–17:00 UTC
        if 7 <= hour < 17:
            return self.london_ny_spread
        return self.asian_spread

    def entry_fill(self, side: str, bar_open: float, atr: float) -> tuple[float, float]:
        """
        Compute fill price and slippage cost for an entry at bar open.

        Returns (fill_price, slippage_paid).
        Slippage is adverse — added to cost of entry.
        """
        slip = atr * self.entry_slippage_frac
        if side == "LONG":
            fill_px = bar_open + slip
        else:
            fill_px = bar_open - slip
        return fill_px, slip

    def exit_fill(self, side: str, bar_open: float, atr: float) -> tuple[float, float]:
        """
        Fill price for a normal (signal-triggered) exit at next-bar open.
        Slippage is adverse to the exit direction.
        """
        slip = atr * self.entry_slippage_frac
        if side == "LONG":
            fill_px = bar_open - slip
        else:
            fill_px = bar_open + slip
        return fill_px, slip

    def stop_fill(
        self,
        side: str,
        stop_price: float,
        bar_open: float,
        bar_low: float,
        bar_high: float,
        atr: float,
    ) -> tuple[bool, float, float]:
        """
        Determine whether the stop was hit during a bar, and at what price.

        Returns (was_hit, fill_price, slippage_paid).

        Gap rule: if bar opens on the adverse side of the stop, fill at bar open
        (not the stop price).  This is where trend systems actually lose money —
        a stop at 3 300 means nothing if the bar opens at 3 270.
        """
        slip = atr * self.stop_slippage_frac

        if side == "LONG":
            if bar_low <= stop_price:
                # Gap-down: bar opened below stop → fill at open
                if bar_open <= stop_price:
                    return True, bar_open, abs(stop_price - bar_open)
                # Normal stop hit intrabar
                return True, stop_price - slip, slip
        else:  # SHORT
            if bar_high >= stop_price:
                # Gap-up: bar opened above stop → fill at open
                if bar_open >= stop_price:
                    return True, bar_open, abs(bar_open - stop_price)
                # Normal stop hit intrabar
                return True, stop_price + slip, slip

        return False, 0.0, 0.0
