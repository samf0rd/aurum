# Aurum — Intraday Retune: The Right Way

## The core principle

You have a **backtested, validation-gated strategy**. Its credibility comes entirely
from the fact that its parameters were chosen *before* seeing results, are round/
conventional (no overfitting), and passed walk-forward + Monte Carlo + cost-stress tests.

The moment you change parameters specifically to "generate more trades for the demo,"
you forfeit that credibility. A recruiter who knows quant will ask one question:
**"What's your out-of-sample Sharpe on the M15 parameters?"** — and if the answer is
"we didn't backtest those, we just wanted activity," the whole project reads as theater.

So we do NOT blindly rescale. We do this properly.

---

## What's actually true about your current system

1. The engine already runs on **H1 bars** (not daily — Gemini's premise is outdated).
2. The strategy is sitting at 0 trades because:
   - Live regime is **NEUTRAL, ADX ~13** → entries correctly blocked (working as designed)
   - In **paper mode the feed is a synthetic random walk** → rarely trends → rarely fires
3. So the "ultra slow" feeling is partly the strategy being correctly patient, and
   partly the synthetic feed never producing tradeable trends.

---

## Decision: run TWO timeframe profiles, properly separated

Instead of overwriting your validated H1 strategy, parameterize the timeframe and
treat the faster profile as **a second, clearly-labelled strategy variant that must
be independently backtested before it's trusted.** Nothing gets destroyed.

### Profile A — "Swing" (your existing, validated strategy)
- Timeframe: H1
- SMA 200, Donchian 20/10, ADX 14, all exactly as backtested
- This is the one with research backing. It stays untouched and remains the default.

### Profile B — "Intraday" (new, faster — must be backtested before going live)
- Timeframe: M15
- Same *structure* (regime filter → breakout → trail), same rule logic
- Faster lookbacks, but chosen as round numbers, not optimized
- **Labelled clearly in the UI as a separate strategy** — not a replacement

The key engineering move: **make the timeframe and lookbacks config values, not
hardcoded numbers.** Then both profiles run from the same code, and switching is a
config change, not a rewrite. This is how real shops handle multi-timeframe systems.

---

## Implementation — give this to Claude Code

### Step 1 — Parameterize, don't hardcode

In `src/strategy/signal_generator.py` and `src/orchestrator/engine.py`, the strategy
parameters are currently constants. Extract them into a config object:

```python
# src/core/config.py  (new file)
from dataclasses import dataclass

@dataclass(frozen=True)
class StrategyProfile:
    name: str
    timeframe: str          # "H1" or "M15"
    bar_seconds: int        # 3600 for H1, 900 for M15
    sma_period: int         # regime filter lookback
    adx_period: int
    adx_threshold: float
    donchian_entry: int     # breakout lookback
    donchian_exit: int      # trailing exit lookback
    atr_period: int
    atr_stop_mult: float
    vol_ratio_cap: float
    risk_per_trade: float

# Your existing, validated strategy — DO NOT CHANGE THESE NUMBERS
SWING = StrategyProfile(
    name="Swing (H1)",
    timeframe="H1", bar_seconds=3600,
    sma_period=200, adx_period=14, adx_threshold=20.0,
    donchian_entry=20, donchian_exit=10,
    atr_period=14, atr_stop_mult=2.0,
    vol_ratio_cap=2.0, risk_per_trade=0.01,
)

# New faster profile — round numbers, NOT optimized.
# MUST be backtested before being trusted with real evaluation.
INTRADAY = StrategyProfile(
    name="Intraday (M15)",
    timeframe="M15", bar_seconds=900,
    sma_period=200, adx_period=14, adx_threshold=20.0,
    donchian_entry=20, donchian_exit=10,
    atr_period=14, atr_stop_mult=2.0,
    vol_ratio_cap=2.0, risk_per_trade=0.01,
)

# Selected via env var — defaults to the validated strategy
import os
ACTIVE_PROFILE = {"swing": SWING, "intraday": INTRADAY}[
    os.getenv("STRATEGY_PROFILE", "swing").lower()
]
```

Note: the lookback *numbers* (200, 20, 10, 14) stay identical between profiles. Only the
**timeframe** changes. That's the honest move — "we run the same proven structure on a
faster clock" is defensible. "We hand-tuned new numbers to trade more" is not. On M15,
a 200-bar SMA = ~2 days of trend context, a 20-bar Donchian = 5 hours. The strategy
naturally trades more often because the bars arrive 4× faster, WITHOUT changing the logic.

### Step 2 — Wire the profile into the engine

- `engine.py` `_bar_loop`: replace the hardcoded `3600` / H1 interval with
  `ACTIVE_PROFILE.bar_seconds` and `ACTIVE_PROFILE.timeframe`.
- `signal_generator.py`: replace hardcoded lookbacks with `ACTIVE_PROFILE.donchian_entry`,
  `.sma_period`, etc.
- `oanda_feed.py` `get_bars()`: pass `granularity=ACTIVE_PROFILE.timeframe`.

### Step 3 — Make the synthetic paper feed actually trend

This is the real reason you see no trades in paper mode. A pure random walk almost never
produces ADX > 20. Fix the synthetic generator in `oanda_feed.py` to inject trending
regimes so the strategy has something to react to:

```python
# In the synthetic bar generator, replace pure random walk with regime-switching:
# alternate between trending and choppy phases so the bot actually sees signals
def _synthetic_bar(self, prev_close):
    # Every ~40 bars, flip between trend and chop
    if self._bar_count % 40 == 0:
        self._regime = random.choice(["trend_up", "trend_down", "chop"])
    drift = {"trend_up": 0.8, "trend_down": -0.8, "chop": 0.0}[self._regime]
    noise = random.gauss(0, 1.5)
    move = drift + noise
    # ... build OHLC from move ...
```

This makes paper mode *demonstrate* the strategy — trends appear, ADX rises above 20,
breakouts trigger, trades open and close. The dashboard comes alive. And critically,
it's honest: you're showing how the strategy behaves in trending vs choppy conditions,
which is exactly what your regime-partition testing already validated.

### Step 4 — Surface the profile in the UI

In `index.html`, add a small label near the header showing which profile is active:
`Aurum · Intraday (M15)` or `Aurum · Swing (H1)`. So it's never ambiguous which
strategy is running. Optionally a read-only dropdown that explains both (switching
requires a restart with a different env var — don't make it hot-swappable, that
invites running an unbacktested profile by accident).

### Step 5 — The honest caveat (keep this in the README + Strategy view)

Add to the Strategy page, under the profile label:

> **Swing (H1)** is the backtested, validation-gated strategy. **Intraday (M15)** runs
> the identical rule structure on a faster timeframe for higher activity; it inherits the
> logic but must clear the same validation gates (walk-forward, Monte Carlo, cost stress)
> before being treated as proven. Until then it is a forward-test in progress.

---

## What to run for the demo

For a live dashboard recruiters will watch: run **Intraday (M15)** in paper mode with the
trending synthetic feed. You'll get several trades a week of visible activity, the metrics
populate, the equity curve moves — and you can honestly explain "this is the same proven
structure on a faster clock, currently in forward-test."

For the "this is rigorous" story: point to **Swing (H1)** as the backtested core.

You get activity AND integrity. You don't have to choose.

---

## What NOT to do (the Gemini prompt)

Do not paste Gemini's prompt. It (a) assumes daily-bar math you no longer run, (b)
rescales lookbacks in a way that's indistinguishable from overfitting, and (c) frames
the change as "so the dashboard looks busy for recruiters" — which is the exact framing
that destroys credibility if anyone technical reads the code or asks about the backtest.
Same outcome (more trades), wrong method (unvalidated parameter changes for optics).
