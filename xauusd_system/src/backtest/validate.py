"""
backtest/validate.py — walk-forward validation and robustness stress tests.

Produces a validation report (JSON + brief Markdown summary) for a given
strategy profile.  The "validated" badge in the UI is driven by the
pass/fail field in this report — not by prose claims.

Usage
─────
    python -m backtest.validate --profile swing --start 2020-01-01 --end 2026-01-01

Validation gates (a profile must pass ALL to be promoted to default):
  1. Walk-forward: rolling train/test splits, OOS profit_factor > 1.20
  2. Cost stress: re-run at 1.5× and 2× spread/slippage — edge must survive
  3. Monte Carlo: 500-sample trade-order reshuffling, P(drawdown > 25%) < 5%
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

_SRC = Path(__file__).resolve().parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from dotenv import load_dotenv
load_dotenv(_SRC.parent / ".env", override=True)

from backtest.data_loader import load_bars
from backtest.engine import BacktestEngine, BacktestResult
from backtest.costs import CostModel
from core.config import SWING, INTRADAY, StrategyProfile

logger = logging.getLogger(__name__)

_PROFILES = {"swing": SWING, "intraday": INTRADAY}


# ── Gate thresholds ────────────────────────────────────────────────────────────

GATE_OOS_PROFIT_FACTOR   = 1.20   # out-of-sample profit factor floor
GATE_STRESS_MIN_PF       = 1.00   # min PF under 2× cost stress (just above break-even)
GATE_MONTE_CARLO_P_DD    = 0.05   # max probability that MC max drawdown exceeds 25%
GATE_MONTE_CARLO_DD_LIMIT = 0.25  # drawdown threshold for MC p-value
GATE_MIN_TRADES          = 20     # require at least this many OOS trades


# ── Walk-forward ──────────────────────────────────────────────────────────────

def walk_forward(
    bars:          list,
    profile:       StrategyProfile,
    initial_equity: float = 100_000.0,
    n_splits:      int   = 4,
    train_frac:    float = 0.70,
) -> dict:
    """
    Split bars into rolling train/test windows.
    Returns aggregated OOS stats across all splits.
    """
    n       = len(bars)
    results = []

    for k in range(n_splits):
        fold_start = int(k * (n / n_splits))
        fold_end   = int((k + 1) * (n / n_splits))
        fold       = bars[fold_start:fold_end]

        split_idx  = int(len(fold) * train_frac)
        oos_bars   = fold[split_idx:]

        if len(oos_bars) < 260:   # need warmup + some signal bars
            continue

        engine = BacktestEngine(
            profile        = profile,
            initial_equity = initial_equity,
            cost_model     = CostModel(),
        )
        result = engine.run(oos_bars)

        if result.n_trades >= 3:
            results.append({
                "split":         k,
                "n_trades":      result.n_trades,
                "profit_factor": result.profit_factor,
                "win_rate":      result.win_rate,
                "max_drawdown":  result.max_drawdown,
                "net_pnl":       sum(t["pnl"] for t in result.trades),
            })

    if not results:
        return {"passed": False, "reason": "no_valid_oos_splits", "splits": []}

    avg_pf        = sum(r["profit_factor"] for r in results) / len(results)
    oos_trades    = sum(r["n_trades"] for r in results)
    pf_pass       = avg_pf >= GATE_OOS_PROFIT_FACTOR
    trades_pass   = oos_trades >= GATE_MIN_TRADES

    return {
        "passed":          pf_pass and trades_pass,
        "avg_pf":          round(avg_pf, 3),
        "oos_trades":      oos_trades,
        "pf_gate":         GATE_OOS_PROFIT_FACTOR,
        "trade_gate":      GATE_MIN_TRADES,
        "splits":          results,
    }


# ── Cost stress ───────────────────────────────────────────────────────────────

def cost_stress(
    bars:          list,
    profile:       StrategyProfile,
    initial_equity: float = 100_000.0,
) -> dict:
    """
    Re-run backtest at 1.5× and 2× baseline spread/slippage.
    Gate: PF must stay > 1.0 at 2× cost.
    """
    base = CostModel()
    results = {}

    for multiplier in (1.0, 1.5, 2.0):
        stressed = CostModel(
            entry_slippage_frac = base.entry_slippage_frac * multiplier,
            stop_slippage_frac  = base.stop_slippage_frac  * multiplier,
            london_ny_spread    = base.london_ny_spread     * multiplier,
            asian_spread        = base.asian_spread         * multiplier,
        )
        engine = BacktestEngine(
            profile        = profile,
            initial_equity = initial_equity,
            cost_model     = stressed,
        )
        r = engine.run(bars)
        results[f"{multiplier:.1f}x"] = {
            "n_trades":      r.n_trades,
            "profit_factor": round(r.profit_factor, 3) if r.n_trades else 0,
            "max_drawdown":  round(r.max_drawdown, 4),
        }

    pf_at_2x = results["2.0x"]["profit_factor"]
    return {
        "passed": pf_at_2x >= GATE_STRESS_MIN_PF,
        "pf_gate_at_2x": GATE_STRESS_MIN_PF,
        "runs": results,
    }


# ── Monte Carlo ────────────────────────────────────────────────────────────────

def monte_carlo(
    trades:         list[dict],
    initial_equity: float = 100_000.0,
    n_samples:      int   = 500,
    seed:           int   = 42,
) -> dict:
    """
    Reshuffle trade order 500 times and compute distribution of max drawdown.
    Gate: P(max_DD > 25%) < 5%.
    """
    if len(trades) < 5:
        return {"passed": False, "reason": "too_few_trades", "n_samples": 0}

    rng        = random.Random(seed)
    dd_samples = []

    for _ in range(n_samples):
        shuffled = list(trades)
        rng.shuffle(shuffled)
        eq   = initial_equity
        peak = eq
        worst_dd = 0.0
        for t in shuffled:
            eq    += t["pnl"]
            peak   = max(peak, eq)
            dd     = (peak - eq) / peak if peak > 0 else 0.0
            worst_dd = max(worst_dd, dd)
        dd_samples.append(worst_dd)

    dd_samples.sort()
    p_exceed = sum(1 for d in dd_samples if d > GATE_MONTE_CARLO_DD_LIMIT) / n_samples
    p95_dd   = dd_samples[int(0.95 * n_samples)]

    return {
        "passed":         p_exceed < GATE_MONTE_CARLO_P_DD,
        "p_exceed_limit": round(p_exceed, 4),
        "p_exceed_gate":  GATE_MONTE_CARLO_P_DD,
        "dd_limit":       GATE_MONTE_CARLO_DD_LIMIT,
        "p95_drawdown":   round(p95_dd, 4),
        "median_drawdown": round(dd_samples[n_samples // 2], 4),
        "n_samples":      n_samples,
    }


# ── Full validation pipeline ───────────────────────────────────────────────────

def validate(
    profile:        StrategyProfile,
    start:          str,
    end:            str,
    initial_equity: float = 100_000.0,
    api_key:        Optional[str] = None,
) -> dict:
    """Run the full three-gate validation and return a report dict."""
    bars = load_bars(start=start, end=end, timeframe=profile.timeframe, api_key=api_key)
    if not bars:
        return {"validated": False, "error": "no_bars_loaded"}

    logger.info("Loaded %d bars for validation", len(bars))

    # Full in-sample run (for MC and stats)
    engine = BacktestEngine(
        profile=profile, initial_equity=initial_equity, cost_model=CostModel()
    )
    full_result = engine.run(bars)

    wf   = walk_forward(bars, profile, initial_equity)
    cs   = cost_stress(bars, profile, initial_equity)
    mc   = monte_carlo(full_result.trades, initial_equity)

    all_passed = wf["passed"] and cs["passed"] and mc["passed"]

    # In-sample aggregate stats
    n      = full_result.n_trades
    wins   = [t for t in full_result.trades if t["pnl"] > 0]
    losses = [t for t in full_result.trades if t["pnl"] < 0]

    report = {
        "validated":       all_passed,
        "generated":       datetime.now(timezone.utc).isoformat(),
        "profile":         profile.name,
        "date_range":      {"start": start, "end": end},
        "in_sample": {
            "n_trades":      n,
            "profit_factor": round(full_result.profit_factor, 3) if n else None,
            "expectancy_usd": round(full_result.expectancy, 2) if n else None,
            "win_rate":      round(full_result.win_rate, 4) if n else None,
            "max_drawdown":  round(full_result.max_drawdown, 4),
            "gross_win":     round(sum(t["pnl"] for t in wins), 2),
            "gross_loss":    round(abs(sum(t["pnl"] for t in losses)), 2),
        },
        "gates": {
            "walk_forward":  wf,
            "cost_stress":   cs,
            "monte_carlo":   mc,
        },
    }
    return report


def _markdown_summary(report: dict) -> str:
    v    = report.get("validated", False)
    ins  = report.get("in_sample", {})
    gate = report.get("gates", {})

    lines = [
        f"# Validation Report — {report.get('profile', '?')}",
        f"**Status:** {'[PASS] VALIDATED' if v else '[FAIL] NOT VALIDATED'}",
        f"**Date range:** {report.get('date_range', {}).get('start')} -> "
        f"{report.get('date_range', {}).get('end')}",
        "",
        "## In-sample statistics",
        f"- Trades: {ins.get('n_trades')}",
        f"- Profit factor: {ins.get('profit_factor')}",
        f"- Expectancy: ${ins.get('expectancy_usd')}",
        f"- Win rate: {(ins.get('win_rate') or 0) * 100:.1f}%",
        f"- Max drawdown: {(ins.get('max_drawdown') or 0) * 100:.1f}%",
        "",
        "## Gate results",
        f"- Walk-forward: {'PASS' if gate.get('walk_forward', {}).get('passed') else 'FAIL'} "
        f"(avg OOS PF = {gate.get('walk_forward', {}).get('avg_pf', '?')})",
        f"- Cost stress (2×): {'PASS' if gate.get('cost_stress', {}).get('passed') else 'FAIL'} "
        f"(PF = {gate.get('cost_stress', {}).get('runs', {}).get('2.0x', {}).get('profit_factor', '?')})",
        f"- Monte Carlo: {'PASS' if gate.get('monte_carlo', {}).get('passed') else 'FAIL'} "
        f"(p(DD>{GATE_MONTE_CARLO_DD_LIMIT:.0%}) = "
        f"{gate.get('monte_carlo', {}).get('p_exceed_limit', '?')})",
    ]
    return "\n".join(lines)


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)-8s  %(message)s")
    p = argparse.ArgumentParser(description="Aurum strategy validator")
    p.add_argument("--profile", default="swing", choices=list(_PROFILES))
    p.add_argument("--start",   required=True)
    p.add_argument("--end",     required=True)
    p.add_argument("--equity",  type=float, default=100_000.0)
    p.add_argument("--out",     default=None, help="Output JSON path")
    args = p.parse_args()

    profile = _PROFILES[args.profile]
    report  = validate(profile=profile, start=args.start, end=args.end,
                       initial_equity=args.equity)

    out_path = args.out or f"validation_{args.profile}_{args.start}_{args.end}.json"
    with open(out_path, "w") as fh:
        json.dump(report, fh, indent=2)
    logger.info("Report written -> %s", out_path)

    print(_markdown_summary(report))
    sys.exit(0 if report.get("validated") else 1)


if __name__ == "__main__":
    main()
