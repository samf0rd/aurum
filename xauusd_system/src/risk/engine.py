"""
risk/engine.py
──────────────
Standalone risk-management engine for systematic trading.

Controls implemented
────────────────────
1. Fixed fractional sizing        — risk_pct × equity / stop_distance
2. Volatility-adjusted sizing     — vol-target mode scales by ATR ratio
3. Daily loss limits              — halt after 2% daily loss
4. Weekly loss limits             — halt after 5% weekly loss
5. Maximum drawdown limits        — circuit breaker at 15% from peak
6. Exposure limits                — gross, net, and open-position caps
7. Consecutive-loss controls      — soft (reduced size) + hard (halt)
8. Emergency shutdown logic       — auto + manual; requires explicit reset

Design principles
─────────────────
• Single authority  — engine is the ONLY place sizing and gating decisions live
• Fail-closed       — any uncertainty → reject
• Immutable config  — RiskConfig frozen after instantiation
• Pure logic        — no I/O, no broker calls, no async; test-friendly
• Full audit trail  — every approve/reject logged with reason
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Optional

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

logger = logging.getLogger(__name__)


class RiskEngine:
    """
    Standalone risk management engine.

    Usage
    ─────
        cfg    = RiskConfig()
        engine = RiskEngine(initial_equity=Decimal("100000"), config=cfg)

        decision = engine.approve_order(order_request)
        if decision.approved:
            send_order(decision.quantity)

        # After each trade closes:
        engine.record_trade_result(pnl=Decimal("-450"))

        # Each new session:
        engine.on_session_open(current_equity=Decimal("99550"))
    """

    def __init__(
        self,
        initial_equity: Decimal,
        config: Optional[RiskConfig] = None,
    ) -> None:
        self._config   = config or RiskConfig()
        self._equity   = initial_equity
        self._peak_eq  = initial_equity

        self._state         = EngineState.ACTIVE
        self._sizing_mode   = SizingMode.FIXED_FRACTIONAL

        self._pnl       = PnLTracker()
        self._exposure  = ExposureTracker()
        self._consec    = ConsecutiveLossTracker()

        self._cb_bars_elapsed: int = 0    # bars since drawdown CB fired
        self._emergency_reason: str = ""

        # Date/week tracking — initialise NOW so first approve_order() doesn't
        # trigger a spurious window roll that wipes any P&L set before the call
        _now = datetime.now(timezone.utc)
        self._current_date: Optional[date] = _now.date()
        self._current_week: Optional[int]  = _now.isocalendar()[1]

        logger.info(
            "RiskEngine initialised | equity=%s | state=%s",
            initial_equity, self._state.name,
        )

    # ──────────────────────────────────────────────────────────────────────
    # Primary API
    # ──────────────────────────────────────────────────────────────────────

    def approve_order(self, req: OrderRequest) -> OrderDecision:
        """
        Gate all risk controls before allowing a new order.

        Returns OrderDecision with:
          - approved=True  → quantity to trade
          - approved=False → rejection_reason + zero quantity
        """
        # ── 1. Auto-roll daily/weekly windows if date changed ──────────────
        self._maybe_roll_windows()

        # ── 2. Hard gates (engine state) ──────────────────────────────────
        gate_result = self._check_hard_gates()
        if gate_result is not None:
            return self._reject(gate_result[0], gate_result[1], req)

        # ── 3. Spread gate (data quality) ─────────────────────────────────
        if req.current_spread > Decimal("0") and req.median_spread > Decimal("0"):
            threshold = req.median_spread * self._config.spread_gate_multiplier
            if req.current_spread > threshold:
                return self._reject(
                    RejectionReason.SPREAD_TOO_WIDE,
                    f"spread {req.current_spread} > {threshold} ({self._config.spread_gate_multiplier}× median)",
                    req,
                )

        # ── 4. Validate stop distance ──────────────────────────────────────
        if req.stop_distance <= Decimal("0"):
            return self._reject(
                RejectionReason.ZERO_STOP_DISTANCE,
                "stop_distance must be > 0",
                req,
            )

        # ── 5. Loss limits ─────────────────────────────────────────────────
        loss_gate = self._check_loss_limits()
        if loss_gate is not None:
            return self._reject(loss_gate[0], loss_gate[1], req)

        # ── 6. Drawdown limit ─────────────────────────────────────────────
        dd_gate = self._check_drawdown()
        if dd_gate is not None:
            return self._reject(dd_gate[0], dd_gate[1], req)

        # ── 7. Exposure limits ────────────────────────────────────────────
        exp_gate = self._check_exposure(req)
        if exp_gate is not None:
            return self._reject(exp_gate[0], exp_gate[1], req)

        # ── 8. Consecutive-loss hard limit ────────────────────────────────
        consec_gate = self._check_consecutive_loss_hard()
        if consec_gate is not None:
            return self._reject(consec_gate[0], consec_gate[1], req)

        # ── 9. Compute size ───────────────────────────────────────────────
        qty, sizing_mode, risk_amt = self._compute_size(req)

        if qty < self._config.min_lot:
            return self._reject(
                RejectionReason.QUANTITY_BELOW_MINIMUM,
                f"computed qty {qty} < min_lot {self._config.min_lot}",
                req,
            )

        # ── 10. Approve ───────────────────────────────────────────────────
        logger.info(
            "ORDER APPROVED | sym=%s side=%s qty=%s risk=$%s mode=%s",
            req.symbol, req.side, qty, risk_amt, sizing_mode.name,
        )
        return OrderDecision(
            approved=True,
            rejection_reason=RejectionReason.OK,
            rejection_detail="",
            quantity=qty,
            risk_amount=risk_amt,
            sizing_mode=sizing_mode,
            snapshot=self._snapshot(RejectionReason.OK, ""),
        )

    # ──────────────────────────────────────────────────────────────────────
    # State update API — call these from the order/position manager
    # ──────────────────────────────────────────────────────────────────────

    def on_session_open(
        self,
        current_equity: Decimal,
        session_date: Optional[date] = None,
    ) -> None:
        """
        Call at the start of every trading session (daily bar open).
        Updates equity, checks for daily/weekly window roll.
        """
        self._equity = current_equity
        self._peak_eq = max(self._peak_eq, current_equity)
        self._cb_bars_elapsed += 1  # progress drawdown cooldown
        self._maybe_roll_windows(force_date=session_date)

        # Re-check if drawdown CB can be lifted
        if self._state == EngineState.DRAWDOWN_HALT:
            self._maybe_lift_drawdown_halt()

        logger.debug(
            "session_open | equity=%s peak=%s dd=%.2f%% state=%s",
            current_equity, self._peak_eq,
            float(self._drawdown_pct() * 100),
            self._state.name,
        )

    def update_open_pnl(
        self,
        open_pnl: Decimal,
        long_notional: Optional[Decimal] = None,
        short_notional: Optional[Decimal] = None,
    ) -> None:
        """
        Update unrealised P&L and exposure. Call on every price tick or bar.
        Triggers intraday emergency check.
        """
        self._pnl.daily_open   = open_pnl
        self._pnl.weekly_open  = open_pnl   # simplification — override weekly separately if needed

        if long_notional is not None:
            self._exposure.long_notional = long_notional
        if short_notional is not None:
            self._exposure.short_notional = short_notional

        self._check_emergency()

    def record_position_opened(
        self,
        long_notional: Decimal = Decimal("0"),
        short_notional: Decimal = Decimal("0"),
    ) -> None:
        """Call when a position is confirmed filled."""
        self._exposure.long_notional  += long_notional
        self._exposure.short_notional += short_notional
        self._exposure.open_positions += 1

    def record_position_closed(
        self,
        realized_pnl: Decimal,
        long_notional: Decimal = Decimal("0"),
        short_notional: Decimal = Decimal("0"),
    ) -> None:
        """
        Call when a position closes. Updates P&L, exposure, consecutive-loss tracker.
        """
        self._pnl.daily_realized  += realized_pnl
        self._pnl.weekly_realized += realized_pnl
        self._equity              += realized_pnl
        self._peak_eq              = max(self._peak_eq, self._equity)

        self._exposure.long_notional  = max(Decimal("0"), self._exposure.long_notional  - long_notional)
        self._exposure.short_notional = max(Decimal("0"), self._exposure.short_notional - short_notional)
        self._exposure.open_positions = max(0, self._exposure.open_positions - 1)

        if realized_pnl >= Decimal("0"):
            self._consec.record_win()
            # Reset consecutive-loss halt if back within normal range
            if (
                self._state == EngineState.ACTIVE
                and self._consec.wins_in_row >= self._config.consec_loss_reset_wins
            ):
                self._sizing_mode = SizingMode.FIXED_FRACTIONAL
                logger.info("consec_loss_counter_reset after %d wins", self._consec.wins_in_row)
        else:
            self._consec.record_loss()
            self._check_consecutive_loss_auto()

        self._check_emergency()
        logger.info(
            "position_closed | pnl=%s equity=%s consec_losses=%d state=%s",
            realized_pnl, self._equity,
            self._consec.losses_in_row, self._state.name,
        )

    def record_trade_result(self, pnl: Decimal) -> None:
        """Convenience alias when you only have a scalar P&L."""
        self.record_position_closed(realized_pnl=pnl)

    # ──────────────────────────────────────────────────────────────────────
    # Emergency controls
    # ──────────────────────────────────────────────────────────────────────

    def emergency_shutdown(self, reason: str = "manual") -> None:
        """
        Immediately halt all new trading. Requires explicit reset_emergency()
        to re-activate. Use for manual overrides or critical system errors.
        """
        self._state            = EngineState.EMERGENCY
        self._emergency_reason = reason
        logger.critical("EMERGENCY SHUTDOWN | reason=%s | equity=%s", reason, self._equity)

    def reset_emergency(self, operator_override: bool = False) -> bool:
        """
        Attempt to clear the emergency state.
        Returns True only if conditions have improved enough, or operator forces it.
        """
        if not operator_override:
            dd = self._drawdown_pct()
            if dd >= self._config.emergency_drawdown_pct:
                logger.warning(
                    "reset_emergency denied — drawdown %.2f%% still above emergency threshold %.2f%%",
                    float(dd * 100), float(self._config.emergency_drawdown_pct * 100),
                )
                return False

        self._state            = EngineState.ACTIVE
        self._emergency_reason = ""
        logger.warning("emergency_reset | operator_override=%s", operator_override)
        return True

    # ──────────────────────────────────────────────────────────────────────
    # Introspection
    # ──────────────────────────────────────────────────────────────────────

    @property
    def state(self) -> EngineState:
        return self._state

    @property
    def equity(self) -> Decimal:
        return self._equity

    @property
    def config(self) -> RiskConfig:
        return self._config

    def snapshot(self) -> RiskSnapshot:
        return self._snapshot(RejectionReason.OK, "")

    # ──────────────────────────────────────────────────────────────────────
    # Internal — gates
    # ──────────────────────────────────────────────────────────────────────

    def _check_hard_gates(self) -> Optional[tuple[RejectionReason, str]]:
        """Returns (reason, detail) if order must be blocked, else None."""
        if self._state == EngineState.EMERGENCY:
            return RejectionReason.EMERGENCY_SHUTDOWN, self._emergency_reason
        if self._state == EngineState.DAILY_HALTED:
            return RejectionReason.DAILY_LIMIT_HIT, "daily loss limit active — resumes next session"
        if self._state == EngineState.WEEKLY_HALTED:
            return RejectionReason.WEEKLY_LIMIT_HIT, "weekly loss limit active — resumes Monday"
        if self._state == EngineState.DRAWDOWN_HALT:
            return (
                RejectionReason.DRAWDOWN_LIMIT_HIT,
                f"drawdown CB — {self._cb_bars_elapsed}/{self._config.drawdown_cooldown_bars} bars elapsed",
            )
        return None

    def _check_loss_limits(self) -> Optional[tuple[RejectionReason, str]]:
        eq = self._equity
        if eq <= Decimal("0"):
            return RejectionReason.DAILY_LIMIT_HIT, "equity <= 0"

        daily_loss_pct = -self._pnl.daily_total / eq
        if daily_loss_pct >= self._config.daily_loss_limit_pct:
            self._state = EngineState.DAILY_HALTED
            logger.warning("DAILY_LOSS_LIMIT_HIT | loss=%.2f%%", float(daily_loss_pct * 100))
            return (
                RejectionReason.DAILY_LIMIT_HIT,
                f"daily loss {daily_loss_pct:.2%} >= limit {self._config.daily_loss_limit_pct:.2%}",
            )

        weekly_loss_pct = -self._pnl.weekly_total / eq
        if weekly_loss_pct >= self._config.weekly_loss_limit_pct:
            self._state = EngineState.WEEKLY_HALTED
            logger.warning("WEEKLY_LOSS_LIMIT_HIT | loss=%.2f%%", float(weekly_loss_pct * 100))
            return (
                RejectionReason.WEEKLY_LIMIT_HIT,
                f"weekly loss {weekly_loss_pct:.2%} >= limit {self._config.weekly_loss_limit_pct:.2%}",
            )

        return None

    def _check_drawdown(self) -> Optional[tuple[RejectionReason, str]]:
        dd = self._drawdown_pct()
        if dd >= self._config.max_drawdown_pct:
            if self._state != EngineState.DRAWDOWN_HALT:
                self._state           = EngineState.DRAWDOWN_HALT
                self._cb_bars_elapsed = 0
                logger.warning("DRAWDOWN_CB_FIRED | drawdown=%.2f%%", float(dd * 100))
            return (
                RejectionReason.DRAWDOWN_LIMIT_HIT,
                f"drawdown {dd:.2%} >= max {self._config.max_drawdown_pct:.2%}",
            )
        return None

    def _check_exposure(self, req: OrderRequest) -> Optional[tuple[RejectionReason, str]]:
        if self._exposure.open_positions >= self._config.max_open_positions:
            return (
                RejectionReason.EXPOSURE_LIMIT_HIT,
                f"open positions {self._exposure.open_positions} >= max {self._config.max_open_positions}",
            )

        cv = req.notional_per_lot or self._config.contract_value
        # Estimate worst-case new notional (use max plausible qty = 10% of equity / min-stop)
        gross_pct = self._exposure.gross_exposure / max(self._equity, Decimal("1"))
        if gross_pct >= self._config.max_gross_exposure_pct:
            return (
                RejectionReason.EXPOSURE_LIMIT_HIT,
                f"gross exposure {gross_pct:.2%} >= max {self._config.max_gross_exposure_pct:.2%}",
            )

        net_pct = abs(self._exposure.net_exposure) / max(self._equity, Decimal("1"))
        if net_pct >= self._config.max_net_exposure_pct:
            return (
                RejectionReason.EXPOSURE_LIMIT_HIT,
                f"net exposure {net_pct:.2%} >= max {self._config.max_net_exposure_pct:.2%}",
            )

        return None

    def _check_consecutive_loss_hard(self) -> Optional[tuple[RejectionReason, str]]:
        n = self._consec.losses_in_row
        if n >= self._config.consec_loss_hard_limit:
            return (
                RejectionReason.CONSECUTIVE_LOSS_HALT,
                f"{n} consecutive losses >= hard limit {self._config.consec_loss_hard_limit}",
            )
        return None

    def _check_consecutive_loss_auto(self) -> None:
        """Automatically adjust sizing mode after soft/hard consecutive-loss thresholds."""
        n = self._consec.losses_in_row
        if n >= self._config.consec_loss_hard_limit:
            logger.warning(
                "CONSEC_LOSS_HARD_LIMIT | %d losses — new entries blocked", n
            )
        elif n >= self._config.consec_loss_soft_limit:
            self._sizing_mode = SizingMode.REDUCED
            logger.warning(
                "CONSEC_LOSS_SOFT_LIMIT | %d losses — switching to reduced sizing", n
            )

    def _check_emergency(self) -> None:
        """Auto-trigger emergency shutdown on extreme conditions."""
        if self._state == EngineState.EMERGENCY:
            return

        eq = self._equity
        if eq <= Decimal("0"):
            self.emergency_shutdown("equity <= 0")
            return

        # Emergency daily loss
        daily_loss_pct = -self._pnl.daily_total / eq
        if daily_loss_pct >= self._config.emergency_daily_loss_pct:
            self.emergency_shutdown(
                f"emergency_daily_loss {daily_loss_pct:.2%} >= {self._config.emergency_daily_loss_pct:.2%}"
            )
            return

        # Emergency drawdown
        dd = self._drawdown_pct()
        if dd >= self._config.emergency_drawdown_pct:
            self.emergency_shutdown(
                f"emergency_drawdown {dd:.2%} >= {self._config.emergency_drawdown_pct:.2%}"
            )
            return

        # Emergency consecutive losses
        if self._consec.losses_in_row >= self._config.emergency_consec_losses:
            self.emergency_shutdown(
                f"emergency_consec_losses {self._consec.losses_in_row} >= {self._config.emergency_consec_losses}"
            )

    def _maybe_lift_drawdown_halt(self) -> None:
        """Attempt to lift drawdown circuit breaker after cooldown + recovery."""
        dd = self._drawdown_pct()
        cooldown_done = self._cb_bars_elapsed >= self._config.drawdown_cooldown_bars
        recovered = dd <= self._config.drawdown_resume_pct

        if cooldown_done and recovered:
            self._state = EngineState.ACTIVE
            logger.info(
                "drawdown_cb_lifted | dd=%.2f%% bars_elapsed=%d",
                float(dd * 100), self._cb_bars_elapsed,
            )

    # ──────────────────────────────────────────────────────────────────────
    # Internal — sizing
    # ──────────────────────────────────────────────────────────────────────

    def _compute_size(
        self, req: OrderRequest
    ) -> tuple[Decimal, SizingMode, Decimal]:
        """
        Returns (quantity, sizing_mode, risk_amount).

        Sizing hierarchy (first matching rule wins):
          1. Consecutive-loss soft limit    → REDUCED (0.5% risk)
          2. Volatility-adjusted mode       → vol-target sizing
          3. Default                        → fixed fractional (1% risk)
        """
        contract_value = req.notional_per_lot or self._config.contract_value

        # ── Determine risk %
        is_reduced = (
            self._sizing_mode == SizingMode.REDUCED
            or self._consec.losses_in_row >= self._config.consec_loss_soft_limit
        )
        risk_pct = self._config.risk_pct_reduced if is_reduced else self._config.risk_pct_normal
        mode     = SizingMode.REDUCED if is_reduced else SizingMode.FIXED_FRACTIONAL

        # ── Fixed fractional base size
        dollar_risk = self._equity * risk_pct
        # qty = $ risk / (stop_distance × contract_value)
        qty_ff = dollar_risk / (req.stop_distance * contract_value)

        # ── Volatility-adjusted override (if ATR available)
        if self._sizing_mode == SizingMode.VOLATILITY_ADJ and req.atr > Decimal("0"):
            # Vol-target: size so that 1-day ATR move = vol_target% of equity / annualise_factor
            daily_vol_target = self._equity * self._config.vol_target_pct / self._config.annualise_factor
            # ATR as a price move per lot
            qty_va = daily_vol_target / (req.atr * contract_value)
            qty    = min(qty_ff, qty_va)    # take the more conservative of the two
            mode   = SizingMode.VOLATILITY_ADJ
        else:
            qty = qty_ff

        # ── Round down to lot precision
        factor = Decimal("10") ** self._config.lot_precision
        qty    = (qty * factor).to_integral_value(rounding=ROUND_DOWN) / factor

        # ── Emergency minimum guard
        if self._consec.losses_in_row >= self._config.consec_loss_soft_limit:
            mode = SizingMode.EMERGENCY_MIN if qty < self._config.min_lot else SizingMode.REDUCED

        risk_amount = qty * req.stop_distance * contract_value
        return qty, mode, risk_amount

    # ──────────────────────────────────────────────────────────────────────
    # Internal — window management
    # ──────────────────────────────────────────────────────────────────────

    def _maybe_roll_windows(self, force_date: Optional[date] = None) -> None:
        today = force_date or datetime.now(timezone.utc).date()
        iso_week = today.isocalendar()[1]

        # Roll daily
        if self._current_date != today:
            if self._current_date is not None:
                # Lift daily halt on new session
                if self._state == EngineState.DAILY_HALTED:
                    self._state = EngineState.ACTIVE
                    logger.info("daily_halt_lifted | new session %s", today)
            self._pnl.daily_realized = Decimal("0")
            self._pnl.daily_open     = Decimal("0")
            self._current_date       = today

        # Roll weekly (ISO Monday)
        if self._current_week != iso_week:
            if self._current_week is not None:
                if self._state == EngineState.WEEKLY_HALTED:
                    self._state = EngineState.ACTIVE
                    logger.info("weekly_halt_lifted | new week %d", iso_week)
            self._pnl.weekly_realized = Decimal("0")
            self._pnl.weekly_open     = Decimal("0")
            self._current_week        = iso_week

    # ──────────────────────────────────────────────────────────────────────
    # Internal — helpers
    # ──────────────────────────────────────────────────────────────────────

    def _drawdown_pct(self) -> Decimal:
        if self._peak_eq <= Decimal("0"):
            return Decimal("0")
        return (self._peak_eq - self._equity) / self._peak_eq

    def _snapshot(self, last_rejection: RejectionReason, detail: str) -> RiskSnapshot:
        eq = max(self._equity, Decimal("1"))  # avoid division by zero
        daily_pnl  = self._pnl.daily_total
        weekly_pnl = self._pnl.weekly_total

        return RiskSnapshot(
            timestamp       = datetime.now(timezone.utc),
            state           = self._state,
            sizing_mode     = self._sizing_mode,
            equity          = self._equity,
            peak_equity     = self._peak_eq,
            drawdown_pct    = self._drawdown_pct(),
            daily_pnl       = daily_pnl,
            weekly_pnl      = weekly_pnl,
            daily_limit_pct = (-daily_pnl / eq) / self._config.daily_loss_limit_pct
                              if daily_pnl < 0 else Decimal("0"),
            weekly_limit_pct= (-weekly_pnl / eq) / self._config.weekly_loss_limit_pct
                              if weekly_pnl < 0 else Decimal("0"),
            gross_exposure_pct  = self._exposure.gross_exposure / eq,
            net_exposure_pct    = abs(self._exposure.net_exposure) / eq,
            open_positions      = self._exposure.open_positions,
            consecutive_losses  = self._consec.losses_in_row,
            consecutive_wins    = self._consec.wins_in_row,
            drawdown_cooldown_bars_remaining = max(
                0, self._config.drawdown_cooldown_bars - self._cb_bars_elapsed
            ),
            last_rejection        = last_rejection,
            last_rejection_detail = detail,
        )

    def _reject(
        self,
        reason: RejectionReason,
        detail: str,
        req: OrderRequest,
    ) -> OrderDecision:
        logger.warning(
            "ORDER_REJECTED | sym=%s side=%s reason=%s | %s",
            req.symbol, req.side, reason.value, detail,
        )
        return OrderDecision(
            approved          = False,
            rejection_reason  = reason,
            rejection_detail  = detail,
            quantity          = Decimal("0"),
            risk_amount       = Decimal("0"),
            sizing_mode       = self._sizing_mode,
            snapshot          = self._snapshot(reason, detail),
        )
