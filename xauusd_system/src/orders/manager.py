"""
orders/manager.py
IOrderManager: in-memory position ledger with fill processing.
"""
from __future__ import annotations
from core.interfaces import (
    IOrderManager, IBrokerAdapter, IRiskEngine, IEventBus,
    Order, Position, Side, OrderStatus
)
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
import logging

logger = logging.getLogger(__name__)

class DefaultOrderManager(IOrderManager):
    def __init__(self, broker: IBrokerAdapter, risk_engine=None, event_bus=None,
                 risk: IRiskEngine = None, bus: IEventBus = None) -> None:
        self._broker    = broker
        self._risk      = risk_engine or risk
        self._bus       = event_bus or bus
        self._positions: dict[str, Position] = {}
        self._orders:    list[Order]          = []

    async def submit(self, order: Order) -> Order:
        submitted = await self._broker.place_order(order)
        self._orders.append(submitted)
        logger.info("order_submitted", extra={"order_id": submitted.order_id,
                                               "broker_ref": submitted.broker_ref})
        # Paper mode fills synchronously — keep the local position ledger in sync
        # so open_positions() reflects reality on the very next call.
        if submitted.status == OrderStatus.FILLED:
            await self.on_fill(submitted)
        return submitted

    async def cancel(self, order_id: str) -> bool:
        order = next((o for o in self._orders if o.order_id == order_id), None)
        if order:
            return await self._broker.cancel_order(order.broker_ref)
        return False

    def open_positions(self) -> list[Position]:
        return list(self._positions.values())

    def open_orders(self) -> list[Order]:
        return [o for o in self._orders if o.status in
                (OrderStatus.PENDING, OrderStatus.SUBMITTED)]

    async def on_fill(self, order: Order) -> None:
        order.status = OrderStatus.FILLED
        order.filled_at = datetime.now(timezone.utc)
        meta = order.metadata or {}
        if meta.get("type") == "entry" and order.filled_price:
            stop_price = Decimal(str(meta.get("stop_price", 0)))
            pos = Position(
                symbol       = order.symbol,
                side         = order.side,
                quantity     = order.quantity,
                entry_price  = order.filled_price,
                entry_time   = order.filled_at,
                hard_stop    = stop_price,
                trailing_low = stop_price,
                atr_at_entry = Decimal("0"),
            )
            self._positions[order.symbol] = pos
            logger.info("position_opened", extra={"symbol": order.symbol,
                                                   "side": order.side.value})
            self._bus.publish("orders.position_opened", {"symbol": order.symbol})
        elif meta.get("type") in ("exit", "hard_stop", "emergency_stop"):
            if order.symbol in self._positions:
                del self._positions[order.symbol]
                close_type = meta.get("type", "exit")
                logger.info("position_closed", extra={"symbol": order.symbol,
                                                       "close_type": close_type})
                self._bus.publish("orders.position_closed", {
                    "symbol":     order.symbol,
                    "close_type": close_type,
                })
