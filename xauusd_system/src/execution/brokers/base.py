"""
execution/brokers/base.py
──────────────────────────
Broker abstraction layer.

IBrokerAdapter defines the contract every broker must satisfy.
All methods are async — even the paper broker — so the execution engine
never has to branch on whether it's live or simulated.

Implementations in this file
─────────────────────────────
- IBrokerAdapter         abstract base (the contract)
- BrokerError hierarchy  structured exception tree
- PaperBrokerAdapter     in-memory simulated fills for testing/dry-run
- RetryingBrokerAdapter  transparent retry wrapper around any IBrokerAdapter
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
import uuid

from ..models import (
    BrokerOrderSnapshot,
    BrokerPositionSnapshot,
    ExecutionOrder,
    Fill,
    OrderSide,
    OrderStatus,
    OrderType,
    RetryConfig,
)

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Exception hierarchy
# ──────────────────────────────────────────────────────────────────────────────

class BrokerError(Exception):
    """Base class for all broker errors."""
    def __init__(self, message: str, broker_code: str = "", retryable: bool = False):
        super().__init__(message)
        self.broker_code = broker_code
        self.retryable   = retryable


class BrokerConnectionError(BrokerError):
    """TCP/TLS connection failed or dropped."""
    def __init__(self, message: str, broker_code: str = ""):
        super().__init__(message, broker_code, retryable=True)


class BrokerTemporaryError(BrokerError):
    """Transient server-side error — safe to retry."""
    def __init__(self, message: str, broker_code: str = ""):
        super().__init__(message, broker_code, retryable=True)


class BrokerRejectedError(BrokerError):
    """Order rejected permanently (insufficient margin, invalid params, etc.)."""
    def __init__(self, message: str, broker_code: str = ""):
        super().__init__(message, broker_code, retryable=False)


class RateLimitError(BrokerError):
    """Rate limit hit — retryable after a delay."""
    def __init__(self, message: str, retry_after_s: float = 1.0):
        super().__init__(message, "RATE_LIMIT", retryable=True)
        self.retry_after_s = retry_after_s


class OrderNotFoundError(BrokerError):
    """Broker has no record of this order_id / broker_ref."""
    def __init__(self, ref: str):
        super().__init__(f"Order not found at broker: {ref}", retryable=False)


# ──────────────────────────────────────────────────────────────────────────────
# Abstract interface
# ──────────────────────────────────────────────────────────────────────────────

class IBrokerAdapter(ABC):
    """
    Contract that every broker implementation must satisfy.

    All methods are async. Implementations must:
    - Never swallow exceptions silently — raise BrokerError subclasses
    - Return broker_ref populated on every submitted/existing order
    - Be thread-safe if used from multiple asyncio tasks
    """

    @abstractmethod
    async def connect(self) -> None:
        """Establish connection / authenticate. Idempotent."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Graceful disconnect. Idempotent."""

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """True if the adapter currently has a live connection."""

    @abstractmethod
    async def place_order(self, order: ExecutionOrder) -> ExecutionOrder:
        """
        Submit order to broker. Mutates order in-place:
          - Sets broker_ref
          - Sets status = SUBMITTED
          - Sets submitted_at
        Returns the same order object.
        Raises BrokerRejectedError if broker refuses immediately.
        """

    @abstractmethod
    async def cancel_order(self, broker_ref: str) -> bool:
        """Cancel an active order. Returns True if successfully cancelled."""

    @abstractmethod
    async def get_order_status(self, broker_ref: str) -> BrokerOrderSnapshot:
        """Fetch current status of a single order from the broker."""

    @abstractmethod
    async def get_open_orders(self) -> list[BrokerOrderSnapshot]:
        """Return all broker-side open orders (SUBMITTED + PARTIAL)."""

    @abstractmethod
    async def get_positions(self) -> list[BrokerPositionSnapshot]:
        """Return all broker-side open positions."""

    @abstractmethod
    async def get_account_equity(self) -> Decimal:
        """Return current account net liquidation value."""


# ──────────────────────────────────────────────────────────────────────────────
# Paper broker — instant simulated fills, in-memory, zero network
# ──────────────────────────────────────────────────────────────────────────────

class PaperBrokerAdapter(IBrokerAdapter):
    """
    In-memory paper trading broker.

    Fills market orders instantly at the supplied reference price.
    Limit/stop orders fill when you call simulate_price_move().

    Suitable for:
    - Unit and integration tests
    - Dry-run / paper-trading mode
    - Strategy development without live risk
    """

    def __init__(
        self,
        initial_equity:   Decimal = Decimal("100_000"),
        fill_price:       Decimal = Decimal("2000.00"),
        slippage_pct:     Decimal = Decimal("0.0001"),  # 1bp slippage
        commission_per_lot: Decimal = Decimal("7.00"),
        reject_next_n:    int = 0,   # for testing: reject next N orders
        fail_next_n:      int = 0,   # for testing: raise BrokerTemporaryError on next N calls
    ) -> None:
        self._equity          = initial_equity
        self._current_price   = fill_price
        self._slippage        = slippage_pct
        self._commission      = commission_per_lot
        self._connected       = False
        self._orders:    dict[str, ExecutionOrder]        = {}
        self._positions: dict[str, BrokerPositionSnapshot] = {}
        self._fill_counter    = 0
        self._reject_counter  = reject_next_n
        self._fail_counter    = fail_next_n

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        await asyncio.sleep(0)
        self._connected = True
        logger.info("paper_broker connected")

    async def disconnect(self) -> None:
        await asyncio.sleep(0)
        self._connected = False
        logger.info("paper_broker disconnected")

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ── Order operations ─────────────────────────────────────────────────────

    async def place_order(self, order: ExecutionOrder) -> ExecutionOrder:
        self._check_connection()
        self._maybe_fail()

        if self._reject_counter > 0:
            self._reject_counter -= 1
            order.status    = OrderStatus.REJECTED
            order.last_error = "Paper broker rejection (test mode)"
            raise BrokerRejectedError("Simulated rejection", "PAPER_REJECT")

        broker_ref = f"PAPER-{uuid.uuid4().hex[:8].upper()}"
        order.broker_ref   = broker_ref
        order.status       = OrderStatus.SUBMITTED
        order.submitted_at = datetime.now(timezone.utc)
        self._orders[broker_ref] = order

        logger.debug("paper_order_submitted | ref=%s qty=%s", broker_ref, order.quantity)

        # Market orders fill instantly
        if order.order_type == OrderType.MARKET:
            await self._fill_order(order)

        return order

    async def cancel_order(self, broker_ref: str) -> bool:
        self._check_connection()
        self._maybe_fail()
        order = self._orders.get(broker_ref)
        if order is None:
            raise OrderNotFoundError(broker_ref)
        if order.is_terminal:
            return False
        order.status       = OrderStatus.CANCELLED
        order.cancelled_at = datetime.now(timezone.utc)
        logger.debug("paper_order_cancelled | ref=%s", broker_ref)
        return True

    async def get_order_status(self, broker_ref: str) -> BrokerOrderSnapshot:
        self._check_connection()
        self._maybe_fail()
        order = self._orders.get(broker_ref)
        if order is None:
            raise OrderNotFoundError(broker_ref)
        return self._order_to_snapshot(order)

    async def get_open_orders(self) -> list[BrokerOrderSnapshot]:
        self._check_connection()
        return [
            self._order_to_snapshot(o)
            for o in self._orders.values()
            if o.is_active
        ]

    async def get_positions(self) -> list[BrokerPositionSnapshot]:
        self._check_connection()
        return list(self._positions.values())

    async def get_account_equity(self) -> Decimal:
        self._check_connection()
        return self._equity

    # ── Test helpers ─────────────────────────────────────────────────────────

    def simulate_price_move(self, new_price: Decimal) -> list[Fill]:
        """
        Move the current price and fill any resting limit/stop orders.
        Returns list of fills generated.
        """
        old_price = self._current_price
        self._current_price = new_price
        fills = []
        for order in list(self._orders.values()):
            if not order.is_active:
                continue
            if order.order_type == OrderType.LIMIT:
                if order.side == OrderSide.BUY and new_price <= (order.limit_price or Decimal("0")):
                    fills.extend(asyncio.get_event_loop().run_until_complete(self._fill_order(order)))
                elif order.side == OrderSide.SELL and new_price >= (order.limit_price or Decimal("999999")):
                    fills.extend(asyncio.get_event_loop().run_until_complete(self._fill_order(order)))
        return fills

    def set_equity(self, equity: Decimal) -> None:
        self._equity = equity

    # ── Internals ────────────────────────────────────────────────────────────

    async def _fill_order(self, order: ExecutionOrder) -> list[Fill]:
        slippage_mult = Decimal("1") + (
            self._slippage if order.side == OrderSide.BUY else -self._slippage
        )
        fill_price = self._current_price * slippage_mult
        commission = order.quantity * self._commission

        self._fill_counter += 1
        fill = Fill(
            fill_id    = f"FILL-{self._fill_counter:06d}",
            order_id   = order.order_id,
            broker_ref = order.broker_ref,
            symbol     = order.symbol,
            side       = order.side,
            quantity   = order.quantity,
            price      = fill_price,
            commission = commission,
            timestamp  = datetime.now(timezone.utc),
            is_partial = False,
        )
        order.apply_fill(fill)
        self._update_position(order, fill)
        self._equity -= commission
        logger.debug(
            "paper_fill | ref=%s qty=%s price=%s",
            order.broker_ref, fill.quantity, fill.price,
        )
        return [fill]

    def _update_position(self, order: ExecutionOrder, fill: Fill) -> None:
        symbol = order.symbol
        existing = self._positions.get(symbol)

        if existing is None:
            self._positions[symbol] = BrokerPositionSnapshot(
                symbol    = symbol,
                side      = fill.side,
                quantity  = fill.quantity,
                avg_price = fill.price,
            )
        else:
            if existing.side == fill.side:
                # Adding to position — compute new VWAP
                total_qty   = existing.quantity + fill.quantity
                new_avg     = (existing.avg_price * existing.quantity + fill.price * fill.quantity) / total_qty
                self._positions[symbol] = BrokerPositionSnapshot(
                    symbol=symbol, side=existing.side,
                    quantity=total_qty, avg_price=new_avg,
                )
            else:
                # Reducing / closing position
                new_qty = existing.quantity - fill.quantity
                if new_qty <= Decimal("0"):
                    del self._positions[symbol]
                else:
                    self._positions[symbol] = BrokerPositionSnapshot(
                        symbol=symbol, side=existing.side,
                        quantity=new_qty, avg_price=existing.avg_price,
                    )

    def _check_connection(self) -> None:
        if not self._connected:
            raise BrokerConnectionError("Paper broker not connected")

    def _maybe_fail(self) -> None:
        if self._fail_counter > 0:
            self._fail_counter -= 1
            raise BrokerTemporaryError("Simulated transient failure", "PAPER_TEMP")

    @staticmethod
    def _order_to_snapshot(order: ExecutionOrder) -> BrokerOrderSnapshot:
        return BrokerOrderSnapshot(
            broker_ref  = order.broker_ref,
            symbol      = order.symbol,
            side        = order.side,
            order_type  = order.order_type,
            quantity    = order.quantity,
            filled_qty  = order.filled_qty,
            status      = order.status,
            avg_price   = order.avg_fill_price,
            timestamp   = order.last_updated,
        )


# ──────────────────────────────────────────────────────────────────────────────
# Retrying wrapper — decorates any IBrokerAdapter with exponential backoff
# ──────────────────────────────────────────────────────────────────────────────

class RetryingBrokerAdapter(IBrokerAdapter):
    """
    Transparent retry wrapper around any IBrokerAdapter.

    Wraps place_order, cancel_order, get_order_status, get_open_orders,
    get_positions, get_account_equity with exponential backoff + jitter.

    Non-retryable errors (BrokerRejectedError) propagate immediately.
    After max_attempts, the last exception is re-raised.

    Connect/disconnect are NOT retried — the caller (NetworkRecoveryManager)
    handles those at a higher level.
    """

    def __init__(self, inner: IBrokerAdapter, config: RetryConfig) -> None:
        self._inner  = inner
        self._config = config

    # ── Delegation with retry ─────────────────────────────────────────────

    async def connect(self) -> None:
        await self._inner.connect()

    async def disconnect(self) -> None:
        await self._inner.disconnect()

    @property
    def is_connected(self) -> bool:
        return self._inner.is_connected

    async def place_order(self, order: ExecutionOrder) -> ExecutionOrder:
        return await self._retry("place_order", self._inner.place_order, order)

    async def cancel_order(self, broker_ref: str) -> bool:
        return await self._retry("cancel_order", self._inner.cancel_order, broker_ref)

    async def get_order_status(self, broker_ref: str) -> BrokerOrderSnapshot:
        return await self._retry("get_order_status", self._inner.get_order_status, broker_ref)

    async def get_open_orders(self) -> list[BrokerOrderSnapshot]:
        return await self._retry("get_open_orders", self._inner.get_open_orders)

    async def get_positions(self) -> list[BrokerPositionSnapshot]:
        return await self._retry("get_positions", self._inner.get_positions)

    async def get_account_equity(self) -> Decimal:
        return await self._retry("get_account_equity", self._inner.get_account_equity)

    # ── Retry core ───────────────────────────────────────────────────────────

    async def _retry(self, op_name: str, fn, *args, **kwargs):
        cfg    = self._config
        delay  = cfg.base_delay_s
        last_exc: Optional[Exception] = None

        for attempt in range(1, cfg.max_attempts + 1):
            try:
                result = await fn(*args, **kwargs)
                if attempt > 1:
                    logger.info(
                        "retry_success | op=%s attempt=%d", op_name, attempt
                    )
                return result

            except BrokerError as exc:
                if not exc.retryable:
                    logger.warning(
                        "non_retryable_error | op=%s error=%s code=%s",
                        op_name, exc, exc.broker_code,
                    )
                    raise

                last_exc = exc
                if isinstance(exc, RateLimitError):
                    wait = exc.retry_after_s
                else:
                    jitter = random.uniform(1 - cfg.jitter_pct, 1 + cfg.jitter_pct)
                    wait   = min(delay * jitter, cfg.max_delay_s)

                logger.warning(
                    "retrying | op=%s attempt=%d/%d delay=%.2fs error=%s",
                    op_name, attempt, cfg.max_attempts, wait, exc,
                )

                if attempt < cfg.max_attempts:
                    await asyncio.sleep(wait)
                    delay = min(delay * cfg.backoff_factor, cfg.max_delay_s)

            except (ConnectionError, TimeoutError, OSError) as exc:
                last_exc = exc
                jitter   = random.uniform(1 - cfg.jitter_pct, 1 + cfg.jitter_pct)
                wait     = min(delay * jitter, cfg.max_delay_s)
                logger.warning(
                    "network_error | op=%s attempt=%d/%d delay=%.2fs error=%s",
                    op_name, attempt, cfg.max_attempts, wait, exc,
                )
                if attempt < cfg.max_attempts:
                    await asyncio.sleep(wait)
                    delay = min(delay * cfg.backoff_factor, cfg.max_delay_s)

        logger.error(
            "retry_exhausted | op=%s attempts=%d last_error=%s",
            op_name, cfg.max_attempts, last_exc,
        )
        raise last_exc  # type: ignore[misc]
