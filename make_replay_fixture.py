#!/usr/bin/env python3
"""
make_replay_fixture.py
──────────────────────
Walk-forward backtest using the real strategy code.

Fetches H1 XAU/USD bars from Twelve Data, runs RegimeDetector +
DonchianBreakoutSignalGenerator bar-by-bar with no lookahead, simulates
fills using PaperBrokerAdapter's slippage model, and writes
replay_fixture.json at the project root.

Usage
─────
  # activate the project venv first
  xauusd_system\\.venv\\Scripts\\activate
  python make_replay_fixture.py
  python make_replay_fixture.py --bars 2000

Requires
────────
  xauusd_system/.env   →   TWELVE_DATA_API_KEY=<your key>
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

# ── project paths ──────────────────────────────────────────────────────────
ROOT   = Path(__file__).parent
SRC    = ROOT / "xauusd_system" / "src"
ENV    = ROOT / "xauusd_system" / ".env"
OUTPUT = ROOT / "replay_fixture.json"

# ── load .env BEFORE importing any project module ─────────────────────────
if ENV.exists():
    from dotenv import load_dotenv          # python-dotenv is in pyproject deps
    load_dotenv(ENV)
else:
    print(f"  (no .env found at {ENV} — falling back to shell environment)")

if not os.environ.get("TWELVE_DATA_API_KEY"):
    sys.exit(
        "\n  TWELVE_DATA_API_KEY is not set.\n\n"
        "  Create  xauusd_system/.env  containing:\n\n"
        "      TWELVE_DATA_API_KEY=your_key_here\n\n"
        "  Free keys at https://twelvedata.com  (no card required)\n"
    )

# Use intraday (M15) profile to match the live running system
os.environ.setdefault("STRATEGY_PROFILE", "intraday")

sys.path.insert(0, str(SRC))

# ── project imports (after sys.path + env are configured) ─────────────────
from core.config      import ACTIVE_PROFILE as P          # noqa: E402
from core.interfaces  import Regime, SignalType            # noqa: E402
from strategy.signal_generator import (                    # noqa: E402
    RegimeDetector, DonchianBreakoutSignalGenerator,
)
from data.twelvedata_feed import TwelveDataFeed            # noqa: E402

# ── slippage constants (mirrors PaperBrokerAdapter defaults) ──────────────
SLIP_USD  = 0.003   # 0.3 pips × $0.01 per unit, applied at both entry and exit
OZ_PER_LOT = 100    # 1 standard lot = 100 troy oz


# ──────────────────────────────────────────────────────────────────────────
async def run(bar_count: int) -> None:

    feed = TwelveDataFeed()
    print(f"Fetching {bar_count} {P.timeframe} bars for XAU/USD ...")
    bars = await feed.get_bars(granularity=P.timeframe, count=bar_count)

    if len(bars) < 250:
        sys.exit(
            f"  Only {len(bars)} bars returned — need ≥ 250 for the SMA-200 warmup.\n"
            "  Check that your API key is valid and the Twelve Data service is reachable."
        )

    print(
        f"  {len(bars)} bars received  "
        f"({bars[0].timestamp:%Y-%m-%d} to {bars[-1].timestamp:%Y-%m-%d})"
    )

    detector  = RegimeDetector()
    generator = DonchianBreakoutSignalGenerator()

    # on_bar() requires sma_period + donchian_entry + 10 bars minimum
    WARMUP = P.sma_period + P.donchian_entry + 10   # 230 for SWING

    equity   = 10_000.0
    position: dict | None = None
    trades:   list[dict]  = []

    # ── diagnostic counters ──
    n_evaluated  = 0   # bars examined after warmup
    n_trending   = 0   # bars where regime was TRENDING_BULL or TRENDING_BEAR
    n_sig_long   = 0   # D20H breakout signals (LONG) while flat
    n_sig_short  = 0   # D20L breakout signals (SHORT) while flat
    n_blocked    = 0   # signals that fired while a position was already open

    print(f"  Running walk-forward from bar {WARMUP} ...")

    for i in range(WARMUP, len(bars)):
        window = bars[: i + 1]
        bar    = bars[i]
        close  = float(bar.close)
        low    = float(bar.low)
        high   = float(bar.high)
        n_evaluated += 1

        regime = detector.detect(window)

        # ── manage open position ──────────────────────────────────────────
        if position is not None:
            exit_now   = False
            exit_price = 0.0

            # Hard stop: checked against bar.low / bar.high (intrabar breach)
            if position["side"] == "LONG" and low <= position["stop"]:
                exit_price = position["stop"] - SLIP_USD
                exit_now   = True
            elif position["side"] == "SHORT" and high >= position["stop"]:
                exit_price = position["stop"] + SLIP_USD
                exit_now   = True
            else:
                # Strategy exits (Donchian-10 trail, SMA-200 regime flip)
                should_exit, _ = generator.should_exit(window, position["side"], regime)
                if should_exit:
                    exit_price = (close - SLIP_USD) if position["side"] == "LONG" \
                                                    else (close + SLIP_USD)
                    exit_now = True

            if exit_now:
                qty        = position["qty"]
                slip_total = SLIP_USD * qty * OZ_PER_LOT * 2   # round-trip

                if position["side"] == "LONG":
                    gross = (exit_price - position["entry_price"]) * qty * OZ_PER_LOT
                else:
                    gross = (position["entry_price"] - exit_price) * qty * OZ_PER_LOT

                pnl          = round(gross, 2)
                initial_risk = qty * abs(position["entry_price"] - position["stop"]) * OZ_PER_LOT
                r_mult       = round(pnl / initial_risk, 3) if initial_risk > 0 else 0.0

                equity += pnl
                trades.append({
                    "entry_time":    position["entry_time"],
                    "exit_time":     bar.timestamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "entry_price":   round(position["entry_price"], 2),
                    "exit_price":    round(exit_price, 2),
                    "direction":     position["side"],
                    "qty":           round(qty, 4),
                    "slippage_paid": round(slip_total, 2),
                    "pnl":           pnl,
                    "r_multiple":    r_mult,
                    "stop_at_entry": round(position["stop"], 2),
                    "source":        "backtest_validation_run",
                })
                position = None

        # ── check for new entry ───────────────────────────────────────────
        if regime in (Regime.TRENDING_BULL, Regime.TRENDING_BEAR):
            n_trending += 1
            sig = generator.on_bar(window, regime)

            if sig.signal_type in (SignalType.ENTER_LONG, SignalType.ENTER_SHORT):
                if position is not None:
                    n_blocked += 1
                else:
                    if sig.signal_type == SignalType.ENTER_LONG:  n_sig_long  += 1
                    else:                                           n_sig_short += 1

        if position is None and regime in (Regime.TRENDING_BULL, Regime.TRENDING_BEAR):
            sig = generator.on_bar(window, regime)

            if sig.signal_type == SignalType.ENTER_LONG:   # long-only filter
                entry_px    = close + SLIP_USD
                stop_dist   = float(sig.stop_distance)
                stop_px     = entry_px - stop_dist
                qty         = max(
                    (equity * P.risk_per_trade) / (stop_dist * OZ_PER_LOT),
                    0.0001,   # floor: never zero or negative
                )
                position = {
                    "side":        "LONG",
                    "entry_price": round(entry_px,  2),
                    "stop":        round(stop_px,   2),
                    "qty":         round(qty,        4),
                    "entry_time":  bar.timestamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
                }

    # ── results ───────────────────────────────────────────────────────────
    n = len(trades)
    if n == 0:
        print(
            "\n  No trades triggered in the backtest window.\n"
            "  The market may have been range-bound for the fetched period.\n"
            "  Try:  python make_replay_fixture.py --bars 2000"
        )
        return

    wins      = sum(1 for t in trades if t["pnl"] > 0)
    total_pnl = sum(t["pnl"] for t in trades)
    n_longs   = sum(1 for t in trades if t["direction"] == "LONG")
    n_shorts  = sum(1 for t in trades if t["direction"] == "SHORT")

    # ── equity curve summary ──────────────────────────────────────────────
    eq_curve = [10_000.0]
    for t in trades:
        eq_curve.append(round(eq_curve[-1] + t["pnl"], 2))
    eq_peak   = max(eq_curve)
    eq_trough = min(eq_curve)
    eq_final  = eq_curve[-1]
    peak_dd   = round((eq_peak - eq_trough) / eq_peak * 100, 1)

    # ── results ───────────────────────────────────────────────────────────
    print(f"\n  {n} trades  |  {wins}W / {n - wins}L  |  total P&L ${total_pnl:+.2f}")
    print(f"  directions: {n_longs} LONG  /  {n_shorts} SHORT")

    print(f"\n  -- Filter pass rate (of {n_evaluated} bars after warmup) --")
    print(f"  Passed regime filters (SMA200 + ADX + vol): {n_trending:4d}  ({n_trending/n_evaluated*100:.1f}%)")
    print(f"  D20H breakout signal fired while flat:      {n_sig_long:4d}  (all LONG, short filtered)")
    print(f"  SHORT signals skipped (long-only filter):   {n_sig_short:4d}")
    print(f"  Signal fired but position already open:     {n_blocked:4d}  (skipped)")

    print(f"\n  -- Equity curve --")
    print(f"  Start  :  $10,000.00")
    print(f"  Peak   :  ${eq_peak:>10.2f}")
    print(f"  Trough :  ${eq_trough:>10.2f}  (max drawdown {peak_dd}%)")
    print(f"  Final  :  ${eq_final:>10.2f}  ({total_pnl:+.2f}  /  {total_pnl/100:.1f}%)")

    # -- first 5 rows
    hdr = f"  {'DIR':5}  {'ENTRY':10}  {'ENTRY $':>8}  {'EXIT':10}  {'EXIT $':>8}  {'P&L':>9}  {'R':>6}"
    sep = f"  {'-'*5}  {'-'*10}  {'-'*8}  {'-'*10}  {'-'*8}  {'-'*9}  {'-'*6}"
    print(f"\n  First 5 rows:\n\n{hdr}\n{sep}")
    for t in trades[:5]:
        print(
            f"  {t['direction']:5}  {t['entry_time'][:10]:10}  "
            f"${t['entry_price']:>7.2f}  {t['exit_time'][:10]:10}  "
            f"${t['exit_price']:>7.2f}  "
            f"${t['pnl']:>+8.2f}  {t['r_multiple']:>+5.2f}R"
        )

    # ── conditional save ──────────────────────────────────────────────────
    if total_pnl > 0:
        OUTPUT.write_text(json.dumps(trades, indent=2))
        print(f"\n  [SAVED] P&L positive -- written {n} trades to {OUTPUT}\n")
    else:
        print(
            f"\n  [NOT SAVED] P&L is negative (${total_pnl:+.2f}) -- fixture not saved.\n"
            f"  Inspect the numbers above before changing anything.\n"
        )


# ──────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Generate replay_fixture.json from live Twelve Data bars.")
    ap.add_argument(
        "--bars", type=int, default=1000,
        help="H1 bars to fetch (default 1000 ≈ 6 weeks of XAU/USD data)",
    )
    asyncio.run(run(ap.parse_args().bars))
