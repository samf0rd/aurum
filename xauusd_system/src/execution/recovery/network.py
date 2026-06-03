"""
execution/recovery/network.py
───────────────────────────────
Network failure detection and recovery manager.

Responsibilities
────────────────
1. Monitor connection health on a heartbeat loop
2. Detect disconnections (broker.is_connected = False, or heartbeat fails)
3. Execute structured reconnection sequence with exponential backoff
4. On reconnection: trigger full reconciliation to catch any fills / state
   changes that occurred while disconnected
5. Provide circuit-breaker: after N consecutive failed reconnections,
   enter CIRCUIT_OPEN state and page the operator rather than hammering
   the broker

State machine
─────────────
  CONNECTED ──────heartbeat_fail──────► RECONNECTING
      ▲                                       │
      │                                  success │ fail×N
      └──────────────────────────────────────────┘  ↓
                                              CIRCUIT_OPEN
                                                   │
                                          operator reset │
                                                   ▼
                                              CONNECTED
"""
from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone
from enum import Enum, auto
from typing import Callable, Coroutine, Optional

from ..brokers.base import IBrokerAdapter, BrokerConnectionError

logger = logging.getLogger(__name__)


class RecoveryState(Enum):
    CONNECTED     = auto()
    RECONNECTING  = auto()
    CIRCUIT_OPEN  = auto()


class NetworkRecoveryManager:
    """
    Monitors broker connection health and handles automatic reconnection.

    Usage:
        recovery = NetworkRecoveryManager(broker, on_reconnect=engine._on_reconnect)
        asyncio.create_task(recovery.run())
        # ...
        await recovery.stop()
    """

    def __init__(
        self,
        broker:               IBrokerAdapter,
        on_reconnect:         Optional[Callable[[], Coroutine]] = None,
        on_circuit_open:      Optional[Callable[[], Coroutine]] = None,
        heartbeat_interval_s: float = 10.0,
        reconnect_base_s:     float = 2.0,
        reconnect_max_s:      float = 120.0,
        reconnect_backoff:    float = 2.0,
        reconnect_jitter:     float = 0.20,
        circuit_open_after:   int   = 5,    # consecutive reconnect failures
    ) -> None:
        self._broker              = broker
        self._on_reconnect        = on_reconnect
        self._on_circuit_open     = on_circuit_open
        self._heartbeat_interval  = heartbeat_interval_s
        self._reconnect_base      = reconnect_base_s
        self._reconnect_max       = reconnect_max_s
        self._reconnect_backoff   = reconnect_backoff
        self._reconnect_jitter    = reconnect_jitter
        self._circuit_open_after  = circuit_open_after

        self._state               = RecoveryState.CONNECTED
        self._consecutive_fails   = 0
        self._total_reconnects    = 0
        self._last_connected_at:  Optional[datetime] = datetime.now(timezone.utc)
        self._disconnected_at:    Optional[datetime] = None
        self._running             = False
        self._stop_event          = asyncio.Event()

    # ── Public API ────────────────────────────────────────────────────────

    @property
    def state(self) -> RecoveryState:
        return self._state

    @property
    def is_healthy(self) -> bool:
        return self._state == RecoveryState.CONNECTED and self._broker.is_connected

    async def run(self) -> None:
        """Main heartbeat loop. Run as an asyncio task."""
        self._running = True
        logger.info("network_recovery_manager started | heartbeat=%.1fs", self._heartbeat_interval)

        while self._running:
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._heartbeat_interval,
                )
                break  # stop was requested
            except asyncio.TimeoutError:
                pass   # normal — heartbeat tick

            await self._heartbeat()

        logger.info("network_recovery_manager stopped")

    async def stop(self) -> None:
        """Signal the run loop to exit cleanly."""
        self._running = False
        self._stop_event.set()

    async def reset_circuit(self) -> None:
        """
        Operator-triggered circuit reset.
        Attempts reconnection; if successful, transitions back to CONNECTED.
        """
        if self._state != RecoveryState.CIRCUIT_OPEN:
            return
        logger.warning("circuit_reset_requested | attempting reconnection")
        self._consecutive_fails = 0
        success = await self._attempt_reconnect()
        if success:
            logger.info("circuit_reset_success")
        else:
            logger.error("circuit_reset_failed — still disconnected")

    # ── Internals ─────────────────────────────────────────────────────────

    async def _heartbeat(self) -> None:
        """Check connection health; start recovery if needed."""
        if self._state == RecoveryState.CIRCUIT_OPEN:
            return   # waiting for operator reset

        if not self._broker.is_connected:
            if self._state == RecoveryState.CONNECTED:
                self._on_disconnect()
            await self._run_recovery()

    def _on_disconnect(self) -> None:
        self._state           = RecoveryState.RECONNECTING
        self._disconnected_at = datetime.now(timezone.utc)
        logger.warning(
            "connection_lost | starting recovery | last_connected=%s",
            self._last_connected_at,
        )

    async def _run_recovery(self) -> None:
        """Reconnection loop with exponential backoff."""
        delay = self._reconnect_base

        while self._running and self._state == RecoveryState.RECONNECTING:
            success = await self._attempt_reconnect()
            if success:
                return

            self._consecutive_fails += 1
            if self._consecutive_fails >= self._circuit_open_after:
                await self._open_circuit()
                return

            jitter = random.uniform(1 - self._reconnect_jitter, 1 + self._reconnect_jitter)
            wait   = min(delay * jitter, self._reconnect_max)
            logger.warning(
                "reconnect_failed | attempt=%d/%d waiting=%.1fs",
                self._consecutive_fails, self._circuit_open_after, wait,
            )
            await asyncio.sleep(wait)
            delay = min(delay * self._reconnect_backoff, self._reconnect_max)

    async def _attempt_reconnect(self) -> bool:
        logger.info("reconnect_attempt | consecutive_fails=%d", self._consecutive_fails)
        try:
            await self._broker.connect()

            # Verify connection actually works
            await self._broker.get_account_equity()

            self._state              = RecoveryState.CONNECTED
            self._consecutive_fails  = 0
            self._total_reconnects  += 1
            downtime_s = (
                (datetime.now(timezone.utc) - self._disconnected_at).total_seconds()
                if self._disconnected_at else 0
            )
            self._last_connected_at  = datetime.now(timezone.utc)
            self._disconnected_at    = None

            logger.info(
                "reconnected | downtime=%.1fs total_reconnects=%d",
                downtime_s, self._total_reconnects,
            )

            # Trigger reconciliation to catch missed fills
            if self._on_reconnect:
                try:
                    await self._on_reconnect()
                except Exception as exc:
                    logger.error("post_reconnect_callback_failed | error=%s", exc)

            return True

        except (BrokerConnectionError, ConnectionError, TimeoutError, OSError) as exc:
            logger.warning("reconnect_attempt_failed | error=%s", exc)
            return False

    async def _open_circuit(self) -> None:
        self._state = RecoveryState.CIRCUIT_OPEN
        logger.critical(
            "CIRCUIT_BREAKER_OPEN | %d consecutive reconnect failures — "
            "operator intervention required", self._consecutive_fails,
        )
        if self._on_circuit_open:
            try:
                await self._on_circuit_open()
            except Exception as exc:
                logger.error("circuit_open_callback_failed | error=%s", exc)
