"""
execution/models.py
────────────────────
All value objects, enums, and data contracts for the execution engine.

Design notes
────────────
- All monetary values use Decimal — never float.
- Frozen dataclasses are used for immutable snapshots passed across
  component boundaries; mutable dataclasses for live order state.
- The ExecutionOrder is the execution engine's richer version of the
  core Order, carrying retry state, fill history, and reconciliation metadata.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum, auto
from typing import Optional
import uuid


# ──────────────────────────────────────────────────────────────────────────────
# Enums
# ──────────────────────────────────────────────────────────────────────────────

class OrderSide(Enum):
    BUY  = "BUY"
    SELL = "SELL"


class OrderType(Enum):
    MARKET      = "MARKET"
    LIMIT       = "LIMIT"
    STOP_MARKET = "STOP_MARKET"
    STOP_LIMIT  = "STOP_LIMIT"


class OrderStatus(Enum):
    PENDING    = "PENDING"     # created locally, not yet sent
    SUBMITTED  = "SUBMITTED"   # accepted by broker, awaiting fill
    PARTIAL    = "PARTIAL"     # partially filled, still live
    FILLED     = "FILLED"      # fully filled
    CANCELLED  = "CANCELLED"   # cancelled (by us or broker)
    REJECTED   = "REJECTED"    # refused by broker
    EXPIRED    = "EXPIRED"     # time-in-force expired
    UNKNOWN    = "UNKNOWN"     # seen in reconciliation but not in local state


class TimeInForce(Enum):
    GTC = "GTC"   # good till cancelled
    DAY = "DAY"   # good for session only
    IOC = "IOC"   # immediate or cancel
    FOK = "FOK"   # fill or kill


class ReconciliationResult(Enum):
    MATCH          = "MATCH"
    GHOST_ORDER    = "GHOST_ORDER"    # local order not found at broker
    PHANTOM_ORDER  = "PHANTOM_ORDER"  # broker order not found locally
    STATUS_MISMATCH = "STATUS_MISMATCH"
    QTY_MISMATCH   = "QTY_MISMATCH"
    PRICE_MISMATCH = "PRICE_MISMATCH"


class PositionReconciliationResult(Enum):
    MATCH          = "MATCH"
    QUANTITY_DRIFT = "QUANTITY_DRIFT"
    SIDE_MISMATCH  = "SIDE_MISMATCH"
    PHANTOM        = "PHANTOM"   # broker has position, we don't
    GHOST          = "GHOST"     # we have position, broker doesn't


# ──────────────────────────────────────────────────────────────────────────────
# Fill tracking
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Fill:
    """A single execution event — one broker confirmation of a trade."""
    fill_id:    str
    order_id:   str
    broker_ref: str
    symbol:     str
    side:       OrderSide
    quantity:   Decimal       # quantity of this specific fill
    price:      Decimal       # execution price of this fill
    commission: Decimal
    timestamp:  datetime
    is_partial: bool = False


@dataclass
class FillAccumulator:
    """Tracks partial fills for a single order, computing VWAP on the fly."""
    order_id:     str
    fills:        list[Fill] = field(default_factory=list)

    @property
    def filled_quantity(self) -> Decimal:
        return sum(f.quantity for f in self.fills)

    @property
    def vwap(self) -> Decimal:
        """Volume-weighted average price across all fills."""
        total_qty = self.filled_quantity
        if total_qty == Decimal("0"):
            return Decimal("0")
        return sum(f.price * f.quantity for f in self.fills) / total_qty

    @property
    def total_commission(self) -> Decimal:
        return sum(f.commission for f in self.fills)

    def add_fill(self, fill: Fill) -> None:
        self.fills.append(fill)


# ──────────────────────────────────────────────────────────────────────────────
# Order
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ExecutionOrder:
    """
    The execution engine's full order representation.
    Richer than the core Order — carries retry state, fill history,
    and reconciliation timestamps.
    """
    # Identity
    order_id:    str         = field(default_factory=lambda: str(uuid.uuid4()))
    broker_ref:  str         = ""     # broker-assigned ID, set after submission
    client_ref:  str         = ""     # optional strategy-level tag

    # Instrument
    symbol:      str         = "XAUUSD"
    side:        OrderSide   = OrderSide.BUY
    order_type:  OrderType   = OrderType.MARKET
    tif:         TimeInForce = TimeInForce.GTC

    # Quantity
    quantity:    Decimal     = Decimal("0")   # intended quantity
    filled_qty:  Decimal     = Decimal("0")   # cumulative filled

    # Prices
    limit_price: Optional[Decimal] = None
    stop_price:  Optional[Decimal] = None
    avg_fill_price: Optional[Decimal] = None

    # Status
    status:      OrderStatus = OrderStatus.PENDING
    created_at:  datetime    = field(default_factory=lambda: datetime.now(timezone.utc))
    submitted_at: Optional[datetime] = None
    filled_at:    Optional[datetime] = None
    cancelled_at: Optional[datetime] = None
    last_updated: datetime    = field(default_factory=lambda: datetime.now(timezone.utc))

    # Retry state
    attempt_count:  int      = 0
    last_error:     str      = ""
    next_retry_at:  Optional[datetime] = None

    # Fill history
    fill_accumulator: FillAccumulator = field(default_factory=lambda: FillAccumulator(""))
    fills:            list[Fill]      = field(default_factory=list)

    # Reconciliation
    last_reconciled_at: Optional[datetime] = None
    reconciliation_status: str = ""

    # Metadata
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        self.fill_accumulator = FillAccumulator(self.order_id)

    @property
    def remaining_qty(self) -> Decimal:
        return self.quantity - self.filled_qty

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            OrderStatus.FILLED, OrderStatus.CANCELLED,
            OrderStatus.REJECTED, OrderStatus.EXPIRED,
        )

    @property
    def is_active(self) -> bool:
        return self.status in (OrderStatus.SUBMITTED, OrderStatus.PARTIAL)

    def apply_fill(self, fill: Fill) -> None:
        self.fills.append(fill)
        self.fill_accumulator.add_fill(fill)
        self.filled_qty = self.fill_accumulator.filled_quantity
        self.avg_fill_price = self.fill_accumulator.vwap
        self.last_updated = datetime.now(timezone.utc)

        if self.filled_qty >= self.quantity:
            self.status   = OrderStatus.FILLED
            self.filled_at = fill.timestamp
        else:
            self.status = OrderStatus.PARTIAL


# ──────────────────────────────────────────────────────────────────────────────
# Position
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ExecutionPosition:
    """Live position as tracked by the execution engine."""
    symbol:       str
    side:         OrderSide
    quantity:     Decimal
    avg_price:    Decimal       # VWAP entry
    open_time:    datetime
    last_updated: datetime      = field(default_factory=lambda: datetime.now(timezone.utc))
    unrealized_pnl: Decimal     = Decimal("0")
    realized_pnl:   Decimal     = Decimal("0")
    hard_stop:      Optional[Decimal] = None
    metadata:       dict        = field(default_factory=dict)

    def update_unrealized(self, current_price: Decimal, contract_value: Decimal = Decimal("100")) -> None:
        direction = Decimal("1") if self.side == OrderSide.BUY else Decimal("-1")
        self.unrealized_pnl = (
            (current_price - self.avg_price) * self.quantity * contract_value * direction
        )
        self.last_updated = datetime.now(timezone.utc)


# ──────────────────────────────────────────────────────────────────────────────
# Broker-level snapshot (used for reconciliation)
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BrokerOrderSnapshot:
    """Minimal broker-side representation of an order, returned during reconciliation."""
    broker_ref:   str
    symbol:       str
    side:         OrderSide
    order_type:   OrderType
    quantity:     Decimal
    filled_qty:   Decimal
    status:       OrderStatus
    avg_price:    Optional[Decimal]
    timestamp:    datetime


@dataclass(frozen=True)
class BrokerPositionSnapshot:
    """Broker-side position. Returned from IBrokerAdapter.get_positions()."""
    symbol:    str
    side:      OrderSide
    quantity:  Decimal
    avg_price: Decimal


# ──────────────────────────────────────────────────────────────────────────────
# Reconciliation results
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class OrderReconciliationReport:
    timestamp:   datetime
    discrepancies: list[OrderDiscrepancy] = field(default_factory=list)
    matched:       int = 0
    total_local:   int = 0
    total_broker:  int = 0

    @property
    def clean(self) -> bool:
        return len(self.discrepancies) == 0


@dataclass
class OrderDiscrepancy:
    result:     ReconciliationResult
    order_id:   str
    broker_ref: str
    detail:     str
    local_snapshot:  Optional[BrokerOrderSnapshot] = None
    broker_snapshot: Optional[BrokerOrderSnapshot] = None


@dataclass
class PositionReconciliationReport:
    timestamp:     datetime
    discrepancies: list[PositionDiscrepancy] = field(default_factory=list)
    matched:       int = 0
    total_local:   int = 0
    total_broker:  int = 0

    @property
    def clean(self) -> bool:
        return len(self.discrepancies) == 0


@dataclass
class PositionDiscrepancy:
    result:  PositionReconciliationResult
    symbol:  str
    detail:  str
    local_qty:  Optional[Decimal] = None
    broker_qty: Optional[Decimal] = None


# ──────────────────────────────────────────────────────────────────────────────
# Network / retry
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class RetryConfig:
    max_attempts:    int     = 5
    base_delay_s:    float   = 1.0    # first retry delay
    max_delay_s:     float   = 30.0   # cap on exponential backoff
    backoff_factor:  float   = 2.0    # multiply delay by this each attempt
    jitter_pct:      float   = 0.15   # ±15% random jitter to prevent thundering herd
    retryable_errors: tuple  = (
        "ConnectionError", "TimeoutError", "OSError",
        "BrokerTemporaryError", "RateLimitError",
    )


@dataclass
class ExecutionConfig:
    symbol:             str     = "XAUUSD"
    contract_value:     Decimal = Decimal("100")
    reconcile_interval_s: float = 30.0    # how often to run reconciliation
    fill_poll_interval_s: float =  0.5    # how often to poll for fills
    max_order_age_s:      float = 300.0   # cancel stale orders after this
    retry:                RetryConfig = field(default_factory=RetryConfig)
    log_fills:            bool  = True
    log_reconciliation:   bool  = True
