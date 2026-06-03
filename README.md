# Aurum — XAU/USD Algorithmic Trading System

> A fully systematic, rule-based trend-following algorithm for Gold (XAU/USD).
> Paper-trades live via OANDA. Tracks forward-test performance on a real-time web dashboard.

---

## Table of Contents

1. [What this is](#1-what-this-is)
2. [The Research Foundation](#2-the-research-foundation)
3. [The Strategy — All Rules](#3-the-strategy--all-rules)
4. [How the System Works](#4-how-the-system-works)
5. [The Four Engines](#5-the-four-engines)
6. [Data Flow — End to End](#6-data-flow--end-to-end)
7. [Risk Controls Reference](#7-risk-controls-reference)
8. [Validation & Backtesting](#8-validation--backtesting)
9. [Running the System](#9-running-the-system)
10. [Project File Map](#10-project-file-map)
11. [Known Limitations & Honest Caveats](#11-known-limitations--honest-caveats)

---

## 1. What This Is

This is not a black-box AI trading bot. Every single decision the system makes
is deterministic, rule-based, and can be explained in plain English. There are
no neural networks, no ML models, no hidden parameters.

The system does three things:

1. **Reads daily gold price data** from OANDA's API
2. **Applies a fixed set of rules** to decide whether to go long, short, or do nothing
3. **Manages risk mechanically** — position size, stop placement, and emergency halts
   are all automatic

The web dashboard shows you exactly what the algorithm is doing and why, in
real time. The goal for now is paper trading — trading with fake money to
validate that the strategy works in live market conditions before risking
real capital.

---

## 2. The Research Foundation

Before writing a single line of code, we spent significant time on research.
Here is what we found and why it matters.

### 2.1 How Gold Actually Behaves

Gold (XAU/USD) is not a simple "safe haven" asset. Its behavior is driven by
several competing forces:

**What drives gold up:**
- Falling real interest rates (nominal rates minus inflation)
- Weakening US dollar
- Geopolitical uncertainty and fear
- Central bank buying (especially China and India since 2022)
- Debasement fears / loss of confidence in fiat currencies

**What drives gold down:**
- Rising real interest rates (opportunity cost of holding gold increases)
- Strong dollar
- Liquidity crises (gold gets sold first because it's liquid)
- Risk-on environments where investors prefer equities

**The post-2022 structural shift (critical context):**
Before 2022, gold moved almost perfectly inverse to US 10-year real yields.
If rates went up, gold went down — reliably, predictably. After 2022, this
relationship broke down. The reason: Russia's reserves were frozen after the
Ukraine invasion. This made every other non-Western central bank realize that
US Treasuries could be weaponized. China, India, Turkey, and dozens of others
started buying gold as a *sovereign-neutral* reserve asset — one no government
can freeze. This created a structural demand floor that didn't exist before.

This means: **trading gold purely as a real-yield inverse is now a broken map.**
The algorithm accounts for this by using price action directly rather than
relying on external macro signals.

### 2.2 The Trading Sessions

Gold trades 24 hours but not all hours are equal:

| Session | Characteristics |
|---|---|
| **Asian (Tokyo/Shanghai)** | Lower volatility, physical demand tone, range-establishment |
| **London** | Highest liquidity, twice-daily LBMA fix events, deepest market |
| **New York** | COMEX futures dominance, US data releases (CPI, NFP, FOMC) drive the sharpest moves |
| **London/NY overlap** | Peak volatility and liquidity — where the most important price discovery happens |

We trade daily bars, so intraday session timing doesn't affect our entries.
But understanding sessions tells us *why* price can gap violently at certain
times — which is why our gap-caution rules exist (see Section 3).

### 2.3 Why Trend Following

We evaluated 12 different strategy categories for gold. The ranking:

| Rank | Strategy | Why it works / doesn't |
|---|---|---|
| 1 | **Trend Following** | Gold trends for structural macro reasons; low cost; easy to validate |
| 2 | Real-Yield Differential | Economically grounded but real-yield decoupling risk |
| 3 | Hybrid Trend + Reversion | Genuine diversification but validation burden doubles |
| 4 | Breakout | Similar to trend but false signals in ranging markets |
| 5-12 | Everything else | Either cost-exposed, data-inaccessible, or overfit-prone at retail |

**The conclusion:** For a retail trader using daily OHLCV data, trend following
is the most robust, lowest-cost, most economically defensible edge in gold.
The strategy we built is a daily-bar trend follower with a regime filter to
avoid trading in choppy, directionless markets.

### 2.4 The Myths We Rejected

Before building, we explicitly stress-tested common beliefs:

- ❌ **"Gold always rises in geopolitical crises"** — False. In Feb/Mar 2026, an active
  US-Iran conflict coincided with a 12% monthly crash because higher-for-longer rates
  dominated the narrative. The opportunity-cost channel is dormant, not dead.
- ❌ **"Gold is a clean dollar inverse"** — Broken since 2022. They now sometimes rise together.
- ❌ **"The central bank bid means gold can't fall hard"** — False. Central banks buy dips
  but don't prevent drawdowns. Turkey and others sold in the March 2026 crisis.
- ❌ **"Fixed take-profit targets are prudent"** — For a trend system, capping upside
  while leaving downside at full stop inverts the favorable risk/reward. No TP targets.

---

## 3. The Strategy — All Rules

This is the complete, precise specification of every decision the algorithm makes.
No rule can be overridden at runtime. No discretion exists.

### Rule 1 — Market Regime Filter

**Before any trade is considered, the market regime is classified using three conditions:**

**Rule 1a — Trend Direction**
The 50-day SMA must be above the 200-day SMA (for longs) or below (for shorts).

*Why:* Ensures we're trading in the direction of the dominant multi-month trend.
The 200-SMA is the most widely-watched long-term trend indicator in financial
markets. When price is above it, institutional money tends to be long.

**Rule 1b — Trend Strength**
ADX(14) must be above 20.

*Why:* ADX measures trend strength, not direction. A reading below 20 means
the market is ranging/choppy. Trend systems in choppy markets produce losses,
not profits. ADX 20 is the conventional threshold for "a trend worth trading."

**Rule 1c — Volatility Cap**
Current 14-day ATR must be below 2× its 100-day median ATR.

*Why:* Extreme volatility (flash crashes, news spikes) makes entries unreliable
and stops unpredictable. This rule keeps the system out of conditions where
the mathematical assumptions (stop at 2×ATR = X% loss) break down.

**Regime classification result:**
- `TRENDING_BULL` = 1a + 1b satisfied with 50-SMA > 200-SMA
- `TRENDING_BEAR` = 1a + 1b satisfied with 50-SMA < 200-SMA
- `CHOPPY` = ADX < 20 — no trades
- `HIGH_VOL` = ATR too extreme — no trades

### Rule 2 — Entry Conditions

**Entry only fires when:** regime is TRENDING_BULL or TRENDING_BEAR, AND:

**For LONG entry:** Today's close breaks above the 20-day Donchian channel high
(the highest high of the last 20 days).

**For SHORT entry:** Today's close breaks below the 20-day Donchian channel low
(the lowest low of the last 20 days).

*Why Donchian breakout:* A new 20-day high in a bullish regime means buyers
are paying progressively higher prices — momentum confirmation that the trend
is accelerating, not just persisting. This is the classic entry signal for
trend-following systems. It doesn't predict tops or bottoms; it captures the
middle of trending moves.

*Why only one position at a time:* Gold is a single instrument. No pyramiding.
No simultaneous long/short (impossible on the same asset). Simplicity is a
feature — it eliminates a family of position-sizing errors.

### Rule 3 — Exit Conditions

**Rule 3a — Trailing Exit (Donchian-10)**
For a LONG position: exit if today's close drops below the 10-day Donchian
channel low (the lowest low of the last 10 days).

For a SHORT position: exit if today's close rises above the 10-day Donchian
channel high (the highest high of the last 10 days).

*Why:* The 10-day trail is tighter than the 20-day entry channel, creating a
natural position management discipline — it locks in a meaningful portion of
a move while giving price room to breathe. This is the mechanism that generates
the strategy's positive skew (small losses, large wins).

**Rule 3b — Regime Invalidation**
Exit immediately if the regime that justified the trade is no longer valid.
Specifically: for a LONG, exit if close drops below the 200-day SMA.
For a SHORT, exit if close rises above the 200-day SMA.

*Why:* If the macro trend context that justified the entry has reversed, the
rationale for the trade is gone. Holding through a 200-SMA cross hoping for
recovery is discretionary behavior. This rule prevents it.

**Rule 3c — No Fixed Take Profit**
There is no take-profit order. Ever.

*Why:* This is the most counterintuitive rule and the most important. Trend
systems make money from a small number of large winners. If you cap gains at
a fixed target, you turn a positively-skewed strategy (occasional home runs)
into a negatively-skewed one (lots of small wins, occasional large losses).
The exit is when the trend ends — not when you've made "enough."

### Rule 4 — Stop Loss

On entry, an immediate hard stop is placed at: **entry price − 2.0 × ATR(14)**
for longs, mirror for shorts.

The stop only moves in the favorable direction (i.e., it trails). It NEVER
widens. This is enforced in code — not a guideline.

*Why 2× ATR:* ATR measures the typical daily price range. A stop at 2× ATR
sits below normal market noise, giving the trade room to breathe, while still
defining a clear "this idea is wrong" level. At higher volatility, the stop
automatically gets placed further away — so position size shrinks (Rule 6)
and risk-per-trade stays constant.

### Rule 5 — No Take Profit

See Rule 3c. Repeated here for emphasis because it surprises people.

### Rule 6 — Position Sizing

```
Quantity (lots) = (Account Equity × 1%) ÷ (Stop Distance × $100 per lot)
```

Where:
- Account Equity = current account value in USD
- 1% = the risk per trade (Rule 9)
- Stop Distance = 2 × ATR(14) in price points
- $100 per lot = 1 standard gold lot = 100 oz (approximate)

**Example:** Account = $10,000. ATR = $15. Stop distance = $30.
Quantity = ($10,000 × 0.01) ÷ ($30 × 100) = $100 ÷ $3,000 = 0.033 lots

*Why:* This formula means every trade risks exactly 1% of current equity
regardless of market volatility. In a high-volatility environment, the
stop is wider so position size automatically shrinks. In calm markets,
position size grows. The system self-adjusts — no manual intervention needed.

In gap-caution mode (Rule 10b), the 1% drops to 0.5% for 10 trades.

### Rule 7 — Daily Loss Limit (2%)

If total losses on a given day reach 2% of account equity, the system:
- Closes all open positions immediately
- Refuses all new entries for the rest of the session
- Resumes next calendar day

*Why:* Bounds single-day catastrophic loss. A bad CPI print, FOMC surprise,
or geopolitical shock can move gold $50-100 in minutes. This rule means one
bad day cannot destroy more than 2% of the account.

### Rule 8 — Weekly Loss Limit (5%)

Same mechanism as Rule 7, but applied over a rolling week. If 5% is lost
in any Monday-Friday period, no new trades until Monday.

*Why:* Prevents the scenario where a trader loses 2% on Monday, resets Tuesday,
loses 2% again, and compounds losses through a bad week. The weekly limit is
the safety net for the daily limit.

### Rule 9 — Risk Per Trade (1%)

Maximum risk on any single trade is 1% of current account equity. This is
the input to Rule 6's position sizing formula.

*Why 1%:* At 1% risk per trade, you need 100 consecutive losses to lose
your entire account. With realistic win rates (40-60%) this is essentially
impossible. At 2% risk, 50 losses wipe you out. The math of position sizing
is not about return — it's about survival. Survival is the prerequisite for
return.

### Rule 10 — Circuit Breakers

**Rule 10a — Drawdown Circuit Breaker (15%)**
If the account draws down 15% from its peak value, the system enters
emergency halt: all positions closed, no new trades until manually reset
and the drawdown recovers to within 10% of peak.

*Why 15%:* A 15% drawdown is a strong signal that the strategy's edge has
temporarily disappeared (regime change, structural break, model failure).
Better to stop, investigate, and re-enter when conditions improve than to
compound losses hoping for a mean reversion that may never come.

**Rule 10b — Gap-Caution Mode**
After a large overnight gap (close-to-open move > 3× ATR), position sizing
drops to 0.5% for the next 10 trades.

*Why:* Gaps are the strategy's primary failure mode. The stop is placed
assuming orderly price action. A gap through the stop means the actual loss
is larger than planned. After a gap event, we know the market is in a
regime where our mathematical assumptions are unreliable. We don't stop
trading — we shrink size.

**Rule 10c — Spread Gate**
If the current bid-ask spread is more than 3× the 20-day median spread,
no new entries are allowed. Existing positions are held.

*Why:* A wide spread is a direct cost. For a 20-pip stop and a 3-pip spread,
the breakeven on a long trade is entry + spread + stop = entry + 23 pips.
When spreads blow out (news events, market close, low liquidity), the
mathematical edge disappears. The spread gate keeps us out of expensive
conditions.

---

## 4. How the System Works

Here is the complete picture of how a single daily candle gets processed,
from market data to order execution:

```
Every day at market close:
═══════════════════════════

  OANDA API (raw price data)
       │
       ▼
  DataFeed.fetch_bars()
  └─ returns last 250 daily OHLCV bars
       │
       ▼
  RegimeDetector.detect(bars)
  ├─ computes SMA50, SMA200, ADX14, ATR14
  └─ returns: TRENDING_BULL / TRENDING_BEAR / CHOPPY / HIGH_VOL
       │
       ▼
  [If CHOPPY or HIGH_VOL → stop here, no trade today]
       │
       ▼
  SignalGenerator.on_bar(bars, regime)
  ├─ computes Donchian20 channel
  ├─ checks if close broke above (BULL) or below (BEAR)
  └─ returns: Signal(ENTER_LONG / ENTER_SHORT / NO_SIGNAL)
       │
       ▼
  [If NO_SIGNAL → stop here, no trade today]
       │
       ▼
  RiskEngine.approve_order(signal, risk_state, spread, median_spread)
  ├─ Rule 7: daily loss limit check
  ├─ Rule 8: weekly loss limit check
  ├─ Rule 10a: drawdown circuit breaker check
  ├─ Rule 10c: spread gate check
  └─ returns: (approved: True/False, reason: str)
       │
       ▼
  [If rejected → stop here, reason logged]
       │
       ▼
  RiskEngine.compute_position_size(signal, risk_state)
  └─ applies Rule 6 formula → quantity in lots
       │
       ▼
  ExecutionEngine.submit_order(order)
  ├─ wraps with retry logic
  ├─ handles partial fills
  ├─ reconciles with broker state
  └─ calls on_fill() callback when confirmed
       │
       ▼
  BrokerAdapter.place_order(order)
  ├─ PAPER MODE: PaperBrokerAdapter (simulates fills, no real money)
  └─ LIVE MODE: OandaAdapter (real OANDA v20 REST API)
       │
       ▼
  Fill confirmed → RiskEngine.record_fill(pnl)
  SystemState.record_equity(new_equity)
  Dashboard updates in real time
```

On every subsequent bar, before checking for new entries, the orchestrator
also checks exits (Rules 3a and 3b) for any open positions.

---

## 5. The Four Engines

The system is built as four independent, modular engines that were developed
separately and then wired together. This separation means each engine can be
tested, replaced, or improved independently.

### Engine 1: Backtest Engine (`backtest/`)

**Purpose:** Simulates the strategy on historical data to measure performance
before risking any money.

**What it contains:**
- Event-driven simulation loop (processes bars one at a time, strictly in order)
- Slippage model (realistic fill price including market impact)
- Spread model (bid-ask cost on every entry and exit)
- Commission model (broker fees)
- Monte Carlo analysis (randomizes trade order to test if results are robust)
- Full performance metrics: Sharpe ratio, max drawdown, win rate, expectancy

**Key design principle:** The backtest engine processes bars exactly as the
live system would — one at a time, never looking forward. This prevents the
most common form of backtest fraud: look-ahead bias.

**Used for:** Pre-deployment validation only. Not used in live trading.

### Engine 2: Risk Engine (`risk_engine/`)

**Purpose:** The single authority on all money management decisions.

**What it contains:**
- Fixed fractional position sizing (Rule 6, 9)
- Volatility-adjusted sizing alternative
- Daily loss limit enforcement (Rule 7)
- Weekly loss limit enforcement (Rule 8)
- Maximum drawdown circuit breaker (Rule 10a)
- Exposure limits (no oversizing)
- Consecutive-loss controls (soft: reduce size; hard: halt)
- Emergency shutdown logic (requires manual reset)

**Key design principle:** The risk engine is the ONLY place sizing and
gating decisions live. It has no knowledge of brokers, data, or strategy.
It receives a request and returns approved/rejected. Pure logic, no I/O.
This makes it trivially testable — 58 unit tests, zero external dependencies.

### Engine 3: Execution Engine (`execution_engine/`)

**Purpose:** Handles the mechanical process of sending orders to the broker
reliably, even in the face of network failures.

**What it contains:**
- Broker abstraction layer (works with any broker that implements the interface)
- Retry logic with exponential backoff
- Partial fill handling (accumulates fills until order is complete)
- Order reconciliation (compares local state to broker state every 30s)
- Position reconciliation (same, for positions)
- Network failure recovery (detects disconnection, reconnects, re-reconciles)
- Structured audit logging (JSON format, Datadog/CloudWatch compatible)

**Key design principle:** The execution engine assumes the broker will fail.
It is built around reliability, not speed. Every order has a full lifecycle
tracked from PENDING → SUBMITTED → FILLED (or REJECTED/CANCELLED).

### Engine 4: Trading System (`xauusd_system/`)

**Purpose:** The orchestrator that wires the other three engines together
plus the live data feed, dashboard, and paper trading module.

**What it contains:**
- `main.py` — startup and dependency injection
- `orchestrator/engine.py` — the main daily bar processing loop
- `strategy/signal_generator.py` — Donchian + regime logic (Rules 1-3)
- `data/oanda_feed.py` — live data from OANDA + synthetic generator for paper mode
- `paper/paper_broker.py` — simulates fills with realistic slippage
- `dashboard/` — FastAPI backend + React frontend
- `infrastructure/services.py` — event bus, alerting, Prometheus metrics

---

## 6. Data Flow — End to End

```
┌─────────────────────────────────────────────────────────────────────┐
│                        OANDA API                                     │
│                  (historical + live prices)                          │
└───────────────────────────┬─────────────────────────────────────────┘
                            │ 250 daily OHLCV bars
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    Data Feed (src/data/)                             │
│         OandaDataFeed / SyntheticDataFeed (paper mode)               │
└───────────────────────────┬─────────────────────────────────────────┘
                            │ Sequence[Bar]
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│                 Orchestrator (src/orchestrator/)                     │
│                                                                     │
│  ┌──────────────────┐    ┌──────────────────────────────────────┐   │
│  │ RegimeDetector   │    │ SignalGenerator                      │   │
│  │ SMA50 > SMA200?  │───▶│ Donchian-20 breakout?               │   │
│  │ ADX > 20?        │    │ Returns ENTER_LONG/SHORT/NO_SIGNAL   │   │
│  │ ATR < 2x median? │    └──────────────────┬───────────────────┘   │
│  └──────────────────┘                       │                       │
└────────────────────────────────────────────┼────────────────────────┘
                                             │ Signal
                                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│                   Risk Engine (src/risk/)                            │
│                                                                     │
│  Daily limit OK? → Weekly limit OK? → Drawdown CB OK?              │
│  Spread OK? → Size = (equity × 1%) ÷ (stop × $100)                 │
│                                                                     │
│  Returns: approved=True/False + quantity in lots                    │
└───────────────────────────┬─────────────────────────────────────────┘
                            │ ExecutionOrder
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│               Execution Engine (src/execution/)                      │
│                                                                     │
│  submit → retry loop → fill accumulation → reconciliation           │
│                                                                     │
│  ┌──────────────────────────┐   ┌──────────────────────────────┐   │
│  │  PaperBrokerAdapter      │   │  OandaAdapter                │   │
│  │  (PAPER_MODE=true)       │   │  (live credentials)          │   │
│  │  Simulates fills         │   │  Real REST/WS calls          │   │
│  └──────────────────────────┘   └──────────────────────────────┘   │
└───────────────────────────┬─────────────────────────────────────────┘
                            │ Fill event
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    System State (src/dashboard/)                     │
│                                                                     │
│  equity_curve[]  trades[]  position  regime                         │
│  Written by engine → Read by dashboard → Never reversed             │
└───────────────────────────┬─────────────────────────────────────────┘
                            │ HTTP + WebSocket
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│                  React Dashboard (port 8080)                         │
│                                                                     │
│  Equity curve  │  Open position  │  Trade history  │  Regime badge  │
│  Sharpe ratio  │  Max drawdown   │  Win rate       │  Live P&L      │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 7. Risk Controls Reference

Quick reference for all active risk controls:

| Rule | Control | Threshold | Action When Triggered |
|---|---|---|---|
| 1a | Trend filter | SMA50/SMA200 alignment | No entry signal generated |
| 1b | Strength filter | ADX(14) > 20 | No entry signal generated |
| 1c | Volatility cap | ATR < 2× 100-day median | No entry signal generated |
| 4 | Hard stop | Entry ± 2×ATR | Broker-side stop order placed immediately |
| 6 | Position size | Equity × 1% ÷ stop | Quantity calculated per-trade |
| 7 | Daily limit | 2% of equity | Close positions, halt for session |
| 8 | Weekly limit | 5% of equity | Close positions, halt until Monday |
| 9 | Risk per trade | 1% of equity | Input to position sizing formula |
| 10a | Drawdown CB | 15% from peak | Emergency halt, manual reset required |
| 10b | Gap caution | Close-open > 3×ATR | Half-size for next 10 trades |
| 10c | Spread gate | Spread > 3× median | No new entries |

---

## 8. Validation & Backtesting

The strategy went through a rigorous stress-testing process designed to
break it, not to confirm it. An adversarial risk committee perspective
was applied to find every possible failure mode.

### What was tested

- **Overfitting check:** All parameters are round, conventional numbers
  (50/200 SMA, 20/10 Donchian, 14-period ATR, 1% risk). No optimization
  was run. If a grid search had been run, the parameters would need to be
  adjusted for multiple-comparison bias.

- **Look-ahead bias audit:** The backtest engine processes bars strictly
  in time order. Rolling statistics (SMA, ATR, ADX) only use data available
  at the time of each bar. This was verified in code.

- **Walk-forward validation:** The strategy must work across multiple
  in-sample/out-of-sample splits, not just one.

- **Monte Carlo stress test:** 1000 simulated paths where trade order is
  randomized. If the strategy only works because of a specific sequence of
  lucky trades, this will show it.

- **Cost sensitivity:** Backtest results are re-run at 2× and 3× the
  assumed spread and slippage. A strategy that only works with optimistic
  cost assumptions is not robust.

- **Regime partition testing:** Performance tested separately on
  trending bull, trending bear, and choppy regimes. The strategy should
  underperform (lose less) in choppy regimes, not blow up.

### The validation gates (all must pass before live deployment)

- ☐ Positive PF (> 1.3) and Sharpe (> 0.7) in-sample
- ☐ ≥ 40 independent trades in-sample (statistical significance)
- ☐ Survives top-5-trades-removed test (PF ≥ 1.1 without best 5 trades)
- ☐ Out-of-sample degradation < 30% (live will always be worse than backtest)
- ☐ Walk-forward: ≥ 65% of folds profitable
- ☐ Monte Carlo: ruin probability < 1%
- ☐ Survives 2× cost stress test (still profitable at double spread/slippage)
- ☐ ≥ 1 full quarter of paper trading with metrics inside backtest CI

### Current status

The strategy has been backtested. The system is now in **paper trading mode**
— running on live market data but executing no real orders. Forward-test
performance is tracked on the dashboard. The goal is ≥ 1 quarter of paper
results before considering live capital.

---

## 9. Running the System

### Prerequisites

- Python 3.12+
- OANDA practice account (free) — or use PAPER_MODE without credentials
- Node.js 18+ (for React dashboard development only)

### Setup

```powershell
# 1. Run the bootstrap (from algo_bot/ folder, with zip files present)
py setup_project.py

# 2. Activate virtual environment and install deps
cd xauusd_system
.\.venv\Scripts\Activate.ps1    # Windows
pip install aiohttp numpy prometheus-client python-dotenv fastapi "uvicorn[standard]" websockets

# 3. Verify imports
python -c "import sys; sys.path.insert(0,'src'); from execution import ExecutionEngine; from risk import RiskEngine; print('OK')"

# 4. Configure (copy and edit)
copy .env.example .env
# Set OANDA_ACCOUNT_ID and OANDA_API_TOKEN if live
# Leave blank for paper mode (uses synthetic prices)

# 5. Run
$env:PYTHONPATH = "src"
$env:PAPER_MODE = "true"
python -m src.main

# 6. Open dashboard
start http://localhost:8080
```

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `PAPER_MODE` | `true` | Use paper broker (no real money) |
| `OANDA_ACCOUNT_ID` | — | OANDA account ID (live mode only) |
| `OANDA_API_TOKEN` | — | OANDA API token (live mode only) |
| `INITIAL_EQUITY` | `10000` | Starting capital in USD |
| `RISK_FRACTION` | `0.01` | Risk per trade (1%) |
| `MAX_DAILY_LOSS` | `0.02` | Daily loss limit (2%) |
| `MAX_WEEKLY_LOSS` | `0.05` | Weekly loss limit (5%) |
| `MAX_DRAWDOWN` | `0.15` | Drawdown circuit breaker (15%) |
| `LOOKBACK_BARS` | `250` | Historical bars to fetch |
| `PAPER_MODE` | `true` | Use paper broker (no real money) |
| `DASHBOARD_PORT` | `8080` | Dashboard HTTP port |
| `METRICS_PORT` | `8000` | Prometheus metrics port |
| `LOG_LEVEL` | `INFO` | Logging verbosity |

---

## 10. Project File Map

```
algo_bot/
├── setup_project.py          ← run once to wire everything together
├── CLAUDE.md                 ← instructions for Claude Code AI assistant
├── README.md                 ← this file
│
├── xauusd_system/            ← main trading system
│   ├── src/
│   │   ├── main.py                      startup + dependency injection
│   │   ├── core/
│   │   │   └── interfaces.py            all domain types and ABCs (Bar, Signal, Order…)
│   │   ├── strategy/
│   │   │   └── signal_generator.py      Rules 1-3: regime filter + Donchian entry/exit
│   │   ├── risk/
│   │   │   ├── engine.py                Rules 4,6-10: all money management logic
│   │   │   └── models.py                RiskConfig, OrderRequest, OrderDecision…
│   │   ├── execution/
│   │   │   ├── engine.py                ExecutionEngine: submit, retry, reconcile
│   │   │   ├── models.py                ExecutionOrder, Fill, ExecutionConfig…
│   │   │   ├── brokers/
│   │   │   │   ├── base.py              IBrokerAdapter + RetryingBrokerAdapter
│   │   │   │   └── oanda.py             OandaAdapter (live trading)
│   │   │   ├── reconciliation/
│   │   │   │   └── reconciler.py        order + position reconciliation
│   │   │   └── recovery/
│   │   │       └── network.py           reconnection + circuit breaker
│   │   ├── data/
│   │   │   └── oanda_feed.py            live bars + synthetic data for paper mode
│   │   ├── paper/
│   │   │   └── paper_broker.py          simulated fills for paper trading
│   │   ├── orders/
│   │   │   └── manager.py               in-memory position ledger
│   │   ├── orchestrator/
│   │   │   └── engine.py                main run loop, wires all components
│   │   ├── infrastructure/
│   │   │   └── services.py              event bus, alerting, Prometheus metrics
│   │   └── dashboard/
│   │       ├── api.py                   FastAPI: REST endpoints + WebSocket
│   │       ├── state.py                 SystemState singleton (engine writes, UI reads)
│   │       └── static/                  React dashboard (built output)
│   ├── logs/                            runtime logs (gitignored)
│   │   ├── equity_curve.jsonl           equity history (persists across restarts)
│   │   └── paper_trades.jsonl           trade log
│   ├── tests/
│   ├── pyproject.toml
│   ├── docker-compose.yml
│   ├── Dockerfile
│   └── .env.example
│
├── backtest/                 ← backtesting engine (reference, not used in live)
│   └── src/
│       ├── engine/backtest.py           main simulation loop
│       ├── data/handlers.py             historical + synthetic data
│       ├── execution/models.py          slippage, spread, commission models
│       ├── risk/portfolio.py            position sizing and risk management
│       └── metrics/calculator.py        Sharpe, drawdown, win rate, expectancy
│
├── risk_engine/              ← standalone risk engine (production version)
│   └── src/risk/
│       ├── engine.py                    RiskEngine: all 8 controls
│       └── models.py                    RiskConfig, OrderRequest, OrderDecision…
│
└── execution_engine/         ← standalone execution engine (production version)
    └── src/execution/
        ├── engine.py                    ExecutionEngine
        ├── models.py                    all execution domain types
        ├── brokers/                     broker adapters
        ├── reconciliation/              order + position reconciliation
        └── recovery/                    network failure handling
```

---

## 11. Known Limitations & Honest Caveats

This section exists because we applied an adversarial risk committee mindset
to the strategy. Here are the real risks, stated plainly:

### What could make this strategy fail

**Regime change.** The strategy is a trend follower. In a prolonged choppy,
directionless gold market (ADX consistently below 20), it will produce many
small losses and few large wins. The regime filter will keep us mostly out,
but false signals will still occur. The strategy was explicitly designed
for the post-2022 gold regime. If that regime ends, performance will degrade.

**Gap risk.** The 2×ATR stop assumes orderly price action. A gap (weekend
open, news shock, geopolitical event) can push price through the stop level
before the broker executes. The actual loss could be larger than 1% planned.
Rule 10b (gap-caution) reduces size after a gap but cannot eliminate this risk.

**Broker execution.** We use OANDA for live trading. OANDA is a reputable
retail broker but is not an institutional prime broker. Spread during news
events can be multiples of the normal spread. The spread gate (Rule 10c)
keeps us out of the worst conditions, but cannot catch everything.

**Real yield reassertion.** The post-2022 gold-yield decoupling could
re-couple. If the traditional opportunity-cost model reasserts strongly,
the structural demand floor could disappear quickly. The 200-SMA filter
would catch this eventually, but not before some drawdown.

**Overfitting risk we didn't eliminate.** Even with round-number parameters,
the *choice* of Donchian-20 entry, Donchian-10 exit, and SMA-200 regime
filter is influenced by what has historically worked on trend-following assets.
This is meta-overfitting — the entire industry's historical research is
embedded in the "conventional" parameter choices.

### What this system is NOT

- It is not a market-prediction system. It has no view on where gold is going.
- It is not an arbitrage system. It has no structural edge that can't be competed away.
- It is not a high-frequency system. It trades at most once per day.
- It is not guaranteed to be profitable. No system is.

### The honest pre-deployment checklist

Before putting any real money on this system, all of the following must be true:

- [ ] ≥ 1 full quarter of paper trading results available
- [ ] Paper results are within the confidence interval of backtest results
- [ ] You understand every rule in Section 3 and can explain it without this document
- [ ] You have set the drawdown circuit breaker (Rule 10a) to an amount you
      are genuinely comfortable losing — and then treated that as real
- [ ] You have a written plan for what happens if the drawdown CB triggers
      (investigate vs. restart vs. permanently shut down)
- [ ] You have verified that OANDA's stop execution works as expected on
      your account type (some account types have different stop behavior)

---

*This documentation reflects the complete intellectual work behind the system.
It is not investment advice. Past backtest performance does not guarantee
future live performance. Trading involves risk of loss.*
