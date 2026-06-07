"""
paper/paper_broker.py — simulated broker, drop-in for live broker adapters.
No network calls. Fill at last price +/- slippage.
1 standard lot = 100 oz for XAU/USD P&L calculation.
"""
from __future__ import annotations
import time
import uuid
from decimal import Decimal
from datetime import datetime, timezone
from typing import Optional


class _Pos:
    def __init__(self, symbol: str, side: str, size: float, entry: float) -> None:
        self.symbol       = symbol
        self.side         = side
        self.size         = size
        self.entry_price  = entry
        self.opened_at    = time.time()


class PaperBrokerAdapter:
    PIP = 0.01

    def __init__(
        self,
        initial_equity: Decimal,
        slippage_pips: float = 0.3,
        spread_pips: float = 0.5,
        initial_price: Optional[float] = None,
    ) -> None:
        self._equity  = float(initial_equity)
        self._slip    = slippage_pips * self.PIP
        self._spread  = spread_pips * self.PIP
        self._pos: Optional[_Pos] = None
        # Price initialised to None; will be set by the orchestrator's prewarm
        # call via on_bar() before any orders are placed.  Using None instead of
        # a hardcoded value prevents fills at a fictitious price.
        self._price: Optional[float] = initial_price
        self._fills: list[dict] = []

    # ── IBrokerAdapter lifecycle ─────────────────────────────────────────

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    # ── Order operations ─────────────────────────────────────────────────

    async def place_order(self, order):
        if self._price is None:
            raise RuntimeError(
                "PaperBroker.place_order called before a live price was set. "
                "The orchestrator must call on_bar() during prewarm first."
            )

        side  = getattr(order, "side", None)
        side_s = side.value if hasattr(side, "value") else str(side)
        qty   = float(getattr(order, "quantity", getattr(order, "units", getattr(order, "size", 0.01))))
        sym   = getattr(order, "instrument", getattr(order, "symbol", "XAU_USD"))
        buy   = "LONG" in side_s.upper() or "BUY" in side_s.upper()
        px    = self._price + (self._slip if buy else -self._slip)

        if self._pos:
            opp = (buy and self._pos.side == "SHORT") or (not buy and self._pos.side == "LONG")
            if opp:
                await self._close(self._price)

        if not self._pos:
            self._pos = _Pos(sym, "LONG" if buy else "SHORT", qty, px)

        broker_ref = f"PAPER-{uuid.uuid4().hex[:8].upper()}"
        fill = {"fill_id": broker_ref, "price": px, "size": qty, "side": side_s}
        self._fills.append(fill)

        order.broker_ref   = broker_ref
        order.filled_price = Decimal(str(round(px, 2)))
        order.filled_at    = datetime.now(timezone.utc)
        try:
            from core.interfaces import OrderStatus
            order.status = OrderStatus.FILLED
        except Exception:
            pass
        return order

    async def cancel_order(self, oid: str) -> bool:
        return True

    # ── Account / position queries ────────────────────────────────────────

    async def get_account_summary(self) -> dict:
        return {"balance": str(round(self._equity, 2)), "currency": "USD"}

    async def get_account(self) -> dict:
        return {"balance": str(round(self._equity, 2)), "currency": "USD"}

    async def get_open_positions(self) -> list:
        if not self._pos:
            return []
        return [{
            "instrument": self._pos.symbol,
            "side":       self._pos.side,
            "units":      str(self._pos.size),
            "avg_price":  str(self._pos.entry_price),
        }]

    async def get_positions(self) -> list:
        """Return simulated positions so the reconciler has something to compare."""
        return await self.get_open_positions()

    async def get_open_orders(self) -> list:
        return []

    async def stream_executions(self):
        return
        yield   # make it an async generator

    async def healthcheck(self) -> bool:
        return True

    async def is_connected(self) -> bool:
        return True

    async def heartbeat(self) -> bool:
        return True

    async def close_position(self, sym: str) -> dict:
        return await self._close(self._price or 0.0)

    async def get_current_price(self, sym: str) -> dict:
        px = self._price or 0.0
        return {"bid": px - self._spread / 2, "ask": px + self._spread / 2, "mid": px}

    # ── Price sync ────────────────────────────────────────────────────────

    def on_bar(self, price: float) -> None:
        """Called by orchestrator after each bar fetch to keep fills realistic."""
        self._price = price

    # ── Equity / snapshot helpers ─────────────────────────────────────────

    def get_equity(self) -> float:
        if not self._pos or self._price is None:
            return self._equity
        oz   = self._pos.size * 100
        upnl = (
            (self._price - self._pos.entry_price) * oz
            if self._pos.side == "LONG"
            else (self._pos.entry_price - self._price) * oz
        )
        return self._equity + upnl

    def get_position_snapshot(self) -> Optional[dict]:
        if not self._pos or self._price is None:
            return None
        oz   = self._pos.size * 100
        upnl = (
            (self._price - self._pos.entry_price) * oz
            if self._pos.side == "LONG"
            else (self._pos.entry_price - self._price) * oz
        )
        return {
            "symbol":          self._pos.symbol,
            "side":            self._pos.side,
            "size":            self._pos.size,
            "entry_px":        round(self._pos.entry_price, 2),
            "current_px":      round(self._price, 2),
            "unrealized_pnl":  round(upnl, 2),
        }

    # ── Internal ─────────────────────────────────────────────────────────

    async def _close(self, price: float) -> dict:
        if not self._pos:
            return {}
        oz  = self._pos.size * 100
        pnl = (
            (price - self._pos.entry_price) * oz
            if self._pos.side == "LONG"
            else (self._pos.entry_price - price) * oz
        )
        self._equity += pnl
        self._pos = None
        return {"pnl": round(pnl, 2), "close_price": price}
