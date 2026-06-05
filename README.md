# Aurum — XAU/USD Algorithmic Trading System

> A fully systematic, rule-based trend-following algorithm for Gold (XAU/USD).
> Paper-trades live on real market prices via Twelve Data API.
> Dashboard live at **https://gold.samvgarcia.com**

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

1. **Reads 15-minute gold price data** from Twelve Data API (real XAU/USD prices)
2. **Applies a fixed set of rules** to decide whether to go long, short, or do nothing
3. **Manages risk mechanically** — position size, stop placement, and emergency halts
   are all automatic

The web dashboard shows exactly what the algorithm is doing and why, in real time.
The system runs in **paper mode** — trading with virtual money to validate that the
strategy works in live market conditions before risking real capital. Market data is
real; order fills are simulated internally by the PaperBroker (no external broker
account required).

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
After 2022, this relationship broke down. Russia's reserves were frozen after the
Ukraine invasion, causing every other non-Western central bank to start buying gold
as a sovereign-neutral reserve asset. This created a structural demand floor that
didn't exist before.

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

We trade 15-minute bars (M15). The regime filter and Donchian channel are computed
on M15 OHLCV data, so session timing directly affects intraday entries. The
gap-caution rules exist because news events can move price violently within a single bar.

### 2.3 Why Trend Following

We evaluated multiple strategy categories for gold. The conclusion: for a retail
trader using OHLCV data, trend following is the most robust, lowest-cost, most
economically defensible edge in gold. The strategy we built is an M15 trend follower
with a regime filter to avoid trading in choppy, directionless markets.

### 2.4 The Myths We Rejected

- ❌ **"Gold always rises in geopolitical crises"** — False. In Feb/Mar 2026, an active
  US-Iran conflict coincided with a 12% monthly crash because higher-for-longer rates
  dominated the narrative.
- ❌ **"Gold is a clean dollar inverse"** — Broken since 2022. They now sometimes rise together.
- ❌ **"The central bank bid means gold can't fall hard"** — False. Central banks buy dips
  but don't prevent drawdowns.
- ❌ **"Fixed take-profit targets are prudent"** — For a trend system, capping upside
  while leaving downside at full stop inverts the favorable risk/reward. No TP targets.

---

## 3. The Strategy — All Rules

This is the complete, precise specification of every decision the algorithm makes.
No rule can be overridden at runtime. No discretion exists.

**Timeframe: M15 (15-minute bars).** All lookbacks below refer to M15 bars unless noted.

### Rule 1 — Market Regime Filter

**Before any trade is considered, the market regime is classified using three conditions:**

**Rule 1a — Trend Direction**
Close must be above the 200-period SMA (for longs) or below (for shorts).
On M15, SMA-200 = rolling 50 hours of price history.

*Why:* Ensures we're trading in the direction of the dominant multi-hour trend.
The 200-period SMA is the most widely-watched long-term trend indicator in financial
markets.

**Rule 1b — Trend Strength**
ADX(14) must be above 20.

*Why:* ADX measures trend strength, not direction. A reading below 20 means the market
is ranging or choppy. Trend systems in choppy markets produce losses, not profits.

**Rule 1c — Volatility Cap**
Current ATR(14) must be below 2× its 100-bar median ATR.

*Why:* Extreme volatility makes entries unreliable and stops unpredictable. This rule
keeps the system out of conditions where the mathematical assumptions break down.

**Regime classification result:**
- `TRENDING_BULL` = 1a + 1b satisfied with close > SMA-200
- `TRENDING_BEAR` = 1a + 1b satisfied with close < SMA-200
- `CHOPPY` = ADX < 20 — no trades
- `HIGH_VOL` = ATR too extreme — no trades

### Rule 2 — Entry Conditions

**Entry only fires when:** regime is TRENDING_BULL or TRENDING_BEAR, AND:

**For LONG entry:** Close breaks above the 20-bar Donchian channel high
(the highest high of the last 20 M15 bars = last 5 hours).

**For SHORT entry:** Close breaks below the 20-bar Donchian channel low.

*Why Donchian breakout:* A new 20-bar high in a bullish regime means buyers are paying
progressively higher prices — momentum confirmation. This captures the middle of trending
moves, not tops and bottoms.

### Rule 3 — Exit Conditions

**Rule 3a — Trailing Exit (Donchian-10)**
For a LONG position: exit if close drops below the 10-bar Donchian channel low
(lowest low of the last 10 M15 bars = last 2.5 hours).

For a SHORT position: exit if close rises above the 10-bar Donchian channel high.

*Why:* The 10-bar trail is tighter than the 20-bar entry channel — it locks in a
meaningful portion of a move while giving price room to breathe.

**Rule 3b — Regime Invalidation**
For a LONG, exit if close drops below the 200-bar SMA.
For a SHORT, exit if close rises above the 200-bar SMA.

*Why:* If the macro context that justified the entry has reversed, the rationale for
the trade is gone.

**Rule 3c — No Fixed Take Profit**
There is no take-profit order. The exit is when the trend ends.

### Rule 4 — Stop Loss

On entry, an immediate hard stop is placed at: **entry price − 2.0 × ATR(14)**
for longs, mirror for shorts. The stop only moves in the favorable direction. It
NEVER widens.

### Rule 5 — No Take Profit

See Rule 3c.

### Rule 6 — Position Sizing

```
Quantity (lots) = (Account Equity × 1%) ÷ (Stop Distance × $100 per lot)
```

Every trade risks exactly 1% of current equity regardless of market volatility.

### Rule 7 — Daily Loss Limit (2%)

If total losses on a given day reach 2% of account equity, the system closes all open
positions and refuses new entries for the rest of the session.

### Rule 8 — Weekly Loss Limit (5%)

Same mechanism as Rule 7, applied over a rolling Monday–Friday week.

### Rule 9 — Risk Per Trade (1%)

Maximum risk on any single trade is 1% of current account equity.

### Rule 10 — Circuit Breakers

**Rule 10a — Drawdown Circuit Breaker (15%):**
If the account draws down 15% from its peak, all positions are closed and no new
trades are placed until the system is manually reset.

**Rule 10b — Gap-Caution Mode:**
After a large gap (close-to-open move > 3× ATR), position sizing drops to 0.5%
for the next 10 trades.

**Rule 10c — Spread Gate:**
If the current spread is more than 3× the 20-bar median spread, no new entries.

---

## 4. How the System Works

Here is how a single M15 bar gets processed from market data to order execution:

```
Every 15 minutes at bar close:
═══════════════════════════════

  Twelve Data API (real XAU/USD prices)
       │
       ▼
  TwelveDataFeed.get_bars()
  └─ returns last 250 M15 OHLCV bars
       │
       ▼
  RegimeDetector.detect(bars)
  ├─ computes SMA200, ADX14, ATR14 on M15 bars
  └─ returns: TRENDING_BULL / TRENDING_BEAR / CHOPPY / HIGH_VOL
       │
       ▼
  [If CHOPPY or HIGH_VOL → stop here, no trade this bar]
       │
       ▼
  SignalGenerator.on_bar(bars, regime)
  ├─ computes Donchian20 channel
  ├─ checks if close broke above (BULL) or below (BEAR)
  └─ returns: Signal(ENTER_LONG / ENTER_SHORT / NO_SIGNAL)
       │
       ▼
  [If NO_SIGNAL → stop here]
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
  RiskEngine.compute_position_size(signal, risk_state)
  └─ applies Rule 6 formula → quantity in lots
       │
       ▼
  ExecutionEngine.submit_order(order)
  ├─ wraps with retry logic
  ├─ handles partial fills
  └─ calls on_fill() callback when confirmed
       │
       ▼
  PaperBrokerAdapter.place_order(order)
  └─ Simulates fill with realistic slippage (no real broker needed)
       │
       ▼
  Fill confirmed → RiskEngine.record_fill(pnl)
  SystemState.record_equity(new_equity)
  Dashboard updates in real time via WebSocket
```

On every subsequent bar, before checking for new entries, the orchestrator
also checks exits (Rules 3a and 3b) for any open positions.

---

## 5. The Four Engines

### Engine 1: Strategy Engine (`xauusd_system/src/strategy/`)

Pure strategy logic — stateless, no I/O. `RegimeDetector` implements Rules 1a–1c.
`DonchianBreakoutSignalGenerator` implements Rules 2, 3a, 3b. All indicator
computations (SMA, ATR, ADX, Donchian) live here as pure functions.

### Engine 2: Risk Engine (`xauusd_system/src/risk/`)

The single authority on all money management decisions. Pure logic, no I/O.
Receives a request and returns approved/rejected. Implements Rules 4, 6–10.

### Engine 3: Execution Engine (`xauusd_system/src/execution/`)

Handles the mechanical process of sending orders reliably. Includes retry logic with
exponential backoff, partial fill handling, order/position reconciliation every 30s,
and network failure recovery. In paper mode the broker adapter is `PaperBrokerAdapter`
which simulates fills with realistic slippage.

### Engine 4: Orchestrator (`xauusd_system/src/orchestrator/`)

Wires all components together. Runs three concurrent loops:
- **Bar loop**: polls for new M15 bars every 60 seconds, runs the full strategy
  pipeline when a new bar timestamp is detected
- **Price loop**: fetches live XAU/USD price every 5 minutes (rate-limit budget),
  updates the dashboard and forming candle
- **Daily reset**: resets daily P&L counters at 00:05 UTC

---

## 6. Data Flow — End to End

```
┌─────────────────────────────────────────────────────────────────────┐
│                     Twelve Data API                                  │
│          (real XAU/USD M15 OHLCV bars + live price)                  │
└───────────────────────────┬─────────────────────────────────────────┘
                            │ 250 M15 bars (every 15 min)
                            │ live price (every 5 min)
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    Data Feed (src/data/)                             │
│                       TwelveDataFeed                                 │
│          get_bars(granularity="M15", count=250)                      │
│          get_latest_tick() → {bid, ask, timestamp}                   │
└───────────────────────────┬─────────────────────────────────────────┘
                            │ Sequence[Bar]
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│                 Orchestrator (src/orchestrator/)                     │
│                                                                     │
│  ┌──────────────────┐    ┌──────────────────────────────────────┐   │
│  │ RegimeDetector   │    │ SignalGenerator                      │   │
│  │ Close > SMA200?  │───▶│ Donchian-20 M15 breakout?           │   │
│  │ ADX14 > 20?      │    │ Returns ENTER_LONG/SHORT/NO_SIGNAL   │   │
│  │ ATR < 2× median? │    └──────────────────┬───────────────────┘   │
│  └──────────────────┘                       │                       │
└────────────────────────────────────────────┼────────────────────────┘
                                             │ Signal
                                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│                   Risk Engine (src/risk/)                            │
│                                                                     │
│  Daily limit OK? → Weekly limit OK? → Drawdown CB OK?              │
│  Spread OK? → Size = (equity × 1%) ÷ (stop × $100)                 │
└───────────────────────────┬─────────────────────────────────────────┘
                            │ ExecutionOrder
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│               Execution Engine (src/execution/)                      │
│                                                                     │
│  submit → retry loop → fill accumulation → reconciliation           │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  PaperBrokerAdapter (PAPER_MODE=true)                        │   │
│  │  Simulates fills with realistic slippage — no broker needed  │   │
│  └──────────────────────────────────────────────────────────────┘   │
└───────────────────────────┬─────────────────────────────────────────┘
                            │ Fill event
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    System State (src/dashboard/)                     │
│                                                                     │
│  equity_curve[]  trades[]  position  regime  forming_candle         │
│  Written by engine → Read by dashboard → Never reversed             │
└───────────────────────────┬─────────────────────────────────────────┘
                            │ HTTP + WebSocket
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│           Dashboard (port 8080 / https://gold.samvgarcia.com)        │
│                                                                     │
│  Candlestick chart  │  Decision tree  │  Open position  │  P&L      │
│  Equity curve       │  Trade history  │  Regime badge   │  ADX/ATR  │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 7. Risk Controls Reference

| Rule | Control | Threshold | Action When Triggered |
|---|---|---|---|
| 1a | Trend filter | Close vs SMA-200 (M15) | No entry signal generated |
| 1b | Strength filter | ADX(14) > 20 | No entry signal generated |
| 1c | Volatility cap | ATR < 2× 100-bar median | No entry signal generated |
| 4 | Hard stop | Entry ± 2×ATR | Stop recorded; intrabar circuit breaker checks every 5 min |
| 6 | Position size | Equity × 1% ÷ stop | Quantity calculated per-trade |
| 7 | Daily limit | 2% of equity | Close positions, halt for session |
| 8 | Weekly limit | 5% of equity | Close positions, halt until Monday |
| 9 | Risk per trade | 1% of equity | Input to position sizing formula |
| 10a | Drawdown CB | 15% from peak | Emergency halt, manual reset required |
| 10b | Gap caution | Close-open > 3×ATR | Half-size for next 10 trades |
| 10c | Spread gate | Spread > 3× median | No new entries |

---

## 8. Validation & Backtesting

The strategy went through a rigorous stress-testing process designed to break it,
not confirm it.

### What was tested

- **Overfitting check:** All parameters are round, conventional numbers
  (SMA-200, Donchian-20/10, ADX-14, ATR-14, 1% risk). No optimization was run.
- **Look-ahead bias audit:** The backtest engine processes bars strictly in time order.
- **Walk-forward validation:** Performance across multiple in-sample/out-of-sample splits.
- **Monte Carlo stress test:** 1000 simulated paths with randomized trade order.
- **Cost sensitivity:** Results re-run at 2× and 3× assumed spread and slippage.
- **Regime partition testing:** Performance tested separately on TRENDING_BULL,
  TRENDING_BEAR, and CHOPPY regimes.

### Two strategy profiles

The system supports two profiles via the `STRATEGY_PROFILE` env var:

| Profile | Timeframe | Status |
|---|---|---|
| `swing` | H1 (1-hour bars) | Backtested and walk-forward validated |
| `intraday` | M15 (15-minute bars) | Same rule structure, faster clock — forward-test in progress |

The `intraday` profile runs the identical rule logic on a faster timeframe. It has not
yet cleared the same validation gates (walk-forward, Monte Carlo, cost stress) as the
`swing` profile and should be treated as a forward-test.

### Current status

The system is in **paper trading mode** (virtual money, real prices). Forward-test
performance is tracked on the live dashboard. The goal is ≥ 1 quarter of paper results
before considering live capital.

---

## 9. Running the System

### Prerequisites

- Python 3.12+
- Twelve Data API key — free tier at [twelvedata.com](https://twelvedata.com) (no card required)
- `requests` and the other dependencies listed in `pyproject.toml`

### Setup

```bash
cd xauusd_system

# 1. Create virtual environment and install dependencies
python -m venv .venv
source .venv/bin/activate        # Linux/Mac
# .venv\Scripts\Activate.ps1    # Windows PowerShell

pip install -e .

# 2. Configure environment
cp .env.example .env
# Edit .env — set TWELVE_DATA_API_KEY=<your key>

# 3. Run the system
PYTHONPATH=src PAPER_MODE=true python -m src.main

# 4. Open dashboard
open http://localhost:8080
```

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `TWELVE_DATA_API_KEY` | — | **Required.** Free at twelvedata.com |
| `PAPER_MODE` | `true` | Always true — paper execution via internal PaperBroker |
| `PAPER_EQUITY` | `100000` | Starting virtual equity in USD |
| `STRATEGY_PROFILE` | `intraday` | `intraday` (M15) or `swing` (H1) |
| `PRICE_TICK_INTERVAL` | `300` | Seconds between live price fetches (300 = 5 min) |
| `DASHBOARD_PORT` | `8080` | Dashboard HTTP port |
| `LOOKBACK_BARS` | `250` | Historical bars to fetch on startup |
| `LOG_LEVEL` | `INFO` | Logging verbosity |

**Rate limit note:** Twelve Data free tier = 800 API calls/day. At default settings
(bar loop every 60s polling for new M15 bars + price tick every 300s):
~96 bar calls/day + ~288 price calls/day = ~385 total. Safely under the 800 limit.

### Deployment (production)

The system runs on a Hetzner CAX11 server at **https://gold.samvgarcia.com**:

```
Internet → nginx (HTTPS, port 443) → Aurum container (port 8080, localhost only)
```

- **Auto-deploy:** A cron job polls GitHub every 5 minutes. Any push to `main`
  triggers a `docker compose up -d --build` within 5 minutes.
- **Data persistence:** SQLite database at `/app/data/trading.db` is volume-mounted
  and survives container rebuilds.
- **SSL:** Let's Encrypt certificate managed by certbot, auto-renews every 90 days.

See `AURUM_DEPLOY.md` for the complete server setup guide.

```bash
# Deploy manually (don't wait for cron)
ssh root@<server-ip> "/opt/aurum/deploy.sh"

# Check logs on server
docker compose logs -f aurum
```

---

## 10. Project File Map

```
algo_bot/
├── .gitignore
├── README.md                 ← this file
├── AURUM_DEPLOY.md           ← Hetzner + nginx + SSL setup guide
│
└── xauusd_system/            ← the trading system
    ├── src/
    │   ├── main.py                      startup + dependency injection
    │   │                                checks TWELVE_DATA_API_KEY, wires all components
    │   ├── core/
    │   │   ├── interfaces.py            ALL domain types and ABCs
    │   │   │                            Bar, Tick, Signal, Order, Position, RiskState
    │   │   │                            IDataFeed, ISignalGenerator, IRegimeDetector
    │   │   │                            IRiskEngine, IOrderManager, IBrokerAdapter
    │   │   └── config.py                StrategyProfile dataclass
    │   │                                SWING (H1, validated) and INTRADAY (M15)
    │   │                                ACTIVE_PROFILE selected via STRATEGY_PROFILE env var
    │   ├── strategy/
    │   │   └── signal_generator.py      Rules 1–3: regime filter + Donchian entry/exit
    │   │                                RegimeDetector, DonchianBreakoutSignalGenerator
    │   │                                All lookbacks sourced from ACTIVE_PROFILE
    │   ├── risk/
    │   │   ├── engine.py                Rules 4, 6–10: all money management logic
    │   │   └── models.py                RiskConfig, RiskState
    │   ├── execution/
    │   │   ├── engine.py                submit, retry, reconcile
    │   │   ├── models.py                ExecutionOrder, Fill, ExecutionConfig
    │   │   ├── brokers/
    │   │   │   ├── base.py              IBrokerAdapter + RetryingBrokerAdapter
    │   │   │   └── oanda.py             OandaAdapter (present but not active in paper mode)
    │   │   ├── reconciliation/
    │   │   │   └── reconciler.py        order + position reconciliation (30s cycle)
    │   │   └── recovery/
    │   │       └── network.py           reconnection + circuit breaker
    │   ├── data/
    │   │   └── twelvedata_feed.py       IDataFeed implementation for Twelve Data API
    │   │                                get_bars(granularity, count) → list[Bar]
    │   │                                get_latest_tick() → {bid, ask, timestamp}
    │   │                                Sync HTTP via requests + asyncio.to_thread()
    │   ├── paper/
    │   │   └── paper_broker.py          PaperBrokerAdapter — simulates fills
    │   │                                Realistic slippage model, no broker account needed
    │   ├── orders/
    │   │   └── manager.py               In-memory position ledger
    │   │                                on_fill() → Position creation + stop registration
    │   ├── orchestrator/
    │   │   └── engine.py                TradingOrchestrator — main run loop
    │   │                                _bar_loop: polls for new M15 bars every 60s
    │   │                                _price_loop: fetches live price every 5 min
    │   │                                _daily_reset_loop: resets daily P&L at 00:05 UTC
    │   ├── infrastructure/
    │   │   └── services.py              InProcessEventBus, TelegramAlertService
    │   │                                MetricsCollector (Prometheus)
    │   └── dashboard/
    │       ├── api.py                   FastAPI: REST endpoints + WebSocket /ws/live
    │       │                            /api/bars  /api/price  /api/equity  /api/trades
    │       │                            /api/position  /api/stats  /api/indicators
    │       ├── state.py                 SystemState singleton — engine writes, UI reads
    │       └── static/
    │           └── index.html           Vanilla JS dashboard (Lightweight Charts v5)
    │                                    Real-time via WebSocket, no build step needed
    ├── data/                            Persisted data (gitignored)
    │   └── trading.db                  SQLite — survives container rebuilds
    ├── logs/                            Runtime logs (gitignored)
    │   ├── equity_curve.jsonl           equity history
    │   └── main_stdout.txt             application log
    ├── tests/
    │   └── test_all.py                  Unit + integration test suite
    ├── pyproject.toml                   Dependencies + build config
    ├── docker-compose.yml               Single aurum service, port 8080 localhost-only
    ├── Dockerfile                       Multi-stage: base → builder → test → production
    ├── CLAUDE.md                        Instructions for AI-assisted development
    └── .env.example                     Environment variable template
```

---

## 11. Known Limitations & Honest Caveats

### What could make this strategy fail

**Regime change.** The strategy is a trend follower. In a prolonged choppy,
directionless gold market (ADX consistently below 20), it will produce many
small losses and few large wins. The regime filter will keep the system mostly
out, but false signals will still occur.

**Gap risk.** The 2×ATR stop assumes orderly price action. A gap can push price
through the stop level before execution. The actual loss could be larger than 1%
planned. Rule 10b (gap-caution) reduces size after a gap but cannot eliminate
this risk. On M15 bars, gap risk is primarily from weekend opens and major news.

**Price update frequency.** The live price updates every 5 minutes due to the
Twelve Data free-tier rate limit (800 calls/day). The intrabar stop circuit breaker
runs on the same 5-minute cadence, meaning stops are checked every 5 minutes rather
than continuously. This is acceptable for paper trading.

**Intraday profile not yet validated.** The M15 (intraday) profile runs the same
proven rule structure on a faster timeframe, but has not yet cleared the full
validation suite (walk-forward, Monte Carlo, cost stress). It is a forward-test
in progress. The H1 (swing) profile is the backtested, validated configuration.

**Real yield reassertion.** The post-2022 gold-yield decoupling could re-couple.
The 200-SMA filter would catch this eventually, but not before some drawdown.

**Overfitting risk we didn't eliminate.** Even with round-number parameters, the
*choice* of conventional parameters is influenced by decades of industry research.
This is meta-overfitting.

### What this system is NOT

- It is not a market-prediction system. It has no view on where gold is going.
- It is not an arbitrage system. It has no structural edge that can't be competed away.
- It is not a high-frequency system. It evaluates at most once per 15-minute bar.
- It is not guaranteed to be profitable. No system is.

### The honest pre-deployment checklist

Before putting any real money on this system, all of the following must be true:

- [ ] ≥ 1 full quarter of paper trading results available
- [ ] Paper results are within the confidence interval of backtest results
- [ ] You understand every rule in Section 3 and can explain it without this document
- [ ] The drawdown circuit breaker (Rule 10a) is set to an amount you are genuinely
      comfortable losing, and you have a written plan for when it triggers
- [ ] The intraday (M15) profile has been independently walk-forward validated

---

*This documentation reflects the complete intellectual work behind the system.
It is not investment advice. Past backtest performance does not guarantee
future live performance. Trading involves risk of loss.*
