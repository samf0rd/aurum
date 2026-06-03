"""
execution/engine.py
────────────────────
The ExecutionEngine is the single entry point for all order operations.

It orchestrates:
  - Broker abstraction (via IBrokerAdapter)
  - Retry logic (via RetryingBrokerAdapter)
  - Partial fill handling (FillAccumulator on every ExecutionOrder)
  - Order reconciliation (OrderReconciler on a timer)
  - Position reconciliation (PositionReconciler on a timer)
  - Network failure recovery (NetworkRecoveryManager)
  - Full structured logging

Public API
──────────
    engine = ExecutionEngine(broker, config)
    await engine.start()

    decision = engine.submit_order(order_req)       # fire-and-forget
    # or
    order = await engine.submit_order_async(req)    # await fill confirmation

    await engine.cancel_order(order_id)
    report = await engine.reconcile_now()
    await engine.stop()

Threading model
───────────────
All public methods are async (or return instantly for fire-and-forget calls).
The engine runs three background tasks:
  1. _fill_poll_loop       — polls broker for fill updates
  2. _reconcile_loop       — periodic order + position reconciliation
  3. recovery.run()        — heartbeat / reconnect loop

State is protected by asyncio locks — single-threaded within the event loop.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Coroutine, Optional

from .brokers.base import (
    IBrokerAdapter,
    BrokerError,
    BrokerRejectedError,
    RetryingBrokerAdapter,
)
from .models import (
    ExecutionConfig,
    ExecutionOrder,
    ExecutionPosition,
    Fill,
    OrderReconciliationReport,
    OrderSide,
    OrderStatus,
    PositionReconciliationReport,
    RetryConfig,
)
from .reconciliation.reconciler import OrderReconciler, PositionReconciler
from .recovery.network import NetworkRecoveryManager, RecoveryState

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Callback type aliases
# ──────────────────────────────────────────────────────────────────────────────

OnFillCallback   = Callable[[Fill], Coroutine]
OnRejectCallback = Callable[[ExecutionOrder, str], Coroutine]
OnAlertCallback  = Callable[[str, str], Coroutine]   # (level, message)


class ExecutionEngine:
    """
    Production execution engine for live trading.

    Lifecycle:
        await engine.start()
        # ... trade ...
        await engine.stop()
    """

    def __init__(
        self,
        broker:            IBrokerAdapter,
        config:            Optional[ExecutionConfig] = None,
        on_fill:           Optional[OnFillCallback]   = None,
        on_reject:         Optional[OnRejectCallback] = None,
        on_alert:          Optional[OnAlertCallback]  = None,
    ) -> None:
        self._config  = config or ExecutionConfig()
        cfg           = self._config

        # Wrap broker with retry logic
        self._raw_broker     = broker
        self._broker: IBrokerAdapter = RetryingBrokerAdapter(broker, cfg.retry)

        # Callbacks
        self._on_fill   = on_fill
        self._on_reject = on_reject
        self._on_alert  = on_alert

        # State
        self._orders:    dict[str, ExecutionOrder]   = {}  # order_id → order
        self._ref_index: dict[str, str]              = {}  # broker_ref → order_id
        self._positions: dict[str, ExecutionPosition] = {}  # symbol → position
        self._processed_fill_ids: set[str]           = set()  # global dedup set
        self._lock       = asyncio.Lock()

        # Reconcilers
        self._order_reconciler    = OrderReconciler()
        self._position_reconciler = PositionReconciler()

        # Recovery manager
        self._recovery = NetworkRecoveryManager(
            broker               = broker,  # use raw broker for heartbeat
            on_reconnect         = self._on_reconnect,
            on_circuit_open      = self._on_circuit_open,
            heartbeat_interval_s = 10.0,
        )

        # Background tasks
        self._tasks: list[asyncio.Task] = []
        self._running = False

        logger.info(
            "ExecutionEngine initialised | symbol=%s retry_attempts=%d",
            cfg.symbol, cfg.retry.max_attempts,
        )

    # ──────────────────────────────────────────────────────────────────────
    # Lifecycle
    # ──────────────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Connect to broker and start all background loops."""
        logger.info("execution_engine starting")
        await self._broker.connect()
        self._running = True

        self._tasks = [
            asyncio.create_task(self._fill_poll_loop(),    name="fill_poll"),
            asyncio.create_task(self._reconcile_loop(),    name="reconcile"),
            asyncio.create_task(self._recovery.run(),      name="net_recovery"),
            asyncio.create_task(self._stale_order_loop(),  name="stale_orders"),
        ]
        logger.info("execution_engine started | tasks=%d", len(self._tasks))

    async def stop(self) -> None:
        """Graceful shutdown: cancel tasks, disconnect broker."""
        logger.info("execution_engine stopping")
        self._running = False
        await self._recovery.stop()

        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        await self._broker.disconnect()
        logger.info("execution_engine stopped")

    # ──────────────────────────────────────────────────────────────────────
    # Order submission
    # ──────────────────────────────────────────────────────────────────────

    async def submit_order(self, order: ExecutionOrder) -> ExecutionOrder:
        """
        Submit an order to the broker.

        - Registers order in local book before sending
        - Handles immediate rejection cleanly
        - Non-blocking from the caller's perspective (fills arrive via callback)

        Returns the submitted order with broker_ref populated.
        Raises BrokerRejectedError if broker refuses immediately.
        """
        async with self._lock:
            self._orders[order.order_id] = order

        logger.info(
            "order_submit | id=%s sym=%s side=%s qty=%s type=%s",
            order.order_id, order.symbol, order.side.value,
            order.quantity, order.order_type.value,
        )

        try:
            order.attempt_count += 1
            await self._broker.place_order(order)

            async with self._lock:
                if order.broker_ref:
                    self._ref_index[order.broker_ref] = order.order_id

            logger.info(
                "order_submitted | id=%s broker_ref=%s",
                order.order_id, order.broker_ref,
            )

            # If already filled (market order instant fill), process fill
            if order.status == OrderStatus.FILLED and order.fills:
                for fill in order.fills:
                    await self._process_fill(fill, order)

            return order

        except BrokerRejectedError as exc:
            async with self._lock:
                order.status    = OrderStatus.REJECTED
                order.last_error = str(exc)

            logger.warning(
                "order_rejected | id=%s reason=%s code=%s",
                order.order_id, exc, exc.broker_code,
            )
            if self._on_reject:
                try:
                    await self._on_reject(order, str(exc))
                except Exception as cb_exc:
                    logger.error("on_reject_callback_failed | error=%s", cb_exc)
            raise

        except BrokerError as exc:
            async with self._lock:
                order.last_error = str(exc)
            logger.error("order_submit_failed | id=%s error=%s", order.order_id, exc)
            raise

    async def cancel_order(self, order_id: str) -> bool:
        """
        Cancel an active order by local order_id.
        Returns True if successfully cancelled at broker.
        """
        async with self._lock:
            order = self._orders.get(order_id)
        if order is None:
            logger.warning("cancel_order_not_found | order_id=%s", order_id)
            return False
        if order.is_terminal:
            logger.debug("cancel_noop | order_id=%s status=%s", order_id, order.status.value)
            return False
        if not order.broker_ref:
            logger.warning("cancel_no_broker_ref | order_id=%s", order_id)
            return False

        try:
            result = await self._broker.cancel_order(order.broker_ref)
            if result:
                async with self._lock:
                    order.status       = OrderStatus.CANCELLED
                    order.cancelled_at = datetime.now(timezone.utc)
                logger.info("order_cancelled | id=%s broker_ref=%s", order_id, order.broker_ref)
            return result
        except BrokerError as exc:
            logger.error(
                "cancel_failed | id=%s broker_ref=%s error=%s",
                order_id, order.broker_ref, exc,
            )
            return False

    async def cancel_all_orders(self, symbol: Optional[str] = None) -> int:
        """Cancel all active orders, optionally filtered by symbol. Returns count cancelled."""
        async with self._lock:
            to_cancel = [
                o for o in self._orders.values()
                if o.is_active and (symbol is None or o.symbol == symbol)
            ]

        cancelled = 0
        for order in to_cancel:
            try:
                if await self.cancel_order(order.order_id):
                    cancelled += 1
            except BrokerError:
                pass  # already logged in cancel_order

        logger.info("cancel_all | symbol=%s cancelled=%d", symbol or "ALL", cancelled)
        return cancelled

    # ──────────────────────────────────────────────────────────────────────
    # Fill processing
    # ──────────────────────────────────────────────────────────────────────

    async def _process_fill(self, fill: Fill, order: ExecutionOrder) -> None:
        """
        Central fill processor — called from poll loop and immediate fill path.

        Handles:
        - Idempotency (duplicate fill IDs are ignored)
        - Partial fill accumulation
        - Position update on final fill
        - Callback dispatch
        """
        # Idempotency check — use a global set, not order.fills
        # (broker may pre-populate order.fills before we see the fill here)
        async with self._lock:
            if fill.fill_id in self._processed_fill_ids:
                logger.debug("fill_duplicate_ignored | fill_id=%s", fill.fill_id)
                return
            self._processed_fill_ids.add(fill.fill_id)

        async with self._lock:
            # Only apply fill to order if it's not already in order.fills
            # (broker adapters may pre-apply fills during place_order)
            already_applied = any(f.fill_id == fill.fill_id for f in order.fills)
            if not already_applied:
                order.apply_fill(fill)
            filled_qty = order.filled_qty
            is_done    = order.status == OrderStatus.FILLED

        logger.info(
            "fill_received | order_id=%s fill_id=%s qty=%s price=%s "
            "total_filled=%s/%s partial=%s",
            fill.order_id, fill.fill_id, fill.quantity, fill.price,
            filled_qty, order.quantity, fill.is_partial,
        )

        if is_done:
            await self._update_position_on_fill(order)

        if self._on_fill:
            try:
                await self._on_fill(fill)
            except Exception as exc:
                logger.error("on_fill_callback_failed | fill_id=%s error=%s", fill.fill_id, exc)

    async def _update_position_on_fill(self, order: ExecutionOrder) -> None:
        """Update the position book when an order is fully filled."""
        async with self._lock:
            symbol   = order.symbol
            existing = self._positions.get(symbol)

            if existing is None:
                # New position
                self._positions[symbol] = ExecutionPosition(
                    symbol      = symbol,
                    side        = order.side,
                    quantity    = order.filled_qty,
                    avg_price   = order.avg_fill_price or Decimal("0"),
                    open_time   = order.filled_at or datetime.now(timezone.utc),
                    hard_stop   = order.metadata.get("stop_price"),
                )
                logger.info(
                    "position_opened | sym=%s side=%s qty=%s avg_price=%s",
                    symbol, order.side.value, order.filled_qty, order.avg_fill_price,
                )
            elif existing.side == order.side:
                # Adding to position — VWAP update
                total_qty  = existing.quantity + order.filled_qty
                new_avg    = (
                    existing.avg_price * existing.quantity
                    + (order.avg_fill_price or Decimal("0")) * order.filled_qty
                ) / total_qty
                existing.quantity    = total_qty
                existing.avg_price   = new_avg
                existing.last_updated = datetime.now(timezone.utc)
                logger.info(
                    "position_increased | sym=%s qty=%s new_avg=%s",
                    symbol, total_qty, new_avg,
                )
            else:
                # Reducing or closing
                new_qty = existing.quantity - order.filled_qty
                if new_qty <= Decimal("0"):
                    del self._positions[symbol]
                    logger.info("position_closed | sym=%s", symbol)
                else:
                    existing.quantity     = new_qty
                    existing.last_updated = datetime.now(timezone.utc)
                    logger.info(
                        "position_reduced | sym=%s new_qty=%s", symbol, new_qty
                    )

    # ──────────────────────────────────────────────────────────────────────
    # Background loops
    # ──────────────────────────────────────────────────────────────────────

    async def _fill_poll_loop(self) -> None:
        """
        Poll broker for fill updates on all active orders.
        Runs every fill_poll_interval_s.
        """
        logger.debug("fill_poll_loop started")
        while self._running:
            try:
                await asyncio.sleep(self._config.fill_poll_interval_s)
                if not self._recovery.is_healthy:
                    continue

                async with self._lock:
                    active = [o for o in self._orders.values() if o.is_active]

                for order in active:
                    if not order.broker_ref:
                        continue
                    try:
                        snap = await self._broker.get_order_status(order.broker_ref)

                        # Detect newly filled quantity
                        if snap.filled_qty > order.filled_qty:
                            fill_qty   = snap.filled_qty - order.filled_qty
                            is_partial = snap.status == OrderStatus.PARTIAL
                            fill = Fill(
                                fill_id    = f"POLL-{order.broker_ref}-{int(snap.filled_qty*100):08d}",
                                order_id   = order.order_id,
                                broker_ref = order.broker_ref,
                                symbol     = order.symbol,
                                side       = order.side,
                                quantity   = fill_qty,
                                price      = snap.avg_price or Decimal("0"),
                                commission = Decimal("0"),   # not available from snapshot
                                timestamp  = snap.timestamp,
                                is_partial = is_partial,
                            )
                            await self._process_fill(fill, order)

                        elif snap.status in (OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.EXPIRED):
                            async with self._lock:
                                order.status       = snap.status
                                order.last_updated = datetime.now(timezone.utc)
                            logger.info(
                                "order_status_update | id=%s status=%s",
                                order.order_id, snap.status.value,
                            )
                    except BrokerError as exc:
                        logger.warning(
                            "fill_poll_error | order_id=%s error=%s", order.order_id, exc
                        )

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("fill_poll_loop_error | error=%s", exc)

    async def _reconcile_loop(self) -> None:
        """Run order + position reconciliation on a fixed interval."""
        logger.debug("reconcile_loop started")
        while self._running:
            try:
                await asyncio.sleep(self._config.reconcile_interval_s)
                if not self._recovery.is_healthy:
                    continue
                await self.reconcile_now()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("reconcile_loop_error | error=%s", exc)

    async def _stale_order_loop(self) -> None:
        """Cancel orders that have been live longer than max_order_age_s."""
        while self._running:
            try:
                await asyncio.sleep(60.0)
                now = datetime.now(timezone.utc)
                async with self._lock:
                    stale = [
                        o for o in self._orders.values()
                        if o.is_active
                        and o.submitted_at
                        and (now - o.submitted_at).total_seconds() > self._config.max_order_age_s
                    ]
                for order in stale:
                    logger.warning(
                        "stale_order_cancel | id=%s age_s=%.0f",
                        order.order_id,
                        (now - order.submitted_at).total_seconds(),
                    )
                    await self.cancel_order(order.order_id)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("stale_order_loop_error | error=%s", exc)

    # ──────────────────────────────────────────────────────────────────────
    # Reconciliation
    # ──────────────────────────────────────────────────────────────────────

    async def reconcile_now(self) -> tuple[OrderReconciliationReport, PositionReconciliationReport]:
        """
        Run a full order + position reconciliation immediately.
        Called by the reconcile loop and on every reconnection.
        """
        logger.debug("reconcile_start")
        try:
            broker_orders    = await self._broker.get_open_orders()
            broker_positions = await self._broker.get_positions()
        except BrokerError as exc:
            logger.error("reconcile_fetch_failed | error=%s", exc)
            raise

        async with self._lock:
            # Build broker_ref → order map for order reconciler
            ref_to_order = {
                o.broker_ref: o
                for o in self._orders.values()
                if o.broker_ref and not o.is_terminal
            }
            local_positions = dict(self._positions)

        order_report    = self._order_reconciler.reconcile(ref_to_order, broker_orders)
        position_report = self._position_reconciler.reconcile(local_positions, broker_positions)

        if not order_report.clean or not position_report.clean:
            await self._handle_reconciliation_discrepancies(order_report, position_report)

        if self._config.log_reconciliation:
            logger.info(
                "reconcile_complete | orders_matched=%d order_issues=%d "
                "positions_matched=%d position_issues=%d",
                order_report.matched, len(order_report.discrepancies),
                position_report.matched, len(position_report.discrepancies),
            )

        return order_report, position_report

    async def _handle_reconciliation_discrepancies(
        self,
        order_report:    OrderReconciliationReport,
        position_report: PositionReconciliationReport,
    ) -> None:
        """Log and alert on reconciliation discrepancies."""
        for disc in order_report.discrepancies:
            logger.warning(
                "order_discrepancy | type=%s order_id=%s broker_ref=%s detail=%s",
                disc.result.value, disc.order_id, disc.broker_ref, disc.detail,
            )

        for disc in position_report.discrepancies:
            level = "critical" if disc.result.value == "SIDE_MISMATCH" else "warning"
            getattr(logger, level)(
                "position_discrepancy | type=%s symbol=%s detail=%s",
                disc.result.value, disc.symbol, disc.detail,
            )
            if self._on_alert:
                alert_level = "CRITICAL" if disc.result.value == "SIDE_MISMATCH" else "WARNING"
                try:
                    await self._on_alert(
                        alert_level,
                        f"Position discrepancy {disc.result.value} on {disc.symbol}: {disc.detail}",
                    )
                except Exception as exc:
                    logger.error("alert_callback_failed | error=%s", exc)

    # ──────────────────────────────────────────────────────────────────────
    # Recovery callbacks
    # ──────────────────────────────────────────────────────────────────────

    async def _on_reconnect(self) -> None:
        """Called by NetworkRecoveryManager after successful reconnection."""
        logger.info("post_reconnect_reconciliation starting")
        try:
            await self.reconcile_now()
        except BrokerError as exc:
            logger.error("post_reconnect_reconciliation_failed | error=%s", exc)

    async def _on_circuit_open(self) -> None:
        """Called when the circuit breaker opens after repeated reconnect failures."""
        logger.critical(
            "CIRCUIT_OPEN — all new order submissions blocked. "
            "Operator must call engine.reset_circuit() after investigating."
        )
        if self._on_alert:
            try:
                await self._on_alert(
                    "CRITICAL",
                    "Execution engine circuit breaker open — broker unreachable. "
                    "Manual intervention required.",
                )
            except Exception as exc:
                logger.error("circuit_open_alert_failed | error=%s", exc)

    # ──────────────────────────────────────────────────────────────────────
    # Introspection
    # ──────────────────────────────────────────────────────────────────────

    def get_order(self, order_id: str) -> Optional[ExecutionOrder]:
        return self._orders.get(order_id)

    def get_active_orders(self, symbol: Optional[str] = None) -> list[ExecutionOrder]:
        return [
            o for o in self._orders.values()
            if o.is_active and (symbol is None or o.symbol == symbol)
        ]

    def get_position(self, symbol: str) -> Optional[ExecutionPosition]:
        return self._positions.get(symbol)

    def get_all_positions(self) -> dict[str, ExecutionPosition]:
        return dict(self._positions)

    async def get_equity(self) -> Decimal:
        return await self._broker.get_account_equity()

    @property
    def recovery_state(self) -> RecoveryState:
        return self._recovery.state

    async def reset_circuit(self) -> None:
        """Operator-triggered circuit breaker reset."""
        await self._recovery.reset_circuit()
