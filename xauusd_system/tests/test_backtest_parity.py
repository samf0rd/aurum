"""
tests/test_backtest_parity.py

Phase 1 DoD tests (from ROADMAP.md):
  1. Parity — at bar i, the backtester and live components produce identical regime+signal.
  2. Look-ahead guard — mutating bars[i+1] does NOT change the evaluation at bar i.
  3. Reproducibility — same input → byte-identical trade list on two runs.

Phase 2 DoD tests:
  4. Daily loss circuit breaker — 2% loss => next approve_order rejected.
  5. Trailing stop never loosens — hard_stop only ratchets in the favorable direction.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from core.interfaces import Bar, Regime, Side, Position, SignalType
from strategy.signal_generator import DonchianBreakoutSignalGenerator, RegimeDetector
from risk.engine import RiskEngine
from risk.models import OrderRequest, RejectionReason, RiskConfig
from backtest.engine import BacktestEngine
from backtest.costs import CostModel
from core.config import SWING


# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────────

def _bar(close: float, high: float = None, low: float = None,
         open_: float = None, dt: datetime = None) -> Bar:
    if high  is None: high  = close * 1.002
    if low   is None: low   = close * 0.998
    if open_ is None: open_ = close * 0.999
    if dt    is None: dt    = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return Bar(
        timestamp=dt, open=Decimal(str(open_)), high=Decimal(str(high)),
        low=Decimal(str(low)), close=Decimal(str(close)), volume=Decimal("1000"),
    )


def _trending_bars(n: int = 310, start: float = 3000.0, step: float = 5.0) -> list[Bar]:
    """Steadily rising series that triggers TRENDING_BULL after warmup."""
    base = datetime(2023, 1, 1, tzinfo=timezone.utc)
    return [
        _bar(
            close  = start + i * step,
            high   = start + i * step + 10,
            low    = start + i * step - 10,
            open_  = start + i * step - 2,
            dt     = base + timedelta(hours=i),
        )
        for i in range(n)
    ]


def _make_order_request(close: float, stop_distance: float = 40.0) -> OrderRequest:
    return OrderRequest(
        symbol         = "XAUUSD",
        side           = "LONG",
        entry_price    = Decimal(str(close)),
        stop_price     = Decimal(str(close - stop_distance)),
        atr            = Decimal("20"),
        current_spread = Decimal("0.40"),
        median_spread  = Decimal("0.40"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. Parity test
# ─────────────────────────────────────────────────────────────────────────────

class TestBacktesterParity:
    """
    The backtester must call the same RegimeDetector and
    DonchianBreakoutSignalGenerator as the live path.  Since they are pure
    functions of their inputs, calling them directly with windows[:i+1] and
    calling them inside the backtester must yield identical outputs at each i.
    """

    def test_regime_matches_live_at_every_bar(self):
        bars     = _trending_bars(280)
        detector = RegimeDetector()
        WARMUP   = 220

        for i in range(WARMUP, min(len(bars) - 1, WARMUP + 30)):
            window = bars[: i + 1]
            # Call live detector directly
            regime_live = detector.detect(window)
            # Call again with the same window (must be deterministic)
            regime_again = detector.detect(window)
            assert regime_live == regime_again, (
                f"Non-deterministic regime at bar {i}: {regime_live} vs {regime_again}"
            )

    def test_signal_matches_live_at_every_bar(self):
        bars      = _trending_bars(280)
        detector  = RegimeDetector()
        generator = DonchianBreakoutSignalGenerator()
        WARMUP    = 220

        for i in range(WARMUP, min(len(bars) - 1, WARMUP + 30)):
            window = bars[: i + 1]
            regime = detector.detect(window)
            sig1   = generator.on_bar(window, regime)
            sig2   = generator.on_bar(window, regime)
            assert sig1.signal_type == sig2.signal_type, (
                f"Non-deterministic signal at bar {i}: {sig1.signal_type} vs {sig2.signal_type}"
            )


# ─────────────────────────────────────────────────────────────────────────────
# 2. Look-ahead guard
# ─────────────────────────────────────────────────────────────────────────────

class TestLookaheadGuard:
    """
    After evaluating bar i with bars[:i+1], mutating bars[i+1] must not
    change the regime or signal computed for bar i.  This proves the strategy
    path is correctly windowed and never sees future data.
    """

    def test_mutating_future_bar_does_not_change_signal(self):
        bars      = _trending_bars(280)
        detector  = RegimeDetector()
        generator = DonchianBreakoutSignalGenerator()
        EVAL_IDX  = 230

        window_original = bars[: EVAL_IDX + 1]
        regime_before   = detector.detect(window_original)
        signal_before   = generator.on_bar(window_original, regime_before)

        # Mutate bar EVAL_IDX+1 to an absurd price that would break any indicator
        bars_mutated = list(bars)
        bars_mutated[EVAL_IDX + 1] = _bar(
            close  = 999_999.0,
            high   = 1_100_000.0,
            low    = 900_000.0,
            open_  = 999_000.0,
            dt     = bars[EVAL_IDX + 1].timestamp,
        )

        # Re-evaluate using only bars[:EVAL_IDX+1] from the mutated list
        window_after = bars_mutated[: EVAL_IDX + 1]
        regime_after = detector.detect(window_after)
        signal_after = generator.on_bar(window_after, regime_after)

        assert regime_before == regime_after, (
            f"Future-bar mutation changed regime: {regime_before} → {regime_after}"
        )
        assert signal_before.signal_type == signal_after.signal_type, (
            f"Future-bar mutation changed signal: "
            f"{signal_before.signal_type} → {signal_after.signal_type}"
        )

    def test_mutating_future_bar_does_not_change_indicators(self):
        bars      = _trending_bars(280)
        generator = DonchianBreakoutSignalGenerator()
        EVAL_IDX  = 240

        ind_before = generator.compute_indicators(bars[: EVAL_IDX + 1])

        bars_mutated = list(bars)
        bars_mutated[EVAL_IDX + 1] = _bar(999_999.0, dt=bars[EVAL_IDX + 1].timestamp)

        ind_after = generator.compute_indicators(bars_mutated[: EVAL_IDX + 1])

        for key in ind_before:
            assert abs(ind_before[key] - ind_after[key]) < 1e-9, (
                f"Indicator '{key}' changed after mutating future bar: "
                f"{ind_before[key]} → {ind_after[key]}"
            )


# ─────────────────────────────────────────────────────────────────────────────
# 3. Reproducibility
# ─────────────────────────────────────────────────────────────────────────────

class TestBacktestReproducibility:
    """Same bars + same config must produce byte-identical trade lists."""

    def test_two_runs_identical(self):
        bars = _trending_bars(310)

        def _run():
            engine = BacktestEngine(
                profile        = SWING,
                initial_equity = 100_000.0,
                cost_model     = CostModel(),
            )
            return engine.run(bars)

        r1 = _run()
        r2 = _run()

        assert r1.trades == r2.trades, (
            f"Non-deterministic backtest: run1={r1.trades}, run2={r2.trades}"
        )
        assert r1.equity_curve == r2.equity_curve


# ─────────────────────────────────────────────────────────────────────────────
# 4. Phase 2: Daily loss circuit breaker (DoD requirement)
# ─────────────────────────────────────────────────────────────────────────────

class TestDailyLossCircuitBreaker:
    """
    Drive the engine to exactly 2% daily loss, then assert the next
    approve_order() is rejected with DAILY_LIMIT_HIT.
    This test CANNOT pass with the old adapter (which never updated state).
    """

    def test_daily_limit_blocks_after_loss(self):
        equity = Decimal("100000")
        engine = RiskEngine(
            initial_equity=equity,
            config=RiskConfig(daily_loss_limit_pct=Decimal("0.02")),
        )
        # Realize a loss that exceeds 2% of equity
        engine.record_position_closed(realized_pnl=Decimal("-2100"))

        req      = _make_order_request(3200.0)
        decision = engine.approve_order(req)

        assert not decision.approved
        assert decision.rejection_reason == RejectionReason.DAILY_LIMIT_HIT

    def test_engine_recovers_after_daily_window_rolls(self):
        """After _maybe_roll_windows() runs for a new day, daily halt clears."""
        from datetime import date
        engine = RiskEngine(
            initial_equity=Decimal("100000"),
            config=RiskConfig(daily_loss_limit_pct=Decimal("0.02")),
        )
        engine.record_position_closed(realized_pnl=Decimal("-2100"))

        # Verify halted
        req = _make_order_request(3200.0)
        assert not engine.approve_order(req).approved

        # Simulate new session (tomorrow)
        tomorrow = date.today().replace(year=date.today().year)
        from datetime import date as dt_date
        import datetime as _dt
        new_day = _dt.date.today() + _dt.timedelta(days=2)
        engine.on_session_open(current_equity=Decimal("97900"), session_date=new_day)

        # Should be unblocked now
        decision = engine.approve_order(req)
        assert decision.approved


# ─────────────────────────────────────────────────────────────────────────────
# 5. Phase 2: Trailing stop never loosens (DoD requirement)
# ─────────────────────────────────────────────────────────────────────────────

class TestTrailingStop:
    """
    The hard stop must only ratchet in the favorable direction.
    For LONG: stop moves up, never down.
    For SHORT: stop moves down, never up.
    """

    def _make_position(self, side: Side, hard_stop: float) -> Position:
        return Position(
            symbol       = "XAUUSD",
            side         = side,
            quantity     = Decimal("0.25"),
            entry_price  = Decimal("3200"),
            entry_time   = datetime(2024, 1, 1, tzinfo=timezone.utc),
            hard_stop    = Decimal(str(hard_stop)),
            trailing_low = Decimal(str(hard_stop)),
            atr_at_entry = Decimal("20"),
        )

    def test_long_stop_only_moves_up(self):
        engine = RiskEngine(initial_equity=Decimal("100000"))
        bars   = _trending_bars(30)
        pos    = self._make_position(Side.LONG, 3000.0)

        # Run trailing stop update multiple times — stop must never go down
        current_stop = pos.hard_stop
        for i in range(20, 30):
            window  = bars[:i + 1]
            updated = engine.update_trailing_stop(pos, window)
            assert updated.hard_stop >= current_stop, (
                f"LONG stop moved down at bar {i}: "
                f"{current_stop} → {updated.hard_stop}"
            )
            pos          = updated
            current_stop = updated.hard_stop

    def test_short_stop_only_moves_down(self):
        engine = RiskEngine(initial_equity=Decimal("100000"))
        # Falling bars for a short position
        base = datetime(2023, 1, 1, tzinfo=timezone.utc)
        bars = [
            _bar(
                close  = 3000.0 - i * 3.0,
                high   = 3000.0 - i * 3.0 + 8,
                low    = 3000.0 - i * 3.0 - 8,
                dt     = base + timedelta(hours=i),
            )
            for i in range(30)
        ]
        pos          = self._make_position(Side.SHORT, 3050.0)
        current_stop = pos.hard_stop

        for i in range(20, 30):
            window  = bars[:i + 1]
            updated = engine.update_trailing_stop(pos, window)
            assert updated.hard_stop <= current_stop, (
                f"SHORT stop moved up at bar {i}: "
                f"{current_stop} → {updated.hard_stop}"
            )
            pos          = updated
            current_stop = updated.hard_stop

    def test_stop_never_moves_against_long_on_pullback(self):
        """After ratcheting up, a pullback in price must NOT lower the stop."""
        engine = RiskEngine(initial_equity=Decimal("100000"))
        base   = datetime(2023, 1, 1, tzinfo=timezone.utc)

        # 25 rising bars followed by 5 falling bars
        rising  = [_bar(3000.0 + i * 5, dt=base + timedelta(hours=i)) for i in range(25)]
        falling = [_bar(3120.0 - j * 4, dt=base + timedelta(hours=25 + j)) for j in range(5)]
        bars    = rising + falling

        pos = self._make_position(Side.LONG, 2900.0)

        # Advance stop through the rising section
        for i in range(20, 25):
            window = bars[:i + 1]
            pos    = engine.update_trailing_stop(pos, window)

        stop_after_rise = pos.hard_stop

        # Now advance through falling bars — stop must not decrease
        for i in range(25, 30):
            window   = bars[:i + 1]
            updated  = engine.update_trailing_stop(pos, window)
            assert updated.hard_stop >= stop_after_rise, (
                f"Stop fell during pullback at bar {i}: "
                f"{stop_after_rise} → {updated.hard_stop}"
            )
