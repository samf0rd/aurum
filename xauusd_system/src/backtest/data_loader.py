"""
backtest/data_loader.py — historical XAU/USD bar loader with on-disk caching.

Twelve Data free tier: 800 calls/day.  One /time_series call with
outputsize=5000 covers ~5 years of hourly data; caching avoids re-fetching.

Cache layout:
    data/cache/XAUUSD_{interval}_{start}_{end}.json

The loader is offline-replayable: if the cache file exists it is used
unconditionally, with no network call.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

import requests

from core.interfaces import Bar

logger = logging.getLogger(__name__)

_BASE = "https://api.twelvedata.com"

_INTERVAL_MAP = {
    "M1":  "1min",
    "M5":  "5min",
    "M15": "15min",
    "M30": "30min",
    "H1":  "1h",
    "H4":  "4h",
    "D":   "1day",
}

_CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "cache"


def _cache_path(interval: str, start: str, end: str) -> Path:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _CACHE_DIR / f"XAUUSD_{interval}_{start}_{end}.json"


def _raw_to_bar(rb: dict) -> Optional[Bar]:
    try:
        return Bar(
            timestamp = datetime.fromisoformat(rb["datetime"]).replace(tzinfo=timezone.utc),
            open      = Decimal(rb["open"]),
            high      = Decimal(rb["high"]),
            low       = Decimal(rb["low"]),
            close     = Decimal(rb["close"]),
            volume    = Decimal(str(int(float(rb.get("volume", 0) or 0)))),
            symbol    = "XAUUSD",
        )
    except (KeyError, ValueError) as exc:
        logger.warning("Skipping malformed bar: %s — %s", rb, exc)
        return None


def _find_covering_cache(interval: str, start: str, end: str) -> Optional[Path]:
    """Return the first cache file whose date range fully covers [start, end]."""
    prefix = f"XAUUSD_{interval}_"
    for p in _CACHE_DIR.glob(f"{prefix}*.json"):
        stem  = p.stem[len(prefix):]      # "2020-01-01_2025-12-31"
        parts = stem.split("_")
        if len(parts) == 2:
            cached_start, cached_end = parts
            if cached_start <= start and cached_end >= end:
                return p
    return None


def load_bars(
    start: str,
    end: str,
    timeframe: str = "H1",
    api_key: Optional[str] = None,
) -> list[Bar]:
    """
    Return historical bars for XAU/USD between start and end (YYYY-MM-DD).
    Uses on-disk cache; fetches from Twelve Data only on cache miss.

    Parameters
    ----------
    start, end  : ISO date strings, e.g. "2023-01-01"
    timeframe   : strategy timeframe key, e.g. "H1" or "M15"
    api_key     : Twelve Data key; falls back to TWELVE_DATA_API_KEY env var
    """
    interval = _INTERVAL_MAP.get(timeframe, timeframe)
    cache    = _cache_path(interval, start, end)

    if cache.exists():
        logger.info("Loading bars from cache: %s", cache)
        with cache.open() as fh:
            raw_list = json.load(fh)
    else:
        # Check if a broader cache covers the requested range; filter in memory.
        covering = _find_covering_cache(interval, start, end)
        if covering:
            logger.info("Loading bars from broader cache: %s", covering)
            with covering.open() as fh:
                raw_list = json.load(fh)
            raw_list = [rb for rb in raw_list if start <= rb["datetime"][:10] <= end]
        else:
            key = api_key or os.environ.get("TWELVE_DATA_API_KEY", "")
            if not key:
                raise ValueError(
                    "TWELVE_DATA_API_KEY not set and no cache found for "
                    f"{timeframe} {start}→{end}"
                )
            raw_list = _fetch(interval, start, end, key)
            with cache.open("w") as fh:
                json.dump(raw_list, fh)
            logger.info("Cached %d raw bars to %s", len(raw_list), cache)

    bars = [b for rb in raw_list if (b := _raw_to_bar(rb)) is not None]
    bars.sort(key=lambda b: b.timestamp)
    logger.info(
        "Loaded %d bars | %s | %s → %s",
        len(bars), timeframe,
        bars[0].timestamp.date() if bars else "?",
        bars[-1].timestamp.date() if bars else "?",
    )
    return bars


def _fetch(interval: str, start: str, end: str, api_key: str) -> list[dict]:
    """Pull bars from Twelve Data in pages of 5 000 if needed."""
    import time

    session    = requests.Session()
    all_raw: list[dict] = []
    page_start = start

    while True:
        params = {
            "symbol":     "XAU/USD",
            "interval":   interval,
            "start_date": page_start,
            "end_date":   end,
            "outputsize": 5000,
            "order":      "ASC",
            "format":     "JSON",
            "apikey":     api_key,   # query-param — required by Twelve Data
        }
        logger.info("Fetching bars from Twelve Data: %s %s → %s", interval, page_start, end)

        # Retry up to 4 times on 429 (per-minute rate limit)
        for attempt in range(4):
            resp = session.get(f"{_BASE}/time_series", params=params, timeout=30)
            if resp.status_code == 429:
                wait = 15 * (attempt + 1)
                logger.warning("429 rate-limit — sleeping %ds (attempt %d/4)", wait, attempt + 1)
                time.sleep(wait)
                continue
            break
        resp.raise_for_status()
        data = resp.json()

        if data.get("status") == "error":
            raise RuntimeError(f"Twelve Data error: {data.get('message', data)}")

        raw = data.get("values", [])
        if not raw:
            break

        all_raw.extend(raw)

        # If we got fewer than 5 000, we've reached the end
        if len(raw) < 5000:
            break

        # Advance page_start to the timestamp after the last bar received
        last_ts = raw[-1]["datetime"]
        page_start = last_ts  # Twelve Data will exclude it on the next call

        if page_start >= end:
            break

    return all_raw
