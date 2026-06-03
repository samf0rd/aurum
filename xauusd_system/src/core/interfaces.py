"""
core/interfaces.py
──────────────────
All abstract base classes and data contracts for the XAU/USD trading system.
Every component depends only on these interfaces, never on concrete implementations.
This enables clean testing, hot-swapping of brokers/feeds, and isolated unit tests.
"""

from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum, auto
from typing import AsyncIterator, Callable, Optional, Sequence


# ─────────────────────────────────────────────
# Domain enumerations
# ─────────────────────────────────────────────

class Side(Enum):
    LONG  = "LONG"
    SHORT = "SHORT"


class OrderType(Enum):
    MARKET      = "MARKET"
    LIMIT       = "LIMIT"
    STOP_MARKET = "STOP_MARKET"


class OrderStatus(Enum):
    PENDING   = auto()
    SUBMITTED = auto()
    FILLED    = auto()
    REJECTED  = auto()
    CANCELLED = auto()


class SignalType(Enum):
    ENTER_LONG   = auto()
    ENTER_SHORT  = auto()
    EXIT         = auto()
    NO_SIGNAL    = auto()


class Regime(Enum):
    TRENDING_BULL = auto()
    TRENDING_BEAR = auto()
    CHOPPY        = auto()
    HIGH_VOL      = auto()   # vol cap triggered — no new entries
    UNDEFINED     = auto()


# ─────────────────────────────────────────────
# Core data contracts
# ─────────────────────────────────────────────

@dataclass(frozen=True)
class Bar:
    """Single OHLCV bar. Frozen so it can be used as a dict key / cache key."""
    timestamp: datetime
    open:  Decimal
    high:  Decimal
    low:   Decimal
    close: Decimal
    volume: Decimal
    symbol: str = "XAUUSD"

    def typical_price(self) -> Decimal:
        return (self.high + self.low + self.close) / 3


@dataclass(frozen=True)
class Tick:
    """Live bid/ask quote."""
    timestamp: datetime
    bid:   Decimal
    ask:   Decimal
    symbol: str = "XAUUSD"

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid


@dataclass(frozen=True)
class Signal:
    signal_type: SignalType
    timestamp:   datetime
    symbol:      str
    regime:      Regime
    atr:         Decimal           # ATR(14) at signal time
    stop_distance: Decimal         # 2 × ATR — used by risk engine for sizing
    reason:      str = ""          # human-readable audit string


@dataclass
class Order:
    symbol:     str
    side:       Side
    order_type: OrderType
    quantity:   Decimal            # in units (oz)
    limit_price:  Optional[Decimal] = None
    stop_price:   Optional[Decimal] = None
    order_id:     str              = ""
    status:       OrderStatus      = OrderStatus.PENDING
    filled_price: Optional[Decimal] = None
    filled_at:    Optional[datetime] = None
    broker_ref:   str              = ""
    metadata:     dict             = field(default_factory=dict)


@dataclass
class Position:
    symbol:       str
    side:         Side
    quantity:     Decimal
    entry_price:  Decimal
    entry_time:   datetime
    hard_stop:    Decimal          # broker-side stop price — never loosened
    trailing_low: Decimal          # Donchian-10 trailing exit level
    atr_at_entry: Decimal
    unrealized_pnl: Decimal = Decimal("0")
    open_risk:      Decimal = Decimal("0")  # current $ risk relative to hard stop


@dataclass(frozen=True)
class RiskState:
    """Snapshot of the risk engine for a given moment."""
    equity:             Decimal
    peak_equity:        Decimal
    daily_pnl:          Decimal
    weekly_pnl:         Decimal
    drawdown_pct:       Decimal
    daily_limit_hit:    bool
    weekly_limit_hit:   bool
    circuit_breaker_on: bool
    gap_caution:        bool       # True → size at 0.5% instead of 1%


# ─────────────────────────────────────────────
# Abstract component interfaces
# ─────────────────────────────────────────────

class IDataFeed(ABC):
    """Abstraction over both historical and live data sources."""

    @abstractmethod
    async def fetch_bars(
        self,
        symbol: str,
        start:  datetime,
        end:    datetime,
        timeframe: str = "D",
    ) -> Sequence[Bar]:
        """Pull a closed history of OHLCV bars."""

    @abstractmethod
    async def stream_ticks(self, symbol: str) -> AsyncIterator[Tick]:
        """Yield live bid/ask ticks. Used only for spread monitoring and fills."""

    @abstractmethod
    async def latest_bar(self, symbol: str) -> Bar:
        """Most recent completed daily bar."""


class ISignalGenerator(ABC):
    """Pure strategy logic — no side effects, no I/O."""

    @abstractmethod
    def on_bar(self, bars: Sequence[Bar], regime: Regime) -> Signal:
        """
        Evaluate the most recent bar in context and return a Signal.
        bars: ordered oldest-first, at least 200 bars long.
        regime: pre-computed by IRegimeDetector.
        Returns NO_SIGNAL when no action is warranted.
        """

    @abstractmethod
    def compute_indicators(self, bars: Sequence[Bar]) -> dict:
        """
        Compute and return all raw indicators (SMA200, SMA50, ATR14,
        Donchian20/10, ADX14). Used for logging and validation.
        """


class IRegimeDetector(ABC):
    """Classifies market regime from bars alone."""

    @abstractmethod
    def detect(self, bars: Sequence[Bar]) -> Regime:
        """
        Returns the current regime based on the last bar.
        Called before ISignalGenerator.on_bar() so the signal
        receives the regime rather than re-computing it.
        """


class IRiskEngine(ABC):
    """
    Validates orders before submission and manages account-level limits.
    All money management logic lives here — the signal generator never sizes.
    """

    @abstractmethod
    def approve_order(
        self,
        signal: Signal,
        risk_state: RiskState,
        current_spread: Decimal,
        median_spread: Decimal,
    ) -> tuple[bool, str]:
        """
        Returns (approved: bool, reason: str).
        Checks: circuit breakers, daily/weekly limits, spread gate (Rule 10c),
        vol regime (Rule 1c), position already open.
        """

    @abstractmethod
    def compute_position_size(
        self,
        signal: Signal,
        risk_state: RiskState,
    ) -> Decimal:
        """
        Rule 6: qty = (equity × risk_pct) / (stop_distance × contract_value).
        Halves risk_pct when gap_caution is True (Rule 10b).
        """

    @abstractmethod
    def update_risk_state(
        self,
        current_state: RiskState,
        realized_pnl: Decimal,
        open_positions: list[Position],
        current_prices: dict[str, Decimal],
    ) -> RiskState:
        """Recompute daily/weekly PnL, drawdown, and flip circuit breakers."""

    @abstractmethod
    def update_trailing_stop(
        self,
        position: Position,
        bars: Sequence[Bar],
    ) -> Position:
        """
        Move hard_stop up (for longs) to match Donchian-10 trailing level.
        Stop never moves against the position.
        """


class IOrderManager(ABC):
    """Stateful in-flight order and position ledger."""

    @abstractmethod
    async def submit(self, order: Order) -> Order:
        """Send to broker. Returns order with updated status/broker_ref."""

    @abstractmethod
    async def cancel(self, order_id: str) -> bool:
        """Cancel a pending order. Returns True if confirmed cancelled."""

    @abstractmethod
    def open_positions(self) -> list[Position]:
        """Current open positions snapshot."""

    @abstractmethod
    def open_orders(self) -> list[Order]:
        """Pending/submitted orders not yet filled."""

    @abstractmethod
    async def on_fill(self, order: Order) -> None:
        """
        Called by broker adapter when a fill arrives.
        Converts a filled order into a Position and registers the hard stop.
        """


class IBrokerAdapter(ABC):
    """
    Thin translation layer between our domain objects and a specific broker API.
    Only this class knows about broker-specific auth, REST/WS protocols, etc.
    """

    @abstractmethod
    async def place_order(self, order: Order) -> Order:
        """Translate Order → broker API call → return with broker_ref."""

    @abstractmethod
    async def cancel_order(self, broker_ref: str) -> bool:
        """Cancel by broker reference."""

    @abstractmethod
    async def get_account(self) -> dict:
        """Raw account snapshot: equity, margin, open positions."""

    @abstractmethod
    async def stream_executions(self) -> AsyncIterator[Order]:
        """Yield fill/rejection/cancellation events from broker."""

    @abstractmethod
    async def healthcheck(self) -> bool:
        """Ping the broker — used by the monitoring layer."""


class IEventBus(ABC):
    """
    Simple in-process pub/sub. All components communicate through events,
    never by calling each other directly. Enables easy test injection.
    """

    @abstractmethod
    def publish(self, event_type: str, payload: dict) -> None:
        """Emit an event to all registered handlers."""

    @abstractmethod
    def subscribe(self, event_type: str, handler: Callable[[dict], None]) -> None:
        """Register a handler for an event type."""


class IAlertService(ABC):
    """Out-of-band notifications: Telegram, email, PagerDuty, etc."""

    @abstractmethod
    async def send(
        self,
        level: str,          # "INFO" | "WARNING" | "CRITICAL"
        message: str,
        metadata: Optional[dict] = None,
    ) -> None:
        """Fire-and-forget — must never raise in the critical path."""
