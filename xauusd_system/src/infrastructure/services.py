"""
infrastructure/event_bus.py
────────────────────────────
In-process pub/sub. All components publish events here;
the monitoring/alerting layer subscribes.
Thread-safe with asyncio support.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Callable

from core.interfaces import IEventBus

logger = logging.getLogger(__name__)


class InProcessEventBus(IEventBus):
    """
    Simple synchronous pub/sub for use within a single process.
    Handlers are called synchronously in publication order.
    Exceptions in handlers are caught and logged — never propagated
    to the publisher (prevents a bad subscriber killing the run loop).
    """

    def __init__(self) -> None:
        self._handlers: dict[str, list[Callable]] = defaultdict(list)

    def publish(self, event_type: str, payload: dict) -> None:
        handlers = self._handlers.get(event_type, [])
        wildcard = self._handlers.get("*", [])
        for handler in handlers + wildcard:
            try:
                handler(payload)
            except Exception as exc:
                logger.error(
                    "event_handler_error",
                    extra={"event_type": event_type, "handler": handler.__name__},
                    exc_info=exc,
                )

    def subscribe(self, event_type: str, handler: Callable[[dict], None]) -> None:
        self._handlers[event_type].append(handler)
        logger.debug("event_subscribed", extra={"event_type": event_type, "handler": handler.__name__})


# ──────────────────────────────────────────────────────────────────
# infrastructure/alerting.py
# ──────────────────────────────────────────────────────────────────

"""
infrastructure/alerting.py
───────────────────────────
Multi-channel alert dispatcher.
Supports Telegram, email (SMTP), and console fallback.
Alerts are fire-and-forget — never block the trading path.
Credentials loaded exclusively from environment variables.
"""

import os
import smtplib
import traceback
from email.message import EmailMessage
from typing import Optional

import aiohttp

from core.interfaces import IAlertService


class TelegramAlertService(IAlertService):
    """
    Sends alerts to a Telegram bot/channel.
    Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID env vars.
    Falls back to console log on any failure — never raises.
    """

    API_URL = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self) -> None:
        self._token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self._chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        self._enabled = bool(self._token and self._chat_id)

    def send_sync(self, level: str, message: str) -> None:
        """Synchronous fallback — just prints. Used in non-async callbacks."""
        print(f"[ALERT][{level}] {message}")

    async def send(
        self,
        level:    str,
        message:  str,
        metadata: Optional[dict] = None,
    ) -> None:
        emoji   = {"INFO": "ℹ️", "WARNING": "⚠️", "CRITICAL": "🚨"}.get(level, "📌")
        meta_str = "\n" + str(metadata) if metadata else ""
        text    = f"{emoji} [{level}] XAU/USD System\n{message}{meta_str}"

        if not self._enabled:
            print(f"[ALERT][{level}] {message}")
            return

        try:
            url = self.API_URL.format(token=self._token)
            async with aiohttp.ClientSession() as session:
                await session.post(url, json={
                    "chat_id": self._chat_id,
                    "text":    text,
                    "parse_mode": "HTML",
                }, timeout=aiohttp.ClientTimeout(total=5))
        except Exception as exc:
            # Alert failure must NEVER propagate — log and continue
            logger.error("alert_send_failed", exc_info=exc)
            print(f"[ALERT FALLBACK][{level}] {message}")


class EmailAlertService(IAlertService):
    """SMTP-based alerts. Set SMTP_HOST, SMTP_USER, SMTP_PASS, ALERT_EMAIL env vars."""

    async def send(self, level: str, message: str, metadata: Optional[dict] = None) -> None:
        try:
            msg = EmailMessage()
            msg["Subject"] = f"[{level}] XAU/USD Trading System Alert"
            msg["From"]    = os.environ.get("SMTP_USER", "")
            msg["To"]      = os.environ.get("ALERT_EMAIL", "")
            msg.set_content(f"{message}\n\nMetadata: {metadata}")
            with smtplib.SMTP_SSL(os.environ.get("SMTP_HOST", "smtp.gmail.com")) as smtp:
                smtp.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
                smtp.send_message(msg)
        except Exception as exc:
            logger.error("email_alert_failed", exc_info=exc)


# ──────────────────────────────────────────────────────────────────
# infrastructure/logging_config.py
# ──────────────────────────────────────────────────────────────────

"""
infrastructure/logging_config.py
──────────────────────────────────
Structured JSON logging.
All log records include: timestamp, level, module, function, line, message, extra fields.
Output to stdout (captured by Docker/ECS log driver) and optionally to a rotating file.
"""

import json
import logging
import logging.handlers
import sys
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    """Emit every log record as a single-line JSON object."""

    ALWAYS_FIELDS = ("levelname", "name", "funcName", "lineno")

    def format(self, record: logging.LogRecord) -> str:
        log_record = {
            "ts":      datetime.now(timezone.utc).isoformat(),
            "level":   record.levelname,
            "logger":  record.name,
            "func":    record.funcName,
            "line":    record.lineno,
            "msg":     record.getMessage(),
        }
        # Merge structured `extra` fields
        for key, value in record.__dict__.items():
            if key not in logging.LogRecord.__dict__ and not key.startswith("_"):
                try:
                    json.dumps(value)   # skip non-serializable extras
                    log_record[key] = value
                except (TypeError, ValueError):
                    log_record[key] = str(value)
        if record.exc_info:
            log_record["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_record)


def configure_logging(
    level: str = "INFO",
    log_file: Optional[str] = None,
) -> None:
    """
    Call once at process startup.
    Sets root logger to JSON output on stdout + optional rotating file.
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Remove any default handlers
    root.handlers.clear()

    # Stdout handler (Docker/cloud log drivers read from stdout)
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(JsonFormatter())
    root.addHandler(stdout_handler)

    # Optional rotating file (useful for local debug)
    if log_file:
        file_handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=50 * 1024 * 1024, backupCount=5
        )
        file_handler.setFormatter(JsonFormatter())
        root.addHandler(file_handler)


# ──────────────────────────────────────────────────────────────────
# infrastructure/monitoring.py
# ──────────────────────────────────────────────────────────────────

"""
infrastructure/monitoring.py
──────────────────────────────
Prometheus metrics + health endpoint.
Exposes /metrics and /health on a lightweight HTTP server.
Zero external dependencies beyond prometheus_client.
"""

from prometheus_client import Counter, Gauge, Histogram, start_http_server
from typing import Optional
from core.interfaces import IEventBus, IAlertService, IBrokerAdapter


SIGNALS_TOTAL     = Counter("xauusd_signals_total",      "Signals generated",       ["type", "regime"])
ORDERS_TOTAL      = Counter("xauusd_orders_total",        "Orders submitted",        ["side", "outcome"])
TRADE_PNL         = Histogram("xauusd_trade_pnl",         "Per-trade PnL ($)",       buckets=[-500, -200, -100, -50, 0, 50, 100, 200, 500, 1000, 2000])
ACCOUNT_EQUITY    = Gauge("xauusd_account_equity",        "Current account equity")
DRAWDOWN_PCT      = Gauge("xauusd_drawdown_pct",          "Current drawdown from peak")
CIRCUIT_BREAKER   = Gauge("xauusd_circuit_breaker",       "1 if circuit breaker active")
DAILY_PNL         = Gauge("xauusd_daily_pnl",             "Realized PnL today")
WEEKLY_PNL        = Gauge("xauusd_weekly_pnl",            "Realized PnL this week")
SPREAD_CURRENT    = Gauge("xauusd_spread_current",        "Current bid/ask spread")
OPEN_POSITIONS    = Gauge("xauusd_open_positions",        "Number of open positions")
BROKER_HEALTH     = Gauge("xauusd_broker_healthy",        "1 if broker API healthy")
BARS_PROCESSED    = Counter("xauusd_bars_processed_total","Daily bars processed")


class MetricsCollector:
    """
    Subscribes to the event bus and updates Prometheus metrics.
    Completely decoupled — the trading components have no dependency on this.
    """

    def __init__(
        self,
        event_bus:      IEventBus,
        alert_service:  IAlertService,
        broker_adapter: IBrokerAdapter,
        metrics_port:   int = 8000,
    ) -> None:
        self._alerts = alert_service
        self._broker = broker_adapter

        # Start Prometheus HTTP server
        start_http_server(metrics_port)
        logger.info("metrics_server_started", extra={"port": metrics_port})

        # Subscribe to all relevant events
        event_bus.subscribe("orchestrator.bar_processed",  self._on_bar)
        event_bus.subscribe("risk.order_rejected",         self._on_rejection)
        event_bus.subscribe("risk.circuit_breaker",        self._on_circuit_breaker)
        event_bus.subscribe("risk.daily_limit",            self._on_daily_limit)
        event_bus.subscribe("risk.weekly_limit",           self._on_weekly_limit)
        event_bus.subscribe("risk.gap_caution",            self._on_gap_caution)

    def _on_bar(self, payload: dict) -> None:
        ACCOUNT_EQUITY.set(payload.get("equity", 0))
        DRAWDOWN_PCT.set(payload.get("drawdown_pct", 0) * 100)
        BARS_PROCESSED.inc()

    def _on_rejection(self, payload: dict) -> None:
        ORDERS_TOTAL.labels(side="n/a", outcome="rejected").inc()

    def _on_circuit_breaker(self, payload: dict) -> None:
        CIRCUIT_BREAKER.set(1)
        asyncio.create_task(self._alerts.send(
            "CRITICAL",
            f"Circuit breaker triggered. Drawdown: {payload.get('drawdown_pct', 0):.1%}",
            payload
        ))

    def _on_daily_limit(self, payload: dict) -> None:
        asyncio.create_task(self._alerts.send(
            "WARNING",
            f"Daily loss limit hit: {payload.get('daily_pnl', 0):.2f}"
        ))

    def _on_weekly_limit(self, payload: dict) -> None:
        asyncio.create_task(self._alerts.send(
            "WARNING",
            f"Weekly loss limit hit: {payload.get('weekly_pnl', 0):.2f}"
        ))

    def _on_gap_caution(self, payload: dict) -> None:
        asyncio.create_task(self._alerts.send(
            "WARNING",
            f"Gap-caution activated. Reduced sizing for next {payload.get('trades_left')} trades."
        ))

    async def broker_healthcheck_loop(self) -> None:
        """Ping broker every 60s and alert on degradation."""
        import asyncio
        while True:
            healthy = await self._broker.healthcheck()
            BROKER_HEALTH.set(1 if healthy else 0)
            if not healthy:
                await self._alerts.send("CRITICAL", "Broker API health check failed")
            await asyncio.sleep(60)
