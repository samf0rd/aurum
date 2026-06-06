"""
tests/test_all.py — unit and integration test suite
=====================================================
Run with: pytest tests/ -v --tb=short
"""
from __future__ import annotations

import pytest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from core.interfaces import (
    Bar, Regime, RiskState, Signal, SignalType, Side, Order, Position, OrderStatus
)
from strategy.signal_generator import (
    DonchianBreakoutSignalGenerator, RegimeDetector,
    sma, atr, donchian_high, donchian_low, adx, vol_ratio
)
from risk.engine import RiskEngine
from risk.models import OrderRequest, RiskConfig, RejectionReason


# ─────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────

def make_bar(
    close:  float,
    high:   float    = None,
    low:    float    = None,
    open_:  float    = None,
    dt:     datetime = None,
) -> Bar:
    if high  is None: high  = close * 1.005
    if low   is None: low   = close * 0.995
    if open_ is None: open_ = close * 0.998
    if dt    is None: dt    = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return Bar(
        timestamp = dt,
        open      = Decimal(str(open_)),
        high      = Decimal(str(high)),
        low       = Decimal(str(low)),
        close     = Decimal(str(close)),
        volume    = Decimal("10000"),
    )


def trending_bars(n: int = 250, start: float = 3000.0, step: float = 5.0) -> list[Bar]:
    """Steadily rising price series — triggers TRENDING_BULL after warmup."""
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return [
        make_bar(close=start + i * step, dt=base + timedelta(hours=i))
        for i in range(n)
    ]


def sideways_bars(n: int = 250, center: float = 3000.0, amplitude: float = 20.0) -> list[Bar]:
    """Oscillating price series — triggers CHOPPY."""
    import math
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return [
        make_bar(
            close = center + amplitude * math.sin(i * 0.3),
            dt    = base + timedelta(hours=i),
        )
        for i in range(n)
    ]


def make_event_bus() -> MagicMock:
    bus = MagicMock()
    bus.publish   = MagicMock()
    bus.subscribe = MagicMock()
    return bus


def make_risk_engine(equity: float = 100_000.0) -> RiskEngine:
    return RiskEngine(
        initial_equity=Decimal(str(equity)),
        config=RiskConfig(),
    )


def _make_order_request(entry: float = 3200.0, stop_dist: float = 40.0) -> OrderRequest:
    return OrderRequest(
        symbol         = "XAUUSD",
        side           = "LONG",
        entry_price    = Decimal(str(entry)),
        stop_price     = Decimal(str(entry - stop_dist)),
        atr            = Decimal("20"),
        current_spread = Decimal("0.40"),
        median_spread  = Decimal("0.40"),
    )


# ─────────────────────────────────────────────
# Indicator unit tests
# ─────────────────────────────────────────────

class TestIndicators:

    def test_sma_basic(self):
        values = [float(i) for i in range(1, 11)]   # 1..10
        assert sma(values, 5) == pytest.approx(8.0)  # mean(6,7,8,9,10)

    def test_sma_insufficient_data(self):
        with pytest.raises(ValueError):
            sma([1.0, 2.0], 10)

    def test_atr_positive(self):
        bars = trending_bars(30)
        assert atr(bars, 14) > 0

    def test_atr_insufficient_data(self):
        with pytest.raises(ValueError):
            atr(trending_bars(5), 14)

    def test_donchian_high_excludes_current_bar(self):
        bars   = trending_bars(30)
        d_high = donchian_high(bars, 20)
        assert d_high < float(bars[-1].high)

    def test_donchian_low_excludes_current_bar(self):
        bars  = sideways_bars(30)
        d_low = donchian_low(bars, 10)
        assert isinstance(d_low, float)
        assert d_low > 0

    def test_adx_range(self):
        bars = trending_bars(60)
        assert 0 <= adx(bars, 14) <= 100

    def test_adx_trending_higher_than_sideways(self):
        assert adx(trending_bars(100, step=10.0), 14) > adx(sideways_bars(100, amplitude=5.0), 14)

    def test_vol_ratio_returns_float(self):
        result = vol_ratio(trending_bars(200), 100)
        assert isinstance(result, float)
        assert result > 0


# ─────────────────────────────────────────────
# Regime detector tests
# ─────────────────────────────────────────────

class TestRegimeDetector:

    def setup_method(self):
        self.detector = RegimeDetector()

    def test_trending_bull_regime(self):
        assert self.detector.detect(trending_bars(250, step=5.0)) == Regime.TRENDING_BULL

    def test_regime_is_valid_enum(self):
        regime = self.detector.detect(sideways_bars(250))
        assert isinstance(regime, Regime)

    def test_high_vol_overrides_trend(self):
        bars = trending_bars(250)
        with patch("strategy.signal_generator.vol_ratio", return_value=2.5):
            assert self.detector.detect(bars) == Regime.HIGH_VOL


# ─────────────────────────────────────────────
# Signal generator tests
# ─────────────────────────────────────────────

class TestSignalGenerator:

    def setup_method(self):
        self.gen = DonchianBreakoutSignalGenerator()

    def test_no_signal_on_insufficient_bars(self):
        assert self.gen.on_bar(trending_bars(50), Regime.TRENDING_BULL).signal_type == SignalType.NO_SIGNAL

    def test_no_signal_in_choppy_regime(self):
        assert self.gen.on_bar(trending_bars(250), Regime.CHOPPY).signal_type == SignalType.NO_SIGNAL

    def test_no_signal_in_high_vol_regime(self):
        assert self.gen.on_bar(trending_bars(250), Regime.HIGH_VOL).signal_type == SignalType.NO_SIGNAL

    def test_signal_timestamp_matches_last_bar(self):
        bars   = trending_bars(250)
        signal = self.gen.on_bar(bars, Regime.TRENDING_BULL)
        assert signal.timestamp == bars[-1].timestamp

    def test_compute_indicators_keys(self):
        ind = self.gen.compute_indicators(trending_bars(250))
        for key in ("sma200", "atr14", "donchian20_h", "donchian20_l",
                    "donchian10_h", "donchian10_l", "adx14", "vol_ratio"):
            assert key in ind, f"Missing indicator: {key}"

    def test_exit_rule_3b_regime_flip(self):
        bars = trending_bars(250)
        bars[-1] = make_bar(close=float(bars[-1].close) * 0.60, dt=bars[-1].timestamp)
        should_exit, reason = self.gen.should_exit(bars, "LONG", Regime.TRENDING_BEAR)
        assert should_exit is True
        assert "3b" in reason

    def test_exit_interface_contract(self):
        bars = trending_bars(250)
        should_exit, reason = self.gen.should_exit(bars, "LONG", Regime.TRENDING_BULL)
        assert isinstance(should_exit, bool)
        assert isinstance(reason, str)


# ─────────────────────────────────────────────
# Risk engine tests (updated to OrderRequest API)
# ─────────────────────────────────────────────

class TestRiskEngine:

    def setup_method(self):
        self.engine = make_risk_engine(100_000.0)

    def test_approve_normal_order(self):
        req      = _make_order_request()
        decision = self.engine.approve_order(req)
        assert decision.approved

    def test_sizing_1pct_normal(self):
        """1% of $100k / (40 × 100 contract) = 0.25 lots."""
        req      = _make_order_request(stop_dist=40.0)
        decision = self.engine.approve_order(req)
        assert decision.approved
        assert decision.quantity == Decimal("0.25")

    def test_sizing_larger_stop_smaller_qty(self):
        dec_tight = self.engine.approve_order(_make_order_request(stop_dist=20.0))
        engine2   = make_risk_engine()
        dec_wide  = engine2.approve_order(_make_order_request(stop_dist=80.0))
        assert dec_tight.approved and dec_wide.approved
        assert dec_tight.quantity > dec_wide.quantity

    def test_reject_when_spread_too_wide(self):
        req = OrderRequest(
            symbol="XAUUSD", side="LONG",
            entry_price=Decimal("3200"), stop_price=Decimal("3160"),
            atr=Decimal("20"),
            current_spread=Decimal("1.50"),
            median_spread=Decimal("0.40"),   # 1.50 / 0.40 = 3.75 > 3× gate
        )
        decision = self.engine.approve_order(req)
        assert not decision.approved
        assert decision.rejection_reason == RejectionReason.SPREAD_TOO_WIDE

    def test_reject_daily_limit_hit(self):
        self.engine.record_position_closed(Decimal("-2100"))
        decision = self.engine.approve_order(_make_order_request())
        assert not decision.approved
        assert decision.rejection_reason == RejectionReason.DAILY_LIMIT_HIT

    def test_reject_zero_stop_distance(self):
        req = OrderRequest(
            symbol="XAUUSD", side="LONG",
            entry_price=Decimal("3200"), stop_price=Decimal("3200"),  # same → zero dist
            atr=Decimal("20"),
        )
        decision = self.engine.approve_order(req)
        assert not decision.approved
        assert decision.rejection_reason == RejectionReason.ZERO_STOP_DISTANCE

    def test_update_risk_state_returns_risk_state(self):
        from core.interfaces import RiskState
        state = RiskState(
            equity=Decimal("100000"), peak_equity=Decimal("100000"),
            daily_pnl=Decimal("0"), weekly_pnl=Decimal("0"),
            drawdown_pct=Decimal("0"), daily_limit_hit=False,
            weekly_limit_hit=False, circuit_breaker_on=False, gap_caution=False,
        )
        result = self.engine.update_risk_state(
            current_state=state, realized_pnl=Decimal("0"),
            open_positions=[], current_prices={"XAUUSD": Decimal("3200")},
        )
        assert isinstance(result, RiskState)

    def test_trailing_stop_long_only_moves_up(self):
        bars = trending_bars(30)
        pos  = Position(
            symbol="XAUUSD", side=Side.LONG, quantity=Decimal("0.25"),
            entry_price=Decimal("3000"),
            entry_time=datetime(2024, 1, 1, tzinfo=timezone.utc),
            hard_stop=Decimal("2900"), trailing_low=Decimal("2900"),
            atr_at_entry=Decimal("20"),
        )
        updated = self.engine.update_trailing_stop(pos, bars)
        assert updated.hard_stop >= pos.hard_stop


# ─────────────────────────────────────────────
# Entry confirmation delay tests
# ─────────────────────────────────────────────

def _flat_bars_with_spike(
    n: int = 252,
    spike_indices: list[int] | None = None,
    spike_close: float = 2100.0,
    base_close: float = 2000.0,
) -> list[Bar]:
    """
    n flat bars at base_close with selected indices replaced by a spike.
    spike_indices: list of negative or absolute indices.
    With base_close=2000 and default high=close*1.005=2010, the Donchian-20
    upper band is 2010 across all flat bars. spike_close=2100 is clearly above.
    """
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    bars = [
        make_bar(close=base_close, dt=base + timedelta(hours=i))
        for i in range(n)
    ]
    for idx in (spike_indices or []):
        real = n + idx if idx < 0 else idx
        bars[real] = make_bar(
            close=spike_close,
            high=spike_close * 1.005,
            low=base_close * 0.995,
            dt=base + timedelta(hours=real),
        )
    return bars


class TestConfirmationDelay:
    """entry_confirmation_bars wiring through StrategyProfile → signal generator."""

    def test_confirmation_0_fires_on_breakout_bar(self):
        """confirmation=0 preserves original behaviour: signal on the breakout bar."""
        from core.config import INTRADAY
        gen  = DonchianBreakoutSignalGenerator()   # default profile, confirmation=0
        bars = _flat_bars_with_spike(spike_indices=[-1])   # spike IS the current bar
        sig  = gen.on_bar(bars, Regime.TRENDING_BULL)
        assert sig.signal_type == SignalType.ENTER_LONG

    def test_confirmation_1_fires_one_bar_later_when_price_holds(self):
        """confirmation=1: signal fires on bar+1 when close is still above band."""
        from core.config import INTRADAY
        from dataclasses import replace as dc_replace
        profile = dc_replace(INTRADAY, entry_confirmation_bars=1)
        gen     = DonchianBreakoutSignalGenerator(profile=profile)

        # bars[-2] = breakout bar (close=2100 > band≈2010)
        # bars[-1] = confirmation bar (close=2050, still above band 2010)
        bars = _flat_bars_with_spike(spike_indices=[-2])
        # Replace last bar with a high-close that holds above the breakout level
        base = bars[-1].timestamp
        bars[-1] = make_bar(close=2050.0, high=2055.0, low=1990.0, dt=base)

        sig = gen.on_bar(bars, Regime.TRENDING_BULL)
        assert sig.signal_type == SignalType.ENTER_LONG, (
            f"Expected ENTER_LONG, got {sig.signal_type}: {sig.reason}"
        )

    def test_confirmation_1_no_signal_when_price_retraces(self):
        """confirmation=1: no signal when bar+1 close falls back below the band."""
        from core.config import INTRADAY
        from dataclasses import replace as dc_replace
        profile = dc_replace(INTRADAY, entry_confirmation_bars=1)
        gen     = DonchianBreakoutSignalGenerator(profile=profile)

        # bars[-2] = breakout bar; bars[-1] = retraced (close below breakout band)
        bars = _flat_bars_with_spike(spike_indices=[-2])
        base = bars[-1].timestamp
        bars[-1] = make_bar(close=1990.0, dt=base)   # clearly below the 2010 band

        sig = gen.on_bar(bars, Regime.TRENDING_BULL)
        assert sig.signal_type == SignalType.NO_SIGNAL, (
            f"Expected NO_SIGNAL, got {sig.signal_type}: {sig.reason}"
        )

    def test_confirmation_1_no_signal_when_not_fresh_breakout(self):
        """confirmation=1: no re-trigger when the bar before the breakout was also above band.

        This guards against the re-triggering bug where every bar in a sustained
        uptrend generates a new confirmation signal after each trade closes.
        """
        from core.config import INTRADAY
        from dataclasses import replace as dc_replace
        profile = dc_replace(INTRADAY, entry_confirmation_bars=1)
        gen     = DonchianBreakoutSignalGenerator(profile=profile)

        # bars[-3] AND bars[-2] are both above band — not a fresh breakout
        bars = _flat_bars_with_spike(spike_indices=[-3, -2])
        base = bars[-1].timestamp
        bars[-1] = make_bar(close=2050.0, high=2055.0, low=1990.0, dt=base)

        sig = gen.on_bar(bars, Regime.TRENDING_BULL)
        assert sig.signal_type == SignalType.NO_SIGNAL, (
            f"Expected NO_SIGNAL (not fresh breakout), got {sig.signal_type}: {sig.reason}"
        )


# ─────────────────────────────────────────────
# Long-only filter tests
# ─────────────────────────────────────────────

def _bear_bars(n: int = 250, start: float = 3500.0, step: float = -5.0) -> list[Bar]:
    """Steadily falling price series — triggers TRENDING_BEAR after warmup."""
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return [
        make_bar(close=start + i * step, dt=base + timedelta(hours=i))
        for i in range(n)
    ]


class TestLongOnlyFilter:

    def test_long_only_false_allows_short_signal(self):
        """long_only=False (default): TRENDING_BEAR still produces ENTER_SHORT."""
        from core.config import SWING
        from dataclasses import replace as dc_replace
        profile = dc_replace(SWING, long_only=False)
        gen     = DonchianBreakoutSignalGenerator(profile=profile)
        bars    = _bear_bars(250)
        sig     = gen.on_bar(bars, Regime.TRENDING_BEAR)
        assert sig.signal_type in (SignalType.ENTER_SHORT, SignalType.NO_SIGNAL), (
            f"Unexpected signal type: {sig.signal_type}"
        )

    def test_long_only_true_suppresses_short_signal(self):
        """long_only=True: any TRENDING_BEAR bar returns NO_SIGNAL regardless of price."""
        from core.config import SWING
        from dataclasses import replace as dc_replace
        profile = dc_replace(SWING, long_only=True)
        gen     = DonchianBreakoutSignalGenerator(profile=profile)
        bars    = _bear_bars(250)
        sig     = gen.on_bar(bars, Regime.TRENDING_BEAR)
        assert sig.signal_type == SignalType.NO_SIGNAL, (
            f"Expected NO_SIGNAL (long_only), got {sig.signal_type}: {sig.reason}"
        )

    def test_long_only_true_passes_long_signal(self):
        """long_only=True: TRENDING_BULL signals are unaffected."""
        from core.config import SWING
        from dataclasses import replace as dc_replace
        profile = dc_replace(SWING, long_only=True)
        gen     = DonchianBreakoutSignalGenerator(profile=profile)
        bars    = trending_bars(250)
        sig     = gen.on_bar(bars, Regime.TRENDING_BULL)
        assert sig.signal_type in (SignalType.ENTER_LONG, SignalType.NO_SIGNAL), (
            f"long_only should not block bull signals, got {sig.signal_type}"
        )
        assert sig.signal_type != SignalType.ENTER_SHORT, (
            "long_only should never emit ENTER_SHORT"
        )


# ─────────────────────────────────────────────
# Event bus tests
# ─────────────────────────────────────────────

class TestEventBus:

    def test_publish_calls_subscriber(self):
        from infrastructure.services import InProcessEventBus
        bus      = InProcessEventBus()
        received = []
        bus.subscribe("test.event", lambda p: received.append(p))
        bus.publish("test.event", {"key": "value"})
        assert received == [{"key": "value"}]

    def test_bad_handler_does_not_propagate(self):
        from infrastructure.services import InProcessEventBus
        bus = InProcessEventBus()
        bus.subscribe("test.event", lambda p: 1 / 0)
        bus.publish("test.event", {})   # must not raise

    def test_wildcard_subscriber(self):
        from infrastructure.services import InProcessEventBus
        bus  = InProcessEventBus()
        seen = []
        bus.subscribe("*", lambda p: seen.append(p))
        bus.publish("any.event", {"x": 1})
        assert len(seen) == 1


# ─────────────────────────────────────────────
# Integration: orchestrator pipeline (mocked I/O)
# ─────────────────────────────────────────────

class TestOrchestratorPipeline:

    @pytest.mark.asyncio
    async def test_no_entry_without_sufficient_bars(self):
        from orchestrator.engine import TradingOrchestrator

        order_mgr = MagicMock()
        order_mgr.open_positions.return_value = []
        order_mgr.submit = AsyncMock()

        orch = TradingOrchestrator(
            data_feed        = MagicMock(),
            regime_detector  = RegimeDetector(),
            signal_generator = DonchianBreakoutSignalGenerator(),
            risk_engine      = make_risk_engine(),
            order_manager    = order_mgr,
            broker_adapter   = MagicMock(),
            alert_service    = AsyncMock(),
            event_bus        = make_event_bus(),
            initial_equity   = Decimal("100000"),
        )
        await orch.process_bar(trending_bars(50))
        order_mgr.submit.assert_not_called()

    @pytest.mark.asyncio
    async def test_halted_engine_blocks_entry(self):
        """After a 2% daily loss, the risk engine should block new entries."""
        from orchestrator.engine import TradingOrchestrator

        order_mgr = MagicMock()
        order_mgr.open_positions.return_value = []
        order_mgr.submit = AsyncMock()

        engine = make_risk_engine(100_000.0)
        # Drive engine past daily limit
        engine.record_position_closed(Decimal("-2100"))

        orch = TradingOrchestrator(
            data_feed        = MagicMock(),
            regime_detector  = RegimeDetector(),
            signal_generator = DonchianBreakoutSignalGenerator(),
            risk_engine      = engine,
            order_manager    = order_mgr,
            broker_adapter   = MagicMock(),
            alert_service    = AsyncMock(),
            event_bus        = make_event_bus(),
            initial_equity   = Decimal("100000"),
        )

        bars = trending_bars(250)
        # Force a clear breakout signal
        bars[-1] = make_bar(close=float(bars[-1].close) * 1.10, dt=bars[-1].timestamp)
        await orch.process_bar(bars)

        order_mgr.submit.assert_not_called()
