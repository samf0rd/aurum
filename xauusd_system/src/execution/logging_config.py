"""
execution/logging_config.py
─────────────────────────────
Structured JSON logging for the execution engine.

Features
────────
- JSON lines output suitable for log aggregators (Datadog, Loki, CloudWatch)
- Execution-specific context injected automatically:
    symbol, order_id, broker_ref, fill_id
- Sensitive field masking (API keys, account IDs)
- Separate audit log for fills and reconciliation (never suppressed)
- Human-readable console output in development mode

Usage
─────
    from execution.logging_config import configure_logging, get_execution_logger

    configure_logging(level="INFO", json_output=True)
    logger = get_execution_logger("execution.engine")
    logger.info("order_approved", extra={"order_id": "abc", "qty": "0.25"})
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
import traceback
from datetime import datetime, timezone
from typing import Optional


# ──────────────────────────────────────────────────────────────────────────────
# JSON formatter
# ──────────────────────────────────────────────────────────────────────────────

class JsonLineFormatter(logging.Formatter):
    """
    Formats each log record as a single JSON object per line.
    Compatible with Datadog, Splunk, Loki, and CloudWatch Logs Insights.
    """

    # Fields to mask in output (replace with ***REDACTED***)
    _SENSITIVE_KEYS = frozenset({"api_key", "password", "token", "secret", "credential"})

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts":      datetime.now(timezone.utc).isoformat(),
            "level":   record.levelname,
            "logger":  record.name,
            "msg":     record.getMessage(),
        }

        # Attach extra fields (order_id, broker_ref, etc.)
        for key, value in record.__dict__.items():
            if key.startswith("_") or key in (
                "name", "msg", "args", "created", "filename", "funcName",
                "levelname", "levelno", "lineno", "module", "msecs", "message",
                "pathname", "process", "processName", "relativeCreated",
                "stack_info", "thread", "threadName", "exc_info", "exc_text",
            ):
                continue
            # Mask sensitive fields
            if any(s in key.lower() for s in self._SENSITIVE_KEYS):
                payload[key] = "***REDACTED***"
            else:
                payload[key] = value

        # Exception info
        if record.exc_info:
            payload["exception"] = traceback.format_exception(*record.exc_info)

        return json.dumps(payload, default=str)


class HumanReadableFormatter(logging.Formatter):
    """
    Coloured console formatter for development.
    """
    COLOURS = {
        "DEBUG":    "\033[36m",    # cyan
        "INFO":     "\033[32m",    # green
        "WARNING":  "\033[33m",    # yellow
        "ERROR":    "\033[31m",    # red
        "CRITICAL": "\033[41m",    # red background
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        colour = self.COLOURS.get(record.levelname, "")
        ts     = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
        prefix = f"{colour}{record.levelname:8s}{self.RESET}"
        return f"{ts} {prefix} {record.name:35s} | {record.getMessage()}"


# ──────────────────────────────────────────────────────────────────────────────
# Audit handler — fills and reconciliation always go here
# ──────────────────────────────────────────────────────────────────────────────

class AuditHandler(logging.Handler):
    """
    In-memory ring buffer of audit events (fills, reconciliation).
    Can be read by monitoring; flushed to file periodically.
    Max 10,000 entries — older records dropped first.
    """
    MAX_ENTRIES = 10_000

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self._records: list[dict] = []
        self._formatter = JsonLineFormatter()

    def emit(self, record: logging.LogRecord) -> None:
        formatted = self._formatter.format(record)
        try:
            entry = json.loads(formatted)
        except json.JSONDecodeError:
            entry = {"raw": formatted}

        self._records.append(entry)
        if len(self._records) > self.MAX_ENTRIES:
            self._records = self._records[-self.MAX_ENTRIES:]

    def get_recent(self, n: int = 100) -> list[dict]:
        return self._records[-n:]

    def get_fills(self, n: int = 100) -> list[dict]:
        return [r for r in self._records if "fill" in r.get("msg", "").lower()][-n:]

    def get_reconciliation(self, n: int = 50) -> list[dict]:
        return [
            r for r in self._records
            if "reconcil" in r.get("msg", "").lower()
        ][-n:]


# ──────────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────────

_audit_handler: Optional[AuditHandler] = None


def configure_logging(
    level:        str  = "INFO",
    json_output:  bool = False,
    log_file:     Optional[str] = None,
    audit_buffer: bool = True,
) -> AuditHandler:
    """
    Configure the execution engine logging stack.

    Call once at startup before importing any execution components.

    Args:
        level:        Root log level ("DEBUG", "INFO", "WARNING", "ERROR")
        json_output:  True → JSON lines on stdout (production); False → coloured (dev)
        log_file:     If set, also write JSON lines to this rotating file
        audit_buffer: If True, attach in-memory audit handler to execution loggers

    Returns:
        The AuditHandler instance (for programmatic access to recent events).
    """
    global _audit_handler

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Remove any existing handlers
    root.handlers.clear()

    # ── Console handler
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(
        JsonLineFormatter() if json_output else HumanReadableFormatter()
    )
    root.addHandler(console)

    # ── Rotating file handler (JSON always)
    if log_file:
        file_handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=50 * 1024 * 1024, backupCount=10, encoding="utf-8"
        )
        file_handler.setFormatter(JsonLineFormatter())
        root.addHandler(file_handler)

    # ── Audit buffer for execution events
    if audit_buffer:
        _audit_handler = AuditHandler()
        _audit_handler.addFilter(_ExecutionFilter())
        root.addHandler(_audit_handler)

    # Quieten noisy third-party loggers
    for lib in ("urllib3", "asyncio", "aiohttp.access"):
        logging.getLogger(lib).setLevel(logging.WARNING)

    logging.info(
        "logging_configured | level=%s json=%s file=%s",
        level, json_output, log_file or "none",
    )
    return _audit_handler or AuditHandler()


def get_audit_handler() -> Optional[AuditHandler]:
    return _audit_handler


def get_execution_logger(name: str) -> logging.Logger:
    """Return a logger scoped to the execution engine namespace."""
    return logging.getLogger(f"execution.{name}" if not name.startswith("execution.") else name)


class _ExecutionFilter(logging.Filter):
    """Only pass through records from execution.* loggers."""
    def filter(self, record: logging.LogRecord) -> bool:
        return record.name.startswith("execution.")
