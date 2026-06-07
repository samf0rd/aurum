# Kelly Sizing Analysis — EXP-010 trade distribution (2020-2025)

**Date:** 2026-06-07
**Source data:** results/exp-010.json (936 trades, EXP-010 config, research_mode=True)
**Status:** ANALYSIS COMPLETE

---

## Kelly Criterion computation

Using the classical Kelly formula for a discrete win/loss distribution:

> f* = p/|avg_loss_R| − q/avg_win_R = p − q/b
>
> where b = avg_win_R / avg_loss_R (win:loss ratio in R-multiples)

### Full system (all 936 trades)

| Metric | Value |
|--------|-------|
| Win rate (p) | 33.4% |
| Avg win (R) | +1.445 |
| Avg loss (R) | -0.663 |
| Win:loss ratio (b) | 2.181 |
| **Kelly f*** | **2.92% per trade** |
| Half-Kelly | 1.46% |
| Quarter-Kelly | 0.73% |
| EV per trade (R) | +0.044 |
| Std Dev (R) | 1.609 |

### Strip-top-3 distribution (933 trades)

| Metric | Value |
|--------|-------|
| Win rate (p) | 33.2% |
| Avg win (R) | +1.302 |
| Avg loss (R) | -0.663 |
| **Kelly f*** | **-0.75%** |

A **negative Kelly** means: the optimal bet on this distribution is zero. Don't trade it.

The full-system Kelly (2.92%) and the strip-top-3 Kelly (-0.75%) differ by 3.67 percentage
points — entirely explained by 3 trades out of 936.

### Annual Kelly

| Year | Win rate | b (win:loss R ratio) | Kelly f* |
|------|----------|----------------------|----------|
| 2020 | 30.3%    | 2.78                 | +5.3%    |
| 2021 | 31.3%    | 1.96                 | **-3.8%** |
| 2022 | 30.8%    | 1.82                 | **-7.2%** |
| 2023 | 36.3%    | 2.25                 | +8.0%    |
| 2024 | 32.7%    | 1.62                 | **-8.8%** |
| 2025 | 38.9%    | 2.58                 | +14.6%   |

**Positive Kelly years:** 2020, 2023, 2025 (three years, all gold event-driven)
**Negative Kelly years:** 2021, 2022, 2024 (three years — don't trade these)

---

## What the Kelly analysis says

### 1. The current 1% risk per trade is appropriate

Quarter-Kelly = 0.73%. Half-Kelly = 1.46%. The current 1% risk/trade sits between
quarter-Kelly and half-Kelly. This is the correct zone:
- **Not too large**: Full Kelly (2.92%) would cause geometric mean drag from high variance
  (std_R = 1.609 vs mean_R = 0.044). The coefficient of variation = 37× makes full Kelly
  dangerous — it would destroy capital in the three negative Kelly years.
- **Not too small**: Below quarter-Kelly leaves meaningful expected value unrealised.

The 1% risk/trade is an empirically reasonable fractional Kelly for this distribution.

### 2. The strategy's edge is entirely in the right tail

Top 5 R-multiples: +24.14, +15.16, +9.25, +8.32, +8.28
Bottom 5 R-multiples: -1.60, -1.48, -1.45, -1.28, -1.22

The loss distribution is **bounded and symmetrical** (stops limit losses to ~1–1.5R).
The win distribution is **heavily right-skewed** (outliers to +24R).

This is not a "high win rate grinds profit" distribution. It is a **rare large-win
distribution** (lottery ticket structure). The Kelly framework for binary outcomes
underestimates the risk because it doesn't capture the tail volatility correctly.

A more appropriate formula is the continuous Kelly:
> f* = μ/σ² = 0.044 / (1.609²) = 0.044 / 2.59 ≈ 1.7% per trade

The continuous Kelly (1.7%) is actually very close to the current 1% sizing, giving
additional confirmation that 1% is in the right range.

### 3. The sizing implication of strip-top-3 Kelly = -0.75%

This is the most important single number in the Kelly analysis.

Kelly -0.75% means: **without the three outlier trades, the optimal strategy is to
not trade at all.** You are betting 1% per trade on a system that the Kelly criterion
says should bet 0% (or actually go short the distribution via fees).

This doesn't mean "don't trade the system." It means: **the edge depends entirely on
being positioned to capture the outlier events.** You must accept 933 "expected-to-lose"
trades in order to be in the market when the 3 large outliers arrive.

The practical implication is counter-intuitive: **do not cut losers early, do not
reduce size during losing streaks.** The losing trades are the cost of being in the
market. If you implement a drawdown CB that kicks you out during the losing run
(which always precedes the big wins), you miss the very trades that justify the strategy.

This is the same finding as EXP-005 (the original circular-trap experiment). The
Kelly analysis provides the theoretical framework for why it's true.

### 4. Annual Kelly confirms the regime detection is not sufficient

Annual Kelly varies from -8.8% (2024) to +14.6% (2025). If you could know in advance
which regime you were in, you'd size 0% in 2021, 2022, 2024 and maximum Kelly in 2025.
But you cannot know in advance.

The ADX≥25 + long-only filter is a partial regime detector — it reduces losing trades
in bear regimes but does not eliminate them (as EXP-013 showed: 2024 had 165 trades
at PF 0.76 despite the ADX filter). A better regime filter could identify the 3 negative
Kelly years and reduce position size or sit out entirely. This is a research direction
but beyond current scope.

---

## Position sizing recommendation

Given the distribution characteristics:
- **Current setting (1% risk/trade)** is appropriate and within the defensible
  fractional Kelly range (0.73% – 1.46%). No change needed.
- **Do not increase above 1.5%** (half-Kelly) — the strip-top-3 distribution is
  negative Kelly, meaning the base rate doesn't support larger bets. Only the
  outliers justify the current 1%.
- **Do not implement Kelly scaling by year** — you don't know which year is positive
  vs negative in advance. Fixed 1% is better than a regime-scaling system that
  gets it wrong and misses 2025.
- **Do not reduce size during drawdowns below 10 consecutive losses** — the "reduce
  during drawdown" intuition is wrong for this distribution. The drawdown periods are
  when you need to stay in to capture the large move that ends them.
- **Fractional sizing recommendation**: 1% risk/trade is the right answer. It's
  sub-half-Kelly (half-Kelly = 1.46%) which is the standard prudent practitioner
  rule for fat-tailed distributions.

---

## Comparison to other Donchian breakout systems

For reference, classic trend-following CTA research (e.g. Lintner 1983, Hurst 2013)
typically shows:
- Win rates: 35-45%
- Win:loss ratios: 2.0-3.5
- Kelly fractions typically used: 10-25% (full Kelly), practitioner typically 5-15%

The EXP-010 distribution has:
- Win rate 33.4% (low-end of trend-following range)
- Win:loss ratio 2.18 (mid-range)
- Kelly fraction 2.92% of per-trade equity risk (very low vs. CTA practice)

The low Kelly is a consequence of **low trade frequency** (936 M15 trades in 6 years =
~156 trades/year vs. CTA portfolios trading 20-30 markets = 1000s of trades/year).
With higher frequency or more diversification, the effective Kelly would be higher.

---

*Filed: 2026-06-07*
