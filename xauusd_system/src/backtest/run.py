"""
backtest/run.py — CLI entry point for the XAU/USD strategy backtester.

Usage
─────
    cd xauusd_system
    python -m backtest.run --profile swing --start 2023-01-01 --end 2026-01-01

    # Custom output file:
    python -m backtest.run --profile swing --start 2022-01-01 --end 2026-01-01 \\
           --out replay_fixture.json --equity 100000

The generated file is the ONLY sanctioned way replay_fixture.json is ever created.
All numbers on the dashboard must trace back to a run of this command.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow running as `python -m backtest.run` from the xauusd_system directory
_SRC = Path(__file__).resolve().parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from dotenv import load_dotenv
load_dotenv(_SRC.parent / ".env", override=True)

from backtest.data_loader import load_bars
from backtest.engine import BacktestEngine
from backtest.costs import CostModel
from core.config import SWING, INTRADAY


_PROFILES = {"swing": SWING, "intraday": INTRADAY}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
logger = logging.getLogger(__name__)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Aurum XAU/USD strategy backtester")
    p.add_argument("--profile", default="swing", choices=list(_PROFILES),
                   help="Strategy profile to backtest (default: swing)")
    p.add_argument("--start",   required=True,
                   help="Start date YYYY-MM-DD")
    p.add_argument("--end",     required=True,
                   help="End date YYYY-MM-DD (inclusive)")
    p.add_argument("--equity",  type=float, default=100_000.0,
                   help="Starting equity in USD (default: 100 000)")
    p.add_argument("--out",     default="replay_fixture.json",
                   help="Output JSON file path (default: replay_fixture.json)")
    p.add_argument("--entry-slip", type=float, default=0.05,
                   help="Entry slippage as fraction of ATR (default: 0.05)")
    p.add_argument("--stop-slip",  type=float, default=0.10,
                   help="Stop slippage as fraction of ATR (default: 0.10)")
    return p.parse_args()


def _compute_stats(result) -> dict:
    """Aggregate trade statistics for the output artifact."""
    trades = result.trades
    n      = len(trades)
    if n == 0:
        return {"n_trades": 0, "profit_factor": None, "expectancy": None,
                "win_rate": None, "max_drawdown": None}

    wins        = [t for t in trades if t["pnl"] > 0]
    losses      = [t for t in trades if t["pnl"] < 0]
    gross_win   = sum(t["pnl"] for t in wins)
    gross_loss  = abs(sum(t["pnl"] for t in losses))
    r_multiples = [t["r_multiple"] for t in trades]

    return {
        "n_trades":      n,
        "n_wins":        len(wins),
        "n_losses":      len(losses),
        "win_rate":      round(len(wins) / n, 4),
        "profit_factor": round(gross_win / gross_loss, 4) if gross_loss > 0 else None,
        "expectancy_usd": round(sum(t["pnl"] for t in trades) / n, 2),
        "avg_r":         round(sum(r_multiples) / n, 3),
        "gross_win":     round(gross_win, 2),
        "gross_loss":    round(gross_loss, 2),
        "net_pnl":       round(gross_win - gross_loss, 2),
        "max_drawdown":  round(result.max_drawdown, 4),
        "max_r_winner":  round(max(r_multiples), 3),
        "min_r_loser":   round(min(r_multiples), 3),
    }


def main() -> None:
    args = _PARSE_ARGS = _parse_args()

    profile = _PROFILES[args.profile]
    logger.info(
        "Backtesting %s | %s → %s | equity=%.0f",
        profile.name, args.start, args.end, args.equity,
    )

    # Load bars (from cache or API)
    bars = load_bars(
        start     = args.start,
        end       = args.end,
        timeframe = profile.timeframe,
    )
    if not bars:
        logger.error("No bars loaded — check date range and API key")
        sys.exit(1)

    logger.info("Loaded %d bars covering %s → %s",
                len(bars), bars[0].timestamp.date(), bars[-1].timestamp.date())

    # Run backtest
    engine = BacktestEngine(
        profile        = profile,
        initial_equity = args.equity,
        cost_model     = CostModel(
            entry_slippage_frac = args.entry_slip,
            stop_slippage_frac  = args.stop_slip,
        ),
    )
    result = engine.run(bars)

    stats = _compute_stats(result)
    logger.info(
        "Results: %d trades | PF=%.2f | Expectancy=$%.0f | Win%%=%.0f%% | MaxDD=%.1f%%",
        stats["n_trades"],
        stats["profit_factor"] or 0,
        stats["expectancy_usd"] or 0,
        (stats["win_rate"] or 0) * 100,
        (stats["max_drawdown"] or 0) * 100,
    )

    # Build the fixture the dashboard /api/replay endpoint consumes
    fixture = {
        "source":     "backtest_run",
        "generated":  datetime.now(timezone.utc).isoformat(),
        "profile":    profile.name,
        "start_date": args.start,
        "end_date":   args.end,
        "parameters": {
            "initial_equity":     args.equity,
            "entry_slippage_frac": args.entry_slip,
            "stop_slippage_frac":  args.stop_slip,
        },
        "stats":        stats,
        "trades":       result.trades,
        "equity_curve": result.equity_curve,
    }

    out = Path(args.out)
    with out.open("w") as fh:
        json.dump(fixture, fh, indent=2)
    logger.info("Wrote fixture → %s  (%d trades)", out, stats["n_trades"])


if __name__ == "__main__":
    main()
