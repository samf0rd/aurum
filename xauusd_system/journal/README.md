# Aurum research journal

## Strategy experiments

| ID | Date | Hypothesis | Verdict | PF |
|----|------|------------|---------|-----|
| EXP-001 | 2025-11-01 | Donchian-20 M15 long-only breakout has edge on gold | REJECTED | 0.47 |
| EXP-002 | 2026-06-06 | Entry confirmation delay (bar+1 hold) filters false breakouts | SUPPORTED | 1.17 |
| EXP-003 | 2026-06-06 | Confirmation=2 further improves PF over confirmation=1 | INCONCLUSIVE | 1.05 |
| EXP-004 | 2026-06-06 | Long-only filter isolates TRENDING_BULL edge | REJECTED | 1.03 |

## Strategy versions

| Version | Date | Description | Status |
|---------|------|-------------|--------|
| v1.0 | 2025-11-01 | Donchian-20, M15, long-only, ADX+vol filters | Abandoned — no edge |

## Key findings (running list)
- EXP-001: 48% of entries retrace on bar+1. First 8 bars have zero wins across 44 trades.
- EXP-001: Short side PF 1.21, long side PF 0.31. Strategy works better against trend.
- EXP-001: Strip top 3 trades → PF collapses to 0.27. Lottery-ticket shaped, not structural edge.
- EXP-002: Confirmation delay reduced trade frequency (11.4 → 7.6/mo) and lifted PF to 1.17. Account survived full 19-month period vs baseline stopped by risk limit at month 8.
- EXP-002: Retracement rate barely changed (81% → 78%) — confirmation filters weaker breakouts overall, not retracers specifically.
- EXP-002: Engine bug fixed (bars_held = -1 stop check against signal bar pre-dated the entry). Post-fix PF improved from 1.05 → 1.17.
- EXP-003: Confirmation=2 degraded PF (1.17 → 1.05) vs confirmation=1. Sweet spot confirmed at bars=1.
- EXP-003: TRENDING_BEAR PF consistently 0.53–0.61 on H1 across all three runs. Short side is the drag — timeframe reversal vs M15 baseline where shorts had PF 1.21.
- EXP-004: Long-only is WORSE than combined (PF 1.03 vs 1.17). TRENDING_BULL PF fell from 1.49 → 1.07. Path dependency: shorts cycle the engine flat at key moments; the R=6.56 August 2025 trade only fires in EXP-002 because preceding shorts freed the engine to enter. The 1.49 TRENDING_BULL PF is not decomposable from the two-sided system.
- EXP-004: Best validated config remains EXP-002 — both sides, confirmation=1, H1, PF 1.17.
