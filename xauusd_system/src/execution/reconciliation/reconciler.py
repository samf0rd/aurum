"""
execution/reconciliation/reconciler.py
────────────────────────────────────────
Order and position reconciliation engine.

Reconciliation philosophy
──────────────────────────
The reconciler is a safety net, not a primary data path.
It runs on a timer (default 30s) and compares the engine's local state
against what the broker actually holds.

Discrepancy taxonomy
─────────────────────
Orders
  GHOST_ORDER    — we have it locally but broker has no record
                   Action: mark local order UNKNOWN, alert operator
  PHANTOM_ORDER  — broker has it but we don't
                   Action: import it into local state
  STATUS_MISMATCH — same order, different status
                   Action: update local status to match broker (broker wins)
  QTY_MISMATCH   — fill quantity differs
                   Action: reconcile fills, update local fill accumulator

Positions
  QUANTITY_DRIFT — same symbol/side, different quantity
                   Action: update local quantity, alert
  SIDE_MISMATCH  — same symbol, opposite side
                   Action: CRITICAL alert — immediate manual review
  PHANTOM        — broker holds position, we don't track it
                   Action: import + alert
  GHOST          — we track position, broker has none
                   Action: close local position, alert
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

from ..models import (
    BrokerOrderSnapshot,
    BrokerPositionSnapshot,
    ExecutionOrder,
    ExecutionPosition,
    OrderDiscrepancy,
    OrderReconciliationReport,
    OrderStatus,
    PositionDiscrepancy,
    PositionReconciliationReport,
    PositionReconciliationResult,
    ReconciliationResult,
)

if TYPE_CHECKING:
    from ..brokers.base import IBrokerAdapter

logger = logging.getLogger(__name__)

# Price tolerance for fill-price comparison (0.01 = $0.01)
PRICE_TOLERANCE   = Decimal("0.01")
# Quantity tolerance (absolute lots)
QUANTITY_TOLERANCE = Decimal("0.001")


class OrderReconciler:
    """
    Compares local order book against broker's open orders.
    Returns a full report with every discrepancy typed and described.
    """

    def reconcile(
        self,
        local_orders: dict[str, ExecutionOrder],       # broker_ref → order
        broker_orders: list[BrokerOrderSnapshot],
    ) -> OrderReconciliationReport:
        now    = datetime.now(timezone.utc)
        report = OrderReconciliationReport(
            timestamp    = now,
            total_local  = len(local_orders),
            total_broker = len(broker_orders),
        )

        broker_map: dict[str, BrokerOrderSnapshot] = {
            s.broker_ref: s for s in broker_orders
        }
        local_map: dict[str, ExecutionOrder] = {
            ref: o for ref, o in local_orders.items()
            if not o.is_terminal   # only compare active orders
        }

        # ── Check every local active order against broker ─────────────────
        for broker_ref, local_order in local_map.items():
            broker_snap = broker_map.get(broker_ref)

            if broker_snap is None:
                # Broker has no record — could be filled (and we missed the fill)
                # or truly missing
                disc = OrderDiscrepancy(
                    result     = ReconciliationResult.GHOST_ORDER,
                    order_id   = local_order.order_id,
                    broker_ref = broker_ref,
                    detail     = (
                        f"Local order {local_order.order_id} (ref={broker_ref}) "
                        f"not found at broker. Local status={local_order.status.value}"
                    ),
                )
                report.discrepancies.append(disc)
                logger.warning(
                    "reconcile_ghost_order | order_id=%s broker_ref=%s",
                    local_order.order_id, broker_ref,
                )
                continue

            # ── Status mismatch ────────────────────────────────────────────
            if broker_snap.status != local_order.status:
                disc = OrderDiscrepancy(
                    result           = ReconciliationResult.STATUS_MISMATCH,
                    order_id         = local_order.order_id,
                    broker_ref       = broker_ref,
                    detail           = (
                        f"Status mismatch: local={local_order.status.value} "
                        f"broker={broker_snap.status.value}"
                    ),
                    broker_snapshot  = broker_snap,
                )
                report.discrepancies.append(disc)
                # Broker is source of truth — update local
                local_order.status       = broker_snap.status
                local_order.last_updated = now
                logger.warning(
                    "reconcile_status_mismatch | order_id=%s local=%s broker=%s",
                    local_order.order_id, local_order.status.value, broker_snap.status.value,
                )

            # ── Quantity mismatch ──────────────────────────────────────────
            elif abs(broker_snap.filled_qty - local_order.filled_qty) > QUANTITY_TOLERANCE:
                disc = OrderDiscrepancy(
                    result           = ReconciliationResult.QTY_MISMATCH,
                    order_id         = local_order.order_id,
                    broker_ref       = broker_ref,
                    detail           = (
                        f"Fill qty mismatch: local={local_order.filled_qty} "
                        f"broker={broker_snap.filled_qty}"
                    ),
                    broker_snapshot  = broker_snap,
                )
                report.discrepancies.append(disc)
                logger.warning(
                    "reconcile_qty_mismatch | order_id=%s local_filled=%s broker_filled=%s",
                    local_order.order_id, local_order.filled_qty, broker_snap.filled_qty,
                )

            else:
                report.matched += 1
                local_order.last_reconciled_at = now
                local_order.reconciliation_status = "OK"

        # ── Phantom orders — broker has them, we don't ────────────────────
        local_refs = set(local_map.keys())
        for broker_ref, broker_snap in broker_map.items():
            if broker_ref not in local_refs:
                disc = OrderDiscrepancy(
                    result          = ReconciliationResult.PHANTOM_ORDER,
                    order_id        = "",
                    broker_ref      = broker_ref,
                    detail          = (
                        f"Broker has order ref={broker_ref} sym={broker_snap.symbol} "
                        f"status={broker_snap.status.value} that is not in local book"
                    ),
                    broker_snapshot = broker_snap,
                )
                report.discrepancies.append(disc)
                logger.error(
                    "reconcile_phantom_order | broker_ref=%s symbol=%s",
                    broker_ref, broker_snap.symbol,
                )

        if report.clean:
            logger.debug(
                "order_reconciliation_clean | matched=%d", report.matched
            )
        else:
            logger.warning(
                "order_reconciliation_discrepancies | count=%d matched=%d",
                len(report.discrepancies), report.matched,
            )

        return report


class PositionReconciler:
    """
    Compares local position book against broker's open positions.
    Position reconciliation is more critical than order reconciliation —
    SIDE_MISMATCH is treated as a critical alert.
    """

    def reconcile(
        self,
        local_positions:  dict[str, ExecutionPosition],    # symbol → position
        broker_positions: list[BrokerPositionSnapshot],
    ) -> PositionReconciliationReport:
        now    = datetime.now(timezone.utc)
        report = PositionReconciliationReport(
            timestamp    = now,
            total_local  = len(local_positions),
            total_broker = len(broker_positions),
        )

        # Key: (symbol, side)
        broker_map: dict[tuple[str, str], BrokerPositionSnapshot] = {
            (s.symbol, s.side.value): s for s in broker_positions
        }

        # ── Check every local position ─────────────────────────────────────
        for symbol, local_pos in local_positions.items():
            key = (symbol, local_pos.side.value)
            broker_pos = broker_map.get(key)

            if broker_pos is None:
                # Check if broker has it on the other side (critical)
                opposite_key = (
                    symbol,
                    "SELL" if local_pos.side.value == "BUY" else "BUY",
                )
                if opposite_key in broker_map:
                    disc = PositionDiscrepancy(
                        result     = PositionReconciliationResult.SIDE_MISMATCH,
                        symbol     = symbol,
                        detail     = (
                            f"CRITICAL: local={local_pos.side.value} "
                            f"but broker has opposite side"
                        ),
                        local_qty  = local_pos.quantity,
                        broker_qty = broker_map[opposite_key].quantity,
                    )
                    report.discrepancies.append(disc)
                    logger.critical(
                        "reconcile_SIDE_MISMATCH | symbol=%s local=%s broker=opposite",
                        symbol, local_pos.side.value,
                    )
                else:
                    disc = PositionDiscrepancy(
                        result    = PositionReconciliationResult.GHOST,
                        symbol    = symbol,
                        detail    = (
                            f"Local position {symbol}/{local_pos.side.value} "
                            f"qty={local_pos.quantity} not found at broker"
                        ),
                        local_qty = local_pos.quantity,
                    )
                    report.discrepancies.append(disc)
                    logger.error(
                        "reconcile_ghost_position | symbol=%s side=%s qty=%s",
                        symbol, local_pos.side.value, local_pos.quantity,
                    )
                continue

            qty_diff = abs(broker_pos.quantity - local_pos.quantity)
            if qty_diff > QUANTITY_TOLERANCE:
                disc = PositionDiscrepancy(
                    result     = PositionReconciliationResult.QUANTITY_DRIFT,
                    symbol     = symbol,
                    detail     = (
                        f"Quantity drift: local={local_pos.quantity} "
                        f"broker={broker_pos.quantity} diff={qty_diff}"
                    ),
                    local_qty  = local_pos.quantity,
                    broker_qty = broker_pos.quantity,
                )
                report.discrepancies.append(disc)
                logger.warning(
                    "reconcile_qty_drift | symbol=%s local=%s broker=%s",
                    symbol, local_pos.quantity, broker_pos.quantity,
                )
                # Sync to broker quantity — broker wins
                local_pos.quantity     = broker_pos.quantity
                local_pos.last_updated = now
            else:
                report.matched += 1

        # ── Phantom positions — broker has them, we don't ─────────────────
        local_keys = {(s, p.side.value) for s, p in local_positions.items()}
        for (symbol, side_str), broker_pos in broker_map.items():
            if (symbol, side_str) not in local_keys:
                disc = PositionDiscrepancy(
                    result     = PositionReconciliationResult.PHANTOM,
                    symbol     = symbol,
                    detail     = (
                        f"Broker has position {symbol}/{side_str} "
                        f"qty={broker_pos.quantity} not in local book"
                    ),
                    broker_qty = broker_pos.quantity,
                )
                report.discrepancies.append(disc)
                logger.error(
                    "reconcile_phantom_position | symbol=%s side=%s qty=%s",
                    symbol, side_str, broker_pos.quantity,
                )

        if report.clean:
            logger.debug("position_reconciliation_clean | matched=%d", report.matched)
        else:
            logger.warning(
                "position_reconciliation_discrepancies | count=%d matched=%d",
                len(report.discrepancies), report.matched,
            )

        return report
