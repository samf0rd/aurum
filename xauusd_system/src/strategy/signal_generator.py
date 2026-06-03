"""
strategy/signal_generator.py
─────────────────────────────
Pure strategy logic implementing the XAU/USD trend-following rules
from the validated strategy specification.

Key design decisions:
  - Stateless: on_bar() takes a full bar history, returns a Signal.
    No internal state means trivial unit testing and no hidden bugs from
    out-of-order bar delivery.
  - No I/O: no logging, no network calls. Logging is injected externally.
  - All indicators computed from raw OHLCV only.
"""

from __future__ import annotations

from decimal import Decimal
from datetime import datetime
from typing import Sequence

import numpy as np

from core.interfaces import (
    Bar, ISignalGenerator, IRegimeDetector, Regime,
    Signal, SignalType
)
from core.config import ACTIVE_PROFILE


# ──────────────────────────────────────────────────────────────────
# Indicator helpers (pure functions, no side effects)
# ──────────────────────────────────────────────────────────────────

def _closes(bars: Sequence[Bar]) -> np.ndarray:
    return np.array([float(b.close) for b in bars], dtype=np.float64)

def _highs(bars: Sequence[Bar]) -> np.ndarray:
    return np.array([float(b.high) for b in bars], dtype=np.float64)

def _lows(bars: Sequence[Bar]) -> np.ndarray:
    return np.array([float(b.low) for b in bars], dtype=np.float64)


def sma(values: np.ndarray, period: int) -> float:
    """Simple moving average of last `period` values."""
    if len(values) < period:
        raise ValueError(f"Need {period} bars, got {len(values)}")
    return float(np.mean(values[-period:]))


def atr(bars: Sequence[Bar], period: int = 14) -> float:
    """
    Wilder ATR. True range = max(H-L, |H-Cprev|, |L-Cprev|).
    Uses Wilder's smoothing (EMA with alpha=1/period).
    """
    if len(bars) < period + 1:
        raise ValueError(f"ATR requires {period + 1} bars, got {len(bars)}")

    highs  = _highs(bars)
    lows   = _lows(bars)
    closes = _closes(bars)

    tr = np.maximum(
        highs[1:] - lows[1:],
        np.maximum(
            np.abs(highs[1:] - closes[:-1]),
            np.abs(lows[1:]  - closes[:-1])
        )
    )
    # Seed with simple mean of first `period` TRs, then Wilder smooth
    atr_val = float(np.mean(tr[:period]))
    alpha   = 1.0 / period
    for tr_val in tr[period:]:
        atr_val = alpha * tr_val + (1 - alpha) * atr_val
    return atr_val


def donchian_high(bars: Sequence[Bar], period: int) -> float:
    """Highest high over last `period` bars (excluding the current bar)."""
    return float(np.max(_highs(bars[-period - 1:-1])))


def donchian_low(bars: Sequence[Bar], period: int) -> float:
    """Lowest low over last `period` bars (excluding the current bar)."""
    return float(np.min(_lows(bars[-period - 1:-1])))


def adx(bars: Sequence[Bar], period: int = 14) -> float:
    """
    Average Directional Index (Wilder smoothing).
    Pure-OHLCV implementation — no external TA library needed.
    """
    if len(bars) < period * 2:
        return 0.0

    highs  = _highs(bars)
    lows   = _lows(bars)
    closes = _closes(bars)

    plus_dm  = np.where((highs[1:] - highs[:-1]) > (lows[:-1] - lows[1:]),
                         np.maximum(highs[1:] - highs[:-1], 0), 0)
    minus_dm = np.where((lows[:-1] - lows[1:]) > (highs[1:] - highs[:-1]),
                         np.maximum(lows[:-1] - lows[1:], 0), 0)

    tr = np.maximum(
        highs[1:] - lows[1:],
        np.maximum(
            np.abs(highs[1:] - closes[:-1]),
            np.abs(lows[1:]  - closes[:-1])
        )
    )

    def wilder_smooth(arr: np.ndarray) -> np.ndarray:
        result = np.zeros_like(arr)
        result[period - 1] = np.sum(arr[:period])
        for i in range(period, len(arr)):
            result[i] = result[i-1] - (result[i-1] / period) + arr[i]
        return result

    atr14     = wilder_smooth(tr)
    plus_di   = 100 * wilder_smooth(plus_dm) / np.where(atr14 == 0, 1, atr14)
    minus_di  = 100 * wilder_smooth(minus_dm) / np.where(atr14 == 0, 1, atr14)
    dx        = 100 * np.abs(plus_di - minus_di) / np.where(
                    (plus_di + minus_di) == 0, 1, (plus_di + minus_di))

    # Smooth DX into ADX
    adx_arr      = np.zeros_like(dx)
    adx_arr[period - 1] = np.mean(dx[:period])
    for i in range(period, len(dx)):
        adx_arr[i] = ((adx_arr[i-1] * (period - 1)) + dx[i]) / period

    return float(adx_arr[-1])


def vol_ratio(bars: Sequence[Bar], lookback: int = 100) -> float:
    """
    Rule 1c: current ATR/Close ratio vs its 100-day median.
    Returns the ratio of today's value to the median.
    > 2.0 triggers the volatility cap.
    """
    if len(bars) < lookback + 15:
        return 1.0

    atr14    = atr(bars, 14)
    close    = float(bars[-1].close)
    today    = atr14 / close if close > 0 else 0

    # Historical ATR/Close ratios (each day in the lookback window)
    ratios   = []
    for i in range(lookback):
        idx   = len(bars) - lookback + i
        a     = atr(bars[:idx], 14)
        c     = float(bars[idx - 1].close)
        ratios.append(a / c if c > 0 else 0)

    median = float(np.median(ratios))
    return today / median if median > 0 else 1.0


# ──────────────────────────────────────────────────────────────────
# Regime Detector
# ──────────────────────────────────────────────────────────────────

class RegimeDetector(IRegimeDetector):
    """
    Implements Rules 1a, 1b, 1c from the strategy spec.

    1a — Trend direction  : Close vs 200-SMA
    1b — Trend strength   : ADX(14) ≥ 20
    1c — Vol regime cap   : ATR/Close ratio ≤ 2× 100-day median
    """

    ADX_THRESHOLD   = ACTIVE_PROFILE.adx_threshold
    VOL_CAP_RATIO   = ACTIVE_PROFILE.vol_ratio_cap
    SMA_TREND       = ACTIVE_PROFILE.sma_period
    SMA_SLOPE       = 50
    SLOPE_PERIOD    = 20

    def detect(self, bars: Sequence[Bar]) -> Regime:
        closes = _closes(bars)
        last_close = float(bars[-1].close)

        # ── Rule 1c: vol cap (checked first — overrides everything)
        if vol_ratio(bars) > self.VOL_CAP_RATIO:
            return Regime.HIGH_VOL

        # ── Rule 1a: trend direction
        sma200 = sma(closes, self.SMA_TREND)
        above_200 = last_close > sma200

        # ── Rule 1b: trend strength via ADX
        adx14 = adx(bars, 14)
        trending = adx14 >= self.ADX_THRESHOLD

        if not trending:
            return Regime.CHOPPY

        return Regime.TRENDING_BULL if above_200 else Regime.TRENDING_BEAR


# ──────────────────────────────────────────────────────────────────
# Signal Generator
# ──────────────────────────────────────────────────────────────────

class DonchianBreakoutSignalGenerator(ISignalGenerator):
    """
    Implements Rules 2, 3a, 3b from the strategy spec.

    Entry  (Rule 2):  Close > Donchian-20 high (long) or < Donchian-20 low (short)
    Exit 3a (Rule 3a): Close < Donchian-10 low (long) or > Donchian-10 high (short)
    Exit 3b (Rule 3b): Close crosses 200-SMA against position
    """

    ENTRY_PERIOD    = ACTIVE_PROFILE.donchian_entry
    EXIT_PERIOD     = ACTIVE_PROFILE.donchian_exit
    ATR_PERIOD      = ACTIVE_PROFILE.atr_period
    SMA_PERIOD      = ACTIVE_PROFILE.sma_period
    ATR_STOP_MULT   = Decimal(str(ACTIVE_PROFILE.atr_stop_mult))

    TRADEABLE_REGIMES = {
        Regime.TRENDING_BULL,
        Regime.TRENDING_BEAR,
    }

    def compute_indicators(self, bars: Sequence[Bar]) -> dict:
        closes = _closes(bars)
        return {
            "sma200":       sma(closes, self.SMA_PERIOD),
            "atr14":        atr(bars, self.ATR_PERIOD),
            "donchian20_h": donchian_high(bars, self.ENTRY_PERIOD),
            "donchian20_l": donchian_low(bars,  self.ENTRY_PERIOD),
            "donchian10_h": donchian_high(bars, self.EXIT_PERIOD),
            "donchian10_l": donchian_low(bars,  self.EXIT_PERIOD),
            "adx14":        adx(bars, 14),
            "vol_ratio":    vol_ratio(bars),
            "close":        float(bars[-1].close),
        }

    def on_bar(self, bars: Sequence[Bar], regime: Regime) -> Signal:
        """
        Evaluates the last completed bar.
        `bars` must be sorted oldest-first with ≥ 220 bars.
        """
        if len(bars) < self.SMA_PERIOD + self.ENTRY_PERIOD + 10:
            return self._no_signal(bars, regime, "insufficient history")

        if regime not in self.TRADEABLE_REGIMES:
            return self._no_signal(bars, regime, f"regime={regime.name}")

        ind    = self.compute_indicators(bars)
        last   = bars[-1]
        close  = float(last.close)
        atr14  = Decimal(str(round(ind["atr14"], 4)))
        stop_d = atr14 * self.ATR_STOP_MULT

        # ── Check exit conditions first (open position handled in orchestrator)
        # These are evaluated by the orchestrator against the active Position;
        # the signal generator returns the raw directional signal only.

        # ── Entry: Donchian-20 breakout in regime direction
        if regime == Regime.TRENDING_BULL:
            if close > ind["donchian20_h"]:
                return Signal(
                    signal_type   = SignalType.ENTER_LONG,
                    timestamp     = last.timestamp,
                    symbol        = last.symbol,
                    regime        = regime,
                    atr           = atr14,
                    stop_distance = stop_d,
                    reason        = (
                        f"Long breakout: close={close:.2f} > "
                        f"Donchian20H={ind['donchian20_h']:.2f}, "
                        f"ADX={ind['adx14']:.1f}, SMA200={ind['sma200']:.2f}"
                    ),
                )

        elif regime == Regime.TRENDING_BEAR:
            if close < ind["donchian20_l"]:
                return Signal(
                    signal_type   = SignalType.ENTER_SHORT,
                    timestamp     = last.timestamp,
                    symbol        = last.symbol,
                    regime        = regime,
                    atr           = atr14,
                    stop_distance = stop_d,
                    reason        = (
                        f"Short breakout: close={close:.2f} < "
                        f"Donchian20L={ind['donchian20_l']:.2f}, "
                        f"ADX={ind['adx14']:.1f}, SMA200={ind['sma200']:.2f}"
                    ),
                )

        return self._no_signal(bars, regime, "no breakout")

    def should_exit(
        self,
        bars: Sequence[Bar],
        position_side: str,
        regime: Regime,
    ) -> tuple[bool, str]:
        """
        Checks Rules 3a and 3b for an open position.
        Called by the orchestrator each bar; separate from on_bar()
        so we can check entries and exits independently.
        """
        ind   = self.compute_indicators(bars)
        close = float(bars[-1].close)

        # Rule 3b: regime invalidated
        if position_side == "LONG" and close < ind["sma200"]:
            return True, f"Rule 3b: close {close:.2f} < SMA200 {ind['sma200']:.2f}"
        if position_side == "SHORT" and close > ind["sma200"]:
            return True, f"Rule 3b: close {close:.2f} > SMA200 {ind['sma200']:.2f}"

        # Rule 3a: Donchian-10 trailing exit
        if position_side == "LONG" and close < ind["donchian10_l"]:
            return True, f"Rule 3a: close {close:.2f} < Donchian10L {ind['donchian10_l']:.2f}"
        if position_side == "SHORT" and close > ind["donchian10_h"]:
            return True, f"Rule 3a: close {close:.2f} > Donchian10H {ind['donchian10_h']:.2f}"

        return False, ""

    # ── helpers

    def _no_signal(self, bars: Sequence[Bar], regime: Regime, reason: str) -> Signal:
        last = bars[-1]
        try:
            atr14 = Decimal(str(round(atr(bars, self.ATR_PERIOD), 4)))
        except ValueError:
            atr14 = Decimal("0")
        return Signal(
            signal_type   = SignalType.NO_SIGNAL,
            timestamp     = last.timestamp,
            symbol        = last.symbol,
            regime        = regime,
            atr           = atr14,
            stop_distance = atr14 * self.ATR_STOP_MULT,
            reason        = reason,
        )
