"""
diagnose.py - five-cut diagnostic on replay_fixture.json.
No data fetches, no strategy changes, no fixture regeneration.

Run from xauusd_system/:
    py diagnose.py
"""
from __future__ import annotations

import io
import json
import sys

# Force UTF-8 output on Windows (cp1252 console blocks some chars)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# ── load fixture ──────────────────────────────────────────────────────────────

FIXTURE = Path(__file__).parent / "replay_fixture.json"
with FIXTURE.open() as fh:
    fx = json.load(fh)

trades = fx["trades"]
stats  = fx["stats"]

print(f"Fixture: {fx['source']}  |  {fx['start_date']} to {fx['end_date']}")
print(f"Total: {stats['n_trades']} trades  |  PF={stats['profit_factor']}  "
      f"|  Win%={stats['win_rate']*100:.1f}%  |  E[R]={stats['avg_r']:.3f}")
print()

# helper
def pf(gross_win: float, gross_loss: float) -> str:
    if gross_loss == 0:
        return "inf"
    return f"{gross_win / gross_loss:.3f}"

def r_avg(trade_list: list[dict]) -> str:
    if not trade_list:
        return "n/a"
    return f"{sum(t['r_multiple'] for t in trade_list) / len(trade_list):.3f}"

# ── CUT 1 ─ P&L by regime (direction is a perfect proxy: LONG=BULL, SHORT=BEAR)

print("=" * 60)
print("CUT 1 — P&L by regime (LONG ≡ TRENDING_BULL, SHORT ≡ TRENDING_BEAR)")
print("=" * 60)

by_dir: dict[str, list[dict]] = defaultdict(list)
for t in trades:
    by_dir[t["direction"]].append(t)

for direction, bucket in sorted(by_dir.items()):
    wins   = [t for t in bucket if t["pnl"] > 0]
    losses = [t for t in bucket if t["pnl"] <= 0]
    gw     = sum(t["pnl"] for t in wins)
    gl     = abs(sum(t["pnl"] for t in losses))
    win_r  = sum(t["r_multiple"] for t in wins)  / len(wins)   if wins   else 0
    los_r  = sum(t["r_multiple"] for t in losses) / len(losses) if losses else 0
    regime = "TRENDING_BULL" if direction == "LONG" else "TRENDING_BEAR"
    print(f"\n  {regime} ({direction})")
    print(f"    Trades:      {len(bucket):3d}   (wins {len(wins)}, losses {len(losses)})")
    print(f"    Win rate:    {len(wins)/len(bucket)*100:.1f}%")
    print(f"    Gross win:   ${gw:>10,.2f}")
    print(f"    Gross loss:  ${gl:>10,.2f}")
    print(f"    PF:          {pf(gw, gl)}")
    print(f"    Avg R (W):   {win_r:.3f}")
    print(f"    Avg R (L):   {los_r:.3f}")

print()

# ── CUT 2 ─ P&L by exit type

print("=" * 60)
print("CUT 2 — P&L by exit type")
print("  NOTE: 'signal_exit' merges Donchian-10 trail AND SMA flip")
print("  (engine records both as 'signal_exit'; would need re-run to split)")
print("=" * 60)

by_exit: dict[str, list[dict]] = defaultdict(list)
for t in trades:
    by_exit[t["exit_type"]].append(t)

for etype, bucket in sorted(by_exit.items()):
    wins   = [t for t in bucket if t["pnl"] > 0]
    losses = [t for t in bucket if t["pnl"] <= 0]
    gw     = sum(t["pnl"] for t in wins)
    gl     = abs(sum(t["pnl"] for t in losses))
    print(f"\n  exit_type = {etype}")
    print(f"    Count:       {len(bucket):3d}   (wins {len(wins)}, losses {len(losses)})")
    print(f"    Win rate:    {len(wins)/len(bucket)*100:.1f}%")
    print(f"    PF:          {pf(gw, gl)}")
    print(f"    Avg R:       {r_avg(bucket)}")
    print(f"    Avg R (W):   {r_avg(wins)}")
    print(f"    Avg R (L):   {r_avg(losses)}")
    print(f"    Best trade:  ${max(t['pnl'] for t in bucket):,.2f}  "
          f"(R={max(t['r_multiple'] for t in bucket):.3f})")
    print(f"    Worst trade: ${min(t['pnl'] for t in bucket):,.2f}  "
          f"(R={min(t['r_multiple'] for t in bucket):.3f})")

print()

# ── CUT 3 ─ Time-in-trade distribution (H1 bars = 1 h each)

print("=" * 60)
print("CUT 3 — Bars-in-trade distribution (H1: 1 bar = 1 hour)")
print("=" * 60)

def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s)

durations = []
for t in trades:
    entry = parse_ts(t["entry_time"])
    exit_ = parse_ts(t["exit_time"])
    delta_h = (exit_ - entry).total_seconds() / 3600
    bars  = max(1, round(delta_h))
    durations.append(bars)

# bucket thresholds
buckets = [(1, 1), (2, 3), (4, 8), (9, 24), (25, 72), (73, 999)]
bucket_labels = ["1 bar", "2–3 bars", "4–8 bars", "9–24 bars", "25–72 bars (1–3 days)", ">72 bars"]

print()
for (lo, hi), label in zip(buckets, bucket_labels):
    idx   = [i for i, d in enumerate(durations) if lo <= d <= hi]
    count = len(idx)
    avg_r = sum(trades[i]["r_multiple"] for i in idx) / count if count else 0
    wins  = sum(1 for i in idx if trades[i]["pnl"] > 0)
    print(f"  {label:<28}  n={count:3d}  wins={wins:2d}  "
          f"avg_R={avg_r:+.3f}")

print()
sorted_d = sorted(durations)
print(f"  Median duration:  {sorted_d[len(sorted_d)//2]} bars")
print(f"  Mean duration:    {sum(durations)/len(durations):.1f} bars")
print(f"  Max duration:     {max(durations)} bars")
print(f"  Min duration:     {min(durations)} bars")

# what fraction of stop_outs are ≤ 3 bars?
short_stop_outs = [
    t for t, d in zip(trades, durations)
    if t["exit_type"] == "stop_out" and d <= 3
]
all_stop_outs = [t for t in trades if t["exit_type"] == "stop_out"]
print(f"\n  stop_outs ≤ 3 bars (false breakout proxy): "
      f"{len(short_stop_outs)} / {len(all_stop_outs)}  "
      f"({len(short_stop_outs)/len(all_stop_outs)*100:.1f}% of all stop_outs)")

print()

# ── CUT 4 ─ Remove top-3 by P&L

print("=" * 60)
print("CUT 4 — PF without top-3 trades by P&L")
print("=" * 60)

sorted_by_pnl = sorted(trades, key=lambda t: t["pnl"], reverse=True)
top3          = sorted_by_pnl[:3]
rest          = sorted_by_pnl[3:]

print(f"\n  Top-3 trades:")
for t in top3:
    print(f"    {t['entry_time'][:10]}  {t['direction']}  "
          f"pnl=${t['pnl']:>8,.2f}  R={t['r_multiple']:.3f}  exit={t['exit_type']}")

rest_wins = [t for t in rest if t["pnl"] > 0]
rest_loss = [t for t in rest if t["pnl"] <= 0]
gw = sum(t["pnl"] for t in rest_wins)
gl = abs(sum(t["pnl"] for t in rest_loss))

print(f"\n  Remaining {len(rest)} trades:")
print(f"    Wins:        {len(rest_wins)}")
print(f"    Losses:      {len(rest_loss)}")
print(f"    Win rate:    {len(rest_wins)/len(rest)*100:.1f}%")
print(f"    Gross win:   ${gw:,.2f}")
print(f"    Gross loss:  ${gl:,.2f}")
print(f"    PF:          {pf(gw, gl)}")
print(f"    Net P&L:     ${gw-gl:,.2f}")
print(f"    Avg R:       {sum(t['r_multiple'] for t in rest)/len(rest):.3f}")

print()

# ── CUT 5 ─ Entry timing: close below breakout level within 2 bars

print("=" * 60)
print("CUT 5 — Entry timing: false breakout check (close ≤ entry price within 2 bars)")
print("  Using existing bar cache — no API call")
print("=" * 60)

# find the bar cache
CACHE_DIR = Path(__file__).parent / "data" / "cache"
cache_files = list(CACHE_DIR.glob("XAUUSD_1h_*.json"))

if not cache_files:
    print("\n  ERROR: no bar cache found — cannot run cut 5")
else:
    # pick the largest cache file (most bars)
    cache_file = max(cache_files, key=lambda p: p.stat().st_size)
    print(f"\n  Cache: {cache_file.name}")

    with cache_file.open() as fh:
        raw_bars = json.load(fh)

    # build timestamp → close lookup
    bar_map: dict[datetime, float] = {}
    for rb in raw_bars:
        try:
            ts = datetime.fromisoformat(rb["datetime"]).replace(tzinfo=timezone.utc)
            bar_map[ts] = float(rb["close"])
        except (KeyError, ValueError):
            pass

    checked  = 0
    pullback = 0  # closed below entry (long) or above entry (short) within 2 bars

    for t in trades:
        entry_ts    = parse_ts(t["entry_time"])
        entry_price = t["entry_price"]
        direction   = t["direction"]

        # bars at entry+1h and entry+2h
        revert_count = 0
        for offset_h in (1, 2):
            from datetime import timedelta
            check_ts = entry_ts + timedelta(hours=offset_h)
            close    = bar_map.get(check_ts)
            if close is None:
                continue
            if direction == "LONG"  and close <= entry_price:
                revert_count += 1
            elif direction == "SHORT" and close >= entry_price:
                revert_count += 1

        # "entered at the top of the move" = both post-entry bars reverted
        if revert_count >= 2:
            pullback += 1
        checked += 1

    # also count 1-bar reverts
    one_bar_revert = 0
    for t in trades:
        entry_ts    = parse_ts(t["entry_time"])
        entry_price = t["entry_price"]
        direction   = t["direction"]
        from datetime import timedelta
        check_ts = entry_ts + timedelta(hours=1)
        close    = bar_map.get(check_ts)
        if close is None:
            continue
        if direction == "LONG"  and close <= entry_price:
            one_bar_revert += 1
        elif direction == "SHORT" and close >= entry_price:
            one_bar_revert += 1

    print(f"\n  Trades checked:                    {checked}")
    print(f"  Reverted by bar+1 (immediate):     {one_bar_revert} / {checked}  "
          f"({one_bar_revert/checked*100:.1f}%)")
    print(f"  Reverted both bar+1 AND bar+2:     {pullback} / {checked}  "
          f"({pullback/checked*100:.1f}%)")

    # avg R for reverting vs non-reverting
    revert_trades = []
    stay_trades   = []
    for t in trades:
        entry_ts    = parse_ts(t["entry_time"])
        entry_price = t["entry_price"]
        direction   = t["direction"]
        from datetime import timedelta
        close1 = bar_map.get(entry_ts + timedelta(hours=1))
        reverted = (
            close1 is not None and (
                (direction == "LONG"  and close1 <= entry_price) or
                (direction == "SHORT" and close1 >= entry_price)
            )
        )
        (revert_trades if reverted else stay_trades).append(t)

    print(f"\n  Avg R when bar+1 reverts:  {r_avg(revert_trades)}")
    print(f"  Avg R when bar+1 holds:    {r_avg(stay_trades)}")

    rv_wins = sum(1 for t in revert_trades if t["pnl"] > 0)
    st_wins = sum(1 for t in stay_trades  if t["pnl"] > 0)
    print(f"  Win rate when bar+1 reverts:  {rv_wins}/{len(revert_trades)}  "
          f"= {rv_wins/len(revert_trades)*100:.1f}%" if revert_trades else "")
    print(f"  Win rate when bar+1 holds:    {st_wins}/{len(stay_trades)}  "
          f"= {st_wins/len(stay_trades)*100:.1f}%" if stay_trades else "")

print()
print("─" * 60)
