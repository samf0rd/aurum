"""
risk/models.py
──────────────
All value objects, enums, and data contracts for the standalone risk engine.
No external dependencies — pure Python stdlib only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum, auto
from typing import Optional


# ──────────────────────────────────────────────────────────────────────────────
# Enums
# ──────────────────────────────────────────────────────────────────────────────

class EngineState(Enum):
    """Lifecycle state of the risk engine."""
    ACTIVE        = auto()   # normal operations
    DAILY_HALTED  = auto()   # daily loss limit hit — resumes next session
    WEEKLY_HALTED = auto()   # weekly loss limit hit — resumes Monday
    DRAWDOWN_HALT = auto()   # drawdown CB fired — cooldown required
    EMERGENCY     = auto()   # manual or automatic emergency shutdown — requires explicit reset


class SizingMode(Enum):
    """Which position-sizing algorithm is active."""
    FIXED_FRACTIONAL  = auto()   # risk % of equity / stop distance
    VOLATILITY_ADJ    = auto()   # volatility-targeted: scales by ATR ratio
    REDUCED           = auto()   # post-consecutive-loss half-size
    EMERGENCY_MIN     = auto()   # minimum allowed size — last resort before shutdown


class RejectionReason(Enum):
    """Why the engine refused to approve an order."""
    OK                      = "ok"
    ENGINE_NOT_ACTIVE       = "engine_not_active"
    DAILY_LIMIT_HIT         = "daily_limit_hit"
    WEEKLY_LIMIT_HIT        = "weekly_limit_hit"
    DRAWDOWN_LIMIT_HIT      = "drawdown_limit_hit"
    EXPOSURE_LIMIT_HIT      = "exposure_limit_hit"
    CONSECUTIVE_LOSS_HALT   = "consecutive_loss_halt"
    EMERGENCY_SHUTDOWN      = "emergency_shutdown"
    ZERO_STOP_DISTANCE      = "zero_stop_distance"
    QUANTITY_BELOW_MINIMUM  = "quantity_below_minimum"
    SPREAD_TOO_WIDE         = "spread_too_wide"


# ──────────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class RiskConfig:
    """
    All tuneable parameters for the risk engine.
    Round-number defaults based on the XAU/USD strategy specification.
    Change these at the strategy level, not inside the engine.
    """
    # ── Fixed fractional sizing ────────────────────────────────────────────
    risk_pct_normal: Decimal        = Decimal("0.01")    # 1% per trade
    risk_pct_reduced: Decimal       = Decimal("0.005")   # 0.5% — post-consec-loss or caution

    # ── Volatility-adjusted sizing ─────────────────────────────────────────
    vol_target_pct: Decimal         = Decimal("0.20")    # 20% annualised vol target
    atr_lookback: int               = 14                 # ATR period for vol-adj mode
    annualise_factor: Decimal       = Decimal("16")      # sqrt(256) trading days

    # ── Daily / weekly loss limits ─────────────────────────────────────────
    daily_loss_limit_pct: Decimal   = Decimal("0.02")    # 2% of equity
    weekly_loss_limit_pct: Decimal  = Decimal("0.05")    # 5% of equity

    # ── Maximum drawdown ───────────────────────────────────────────────────
    max_drawdown_pct: Decimal       = Decimal("0.15")    # 15% from peak
    drawdown_resume_pct: Decimal    = Decimal("0.10")    # resume when within 10% of peak
    drawdown_cooldown_bars: int     = 20                 # min bars before re-enabling

    # ── Exposure limits ────────────────────────────────────────────────────
    max_gross_exposure_pct: Decimal = Decimal("2.00")    # 200% of equity — notional
    max_net_exposure_pct: Decimal   = Decimal("1.00")    # 100% net long/short
    max_open_positions: int         = 4                  # concurrent positions

    # ── Consecutive-loss controls ──────────────────────────────────────────
    consec_loss_soft_limit: int     = 4    # switch to reduced sizing after N losses
    consec_loss_hard_limit: int     = 7    # halt new entries after N losses
    consec_loss_reset_wins: int     = 2    # consecutive wins needed to reset counter

    # ── Emergency shutdown ─────────────────────────────────────────────────
    emergency_daily_loss_pct: Decimal  = Decimal("0.04")  # 2× daily limit = emergency
    emergency_drawdown_pct: Decimal    = Decimal("0.25")  # 25% drawdown = immediate halt
    emergency_consec_losses: int       = 10               # 10 in a row = emergency

    # ── Contract / instrument ──────────────────────────────────────────────
    contract_value: Decimal         = Decimal("100")     # $ value per price unit per lot
    min_lot: Decimal                = Decimal("0.01")    # minimum tradeable quantity
    lot_precision: int              = 2                  # decimal places for rounding

    # ── Spread gate ────────────────────────────────────────────────────────
    spread_gate_multiplier: Decimal = Decimal("3.0")     # reject if spread > N × median


# ──────────────────────────────────────────────────────────────────────────────
# Runtime state
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class PnLTracker:
    """Intraday and intraweek P&L tracking.  All values in account currency."""
    daily_realized: Decimal   = Decimal("0")
    daily_open: Decimal       = Decimal("0")
    weekly_realized: Decimal  = Decimal("0")
    weekly_open: Decimal      = Decimal("0")
    tracking_date: Optional[date] = None
    tracking_week: Optional[int]  = None    # ISO week number

    @property
    def daily_total(self) -> Decimal:
        return self.daily_realized + self.daily_open

    @property
    def weekly_total(self) -> Decimal:
        return self.weekly_realized + self.weekly_open


@dataclass
class ExposureTracker:
    """Gross and net notional exposure."""
    long_notional: Decimal  = Decimal("0")
    short_notional: Decimal = Decimal("0")
    open_positions: int     = 0

    @property
    def gross_exposure(self) -> Decimal:
        return self.long_notional + self.short_notional

    @property
    def net_exposure(self) -> Decimal:
        return self.long_notional - self.short_notional


@dataclass
class ConsecutiveLossTracker:
    """Tracks consecutive wins and losses for circuit-breaker logic."""
    current_streak: int  = 0    # positive = wins, negative = losses
    losses_in_row: int   = 0
    wins_in_row: int     = 0
    total_trades: int    = 0
    total_losses: int    = 0

    def record_win(self) -> None:
        self.current_streak = max(0, self.current_streak) + 1
        self.wins_in_row    = self.current_streak
        self.losses_in_row  = 0
        self.total_trades  += 1

    def record_loss(self) -> None:
        self.current_streak = min(0, self.current_streak) - 1
        self.losses_in_row  = abs(self.current_streak)
        self.wins_in_row    = 0
        self.total_trades  += 1
        self.total_losses  += 1


@dataclass
class RiskSnapshot:
    """
    Full risk-engine state snapshot — returned on every approve_order() call.
    Immutable view for downstream consumers (logging, monitoring, UI).
    """
    timestamp: datetime
    state: EngineState
    sizing_mode: SizingMode

    # Equity
    equity: Decimal
    peak_equity: Decimal
    drawdown_pct: Decimal

    # P&L
    daily_pnl: Decimal
    weekly_pnl: Decimal
    daily_limit_pct: Decimal    # how much of daily limit consumed (0–1+)
    weekly_limit_pct: Decimal   # how much of weekly limit consumed

    # Exposure
    gross_exposure_pct: Decimal
    net_exposure_pct: Decimal
    open_positions: int

    # Consecutive losses
    consecutive_losses: int
    consecutive_wins: int

    # Cooldown
    drawdown_cooldown_bars_remaining: int

    # Last rejection (if any)
    last_rejection: RejectionReason = RejectionReason.OK
    last_rejection_detail: str = ""


# ──────────────────────────────────────────────────────────────────────────────
# Order request / result
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class OrderRequest:
    """Input to approve_order(). Strategy-agnostic — works for any instrument."""
    symbol: str
    side: str                          # "LONG" | "SHORT"
    entry_price: Decimal
    stop_price: Decimal                # hard stop — used to derive stop distance
    atr: Decimal                       # ATR(N) at signal time — used by vol-adj mode
    current_spread: Decimal = Decimal("0")
    median_spread: Decimal  = Decimal("0")
    notional_per_lot: Optional[Decimal] = None  # override contract_value if needed
    metadata: dict = field(default_factory=dict)

    @property
    def stop_distance(self) -> Decimal:
        dist = abs(self.entry_price - self.stop_price)
        return dist if dist > Decimal("0") else Decimal("0")


@dataclass
class OrderDecision:
    """Result from approve_order()."""
    approved: bool
    rejection_reason: RejectionReason
    rejection_detail: str
    quantity: Decimal           # lots — 0 if rejected
    risk_amount: Decimal        # $ risked on this trade (if approved)
    sizing_mode: SizingMode
    snapshot: RiskSnapshot
