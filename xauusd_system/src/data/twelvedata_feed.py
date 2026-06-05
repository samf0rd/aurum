"""
data/twelvedata_feed.py
────────────────────────
Replaces oanda_feed.py.  Market data from Twelve Data API; paper fills
handled by the internal PaperBroker — no OANDA dependency anywhere.

Rate-limit budget (free tier = 800 calls/day):
  Startup backfill : 1 call
  Bar loop (15 min): 96 calls/day
  Price tick (5 min): 288 calls/day
  Total             : ~385 calls/day  ← safely under 800

Set PRICE_TICK_INTERVAL=300 (default) to stay within the free quota.
Twelve Data docs: https://twelvedata.com/docs
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import requests

from core.interfaces import Bar

logger = logging.getLogger(__name__)

_BASE = "https://api.twelvedata.com"

# Map ACTIVE_PROFILE.timeframe strings → Twelve Data interval strings
_INTERVAL = {
    "M1":  "1min",
    "M5":  "5min",
    "M15": "15min",
    "M30": "30min",
    "H1":  "1h",
    "H4":  "4h",
    "D":   "1day",
}

# XAU/USD typical mid-market spread in USD (used to synthesise bid/ask)
_SPREAD = 0.30


class TwelveDataFeed:
    """
    Async market-data feed backed by Twelve Data REST API.

    Exposes the same interface as the old OandaDataFeed so the orchestrator
    and dashboard need zero changes:
      get_bars(granularity, count, include_incomplete=False) -> list[Bar]
      get_latest_tick()                                      -> dict
    """

    def __init__(self, api_key: Optional[str] = None) -> None:
        self.api_key = api_key or os.environ.get("TWELVE_DATA_API_KEY", "")
        if not self.api_key:
            raise ValueError(
                "TWELVE_DATA_API_KEY is not set. "
                "Sign up free at https://twelvedata.com and add it to .env"
            )
        self._session = requests.Session()
        self._session.headers.update({"Authorization": f"apikey {self.api_key}"})
        self._last_price: Optional[float] = None
        self._last_price_ts: float = 0.0

    # ── Public interface ─────────────────────────────────────────────────

    async def get_bars(
        self,
        granularity: str = "M15",
        count: int = 250,
        include_incomplete: bool = False,
    ) -> list[Bar]:
        """
        Return the last `count` completed OHLCV bars for XAU/USD.
        Runs the HTTP call in a thread so it doesn't block the event loop.
        """
        interval = _INTERVAL.get(granularity, granularity)
        # Request one extra bar so we can drop the incomplete (forming) bar
        fetch_count = count + 1 if not include_incomplete else count

        def _call() -> list[Bar]:
            data = self._get("/time_series", {
                "symbol":     "XAU/USD",
                "interval":   interval,
                "outputsize": fetch_count,
                "order":      "ASC",
            })
            if data.get("status") == "error":
                raise RuntimeError(f"Twelve Data error: {data.get('message', data)}")

            raw = data.get("values", [])
            if not raw:
                logger.warning("Twelve Data returned 0 bars (%s %s)", "XAU/USD", interval)
                return []

            bars: list[Bar] = []
            for rb in raw:
                try:
                    bars.append(Bar(
                        timestamp=datetime.fromisoformat(rb["datetime"]).replace(
                            tzinfo=timezone.utc
                        ),
                        open=Decimal(rb["open"]),
                        high=Decimal(rb["high"]),
                        low=Decimal(rb["low"]),
                        close=Decimal(rb["close"]),
                        volume=Decimal(str(int(float(rb.get("volume", 0) or 0)))),
                        symbol="XAUUSD",
                    ))
                except (KeyError, ValueError) as exc:
                    logger.warning("Skipping malformed bar: %s — %s", rb, exc)

            # Drop the last (potentially forming) bar unless caller wants it
            if not include_incomplete and len(bars) > count:
                bars = bars[:count]

            return bars[-count:]

        return await asyncio.to_thread(_call)

    async def get_latest_tick(self) -> dict:
        """
        Return {"bid": float, "ask": float, "timestamp": datetime}.
        Caches the price in-process; actual HTTP call is cheap (1 tiny JSON).
        """
        def _call() -> float:
            data = self._get("/price", {"symbol": "XAU/USD"})
            if "price" not in data:
                raise RuntimeError(f"Twelve Data /price returned: {data}")
            return float(data["price"])

        price = await asyncio.to_thread(_call)
        self._last_price = price
        self._last_price_ts = time.monotonic()

        half_spread = _SPREAD / 2.0
        return {
            "bid":       round(price - half_spread, 2),
            "ask":       round(price + half_spread, 2),
            "timestamp": datetime.now(timezone.utc),
        }

    # ── Internal ─────────────────────────────────────────────────────────

    def _get(self, endpoint: str, params: dict) -> dict:
        url = f"{_BASE}{endpoint}"
        try:
            resp = self._session.get(url, params=params, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.Timeout:
            logger.error("Twelve Data timeout: %s", endpoint)
            raise
        except requests.exceptions.RequestException as exc:
            logger.error("Twelve Data request failed: %s — %s", endpoint, exc)
            raise
