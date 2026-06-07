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

from collections import deque
from decimal import Decimal
from datetime import datetime
from typing import Sequence

import numpy as np

from core.interfaces import (
    Bar, ISignalGenerator, IRegimeDetector, Regime,
    Signal, SignalType
)
from core.config import ACTIVE_PROFILE, StrategyProfile


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


def _atr_series(bars: Sequence[Bar], period: int = 14) -> np.ndarray:
    """Return the full Wilder ATR series (same algorithm as atr(), vectorized)."""
    if len(bars) < period + 1:
        return np.array([])
    highs  = _highs(bars)
    lows   = _lows(bars)
    closes = _closes(bars)
    tr = np.maximum(
        highs[1:] - lows[1:],
        np.maximum(np.abs(highs[1:] - closes[:-1]), np.abs(lows[1:] - closes[:-1]))
    )
    result = np.zeros(len(tr))
    result[period - 1] = float(np.mean(tr[:period]))
    alpha = 1.0 / period
    for i in range(period, len(tr)):
        result[i] = alpha * tr[i] + (1.0 - alpha) * result[i - 1]
    return result


def atr_above_median(bars: Sequence[Bar], lookback: int = 2016) -> bool:
    """
    Returns True when the current ATR/Close ratio is above its N-bar median.
    Used as a volatility FLOOR filter: only trade in "hot" periods.

    lookback = number of bars to compute the ATR/Close median over.
    A bar whose ATR/Close is below the rolling median is a "grinding" bar.
    """
    if len(bars) < lookback + 16:
        return True  # insufficient history → don't block

    atr_vals = _atr_series(bars, 14)
    if len(atr_vals) < lookback + 1:
        return True

    closes = _closes(bars)
    # Last lookback bars of the ATR/Close series (excluding current bar)
    atr_hist   = atr_vals[-lookback - 1:-1]
    close_hist = closes[-lookback - 1:-1]
    hist_ratios = np.where(close_hist > 0, atr_hist / close_hist, 0.0)

    today_atr   = atr_vals[-1]
    today_close = float(bars[-1].close)
    today_ratio = today_atr / today_close if today_close > 0 else 0.0

    median = float(np.median(hist_ratios))
    return today_ratio >= median if median > 0 else True


def vol_ratio(bars: Sequence[Bar], lookback: int = 100) -> float:
    """
    Rule 1c: current ATR/Close ratio vs its 100-day median.
    Returns the ratio of today's value to the median.
    > 2.0 triggers the volatility cap.
    """
    if len(bars) < lookback + 15:
        return 1.0

    # Precompute full ATR series once (O(n)) instead of 100 separate atr() calls
    atr_vals  = _atr_series(bars, 14)
    if len(atr_vals) < lookback + 1:
        return 1.0

    closes = _closes(bars)
    # Match original indexing: old code used ATR/close at bars[N-101+i] for i=0..99
    # atr_vals[j] = ATR at bars[j+1], so ATR at bars[N-101+i] = atr_vals[N-102+i]
    # = atr_vals[-lookback-1:-1]; closes at bars[N-101+i] = closes[-lookback-1:-1]
    atr_hist   = atr_vals[-lookback - 1:-1]
    close_hist = closes[-lookback - 1:-1]
    ratios     = np.where(close_hist > 0, atr_hist / close_hist, 0.0)

    today_atr   = atr_vals[-1]
    today_close = float(bars[-1].close)
    today       = today_atr / today_close if today_close > 0 else 0.0

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

    Pass a StrategyProfile to override any parameter (including
    entry_confirmation_bars). Defaults to ACTIVE_PROFILE so existing
    call sites that pass no arguments continue to work unchanged.
    """

    TRADEABLE_REGIMES = {
        Regime.TRENDING_BULL,
        Regime.TRENDING_BEAR,
    }

    def __init__(
        self,
        profile: StrategyProfile | None = None,
        h1_sma_lookup: dict | None = None,
    ) -> None:
        _p = profile if profile is not None else ACTIVE_PROFILE
        self.ENTRY_PERIOD       = _p.donchian_entry
        self.EXIT_PERIOD        = _p.donchian_exit
        self.ATR_PERIOD         = _p.atr_period
        self.SMA_PERIOD         = _p.sma_period
        self.ATR_STOP_MULT      = Decimal(str(_p.atr_stop_mult))
        self._confirmation_bars = _p.entry_confirmation_bars
        self._long_only         = _p.long_only
        self._atr_vol_filter    = _p.atr_vol_filter
        self._atr_vol_lookback  = _p.atr_vol_lookback
        # Full bar history for the ATR vol floor filter. The engine's MAX_WINDOW
        # (500 bars) is far smaller than the 12096-bar lookback, so we maintain a
        # separate deque fed by accumulate() on every bar rather than relying on
        # the truncated window passed to on_bar().
        self._vol_history: deque[Bar] | None = (
            deque(maxlen=_p.atr_vol_lookback + 20) if _p.atr_vol_filter else None
        )
        self._vol_ratio_floor   = _p.vol_ratio_floor
        self._session_filter    = _p.session_filter
        self._h1_trend_gate     = _p.h1_trend_gate
        # Pre-computed H1 200-SMA lookup: timestamp ISO string → sma value.
        # None means the gate is open for all bars (e.g., warmup period).
        self._h1_sma_lookup: dict | None = h1_sma_lookup

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

    def accumulate(self, bar: Bar) -> None:
        """Feed every bar into the internal ATR vol history, regardless of position state."""
        if self._vol_history is not None:
            self._vol_history.append(bar)

    def on_bar(self, bars: Sequence[Bar], regime: Regime) -> Signal:
        """
        Evaluates the last completed bar.
        `bars` must be sorted oldest-first with ≥ 220 bars.
        """
        if len(bars) < self.SMA_PERIOD + self.ENTRY_PERIOD + 10:
            return self._no_signal(bars, regime, "insufficient history")

        if regime not in self.TRADEABLE_REGIMES:
            return self._no_signal(bars, regime, f"regime={regime.name}")

        if self._long_only and regime == Regime.TRENDING_BEAR:
            return self._no_signal(bars, regime, "long_only: bear signals suppressed")

        if self._atr_vol_filter:
            hist = list(self._vol_history) if self._vol_history is not None else list(bars)
            if not atr_above_median(hist, self._atr_vol_lookback):
                return self._no_signal(bars, regime, "atr_vol_filter: below median ATR")

        if self._vol_ratio_floor > 0.0:
            vr = vol_ratio(bars)
            if vr < self._vol_ratio_floor:
                return self._no_signal(bars, regime, f"vol_ratio_floor: {vr:.2f} < {self._vol_ratio_floor}")

        if self._session_filter:
            h = bars[-1].timestamp.hour
            if not (8 <= h < 12 or 13 <= h < 17):
                return self._no_signal(bars, regime, f"session_filter: hour={h}UTC outside London/NY")

        if self._h1_trend_gate and self._h1_sma_lookup is not None:
            ts_key = bars[-1].timestamp.isoformat()
            h1_sma = self._h1_sma_lookup.get(ts_key)
            if h1_sma is not None and float(bars[-1].close) < h1_sma:
                return self._no_signal(bars, regime, f"h1_trend_gate: close={float(bars[-1].close):.2f} < H1_SMA200={h1_sma:.2f}")

        ind    = self.compute_indicators(bars)
        last   = bars[-1]
        close  = float(last.close)
        atr14  = Decimal(str(round(ind["atr14"], 4)))
        stop_d = atr14 * self.ATR_STOP_MULT

        if self._confirmation_bars > 0:
            confirmed = self._check_confirmation(bars, regime, ind)
            if confirmed is not None:
                return confirmed
            return self._no_signal(bars, regime, "no confirmed breakout")

        # ── Entry: Donchian-20 breakout in regime direction (confirmation=0)
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

    def _check_confirmation(
        self,
        bars: Sequence[Bar],
        regime: Regime,
        ind: dict,
    ) -> "Signal | None":
        """
        Stateless lookback confirmation for entry_confirmation_bars = N (N ≥ 1).

        Checks that:
          1. bars[-N-1] (the bar N steps before the current bar) closed above
             (LONG) or below (SHORT) the Donchian-20 band as it stood at that
             bar's time.
          2. Every bar from bars[-N] through bars[-1] (the current bar) still
             closes on the correct side of that original breakout band level.

        Returns a Signal if all conditions hold, None otherwise.
        The signal is timestamped to the current bar so fill semantics are
        identical to the confirmation=0 path.
        """
        n = self._confirmation_bars
        # Need one extra bar (bars[-n-2]) to verify the fresh-breakout condition.
        if len(bars) < self.SMA_PERIOD + self.ENTRY_PERIOD + 10 + n + 1:
            return None

        # bars[:-n] ends at bars[-n-1], the breakout-candidate bar.
        # donchian_high/low on this sub-window gives the band that bar
        # was compared against (same computation on_bar would have done
        # at that earlier point in time).
        breakout_window = bars[:-n]         # window whose last bar is the breakout candidate
        pre_window      = bars[:-n - 1]     # window whose last bar is the bar BEFORE the breakout
        breakout_close  = float(breakout_window[-1].close)
        pre_close       = float(pre_window[-1].close)   # close of the bar before the breakout
        conf_bars       = bars[-n:]         # the N confirmation bars, incl. current

        last   = bars[-1]
        atr14  = Decimal(str(round(ind["atr14"], 4)))
        stop_d = atr14 * self.ATR_STOP_MULT

        if regime == Regime.TRENDING_BULL:
            band     = donchian_high(breakout_window, self.ENTRY_PERIOD)
            pre_band = donchian_high(pre_window,      self.ENTRY_PERIOD)
            if breakout_close <= band:
                return None  # breakout bar didn't actually break out
            # Fresh-breakout guard: the bar before the breakout candidate must
            # have been BELOW its own Donchian band.  This ensures we only signal
            # on the first crossing, preventing re-triggers on every bar of a
            # sustained uptrend (which would cause double the trades vs confirmation=0).
            if pre_close > pre_band:
                return None
            for cb in conf_bars:
                if float(cb.close) <= band:
                    return None  # price retraced through breakout level
            return Signal(
                signal_type   = SignalType.ENTER_LONG,
                timestamp     = last.timestamp,
                symbol        = last.symbol,
                regime        = regime,
                atr           = atr14,
                stop_distance = stop_d,
                reason        = (
                    f"Long breakout confirmed after {n} bar(s): "
                    f"breakout_close={breakout_close:.2f} > band={band:.2f}"
                ),
            )

        if regime == Regime.TRENDING_BEAR:
            band     = donchian_low(breakout_window, self.ENTRY_PERIOD)
            pre_band = donchian_low(pre_window,      self.ENTRY_PERIOD)
            if breakout_close >= band:
                return None
            # Fresh-breakout guard for shorts: bar before must have been ABOVE its band.
            if pre_close < pre_band:
                return None
            for cb in conf_bars:
                if float(cb.close) >= band:
                    return None
            return Signal(
                signal_type   = SignalType.ENTER_SHORT,
                timestamp     = last.timestamp,
                symbol        = last.symbol,
                regime        = regime,
                atr           = atr14,
                stop_distance = stop_d,
                reason        = (
                    f"Short breakout confirmed after {n} bar(s): "
                    f"breakout_close={breakout_close:.2f} < band={band:.2f}"
                ),
            )

        return None

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
