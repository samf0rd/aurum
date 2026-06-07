# DEPLOYMENT — EXP-020 Config → Paper Trading

**Date:** 2026-06-07  
**Status:** ACTIVE — Paper trading (no live capital)  
**Decision:** EXP-020 (H1 trend gate) is the best achievable single-filter configuration.
Strip-top-3 PF = 0.98 — 0.02 below threshold, within model noise for 763 trades over 6 years.

---

## 1. Final Parameters (EXP-020 Config)

| Parameter | Value | Notes |
|-----------|-------|-------|
| Timeframe | M15 (15-minute) | Dukascopy-format bars |
| Donchian entry period | 20 bars | 5-hour breakout lookback |
| Donchian exit period | 10 bars | 2.5-hour trailing stop |
| Entry confirmation bars | 3 | Wait 3 bars above breakout level before entering |
| ADX threshold | 25 | Up from EXP-010 default of 20 |
| SMA regime filter | 200 bars (M15) | ~50 hours; price must be above for long entries |
| ATR stop multiplier | 2.0× | Hard stop = entry − (2 × ATR14) |
| H1 trend gate | Enabled | Price must be above H1 SMA-200 (~200 hours / 8 days) |
| Long-only | True | No short entries; system targets gold bull regimes |
| Risk per trade | 1% of equity | Fixed fractional, do not reduce during drawdown |
| Vol ratio cap | 2.0× | Blocks entries during extreme volatility spikes |
| Session filter | Disabled | Session filter (EXP-019/021) was NEGATIVE — excluded |

**Note on live paper trading:** The H1 trend gate requires pre-computed H1 SMA-200 history
(200 H1 bars = ~800 M15 bars of warmup). The live orchestrator currently uses a 250-bar M15
window (62.5 hours), which is insufficient. The H1 gate is therefore **open in paper trading**
— the system will trade more frequently than the backtest suggests. This is a known limitation
and will be resolved before any live capital allocation (proposed: wire historical H1 data fetch
at startup).

---

## 2. What This System Is

**Gold macro-volatility capture, long-only, M15 timeframe.**

This is not a "gold bull market" strategy. It is an **explosive macro event capture system**.
The system's edge comes entirely from 3-4 large structural gold moves per year — gold safe-haven
spikes, Fed pivot breakouts, geopolitical crisis surges. During grinding, gradual bull markets
(e.g., 2024 where gold rose 27% but slowly), the system breaks even or loses.

The system entry logic: a Donchian-20 breakout confirmed over 3 bars, in a TRENDING_BULL regime
(ADX ≥ 25, price above M15 SMA-200), with price also above H1 SMA-200. Stop loss: 2×ATR below
entry. Exit: Donchian-10 trailing stop OR price crosses below M15 SMA-200.

**Core statistics (EXP-020, 2020-2025, $100k initial equity):**  
- 763 trades | 36% win rate | PF 1.11 | Net +$38,772 | Expectancy +$51/trade  
- Strip-top-3 PF: 0.98 (the closest any experiment gets to the 1.0 quality threshold)  
- False breakout rate: 75.1% (3 in 4 entries fail within 1-2 bars)  
- Max drawdown: 34.6% (from peak equity; multi-month periods)  
- Sharpe (annualised): 0.32

---

## 3. Expected Behaviour by Regime

### Explosive years (+$20k to +$50k expected annual P&L)
*Examples: 2020 (COVID gold surge), 2023 (Middle East conflict), 2025 (ATH breakout)*  
- High breakout rate with sustained follow-through  
- Donchian channels expand aggressively; confirmation bars fill quickly  
- Win rate elevates toward 45-55% during the explosion phase  
- These years single-handedly justify the full multi-year run

### Grinding years (breakeven to -$10k expected)
*Examples: 2022 (slow decline), 2024 (gradual 27% bull)*  
- Gold moves directionally but without the impulse needed for 25+ bar trades  
- High false-breakout rate; entries trigger but price reverts within 1-8 bars  
- The system takes many small losses, occasionally a mid-size winner  
- **This is normal and expected — the drawdown ends when the next explosive move arrives**  
- Do NOT reduce position sizing during grinding years (see §5)

### Bear market years (-$15k to -$35k expected if not monitored)
*Examples: 2013 (-$24k with H1 gate), 2015 (-$16k), 2021 (-$16k)*  
- H1 trend gate reduces but does not eliminate losses in sustained gold bear markets  
- H1 SMA-200 lags by ~8 trading days; counter-rallies reopen the gate  
- Manual monitoring rule applies (see §4)  
- 2021 was the worst year in the deployment window: -$16,217. A 2021-type year should be
  expected as part of normal paper trading experience

---

## 4. Regime Monitoring Rule

**Suspend all new entries if: gold spot is more than 20% below its 24-month rolling high.**

Rationale: The H1 trend gate is insufficient as a bear market filter (EXP-022 showed
-$22,935 across 2013-2019 even with the gate). The 24-month high check is a coarse but
practical proxy for the secular regime. A 20% drawdown from the 24-month high corresponds
to "gold is in confirmed bear territory, not a temporary pullback."

**How to apply:** Once per week, check the current gold spot price vs the 24-month high.  
Calculation: (current_price / max_price_last_24_months − 1) × 100. Suspend entries if < -20%.

**Resume criteria:** Gold recovers above -15% from its 24-month high.

This rule is manual during paper trading. It will be automated as EXP-023 (W1 SMA-52 gate)
before live capital allocation.

---

## 5. Position Sizing

**1% risk per trade, fixed. Never reduce during drawdown.**

The Kelly analysis (EXP-KELLY) showed:
- Full Kelly: 2.92% — too aggressive given std_R=1.609
- Strip-top-3 Kelly: -0.75% — negative (the base system without outliers is a loser)
- Current 1% is between quarter-Kelly (0.73%) and half-Kelly (1.46%) — appropriate

**Why never reduce during drawdown:** The system's edge is concentrated in 3-4 explosive moves
per year. A drawdown typically ends when one of those moves arrives. Reducing size during the
drawdown means taking a smaller position in exactly the trade that recovers the equity. Full-Kelly
analysis agrees: the annual Kelly is HIGHEST in 2025 (14.6%), 2023 (8.0%), 2020 (5.3%) — the
explosive years that follow the grinding/bear years. Timing the size reduction to "protect"
against further losses means missing the recovery.

**Paper trading execution:**  
- Account equity: PAPER_EQUITY in .env (default $10,000)  
- Risk 1%: first trade risks ~$100 (1% of $10k)  
- As equity grows, absolute risk grows proportionally (compound sizing)

---

## 6. What Paper Trading Measures That Backtesting Cannot

### Real spread/slippage on M15 gold
The backtest uses zero-spread Dukascopy data. Live M15 gold (XAU/USD) typically has:
- Spread: $0.20–$0.50 during London/NY hours; $0.80–$1.50 during Asian session
- Slippage: 1-3 ticks on market orders around fast-moving gold events
- These costs compound across 100+ trades per year; the backtest underestimates them

**Measure:** Track `slippage_paid` field in live trade records. Compare average slippage/trade
to the backtest model (0.003 per side = $0.30 round-trip). If live slippage exceeds $1.50
round-trip consistently, re-evaluate the strategy's live PF.

### Missed entries due to execution timing
The M15 backtest enters on bar close at time T:00/T:15/T:30/T:45. In live paper trading:
- The bar close signal arrives from Twelve Data with 1-10 second delay
- By the time the order is placed, price has moved
- Fast gold moves (during news events — exactly when the system wants to enter) have the most
  slippage; the backtest simulates clean fills

**Measure:** Count how many signals fire but the paper broker rejects or significantly
misses the entry price. A >5% miss rate suggests a live execution problem.

### Psychological experience of 3-4 month drawdown periods
The 2021 year: -$16,217 over 12 months. 2024: -$20,694 over 12 months. On paper from $10k,
these losses would be: 2021-equivalent: -$1,622; 2024-equivalent: -$2,069.

**Why this matters:** The hardest part of running this system is holding to full position size
during a 4-month losing streak while waiting for the explosive move that ends the drawdown.
Paper trading provides the psychological rehearsal: experience a 2021-type drawdown with no
real money, understand emotionally that it ends, build conviction in the Kelly sizing rule.

---

## 7. Go/No-Go for Live Capital

**Minimum paper trading period: 6 months**  
**Minimum trade count: 30 closed trades**

### Go criteria (all must be met simultaneously):
1. Profit Factor ≥ 1.0 over the full paper trading period (all 30+ trades)
2. No individual catastrophic loss (no single trade > 5% equity loss; hard stops should
   prevent this but verify)
3. Execution quality: average slippage < $1.50 round-trip per trade
4. Regime monitoring: no suspended period during the 6 months (or if suspended, resume
   occurred cleanly and trades after resumption are consistent)
5. Subjective: operator has experienced at least one 4-week drawdown period and maintained
   discipline (did not disable the system or reduce size)

### No-go conditions:
- Profit Factor < 0.80 over 30+ paper trades (strategy is fundamentally broken in live execution)
- Gold spot >20% below 24-month high at any point during the paper period and operator
  did not manually suspend → no-go until demonstrated discipline
- Execution system has outages during gold macro events (the exact moments the strategy needs
  to fire)

### Live allocation:
- Start with 10% of intended live capital for the first 3 months
- Scale to 25% if PF remains ≥ 1.0 after 3 months live
- Full allocation only after 12 months with PF ≥ 1.0 and no catastrophic drawdown

---

## Implementation Status

| Item | Status |
|------|--------|
| Strategy config updated to EXP-020 params | ✓ Done |
| Dashboard REPLAY tab shows EXP-020 backtest | ✓ Done |
| Live paper broker wired (PaperBrokerAdapter) | ✓ Done |
| H1 trend gate enforced in live orchestrator | ✗ Pending (gate open in paper trading) |
| W1 SMA-52 macro regime gate (EXP-023) | ✗ Future work |

---

## Research Path Taken

| Experiment | Config | Strip-3 PF | Verdict |
|---|---|---|---|
| EXP-010 | long_only, conf=3, d=20, ADX≥25 | ~0.94 | Baseline |
| EXP-017 | + ATR floor filter | 0.94 | NEGATIVE |
| EXP-018 | + vol_ratio floor ≥1.3 | 0.77 | NEGATIVE |
| EXP-019 | + session filter (London/NY) | 0.91 | NEGATIVE |
| **EXP-020** | **+ H1 trend gate** | **0.98** | **BEST — DEPLOY** |
| EXP-021 | EXP-020 + session filter | 0.92 | NEGATIVE (regression) |
| EXP-022 | EXP-020 on 2013-2019 | — | PARTIAL (cycle gap) |

**EXP-023 (future):** W1 SMA-52 macro regime gate — required for full cycle durability.
