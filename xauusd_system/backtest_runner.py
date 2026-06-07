"""
backtest_runner.py — unified backtest CLI for the Aurum XAU/USD system.

Run mode:
  python backtest_runner.py --profile intraday --from 2025-01-01 --to 2025-12-31
  python backtest_runner.py --profile intraday --from 2025-01-01 --to 2025-12-31 \\
      --out results/exp-001.json --equity 100000

Analyse mode (reads existing JSON, no re-run):
  python backtest_runner.py --analyse results/exp-001.json
  python backtest_runner.py --analyse results/exp-001.json --strip-top 5
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import fields, replace
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

_HERE = Path(__file__).resolve().parent

# Locate xauusd_system/src regardless of whether this script lives at the
# project root (alongside xauusd_system/) or inside xauusd_system/ itself.
def _find_src() -> Path:
    # Script is inside xauusd_system/ → src is a sibling directory
    candidate = _HERE / "src"
    if candidate.is_dir():
        return candidate
    # Script is at project root → xauusd_system/src is one level down
    candidate = _HERE / "xauusd_system" / "src"
    if candidate.is_dir():
        return candidate
    raise RuntimeError(f"Cannot locate xauusd_system/src from {_HERE}")

def _find_env() -> Path:
    for candidate in (_HERE / ".env", _HERE / "xauusd_system" / ".env"):
        if candidate.exists():
            return candidate
    return _HERE / ".env"  # fallback — dotenv is silent on missing file

_SRC = _find_src()
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from dotenv import load_dotenv
load_dotenv(_find_env(), override=True)

from backtest.data_loader import load_bars
from backtest.engine import BacktestEngine
from backtest.costs import CostModel
from core.config import SWING, INTRADAY, StrategyProfile
from risk.models import RiskConfig
from strategy.signal_generator import RegimeDetector

_PROFILES: dict[str, StrategyProfile] = {"swing": SWING, "intraday": INTRADAY}
_MAX_WINDOW = BacktestEngine.MAX_WINDOW

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
logger = logging.getLogger(__name__)


# ── Strategy overrides ────────────────────────────────────────────────────────

def _apply_overrides(profile: StrategyProfile, overrides: dict) -> StrategyProfile:
    if not overrides:
        return profile
    known = {f.name for f in fields(profile)}
    valid: dict = {}
    for k, v in overrides.items():
        if k in known:
            valid[k] = v
        else:
            logger.warning("Unknown strategy override '%s' — skipping", k)
    return replace(profile, **valid) if valid else profile


# ── Trade enrichment (adds entry_bar_index, regime, bars_held) ────────────────

def _enrich_trades(trades: list[dict], bars: list) -> list[dict]:
    ts_to_idx = {bar.timestamp.isoformat(): i for i, bar in enumerate(bars)}
    detector = RegimeDetector()
    enriched: list[dict] = []
    for raw in trades:
        t = dict(raw)
        entry_ts = t.get("entry_time", "")
        exit_ts  = t.get("exit_time", "")
        idx = ts_to_idx.get(entry_ts)
        if idx is not None:
            t["entry_bar_index"] = idx
            window = bars[max(0, idx - _MAX_WINDOW): idx + 1]
            try:
                t["regime"] = detector.detect(window).name
            except Exception:
                t["regime"] = "UNKNOWN"
            exit_idx = ts_to_idx.get(exit_ts)
            t["bars_held"] = (exit_idx - idx) if exit_idx is not None else None
        else:
            t["entry_bar_index"] = None
            t["regime"] = "UNKNOWN"
            t["bars_held"] = None
        enriched.append(t)
    return enriched


# ── Stats ─────────────────────────────────────────────────────────────────────

def _compute_stats(trades: list[dict]) -> dict:
    n = len(trades)
    if n == 0:
        return {"n_trades": 0, "profit_factor": None, "win_rate": None,
                "net_pnl": None, "avg_win": None, "avg_loss": None,
                "max_drawdown": None, "sharpe_ann": None}
    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    gw = sum(t["pnl"] for t in wins)
    gl = abs(sum(t["pnl"] for t in losses))
    return {
        "n_trades":      n,
        "n_wins":        len(wins),
        "n_losses":      len(losses),
        "win_rate":      round(len(wins) / n, 4),
        "profit_factor": round(gw / gl, 4) if gl > 0 else None,
        "net_pnl":       round(gw - gl, 2),
        "avg_win":       round(gw / len(wins), 2) if wins else 0.0,
        "avg_loss":      round(gl / len(losses), 2) if losses else 0.0,
        "gross_win":     round(gw, 2),
        "gross_loss":    round(gl, 2),
        "expectancy_usd": round(sum(t["pnl"] for t in trades) / n, 2),
        "max_drawdown":  None,   # filled in after equity curve
        "sharpe_ann":    None,   # filled in after equity curve
    }


def _max_drawdown(equity_curve: list) -> float:
    if not equity_curve:
        return 0.0
    peak = equity_curve[0][1]
    worst = 0.0
    for _, eq in equity_curve:
        peak  = max(peak, eq)
        dd    = (peak - eq) / peak if peak > 0 else 0.0
        worst = max(worst, dd)
    return round(worst, 4)


def _sharpe(equity_curve: list) -> float | None:
    if len(equity_curve) < 10:
        return None
    daily: dict[str, float] = {}
    for ts_str, eq in equity_curve:
        daily[ts_str[:10]] = eq
    equities = [daily[d] for d in sorted(daily)]
    if len(equities) < 2:
        return None
    rets = np.diff(equities) / np.array(equities[:-1], dtype=np.float64)
    std = float(np.std(rets, ddof=1))
    if std == 0:
        return None
    return round(float(np.mean(rets) / std) * (252 ** 0.5), 2)


# ── Results block printer ─────────────────────────────────────────────────────

def _print_block(result: dict) -> None:
    s    = result["stats"]
    pf   = s.get("profit_factor") or 0.0
    wr   = (s.get("win_rate") or 0.0) * 100
    net  = s.get("net_pnl") or 0.0
    aw   = s.get("avg_win") or 0.0
    al   = s.get("avg_loss") or 0.0
    dd   = (s.get("max_drawdown") or 0.0) * 100
    sh   = s.get("sharpe_ann")
    bars = len(result.get("equity_curve", []))

    print("══════════════════════════════════")
    print(f"BACKTEST RESULTS — {result.get('profile', '?')}")
    print(f"Period: {result.get('start_date')} → {result.get('end_date')}")
    print(f"Bars: {bars:,}  |  Trades: {s.get('n_trades', 0)}")
    print("──────────────────────────────────")
    print(f"Net P&L:        {'+' if net >= 0 else ''}${net:,.0f}")
    print(f"Win rate:        {wr:.1f}%")
    print(f"Profit factor:   {pf:.2f}")
    print(f"Avg win:        +${aw:,.0f}")
    print(f"Avg loss:       -${al:,.0f}")
    print(f"Max drawdown:   -{dd:.1f}%")
    print(f"Sharpe (ann.):  {'N/A' if sh is None else f'{sh:.2f}'}")
    print("══════════════════════════════════")


# ── Run backtest (public API — imported by run_experiment.py) ─────────────────

def _research_risk_config(risk_per_trade: float) -> RiskConfig:
    """Permissive RiskConfig for research experiments — disables all hard gates."""
    from decimal import Decimal as D
    return RiskConfig(
        risk_pct_normal            = D(str(risk_per_trade)),
        daily_loss_limit_pct       = D("0.99"),
        weekly_loss_limit_pct      = D("0.99"),
        max_drawdown_pct           = D("0.99"),
        drawdown_resume_pct        = D("0.99"),
        drawdown_cooldown_bars     = 0,
        consec_loss_soft_limit     = 999,
        consec_loss_hard_limit     = 999,
        emergency_daily_loss_pct   = D("0.99"),
        emergency_drawdown_pct     = D("0.99"),
        emergency_consec_losses    = 999,
    )


def _apply_risk_overrides(base: "RiskConfig", overrides: dict) -> "RiskConfig":
    """Patch a RiskConfig with a dict of field overrides (strings auto-cast to Decimal)."""
    from decimal import Decimal as D
    from dataclasses import fields as dc_fields, replace
    known = {f.name: f for f in dc_fields(base)}
    valid: dict = {}
    for k, v in overrides.items():
        if k not in known:
            logger.warning("Unknown risk override '%s' — skipping", k)
            continue
        # Cast strings/floats to Decimal for Decimal-typed fields
        if known[k].type in ("Decimal", "decimal.Decimal") or isinstance(getattr(base, k), D):
            v = D(str(v))
        valid[k] = v
    return replace(base, **valid) if valid else base


def _compute_h1_sma_lookup(bars: list, sma_period: int = 200) -> dict:
    """
    Pre-compute H1 SMA-{sma_period} for every M15 bar and return a
    {timestamp_iso: sma_value} dict.  Used by the H1 trend gate (EXP-020).

    Resamples M15 bars to H1 by taking the last close in each hour bucket,
    then computes a rolling SMA and maps each M15 bar back to its hour's value.
    """
    hour_closes: dict = {}
    for bar in bars:
        hkey = bar.timestamp.replace(minute=0, second=0, microsecond=0)
        hour_closes[hkey] = float(bar.close)   # last M15 bar per hour wins

    sorted_hours = sorted(hour_closes)
    closes = [hour_closes[h] for h in sorted_hours]
    n = len(sorted_hours)

    h1_sma_by_hour: dict = {}
    if n >= sma_period:
        window_sum = sum(closes[:sma_period])
        for i in range(sma_period - 1, n):
            if i > sma_period - 1:
                window_sum += closes[i] - closes[i - sma_period]
            h1_sma_by_hour[sorted_hours[i]] = window_sum / sma_period

    lookup: dict = {}
    for bar in bars:
        hkey = bar.timestamp.replace(minute=0, second=0, microsecond=0)
        sma_val = h1_sma_by_hour.get(hkey)
        if sma_val is not None:
            lookup[bar.timestamp.isoformat()] = sma_val
    return lookup


def run_backtest(
    profile_name: str,
    start: str,
    end: str,
    equity: float = 100_000.0,
    out_path: str | Path | None = None,
    strategy_overrides: dict | None = None,
    research_mode: bool = False,
    risk_overrides: dict | None = None,
) -> dict:
    """
    Execute a full backtest and return the result dict.
    Writes JSON to out_path if provided.

    research_mode=True: disables all hard risk gates so the full trade sequence
    is visible without early CB/loss-halt interruptions. Use for parameter research
    only — never for production evaluation.

    risk_overrides: dict of RiskConfig field names → values, applied on top of
    defaults. Ignored when research_mode=True (which uses its own permissive config).
    """
    profile = _apply_overrides(_PROFILES[profile_name], strategy_overrides or {})
    logger.info("Backtesting %s | %s → %s | equity=%.0f%s",
                profile.name, start, end, equity,
                " [RESEARCH MODE — risk gates disabled]" if research_mode else "")

    bars = load_bars(start=start, end=end, timeframe=profile.timeframe)
    if not bars:
        logger.error("No bars loaded — check date range and cache / API key")
        sys.exit(1)
    logger.info("Loaded %d bars | %s → %s",
                len(bars), bars[0].timestamp.date(), bars[-1].timestamp.date())

    if research_mode:
        risk_cfg = _research_risk_config(profile.risk_per_trade)
    elif risk_overrides:
        from risk.models import RiskConfig as _RC
        base = _RC(risk_pct_normal=__import__("decimal").Decimal(str(profile.risk_per_trade)))
        risk_cfg = _apply_risk_overrides(base, risk_overrides)
        logger.info("Risk overrides applied: %s", risk_overrides)
    else:
        risk_cfg = None
    h1_sma_lookup = None
    if getattr(profile, "h1_trend_gate", False):
        h1_sma_lookup = _compute_h1_sma_lookup(bars)
        logger.info("H1 SMA-200 lookup computed: %d M15 bars mapped", len(h1_sma_lookup))

    engine = BacktestEngine(
        profile        = profile,
        initial_equity = equity,
        cost_model     = CostModel(),
        risk_config    = risk_cfg,
        h1_sma_lookup  = h1_sma_lookup,
    )
    result_obj = engine.run(bars)

    enriched = _enrich_trades(result_obj.trades, bars)
    eq_curve  = result_obj.equity_curve

    stats = _compute_stats(enriched)
    stats["max_drawdown"] = _max_drawdown(eq_curve)
    stats["sharpe_ann"]   = _sharpe(eq_curve)

    out = {
        "source":      "backtest_runner",
        "generated":   datetime.now(timezone.utc).isoformat(),
        "profile":     profile.name,
        "profile_key": profile_name,
        "start_date":  start,
        "end_date":    end,
        "parameters":  {
            "initial_equity":     equity,
            "strategy_overrides": strategy_overrides or {},
        },
        "stats":        stats,
        "trades":       enriched,
        "equity_curve": eq_curve,
    }

    if out_path:
        p = Path(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w") as fh:
            json.dump(out, fh, indent=2)
        logger.info("Wrote results → %s  (%d trades)", p, stats["n_trades"])

    _print_block(out)
    return out


# ── Analyse mode ──────────────────────────────────────────────────────────────

def _pf_of(subset: list[dict]) -> str:
    wins = [t for t in subset if t["pnl"] > 0]
    losses = [t for t in subset if t["pnl"] < 0]
    gw = sum(t["pnl"] for t in wins)
    gl = abs(sum(t["pnl"] for t in losses))
    return f"{gw/gl:.2f}" if gl > 0 else "∞"


def _avg_r(subset: list[dict]) -> float:
    if not subset:
        return 0.0
    return sum(t.get("r_multiple", 0) for t in subset) / len(subset)


def _cut1_regime(trades: list[dict]) -> None:
    from collections import defaultdict
    by_regime: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        by_regime[t.get("regime", "UNKNOWN")].append(t)

    print("\n─── CUT 1 — P&L by regime ────────────────────────────────────────")
    hdr = f"{'Regime':<18} {'Trades':>6} {'Win%':>6} {'PF':>6} {'Avg R Win':>10} {'Avg R Loss':>10}"
    print(hdr)
    print("─" * len(hdr))
    for regime, ts in sorted(by_regime.items()):
        wins   = [t for t in ts if t["pnl"] > 0]
        losses = [t for t in ts if t["pnl"] < 0]
        wr     = f"{len(wins)/len(ts)*100:.1f}%" if ts else "—"
        ar_w   = f"{_avg_r(wins):+.2f}" if wins else "—"
        ar_l   = f"{_avg_r(losses):+.2f}" if losses else "—"
        print(f"{regime:<18} {len(ts):>6} {wr:>6} {_pf_of(ts):>6} {ar_w:>10} {ar_l:>10}")


def _cut2_exit_type(trades: list[dict]) -> None:
    from collections import defaultdict
    by_exit: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        by_exit[t.get("exit_type", t.get("exit_reason", "unknown"))].append(t)

    print("\n─── CUT 2 — P&L by exit type ─────────────────────────────────────")
    hdr = f"{'Exit type':<18} {'Count':>6} {'Win%':>6} {'PF':>6} {'Avg R':>7}"
    print(hdr)
    print("─" * len(hdr))
    for etype, ts in sorted(by_exit.items()):
        wins = [t for t in ts if t["pnl"] > 0]
        wr   = f"{len(wins)/len(ts)*100:.1f}%" if ts else "—"
        print(f"{etype:<18} {len(ts):>6} {wr:>6} {_pf_of(ts):>6} {_avg_r(ts):>+7.2f}")


def _cut3_bars_held(trades: list[dict]) -> None:
    buckets = [
        ("1 bar",    lambda b: b == 1),
        ("2–3 bars", lambda b: 2 <= b <= 3),
        ("4–8 bars", lambda b: 4 <= b <= 8),
        ("9–24 bars", lambda b: 9 <= b <= 24),
        ("25–72 bars", lambda b: 25 <= b <= 72),
        (">72 bars",  lambda b: b > 72),
    ]

    print("\n─── CUT 3 — Bars-in-trade distribution ────────────────────────────")
    hdr = f"{'Duration':<12} {'Count':>6} {'Wins':>5} {'Avg R':>7}"
    print(hdr)
    print("─" * len(hdr))
    for label, pred in buckets:
        subset = [t for t in trades if t.get("bars_held") is not None and pred(t["bars_held"])]
        wins   = [t for t in subset if t["pnl"] > 0]
        ar     = f"{_avg_r(subset):+.2f}" if subset else "—"
        print(f"{label:<12} {len(subset):>6} {len(wins):>5} {ar:>7}")


def _cut4_strip_top(trades: list[dict], n: int) -> None:
    sorted_by_pnl = sorted(trades, key=lambda t: t["pnl"], reverse=True)
    top_n    = sorted_by_pnl[:n]
    rest     = sorted_by_pnl[n:]

    print(f"\n─── CUT 4 — Strip top-{n} winners ─────────────────────────────────")
    print(f"Removed:")
    for t in top_n:
        et  = t.get("entry_time", "")[:10]
        dir = t.get("direction", "?")
        pnl = t["pnl"]
        r   = t.get("r_multiple", 0)
        print(f"  {et}  {dir:<5}  ${pnl:>+8,.0f}  (R={r:+.2f})")

    net_rest = sum(t["pnl"] for t in rest)
    print(f"\nRemaining {len(rest)} trades:")
    print(f"  PF      = {_pf_of(rest)}")
    print(f"  Net P&L = ${net_rest:>+,.0f}")


def _cut6_annual(trades: list[dict], equity_curve: list) -> None:
    """Year-by-year P&L, win rate, PF, and standalone max drawdown."""
    years = sorted(set(t["entry_time"][:4] for t in trades if "entry_time" in t))

    print("\n─── CUT 6 — Annual P&L breakdown ───────────────────────────────────")
    hdr = f"{'Year':<6} {'Trades':>7} {'Win%':>6} {'PF':>6}  {'Net P&L':>12} {'Max DD':>8}"
    print(hdr)
    print("─" * len(hdr))

    for year in years:
        yt = [t for t in trades if t.get("entry_time", "").startswith(year)]
        if not yt:
            continue
        wins   = [t for t in yt if t["pnl"] > 0]
        losses = [t for t in yt if t["pnl"] < 0]
        gw = sum(t["pnl"] for t in wins)
        gl = abs(sum(t["pnl"] for t in losses))
        net = gw - gl
        pf  = gw / gl if gl > 0 else None
        wr  = len(wins) / len(yt) * 100

        year_eq = [(ts, eq) for ts, eq in equity_curve if ts.startswith(year)]
        max_dd  = 0.0
        if year_eq:
            peak = year_eq[0][1]
            for _, eq in year_eq:
                peak   = max(peak, eq)
                dd     = (peak - eq) / peak if peak > 0 else 0.0
                max_dd = max(max_dd, dd)

        pf_str  = f"{pf:.2f}" if pf is not None else "∞"
        print(f"{year:<6} {len(yt):>7} {wr:>5.1f}% {pf_str:>6} ${net:>+12,.0f} {max_dd*100:>7.1f}%")

    gw_all = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gl_all = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    net_all = gw_all - gl_all
    pf_all  = gw_all / gl_all if gl_all > 0 else None
    wins_all = [t for t in trades if t["pnl"] > 0]
    wr_all   = len(wins_all) / len(trades) * 100 if trades else 0
    pf_str_all = f"{pf_all:.2f}" if pf_all is not None else "∞"
    print("─" * len(hdr))
    print(f"{'TOTAL':<6} {len(trades):>7} {wr_all:>5.1f}% {pf_str_all:>6} ${net_all:>+12,.0f}")


def _cut5_entry_timing(trades: list[dict], bars: list | None) -> None:
    print("\n─── CUT 5 — Entry timing (false breakout rate) ─────────────────────")
    if bars is None:
        print("  [skipped — bar data unavailable for this analyse run]")
        return

    ts_to_bar = {bar.timestamp.isoformat(): bar for bar in bars}
    bar_list   = bars  # index-accessible

    reverted_1 = []
    reverted_12 = []
    held = []

    for t in trades:
        idx = t.get("entry_bar_index")
        if idx is None:
            continue
        ep  = t.get("entry_price", 0)
        dir = t.get("direction", "LONG")

        b1 = bar_list[idx + 1] if idx + 1 < len(bar_list) else None
        b2 = bar_list[idx + 2] if idx + 2 < len(bar_list) else None

        rev1 = False
        rev2 = False
        if b1 is not None:
            if dir == "LONG":
                rev1 = float(b1.low) < ep
            else:
                rev1 = float(b1.high) > ep
        if b2 is not None:
            if dir == "LONG":
                rev2 = float(b2.low) < ep
            else:
                rev2 = float(b2.high) > ep

        if rev1:
            reverted_1.append(t)
            if rev2:
                reverted_12.append(t)
        else:
            held.append(t)

    total = len([t for t in trades if t.get("entry_bar_index") is not None])
    print(f"Total entries analysed: {total}")
    print(f"Bar+1 reverted through entry: {len(reverted_1)} ({len(reverted_1)/total*100:.1f}%)" if total else "")
    print(f"Bar+1 AND bar+2 reverted:     {len(reverted_12)} ({len(reverted_12)/total*100:.1f}%)" if total else "")

    hdr = f"\n{'Group':<20} {'Count':>6} {'Win%':>6} {'Avg R':>7}"
    print(hdr)
    print("─" * 42)
    for label, subset in [("Reverted bar+1", reverted_1), ("Held bar+1", held)]:
        if not subset:
            continue
        wins = [t for t in subset if t["pnl"] > 0]
        wr   = f"{len(wins)/len(subset)*100:.1f}%"
        print(f"{label:<20} {len(subset):>6} {wr:>6} {_avg_r(subset):>+7.2f}")


def analyse(path: str, strip_top: int = 3, annual: bool = False) -> None:
    p = Path(path)
    if not p.exists():
        print(f"ERROR: file not found: {path}", file=sys.stderr)
        sys.exit(1)

    with p.open() as fh:
        data = json.load(fh)

    trades = data.get("trades", [])
    if not trades:
        print("No trades in results file.", file=sys.stderr)
        sys.exit(1)

    equity_curve = data.get("equity_curve", [])

    # Try to load bar data for Cut 5 (and regime enrichment if missing)
    bars = None
    needs_enrich = "entry_bar_index" not in (trades[0] if trades else {})
    profile_key  = data.get("profile_key", "intraday")
    start_date   = data.get("start_date")
    end_date     = data.get("end_date")
    profile      = _PROFILES.get(profile_key, INTRADAY)

    if start_date and end_date:
        try:
            bars = load_bars(start=start_date, end=end_date, timeframe=profile.timeframe)
        except Exception as exc:
            logger.warning("Could not load bars for Cut 5: %s", exc)

    if needs_enrich and bars:
        logger.info("Enriching trades with entry_bar_index and regime (not in source JSON)")
        trades = _enrich_trades(trades, bars)

    print(f"\nAnalysing: {path}  ({len(trades)} trades)")
    _cut1_regime(trades)
    _cut2_exit_type(trades)
    _cut3_bars_held(trades)
    _cut4_strip_top(trades, strip_top)
    _cut5_entry_timing(trades, bars)
    if annual:
        _cut6_annual(trades, equity_curve)
    print()


# ── CLI ───────────────────────────────────────────────────────────────────────

def _parse() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Aurum backtest runner")
    p.add_argument("--analyse",   metavar="FILE",
                   help="Analyse an existing results JSON (no re-run)")
    p.add_argument("--strip-top", type=int, default=3, dest="strip_top",
                   help="Number of top winners to strip in Cut 4 (default: 3)")
    p.add_argument("--annual",    action="store_true",
                   help="Include year-by-year P&L breakdown (Cut 6) in analyse mode")
    p.add_argument("--profile",   default="intraday", choices=list(_PROFILES))
    p.add_argument("--from",      dest="start", help="Start date YYYY-MM-DD")
    p.add_argument("--to",        dest="end",   help="End date YYYY-MM-DD")
    p.add_argument("--equity",    type=float, default=100_000.0)
    p.add_argument("--out",       default=None, help="Output JSON path")
    return p.parse_args()


def main() -> None:
    args = _parse()

    if args.analyse:
        analyse(args.analyse, strip_top=args.strip_top, annual=args.annual)
        return

    if not args.start or not args.end:
        print("ERROR: --from and --to are required in run mode", file=sys.stderr)
        sys.exit(1)

    out = args.out
    if out is None:
        out = (
            f"results/backtest_{args.profile}_{args.start}_{args.end}.json"
        )

    run_backtest(
        profile_name = args.profile,
        start        = args.start,
        end          = args.end,
        equity       = args.equity,
        out_path     = out,
    )


if __name__ == "__main__":
    main()
