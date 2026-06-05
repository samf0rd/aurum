"""
orchestrator/engine.py
───────────────────────
The central loop that wires all components together.
Receives bars → runs regime detection → generates signals →
validates with risk engine → submits to order manager.

Deliberately thin: it owns the sequencing, not the logic.
All logic lives in the injected components.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, date, timedelta, timezone
from decimal import Decimal
from typing import Optional, Sequence

from core.interfaces import (
    Bar, IAlertService, IBrokerAdapter, IDataFeed, IEventBus,
    IOrderManager, IRiskEngine, ISignalGenerator, IRegimeDetector,
    Order, OrderType, Position, Regime, RiskState, Side, Signal, SignalType, Tick
)
from core.config import ACTIVE_PROFILE

logger = logging.getLogger(__name__)

SYMBOL = "XAUUSD"


class TradingOrchestrator:
    """
    The main run loop. Owns:
      - The daily bar processing pipeline
      - Live tick consumption for spread monitoring
      - Position management (trailing stop updates)
      - Daily/weekly PnL reset scheduling
    """

    def __init__(
        self,
        data_feed:        IDataFeed,
        regime_detector:  IRegimeDetector,
        signal_generator: ISignalGenerator,
        risk_engine:      IRiskEngine,
        order_manager:    IOrderManager,
        broker_adapter:   IBrokerAdapter,
        alert_service:    IAlertService,
        event_bus:        IEventBus,
        initial_equity:   Decimal,
        lookback_bars:    int = 250,
        system_state=None,   # dashboard.state.SystemState | None
        exec_engine=None,    # execution.engine.ExecutionEngine | None
    ) -> None:
        self._feed     = data_feed
        self._regime   = regime_detector
        self._signals  = signal_generator
        self._risk     = risk_engine
        self._orders   = order_manager
        self._broker   = broker_adapter
        self._alerts   = alert_service
        self._bus      = event_bus
        self._lookback = lookback_bars
        self._state    = system_state
        self._exec_eng = exec_engine

        self._risk_state = RiskState(
            equity             = initial_equity,
            peak_equity        = initial_equity,
            daily_pnl          = Decimal("0"),
            weekly_pnl         = Decimal("0"),
            drawdown_pct       = Decimal("0"),
            daily_limit_hit    = False,
            weekly_limit_hit   = False,
            circuit_breaker_on = False,
            gap_caution        = False,
        )

        self._spread_history: list[Decimal] = []
        self._running = False
        self._last_bar_time = None
        # Guard set — prevents double-ejection within the same 5s window
        self._ejected: set[str] = set()

    # ─────────────────────────────────────────────
    # Main entry points
    # ─────────────────────────────────────────────

    async def run(self) -> None:
        """
        Dual-loop trading engine.
        _price_loop  — every 5 s   → live tick → update dashboard price + unrealized P&L
        _bar_loop    — every 60 s  → poll for new H1 bar → run strategy pipeline
        _daily_reset — at 00:05 UTC → reset daily accounting
        """
        self._running = True
        logger.info("orchestrator_started", extra={"symbol": SYMBOL})
        await self._alerts.send("INFO", "Trading system started")

        # Pre-warm: initialise the H1 bar cache so the synthetic tick generator
        # and paper broker start from the same price as the last H1 bar close.
        # This prevents a spurious stop-out on the very first _price_loop tick.
        try:
            await self._feed.get_bars(granularity=ACTIVE_PROFILE.timeframe, count=self._lookback)
        except Exception as exc:
            logger.warning("prewarm_bars_failed", exc_info=exc)

        price_interval = int(os.environ.get("PRICE_TICK_INTERVAL", "300"))
        await asyncio.gather(
            self._price_loop(interval=price_interval),
            self._bar_loop(interval=60),
            self._daily_reset_loop(),
        )

    async def process_bar(self, bars: Sequence[Bar]) -> None:
        """
        Process one completed daily bar. Called by _daily_bar_loop and
        directly in backtesting — the only entry point for bar-level logic.
        """
        if len(bars) < 220:
            logger.warning("insufficient_history", extra={"count": len(bars)})
            return

        last = bars[-1]

        # ── 1. Update trailing stops on open positions
        for pos in self._orders.open_positions():
            updated = self._risk.update_trailing_stop(pos, bars)
            if updated.hard_stop != pos.hard_stop:
                await self._sync_broker_stop(updated)

        # ── 2. Check exit conditions for open positions
        for pos in self._orders.open_positions():
            should_exit, reason = self._signals.should_exit(
                bars, pos.side.value, self._regime.detect(bars)
            )
            if should_exit:
                logger.info("exit_triggered", extra={"reason": reason, "pos": str(pos)})
                await self._orders.submit(Order(
                    symbol     = SYMBOL,
                    side       = Side.SHORT if pos.side == Side.LONG else Side.LONG,
                    order_type = OrderType.MARKET,
                    quantity   = pos.quantity,
                    metadata   = {"reason": reason, "type": "exit"},
                ))

        # ── 3. Compute indicators for dashboard (always — even when position is open)
        if self._state is not None:
            try:
                ind = self._signals.compute_indicators(bars)
                self._state.last_indicators = ind
            except Exception:
                pass

        # ── 4. Check for new entry signal (only if no open position)
        if not self._orders.open_positions():
            regime = self._regime.detect(bars)
            signal = self._signals.on_bar(bars, regime)

            if self._state is not None:
                from datetime import timezone as _sigtz
                self._state.last_signal_type   = signal.signal_type.name
                self._state.last_signal_reason = signal.reason
                self._state.last_signal_ts     = signal.timestamp

            if signal.signal_type != SignalType.NO_SIGNAL:
                await self._attempt_entry(signal, last)

        # ── 4. Update risk state
        self._risk_state = self._risk.update_risk_state(
            current_state  = self._risk_state,
            realized_pnl   = Decimal("0"),   # fills flow through on_fill
            open_positions = self._orders.open_positions(),
            current_prices = {SYMBOL: last.close},
        )

        self._bus.publish("orchestrator.bar_processed", {
            "timestamp":    last.timestamp.isoformat(),
            "close":        float(last.close),
            "regime":       self._regime.detect(bars).name,
            "equity":       float(self._risk_state.equity),
            "drawdown_pct": float(self._risk_state.drawdown_pct),
        })

        # Update dashboard state
        if self._state is not None:
            self._state.record_equity(float(self._risk_state.equity))
            regime_str = self._regime.detect(bars).name
            adx_val = float(self._state.last_indicators.get("adx14", 0.0)) \
                      if self._state.last_indicators else 0.0
            label = "BULL" if "BULL" in regime_str else "BEAR" if "BEAR" in regime_str else "NEUTRAL"
            self._state.update_regime(label, adx_val)
            # Stamp the bar evaluation time so the UI countdown can reset
            self._state.last_bar_eval_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # ─────────────────────────────────────────────
    # Private helpers
    # ─────────────────────────────────────────────

    async def _attempt_entry(self, signal: Signal, bar: Bar) -> None:
        spread  = self._current_spread()
        median  = self._median_spread()

        approved, reason = self._risk.approve_order(
            signal, self._risk_state, spread, median
        )
        if not approved:
            return

        qty = self._risk.compute_position_size(signal, self._risk_state)

        if signal.signal_type == SignalType.ENTER_LONG:
            side      = Side.LONG
            stop_price = bar.close - signal.stop_distance
        else:
            side      = Side.SHORT
            stop_price = bar.close + signal.stop_distance

        entry_order = Order(
            symbol     = SYMBOL,
            side       = side,
            order_type = OrderType.MARKET,
            quantity   = qty,
            metadata   = {
                "signal_reason": signal.reason,
                "stop_price":    float(stop_price),
                "type":          "entry",
            },
        )
        submitted = await self._orders.submit(entry_order)
        logger.info("entry_submitted", extra={"order": str(submitted)})

    async def _sync_broker_stop(self, position: Position) -> None:
        """Update the broker-side hard stop after a trail."""
        try:
            await self._broker.cancel_order(f"stop_{position.symbol}")
            stop_order = Order(
                symbol     = position.symbol,
                side       = Side.SHORT if position.side == Side.LONG else Side.LONG,
                order_type = OrderType.STOP_MARKET,
                quantity   = position.quantity,
                stop_price = position.hard_stop,
                metadata   = {"type": "hard_stop"},
            )
            await self._broker.place_order(stop_order)
        except Exception as exc:
            logger.error("stop_sync_failed", exc_info=exc)
            await self._alerts.send("CRITICAL", f"Stop sync failed: {exc}")

    def _current_spread(self) -> Decimal:
        if not self._spread_history:
            return Decimal("0.80")   # conservative default
        return self._spread_history[-1]

    def _median_spread(self) -> Decimal:
        if len(self._spread_history) < 5:
            return Decimal("0.40")
        import statistics
        return Decimal(str(statistics.median(float(s) for s in self._spread_history[-20:])))

    # ─────────────────────────────────────────────
    # Background coroutines
    # ─────────────────────────────────────────────

    async def _price_loop(self, interval: int) -> None:
        """
        Fetches live bid/ask every `interval` seconds.
        Updates dashboard price, spread history, and unrealized P&L.
        Never triggers strategy logic — that lives in _bar_loop only.
        """
        from datetime import timezone as _tz
        while True:
            try:
                tick   = await self._feed.get_latest_tick()
                mid    = (tick["bid"] + tick["ask"]) / 2
                spread = tick["ask"] - tick["bid"]

                # Keep spread history for risk engine gate
                self._spread_history.append(Decimal(str(round(spread, 4))))
                if len(self._spread_history) > 500:
                    self._spread_history.pop(0)

                if self._state is not None:
                    self._state.current_price      = mid
                    self._state.last_tick_ts       = tick["timestamp"]
                    self._state.last_spread        = spread
                    self._state.last_median_spread = float(self._median_spread())

                    # Maintain forming candle for the live candlestick chart
                    _now    = datetime.now(timezone.utc)
                    _bsecs  = ACTIVE_PROFILE.bar_seconds
                    _bmin   = _bsecs // 60
                    _flr    = (_now.minute // _bmin) * _bmin if _bmin < 60 else 0
                    _bar_t  = int(_now.replace(minute=_flr, second=0, microsecond=0).timestamp())
                    _fc     = self._state.forming_candle
                    if not _fc or _fc.get('time') != _bar_t:
                        self._state.forming_candle = {
                            'time': _bar_t, 'open': round(mid, 2),
                            'high': round(mid, 2), 'low': round(mid, 2), 'close': round(mid, 2),
                        }
                    else:
                        _fc['high']  = round(max(_fc['high'], mid), 2)
                        _fc['low']   = round(min(_fc['low'],  mid), 2)
                        _fc['close'] = round(mid, 2)

                    # Sync open position snapshot to live price
                    open_pos = self._orders.open_positions()
                    if open_pos:
                        p    = open_pos[0]
                        upnl = self._calc_unrealized_pnl(p, mid)
                        from dashboard.state import PositionSnapshot
                        self._state.position = PositionSnapshot(
                            symbol         = p.symbol,
                            side           = p.side.value,
                            size           = float(p.quantity),
                            entry_px       = float(p.entry_price),
                            current_px     = round(mid, 2),
                            unrealized_pnl = round(upnl, 2),
                            hard_stop      = float(p.hard_stop),
                        )
                    else:
                        self._state.position = None

                    # ── Intra-bar flash-crash circuit breaker ─────────────────
                    await self._check_intrabar_stop(mid)

                    # Append live equity point (in-memory only; file write happens in bar loop)
                    ts       = datetime.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    base_eq  = float(self._risk_state.equity)
                    total_eq = base_eq + (
                        self._state.position.unrealized_pnl if self._state.position else 0.0
                    )
                    self._state.equity_curve.append((ts, round(total_eq, 2)))
                    if len(self._state.equity_curve) > 10_000:
                        self._state.equity_curve = self._state.equity_curve[-10_000:]

            except Exception as exc:
                logger.warning("price_loop_error", exc_info=exc)

            await asyncio.sleep(interval)

    async def _bar_loop(self, interval: int) -> None:
        """
        Polls for new completed H1 bars every `interval` seconds.
        Only runs the strategy pipeline when the most recent bar timestamp changes.
        """
        while True:
            try:
                bars = await self._feed.get_bars(
                    granularity=ACTIVE_PROFILE.timeframe,
                    count=self._lookback,
                    include_incomplete=False,
                )
                if bars:
                    latest = bars[-1]
                    if latest.timestamp != self._last_bar_time:
                        self._last_bar_time = latest.timestamp
                        logger.info("new_bar", extra={
                            "ts":          latest.timestamp.isoformat(),
                            "close":       float(latest.close),
                            "granularity": ACTIVE_PROFILE.timeframe,
                        })
                        await self.process_bar(bars)
            except Exception as exc:
                logger.error("bar_loop_error", exc_info=exc)
                await self._alerts.send("WARNING", f"Bar loop error: {exc}")

            await asyncio.sleep(interval)

    async def _daily_reset_loop(self) -> None:
        """Fires at 00:05 UTC each day. Resets daily P&L counters."""
        while True:
            now        = datetime.utcnow()
            next_reset = now.replace(hour=0, minute=5, second=0, microsecond=0)
            if next_reset <= now:
                next_reset += timedelta(days=1)
            await asyncio.sleep((next_reset - now).total_seconds())

            logger.info("daily_reset", extra={
                "date":   now.date().isoformat(),
                "equity": float(self._risk_state.equity),
            })
            from dataclasses import replace
            self._risk_state = replace(self._risk_state, daily_pnl=Decimal("0"))
            if now.weekday() == 0:
                self._risk_state = replace(self._risk_state, weekly_pnl=Decimal("0"))
            if self._state is not None:
                self._state.daily_trade_count = 0

    async def _check_intrabar_stop(self, mid: float) -> None:
        """
        Tick-level circuit breaker — runs inside _price_loop every 5 s.

        For every open position, checks whether the live mid price has
        traded *through* the position's hard stop (set at 2×ATR from entry
        by the signal generator).  If breached, fires _emergency_eject
        immediately without waiting for the next H1 bar close.

        The guard set self._ejected prevents double-firing on the same
        symbol within the 5 s window before on_fill() clears the ledger.
        """
        for pos in self._orders.open_positions():
            if pos.symbol in self._ejected:
                continue                         # eject already in flight
            if pos.hard_stop == Decimal("0"):
                continue                         # no stop set for this position

            mid_d    = Decimal(str(round(mid, 5)))
            breached = (
                (pos.side == Side.LONG  and mid_d < pos.hard_stop) or
                (pos.side == Side.SHORT and mid_d > pos.hard_stop)
            )
            if breached:
                self._ejected.add(pos.symbol)
                await self._emergency_eject(pos, mid)
                break   # one eject per tick; re-evaluate next loop

    async def _emergency_eject(self, position, trigger_price: float) -> None:
        """
        Market-exits a position that has breached its intra-bar hard stop.
        Publishes INTRA_BAR_STOP_OUT to the event bus, alerts, and updates
        the dashboard Signal Explainer with a distinct badge.
        """
        symbol    = position.symbol
        exit_side = Side.SHORT if position.side == Side.LONG else Side.LONG
        entry     = float(position.entry_price)
        hard_stop = float(position.hard_stop)
        atr       = float(position.atr_at_entry) if position.atr_at_entry else 1.0
        qty       = position.quantity

        # Dollars the price blew past the stop (slippage beyond the stop level)
        if position.side == Side.LONG:
            breach_usd = round(hard_stop - trigger_price, 2)
            pnl_est    = round((trigger_price - entry) * float(qty) * 100.0, 2)
        else:
            breach_usd = round(trigger_price - hard_stop, 2)
            pnl_est    = round((entry - trigger_price) * float(qty) * 100.0, 2)

        breach_atrs = round(breach_usd / atr, 3) if atr else 0.0

        logger.warning(
            "INTRA_BAR_STOP_OUT",
            extra={
                "symbol":       symbol,
                "side":         position.side.value,
                "entry":        entry,
                "hard_stop":    hard_stop,
                "trigger_px":   round(trigger_price, 2),
                "breach_usd":   breach_usd,
                "breach_atrs":  breach_atrs,
                "pnl_est":      pnl_est,
            }
        )

        eject_order = Order(
            symbol     = symbol,
            side       = exit_side,
            order_type = OrderType.MARKET,
            quantity   = qty,
            metadata   = {
                "type":          "emergency_stop",
                "reason":        "INTRA_BAR_STOP_OUT",
                "trigger_price": round(trigger_price, 2),
                "hard_stop":     hard_stop,
                "breach_usd":    breach_usd,
                "breach_atrs":   breach_atrs,
            },
        )

        try:
            await self._orders.submit(eject_order)
        except Exception as exc:
            logger.error("emergency_eject_failed", exc_info=exc)
            self._ejected.discard(symbol)
            return

        # on_fill() (called inside submit) removes the position from the ledger;
        # clear the guard so a fresh position on the same symbol can be ejected later.
        self._ejected.discard(symbol)

        # Event bus — MetricsCollector / any subscriber can react
        self._bus.publish("risk.intrabar_stop_out", {
            "symbol":      symbol,
            "side":        position.side.value,
            "trigger_px":  round(trigger_price, 2),
            "hard_stop":   hard_stop,
            "breach_usd":  breach_usd,
            "breach_atrs": breach_atrs,
            "pnl_est":     pnl_est,
            "timestamp":   datetime.now(timezone.utc).isoformat(),
        })

        await self._alerts.send(
            "CRITICAL",
            f"INTRA-BAR STOP OUT | {symbol} {position.side.value} | "
            f"Stop {hard_stop:.2f} breached @ {trigger_price:.2f} "
            f"({breach_usd:.2f} = {breach_atrs:.2f}×ATR) | PnL est. {pnl_est:.2f}"
        )

        # Dashboard — update Signal Explainer with a distinct stop-out badge
        if self._state is not None:
            from datetime import timezone as _ejtz
            self._state.last_signal_type   = "INTRA_BAR_STOP_OUT"
            self._state.last_signal_reason = (
                f"Flash-crash stop-out: live price {trigger_price:.2f} breached "
                f"hard stop {hard_stop:.2f} by {breach_usd:.2f} ({breach_atrs:.2f}×ATR) "
                f"intra-bar. Estimated P&L: {pnl_est:.2f}"
            )
            self._state.last_signal_ts = datetime.now(_ejtz.utc)

    def _calc_unrealized_pnl(self, position, mid_price: float) -> float:
        """Rule 6 inverse: (current - entry) × qty × contract_value (100 oz/lot)."""
        side  = position.side.value if hasattr(position.side, 'value') else str(position.side)
        entry = float(position.entry_price)
        qty   = float(position.quantity)
        if side == "LONG":
            return (mid_price - entry) * qty * 100.0
        else:
            return (entry - mid_price) * qty * 100.0


# ──────────────────────────────────────────────────────────────────
# broker/oanda_adapter.py  (stub — replace with live credentials)
# ──────────────────────────────────────────────────────────────────

"""
broker/oanda_adapter.py
────────────────────────
OANDA v20 REST + streaming adapter.
This is the ONLY class with knowledge of broker-specific API details.
To switch brokers, implement IBrokerAdapter for the new broker and
inject it — zero changes elsewhere.
"""

import aiohttp
from typing import AsyncIterator


class OandaBrokerAdapter(IBrokerAdapter):
    """
    Production adapter for OANDA v20 API.
    Credentials loaded from environment — never hardcoded.
    """

    BASE_URL    = "https://api-fxtrade.oanda.com/v3"
    STREAM_URL  = "https://stream-fxtrade.oanda.com/v3"

    def __init__(self, account_id: str, api_token: str) -> None:
        self._account_id = account_id
        self._headers    = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type":  "application/json",
        }
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers=self._headers)
        return self._session

    async def place_order(self, order: Order) -> Order:
        session = await self._get_session()
        payload = self._translate_order(order)
        url = f"{self.BASE_URL}/accounts/{self._account_id}/orders"
        async with session.post(url, json=payload) as resp:
            data = await resp.json()
            if resp.status == 201:
                order.broker_ref = data.get("orderCreateTransaction", {}).get("id", "")
                order.status     = type('OrderStatus', (), {'SUBMITTED': None})  # from enum
                logger.info("order_placed", extra={"broker_ref": order.broker_ref})
            else:
                logger.error("order_place_failed", extra={"response": data})
                raise RuntimeError(f"OANDA order failed: {data}")
        return order

    async def cancel_order(self, broker_ref: str) -> bool:
        session = await self._get_session()
        url = f"{self.BASE_URL}/accounts/{self._account_id}/orders/{broker_ref}/cancel"
        async with session.put(url) as resp:
            return resp.status == 200

    async def get_account(self) -> dict:
        session = await self._get_session()
        url = f"{self.BASE_URL}/accounts/{self._account_id}/summary"
        async with session.get(url) as resp:
            return await resp.json()

    async def stream_executions(self) -> AsyncIterator[Order]:
        """Stream transaction events from OANDA."""
        session = await self._get_session()
        url = f"{self.STREAM_URL}/accounts/{self._account_id}/transactions/stream"
        async with session.get(url) as resp:
            async for line in resp.content:
                if line.strip():
                    import json
                    event = json.loads(line)
                    parsed = self._parse_transaction(event)
                    if parsed:
                        yield parsed

    async def healthcheck(self) -> bool:
        try:
            session = await self._get_session()
            url = f"{self.BASE_URL}/accounts/{self._account_id}/summary"
            async with session.get(url) as resp:
                return resp.status == 200
        except Exception:
            return False

    def _translate_order(self, order: Order) -> dict:
        """Domain Order → OANDA JSON payload."""
        units = float(order.quantity) * (1 if order.side == Side.LONG else -1)
        base = {
            "order": {
                "instrument": "XAU_USD",
                "units": str(units),
            }
        }
        if order.order_type == OrderType.MARKET:
            base["order"]["type"] = "MARKET"
            if order.stop_price:
                base["order"]["stopLossOnFill"] = {
                    "price": str(order.stop_price),
                    "timeInForce": "GTC",
                }
        elif order.order_type == OrderType.STOP_MARKET:
            base["order"]["type"] = "STOP"
            base["order"]["price"] = str(order.stop_price)
        return base

    def _parse_transaction(self, event: dict) -> Optional[Order]:
        """Convert OANDA transaction event → our Order domain object."""
        tx_type = event.get("type", "")
        if tx_type not in ("ORDER_FILL", "ORDER_CANCEL", "ORDER_REJECT"):
            return None
        # Minimal mapping — extend as needed for full execution tracking
        return None  # placeholder; production code maps all fields
