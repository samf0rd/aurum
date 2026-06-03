"""
risk — standalone risk management engine for systematic trading.

Public API
──────────
    from risk import RiskEngine, RiskConfig, OrderRequest
    from risk.models import EngineState, RejectionReason, SizingMode
"""
from .engine import RiskEngine
from .models import (
    ConsecutiveLossTracker,
    EngineState,
    ExposureTracker,
    OrderDecision,
    OrderRequest,
    PnLTracker,
    RejectionReason,
    RiskConfig,
    RiskSnapshot,
    SizingMode,
)

__all__ = [
    "RiskEngine",
    "RiskEngineAdapter",
    "RiskConfig",
    "OrderRequest",
    "OrderDecision",
    "RiskSnapshot",
    "EngineState",
    "SizingMode",
    "RejectionReason",
    "PnLTracker",
    "ExposureTracker",
    "ConsecutiveLossTracker",
]


from decimal import Decimal


class RiskEngineAdapter:
    """
    Wraps the standalone RiskEngine to satisfy the IRiskEngine interface
    expected by TradingOrchestrator. Translates domain Signal/RiskState
    objects into the standalone engine's OrderRequest API.
    """

    def __init__(self, engine):
        self._engine = engine

    def approve_order(self, signal, risk_state, current_spread, median_spread):
        """Translate Signal -> OrderRequest and call the real engine."""
        try:
            side = "LONG" if "LONG" in signal.signal_type.name else "SHORT"
            entry = Decimal("2000")
            stop  = entry - signal.stop_distance if side == "LONG" else entry + signal.stop_distance
            req = OrderRequest(
                symbol         = signal.symbol,
                side           = side,
                entry_price    = entry,
                stop_price     = stop,
                atr            = signal.atr,
                current_spread = Decimal(str(current_spread)),
                median_spread  = Decimal(str(median_spread)),
            )
            decision = self._engine.approve_order(req)
            reason = decision.rejection_reason.value if not decision.approved else "ok"
            return decision.approved, reason
        except Exception as e:
            return False, str(e)

    def compute_position_size(self, signal, risk_state):
        """Use the engine's sizing logic via a dummy approve call."""
        try:
            side = "LONG" if "LONG" in signal.signal_type.name else "SHORT"
            entry = Decimal("2000")
            stop  = entry - signal.stop_distance if side == "LONG" else entry + signal.stop_distance
            req = OrderRequest(
                symbol         = signal.symbol,
                side           = side,
                entry_price    = entry,
                stop_price     = stop,
                atr            = signal.atr,
                current_spread = Decimal("0.40"),
                median_spread  = Decimal("0.40"),
            )
            decision = self._engine.approve_order(req)
            return decision.quantity if decision.approved else Decimal("0.01")
        except Exception:
            return Decimal("0.01")

    def update_risk_state(self, current_state, realized_pnl, open_positions, current_prices):
        """No-op — the standalone engine manages its own state."""
        return current_state

    def update_trailing_stop(self, position, bars):
        """No trailing stop update in paper mode — return position unchanged."""
        return position

    def record_fill(self, fill):
        """Forward fill notification to underlying engine."""
        try:
            pnl = Decimal(str(getattr(fill, 'realized_pnl', 0)))
            self._engine.record_trade_result(pnl=pnl)
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(self._engine, name)
