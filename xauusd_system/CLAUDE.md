# Aurum — Claude Code Instructions
# Task: Replace OANDA data feed with Twelve Data

## Context

Aurum is a XAUUSD paper-trading bot running a Donchian-20 breakout strategy
on M15 bars with a 200-period SMA + ADX regime filter.

The system was built against OANDA's API for both data and paper order execution.
OANDA is blocked for EU users. We are replacing the OANDA dependency with:

- **Market data**: Twelve Data API (twelvedata.com) — free tier, no EU restrictions
- **Paper execution**: internal PaperBroker (already built) — no external broker needed

The rest of the system (risk engine, signal generator, orchestrator, dashboard) is
unchanged. You are only touching the data layer and the .env config.

---

## Project structure (for reference)

```
xauusd_system/
├── src/
│   ├── main.py                        ← startup + dependency injection
│   ├── core/interfaces.py             ← Bar, Signal, Order types (do not touch)
│   ├── strategy/signal_generator.py   ← Donchian + regime logic (do not touch)
│   ├── risk/engine.py                 ← RiskEngine (do not touch)
│   ├── execution/engine.py            ← ExecutionEngine (do not touch)
│   ├── paper/paper_broker.py          ← PaperBroker simulated fills (do not touch)
│   ├── orders/manager.py              ← position ledger (do not touch)
│   ├── orchestrator/engine.py         ← main run loop (minor edit — see below)
│   ├── data/
│   │   └── oanda_feed.py              ← DELETE THIS FILE after creating replacement
│   ├── dashboard/
│   │   ├── api.py                     ← FastAPI endpoints (do not touch)
│   │   └── state.py                   ← SystemState singleton (do not touch)
│   └── infrastructure/services.py
├── data/                              ← trading.db lives here (never delete)
├── logs/
├── .env.example
├── .env                               ← you will update this (see Step 3)
├── Dockerfile
└── docker-compose.yml
```

---

## Step 1 — Create src/data/twelvedata_feed.py

Create this file. It must expose the same interface as `oanda_feed.py` so the
orchestrator requires zero changes.

```python
# src/data/twelvedata_feed.py
"""
Twelve Data feed — replaces oanda_feed.py for Aurum.

Provides:
  TwelveDataFeed.fetch_bars(symbol, interval, count) -> list[Bar]
  TwelveDataFeed.fetch_price(symbol)                 -> float

Bar is imported from src.core.interfaces — it has fields:
  timestamp: datetime
  open: Decimal
  high: Decimal
  low: Decimal
  close: Decimal
  volume: int  (Twelve Data returns volume for forex; use 0 if absent)

Twelve Data free tier: 800 API calls/day.
Our usage:
  - Startup backfill: 1 call (200 bars)
  - Bar loop every 15 min: 1 call — 96 calls/day
  - Price tick every 30 sec: 1 call — 2880 calls/day  ← too many, use 60s interval
  Total at 60s price ticks: ~3000 — upgrade to $29/mo Grow plan OR
  cache the price tick and only call every 5 minutes:
    price tick every 5 min: 288 calls/day
  SAFE: backfill(1) + bars(96) + price(288) = 385 calls/day  < 800 free limit

  So: fetch live price every 5 minutes, not every 5 seconds.
  The dashboard will show "last updated X seconds ago" — that's fine for paper trading.
"""

import os
import time
import logging
from decimal import Decimal
from datetime import datetime, timezone
from typing import Optional

import requests

from src.core.interfaces import Bar

logger = logging.getLogger(__name__)

TWELVE_DATA_BASE = "https://api.twelvedata.com"


class TwelveDataFeed:
    """
    Market data feed using Twelve Data API.
    Symbol for gold: XAU/USD
    """

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.environ.get("TWELVE_DATA_API_KEY", "")
        if not self.api_key:
            raise ValueError(
                "TWELVE_DATA_API_KEY not set. "
                "Sign up free at https://twelvedata.com and add it to .env"
            )
        self._session = requests.Session()
        self._session.headers.update({"Authorization": f"apikey {self.api_key}"})

    # ------------------------------------------------------------------
    # Public interface (matches what orchestrator expects)
    # ------------------------------------------------------------------

    def fetch_bars(
        self,
        symbol: str = "XAU/USD",
        interval: str = "15min",
        count: int = 250,
    ) -> list[Bar]:
        """
        Fetch the last `count` completed OHLCV bars.

        interval options: 1min, 5min, 15min, 30min, 1h, 4h, 1day
        Returns bars in chronological order (oldest first).
        """
        params = {
            "symbol": symbol,
            "interval": interval,
            "outputsize": count,
            "order": "ASC",
        }
        data = self._get("/time_series", params)

        if data.get("status") == "error":
            raise RuntimeError(
                f"Twelve Data API error: {data.get('message', 'unknown')}"
            )

        raw_bars = data.get("values", [])
        if not raw_bars:
            logger.warning("Twelve Data returned 0 bars for %s %s", symbol, interval)
            return []

        bars = []
        for rb in raw_bars:
            try:
                bars.append(
                    Bar(
                        timestamp=datetime.fromisoformat(rb["datetime"]).replace(
                            tzinfo=timezone.utc
                        ),
                        open=Decimal(rb["open"]),
                        high=Decimal(rb["high"]),
                        low=Decimal(rb["low"]),
                        close=Decimal(rb["close"]),
                        volume=int(float(rb.get("volume", 0) or 0)),
                    )
                )
            except (KeyError, ValueError) as e:
                logger.warning("Skipping malformed bar: %s — %s", rb, e)

        return bars

    def fetch_price(self, symbol: str = "XAU/USD") -> float:
        """
        Fetch the latest real-time price for the symbol.
        Used by the dashboard fast-tick loop.
        """
        data = self._get("/price", {"symbol": symbol})
        if "price" not in data:
            raise RuntimeError(
                f"Twelve Data price endpoint returned no price: {data}"
            )
        return float(data["price"])

    def fetch_quote(self, symbol: str = "XAU/USD") -> dict:
        """
        Fetch a full quote including bid/ask/change.
        Optional — used for richer dashboard display if needed.
        """
        return self._get("/quote", {"symbol": symbol})

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get(self, endpoint: str, params: dict) -> dict:
        url = f"{TWELVE_DATA_BASE}{endpoint}"
        try:
            resp = self._session.get(url, params=params, timeout=10)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.Timeout:
            logger.error("Twelve Data request timed out: %s", endpoint)
            raise
        except requests.exceptions.RequestException as e:
            logger.error("Twelve Data request failed: %s — %s", endpoint, e)
            raise
```

---

## Step 2 — Update src/orchestrator/engine.py

Find every reference to `OandaDataFeed` or `oanda_feed` and replace with
`TwelveDataFeed` / `twelvedata_feed`.

Specifically:

**Find (import at top of file):**
```python
from src.data.oanda_feed import OandaDataFeed
```
**Replace with:**
```python
from src.data.twelvedata_feed import TwelveDataFeed
```

**Find (instantiation inside __init__ or run()):**
```python
self.feed = OandaDataFeed(
    api_key=os.environ.get("OANDA_API_KEY"),
    account_id=os.environ.get("OANDA_ACCOUNT_ID"),
    environment=os.environ.get("OANDA_ENV", "practice"),
)
```
**Replace with:**
```python
self.feed = TwelveDataFeed(
    api_key=os.environ.get("TWELVE_DATA_API_KEY"),
)
```

**Also find:**
```python
self.feed = OandaDataFeed(...)
```
anywhere else in the file and replace.

**The bar fetch call itself does NOT change** — it was already:
```python
bars = self.feed.fetch_bars(symbol="XAU/USD", interval="15min", count=250)
```
and
```python
price = self.feed.fetch_price("XAU/USD")
```
Both match the TwelveDataFeed interface exactly.

---

## Step 3 — Update src/main.py

Same as orchestrator — find any direct construction of `OandaDataFeed` in
`main.py` and replace with `TwelveDataFeed`. If the feed is constructed only in
`orchestrator/engine.py`, this step may not be needed — check and skip if so.

Also remove any `OANDA_*` env var reads from `main.py`. They are no longer needed.

---

## Step 4 — Update .env.example

Replace the entire `.env.example` contents with exactly this:

```
# ── Aurum environment config ──────────────────────────────────────────

# Twelve Data API key (free at https://twelvedata.com — no card required)
TWELVE_DATA_API_KEY=your_key_here

# Paper trading mode (always true until you go live)
PAPER_MODE=true

# Starting paper equity in USD
PAPER_EQUITY=100000

# Strategy settings
STRATEGY_PROFILE=intraday

# Dashboard
DASHBOARD_PORT=8080

# Logging
LOG_LEVEL=INFO

# Database path (do not change — must match docker-compose volume)
DB_PATH=/app/data/trading.db
```

---

## Step 5 — Update .env (the live file on disk)

**Do not commit this file.** Edit it manually.

Copy from .env.example and fill in:
```
TWELVE_DATA_API_KEY=<paste your key from twelvedata.com>
PAPER_MODE=true
PAPER_EQUITY=100000
STRATEGY_PROFILE=intraday
DASHBOARD_PORT=8080
LOG_LEVEL=INFO
DB_PATH=/app/data/trading.db
```

Leave `OANDA_API_KEY` and `OANDA_ACCOUNT_ID` out entirely.

---

## Step 6 — Delete the old feed file

```bash
rm src/data/oanda_feed.py
```

This removes the dead import. If anything else in the project still imports
from `oanda_feed`, Python will error immediately on startup — that tells you
where the remaining references are. Fix them by replacing with `twelvedata_feed`.

---

## Step 7 — Update pyproject.toml dependencies

Find the `[project.dependencies]` section.

**Remove** (if present):
```
"oandapyV20",
"oanda-api-v20",
```

**Add** (if not already present):
```
"requests>=2.31",
```

`requests` is almost certainly already there — just confirm.

---

## Step 8 — Smoke test locally

```bash
cd xauusd_system
pip install -e ".[dev]"

# Quick API test — should print a price
python -c "
from src.data.twelvedata_feed import TwelveDataFeed
import os
os.environ['TWELVE_DATA_API_KEY'] = 'YOUR_KEY_HERE'
feed = TwelveDataFeed()
print('Price:', feed.fetch_price())
bars = feed.fetch_bars(count=5)
print('Bars:', len(bars), 'latest close:', bars[-1].close)
"

# Full system test
PAPER_MODE=true python -m src.main
# Should start without errors. Dashboard at http://localhost:8080
# Chart should load candles. Price should show a live gold price.
```

---

## Step 9 — Verify dashboard endpoints

```bash
# After starting the system:
curl http://localhost:8080/api/bars?count=5
# Expected: {"bars": [...]} with 5 OHLCV objects, NOT {"bars": []}

curl http://localhost:8080/api/price
# Expected: {"price": 2300.xx} or whatever XAU/USD is now

curl http://localhost:8080/health
# Expected: {"status": "ok"}
```

If `/api/bars` returns `{"bars": []}`, the feed is not being called or is failing silently.
Check logs: `docker compose logs -f aurum` or `python -m src.main` output.

---

## Step 10 — Commit to git

```bash
git add src/data/twelvedata_feed.py
git add src/orchestrator/engine.py
git add src/main.py
git add .env.example
git add pyproject.toml
git rm src/data/oanda_feed.py
git commit -m "feat: replace OANDA feed with Twelve Data API"
git push origin main
```

Do NOT add `.env` to git. Confirm it is in `.gitignore`.

---

## What you must NOT change

- `src/core/interfaces.py` — the `Bar`, `Signal`, `Order` types are the contract
- `src/risk/` — all files
- `src/execution/` — all files
- `src/paper/paper_broker.py` — the paper broker handles fills
- `src/dashboard/` — api.py and state.py
- `src/strategy/signal_generator.py`
- `docker-compose.yml` — volumes, ports, service name
- `Dockerfile`
- `data/trading.db` — never delete this

---

## Rate limit awareness

Twelve Data free tier = 800 API calls/day = ~33/hour.

The system makes:
- 1 call at startup (bar backfill)
- 1 call per 15-min bar loop = 96/day
- 1 call per 5-min price tick = 288/day
- Total: ~385/day — safely under 800

If you see `429 Too Many Requests` from Twelve Data, add this to the price
fetch loop in `orchestrator/engine.py`:

```python
# Price tick loop — every 5 minutes, not 5 seconds
PRICE_TICK_INTERVAL = 300  # seconds
```

The dashboard will show price updating every 5 minutes. For paper trading this
is perfectly fine.

---

## Done

After these steps, Aurum runs with:
- Live XAU/USD M15 data from Twelve Data (real gold prices)
- Paper fills from internal PaperBroker (no broker account needed)
- Dashboard at https://gold.samvgarcia.com showing live price + trades
- No OANDA dependency anywhere in the codebase
