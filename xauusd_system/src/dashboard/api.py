from __future__ import annotations
import asyncio, json, logging, time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from .state import SystemState
from core.config import ACTIVE_PROFILE

_log = logging.getLogger(__name__)

_state: Optional[SystemState] = None
_data_feed = None


def _calc_proximity(s: SystemState) -> dict:
    """
    Compute dollar distance from current price to the Donchian-20 breakout level.

    distance > 0  →  price has NOT yet reached the trigger (waiting)
    distance <= 0 →  trigger already breached (signal fired or no regime)

    pct_to: where current close sits on the Donchian-20 range [0–100].
    100 = at the breakout level, 0 = at the opposite extreme.
    """
    ind = s.last_indicators
    if not ind or 'close' not in ind:
        return {}

    close = float(ind['close'])
    d20h  = float(ind.get('donchian20_h', 0))
    d20l  = float(ind.get('donchian20_l', 0))
    rng   = max(0.01, d20h - d20l)

    if s.regime == 'BULL':
        dist = round(d20h - close, 2)           # + = still waiting, - = triggered
        pct  = round(min(100.0, max(0.0, (close - d20l) / rng * 100)), 1)
        return {
            'level':     round(d20h, 2),
            'distance':  dist,
            'pct_to':    pct,
            'direction': 'long_breakout',
            'triggered': close > d20h,
        }
    elif s.regime == 'BEAR':
        dist = round(close - d20l, 2)           # + = still waiting, - = triggered
        pct  = round(min(100.0, max(0.0, (d20h - close) / rng * 100)), 1)
        return {
            'level':     round(d20l, 2),
            'distance':  dist,
            'pct_to':    pct,
            'direction': 'short_breakout',
            'triggered': close < d20l,
        }
    return {}


def _trade_dict(t) -> dict:
    """Convert TradeRecord to dict, adding unix `time` field from opened_at."""
    d = t.__dict__.copy()
    try:
        dt = datetime.fromisoformat(t.opened_at.replace('Z', '+00:00'))
        d['time'] = int(dt.timestamp())
    except Exception:
        d['time'] = None
    return d


def build_app(state: SystemState, data_feed=None) -> FastAPI:
    global _state, _data_feed
    _state = state
    _data_feed = data_feed

    app = FastAPI(title="XAUUSD", docs_url=None, redoc_url=None)
    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=str(static)), name="static")

    @app.get("/health")
    async def health(): return {"status": "ok", "uptime_s": round(time.time()-_state.started_at)}

    @app.get("/", response_class=HTMLResponse)
    async def root(): return FileResponse(str(static/"index.html"))

    @app.get("/api/equity")
    async def equity(): return {"curve": _state.equity_curve[-2000:]}

    @app.get("/api/trades")
    async def trades():
        return {"trades": [_trade_dict(t) for t in _state.trades[-200:]]}

    @app.get("/api/stats")
    async def stats(): return _state.get_stats()

    @app.get("/api/position")
    async def position():
        p = _state.position; return p.__dict__ if p else None

    @app.get("/api/regime")
    async def regime(): return {"regime": _state.regime, "adx": round(_state.adx, 2)}

    @app.get("/api/price")
    async def price():
        ts     = _state.last_tick_ts.isoformat()    if _state.last_tick_ts    else None
        sig_ts = _state.last_signal_ts.isoformat()  if _state.last_signal_ts  else None
        return {
            "price":           _state.current_price,
            "ts":              ts,
            "spread":          _state.last_spread,
            "regime":          _state.regime,
            "adx":             round(_state.adx, 2),
            "indicators":      _state.last_indicators,
            "proximity":       _calc_proximity(_state),
            "last_bar_eval_ts": _state.last_bar_eval_ts,
            "last_signal": {
                "type":   _state.last_signal_type,
                "reason": _state.last_signal_reason,
                "ts":     sig_ts,
            },
        }

    @app.get("/api/candles")
    async def candles(granularity: str = "H1", count: int = 250):
        if _data_feed is None:
            return {"candles": []}
        try:
            bars = await _data_feed.get_bars(granularity=granularity, count=count)
        except Exception:
            return {"candles": []}
        result = []
        for b in bars:
            ts = b.timestamp
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            result.append({
                "time":  int(ts.timestamp()),
                "open":  round(float(b.open), 2),
                "high":  round(float(b.high), 2),
                "low":   round(float(b.low),  2),
                "close": round(float(b.close), 2),
            })
        return {"candles": result}

    @app.get("/api/bars")
    async def bars_endpoint(symbol: str = "XAU_USD", granularity: str = "H1", count: int = 250):
        """Return the last `count` OHLCV bars for the candlestick chart."""
        if _data_feed is None:
            return {"bars": []}
        try:
            raw = await _data_feed.get_bars(granularity=granularity, count=count)
        except Exception:
            _log.exception("api_bars_failed symbol=%s granularity=%s count=%d", symbol, granularity, count)
            return {"bars": []}
        result = []
        for b in raw:
            ts = b.timestamp
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            result.append({
                "time":  int(ts.timestamp()),
                "open":  round(float(b.open),  2),
                "high":  round(float(b.high),  2),
                "low":   round(float(b.low),   2),
                "close": round(float(b.close), 2),
            })
        return {"bars": result}

    @app.get("/api/indicators")
    async def indicator_series():
        if _data_feed is None:
            return {"sma200": [], "donchian_high": [], "donchian_low": []}
        try:
            bars = await _data_feed.get_bars(granularity="H1", count=250)
        except Exception:
            return {"sma200": [], "donchian_high": [], "donchian_low": []}

        sma200        = []
        donchian_high = []
        donchian_low  = []

        for i, bar in enumerate(bars):
            ts = bar.timestamp
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            t = int(ts.timestamp())

            if i >= 199:
                closes = [float(bars[j].close) for j in range(i - 199, i + 1)]
                sma200.append({"time": t, "value": round(sum(closes) / 200.0, 2)})

            if i >= 19:
                highs = [float(bars[j].high) for j in range(i - 19, i + 1)]
                lows  = [float(bars[j].low)  for j in range(i - 19, i + 1)]
                donchian_high.append({"time": t, "value": round(max(highs), 2)})
                donchian_low.append( {"time": t, "value": round(min(lows),  2)})

        return {"sma200": sma200, "donchian_high": donchian_high, "donchian_low": donchian_low}

    @app.websocket("/ws/live")
    async def ws(ws: WebSocket):
        await ws.accept()
        try:
            while True:
                await asyncio.sleep(2)
                ts = _state.last_tick_ts.isoformat() if _state.last_tick_ts else None
                sig_ts = _state.last_signal_ts.isoformat() if _state.last_signal_ts else None
                await ws.send_text(json.dumps({
                    "type":             "tick",
                    "profile":          ACTIVE_PROFILE.name,
                    "equity":           _state.equity_curve[-1] if _state.equity_curve else None,
                    "current_price":    _state.current_price,
                    "last_tick_ts":     ts,
                    "position":         _state.position.__dict__ if _state.position else None,
                    "regime":           _state.regime,
                    "adx":              round(_state.adx, 2),
                    "stats":            _state.get_stats(),
                    "indicators":       _state.last_indicators,
                    "proximity":        _calc_proximity(_state),
                    "last_bar_eval_ts": _state.last_bar_eval_ts,
                    "candle":           _state.forming_candle if _state.forming_candle else None,
                    "last_signal": {
                        "type":   _state.last_signal_type,
                        "reason": _state.last_signal_reason,
                        "ts":     sig_ts,
                    },
                }))
        except (WebSocketDisconnect, Exception): pass
    return app
