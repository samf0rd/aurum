"""
core/config.py
──────────────
Strategy profile definitions.

Two profiles exist — Swing (H1) is the backtested, validated strategy.
Intraday (M15) runs the identical rule structure on a faster timeframe;
it must clear the same validation gates before being treated as proven.

Select via env var:  STRATEGY_PROFILE=intraday  (default)  or  swing
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class StrategyProfile:
    name:            str
    timeframe:       str    # OANDA granularity string, e.g. "H1" or "M15"
    bar_seconds:     int    # seconds per bar: 3600 for H1, 900 for M15
    sma_period:      int    # regime filter (SMA lookback)
    adx_period:      int
    adx_threshold:   float
    donchian_entry:  int    # breakout lookback
    donchian_exit:   int    # trailing-exit lookback
    atr_period:      int
    atr_stop_mult:   float
    vol_ratio_cap:   float
    risk_per_trade:  float
    # 0 = enter on breakout bar close (default, preserves original behaviour).
    # N = wait N additional bars; only enter if close still holds above the
    # breakout band on every bar through bar+N.
    entry_confirmation_bars: int = 0
    # When True, TRENDING_BEAR signals are suppressed — only LONG entries fire.
    long_only: bool = False


# ── Validated strategy — DO NOT CHANGE THESE NUMBERS ──────────────────
SWING = StrategyProfile(
    name="Swing (H1)",
    timeframe="H1", bar_seconds=3600,
    sma_period=200, adx_period=14, adx_threshold=20.0,
    donchian_entry=20, donchian_exit=10,
    atr_period=14, atr_stop_mult=2.0,
    vol_ratio_cap=2.0, risk_per_trade=0.01,
)

# ── Faster profile — same structure, faster clock ─────────────────────
# Round-number lookbacks only.  MUST be walk-forward validated before
# being used for any real evaluation.
INTRADAY = StrategyProfile(
    name="Intraday (M15)",
    timeframe="M15", bar_seconds=900,
    sma_period=200, adx_period=14, adx_threshold=20.0,
    donchian_entry=20, donchian_exit=10,
    atr_period=14, atr_stop_mult=2.0,
    vol_ratio_cap=2.0, risk_per_trade=0.01,
)

_PROFILES = {"swing": SWING, "intraday": INTRADAY}

ACTIVE_PROFILE: StrategyProfile = _PROFILES[
    os.getenv("STRATEGY_PROFILE", "swing").lower()
]
