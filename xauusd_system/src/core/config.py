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
    # ATR volatility floor filter: only fire entries when ATR/Close is ABOVE
    # its N-bar median. 0 = disabled. When enabled, gates out low-volatility
    # "grinding" periods where breakouts are less likely to sustain.
    atr_vol_filter: bool = False
    # Lookback period (in bars) for the ATR/Close median in the vol floor filter.
    # Tune to the timescale of the "grinding vs explosive" distinction you want to
    # capture. Default 2016 = ~21 trading days at M15 (96 bars/day).
    atr_vol_lookback: int = 2016
    # EXP-018: vol_ratio floor — only enter when vol_ratio >= this value.
    # vol_ratio = current ATR/Close ÷ median ATR/Close over 100 bars.
    # 0.0 = disabled. 1.3 means ATR must be 30% above its rolling median.
    vol_ratio_floor: float = 0.0
    # EXP-019: session filter — only fire entries during London (08-12 UTC)
    # and NY (13-17 UTC) sessions. False = no session restriction.
    session_filter: bool = False
    # EXP-020: H1 trend gate — only enter when M15 close is above the
    # H1 200-SMA. Requires pre-computed h1_sma_lookup passed to the engine.
    h1_trend_gate: bool = False


# ── Validated strategy — DO NOT CHANGE THESE NUMBERS ──────────────────
SWING = StrategyProfile(
    name="Swing (H1)",
    timeframe="H1", bar_seconds=3600,
    sma_period=200, adx_period=14, adx_threshold=20.0,
    donchian_entry=20, donchian_exit=10,
    atr_period=14, atr_stop_mult=2.0,
    vol_ratio_cap=2.0, risk_per_trade=0.01,
)

# ── Deployment profile — EXP-020 config, paper trading ───────────────
# Validated across 2020-2025: 763 trades, PF 1.11, strip-top-3 PF 0.98.
# H1 trend gate: price must be above H1 SMA-200 to enter (computed from
# M15 data resampled to H1; not enforced in live paper trading until
# historical H1 data fetch is wired into the orchestrator).
INTRADAY = StrategyProfile(
    name="Intraday (M15)",
    timeframe="M15", bar_seconds=900,
    sma_period=200, adx_period=14, adx_threshold=25.0,
    donchian_entry=20, donchian_exit=10,
    atr_period=14, atr_stop_mult=2.0,
    vol_ratio_cap=2.0, risk_per_trade=0.01,
    entry_confirmation_bars=3,
    long_only=True,
    h1_trend_gate=True,
)

_PROFILES = {"swing": SWING, "intraday": INTRADAY}

ACTIVE_PROFILE: StrategyProfile = _PROFILES[
    os.getenv("STRATEGY_PROFILE", "intraday").lower()
]
