"""
execution/brokers/oanda.py
───────────────────────────
OANDA v20 REST API broker adapter.

Authentication: set OANDA_API_KEY and OANDA_ACCOUNT_ID environment variables.
Practice vs Live: set OANDA_PRACTICE=true for practice environment.

Handles
───────
- Place / cancel / query market, limit, stop orders
- Poll fills via order state endpoint
- Get open positions
- Map OANDA API responses to internal models

This adapter does NOT handle retry — wrap it with RetryingBrokerAdapter.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from ..models import (
    BrokerOrderSnapshot,
    BrokerPositionSnapshot,
    ExecutionOrder,
    Fill,
    OrderSide,
    OrderStatus,
    OrderType,
)
from .base import (
    IBrokerAdapter,
    BrokerConnectionError,
    BrokerRejectedError,
    BrokerTemporaryError,
    OrderNotFoundError,
    RateLimitError,
)

logger = logging.getLogger(__name__)

# Optional aiohttp import — only required for live trading
try:
    import aiohttp
    _AIOHTTP_AVAILABLE = True
except ImportError:
    _AIOHTTP_AVAILABLE = False


class OandaAdapter(IBrokerAdapter):
    """
    OANDA v20 REST broker adapter.

    Usage:
        adapter = OandaAdapter()  # reads credentials from env
        wrapped = RetryingBrokerAdapter(adapter, RetryConfig())
        await wrapped.connect()
    """

    PRACTICE_URL = "https://api-fxpractice.oanda.com/v3"
    LIVE_URL     = "https://api-fxtrade.oanda.com/v3"

    # Map OANDA transaction types → our Fill side
    _OANDA_SIDE: dict[str, OrderSide] = {
        "BUY":  OrderSide.BUY,
        "SELL": OrderSide.SELL,
    }

    # Map OANDA order states → our OrderStatus
    _STATUS_MAP: dict[str, OrderStatus] = {
        "PENDING":    OrderStatus.SUBMITTED,
        "FILLED":     OrderStatus.FILLED,
        "TRIGGERED":  OrderStatus.FILLED,
        "CANCELLED":  OrderStatus.CANCELLED,
        "REJECTED":   OrderStatus.REJECTED,
    }

    def __init__(
        self,
        api_key:    Optional[str] = None,
        account_id: Optional[str] = None,
        practice:   Optional[bool] = None,
    ) -> None:
        if not _AIOHTTP_AVAILABLE:
            raise ImportError("aiohttp is required for OandaAdapter: pip install aiohttp")

        self._api_key    = api_key    or os.environ["OANDA_API_KEY"]
        self._account_id = account_id or os.environ["OANDA_ACCOUNT_ID"]
        _practice        = practice if practice is not None else (
            os.environ.get("OANDA_PRACTICE", "true").lower() == "true"
        )
        self._base_url   = self.PRACTICE_URL if _practice else self.LIVE_URL
        self._session:   Optional[aiohttp.ClientSession] = None
        self._connected  = False

        logger.info(
            "OandaAdapter initialised | practice=%s account=%s",
            _practice, self._account_id,
        )

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        if self._connected:
            return
        self._session = aiohttp.ClientSession(
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type":  "application/json",
            },
            timeout=aiohttp.ClientTimeout(total=10),
        )
        # Validate credentials
        try:
            async with self._session.get(
                f"{self._base_url}/accounts/{self._account_id}/summary"
            ) as resp:
                if resp.status == 401:
                    raise BrokerConnectionError("Invalid OANDA API key")
                if resp.status != 200:
                    text = await resp.text()
                    raise BrokerConnectionError(f"OANDA connect failed: {resp.status} {text}")
            self._connected = True
            logger.info("oanda_connected | account=%s", self._account_id)
        except aiohttp.ClientError as exc:
            raise BrokerConnectionError(f"Network error on connect: {exc}") from exc

    async def disconnect(self) -> None:
        if self._session:
            await self._session.close()
            self._session   = None
            self._connected = False
            logger.info("oanda_disconnected")

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ── Order operations ─────────────────────────────────────────────────────

    async def place_order(self, order: ExecutionOrder) -> ExecutionOrder:
        self._require_connection()
        payload = self._build_order_payload(order)
        resp_data = await self._post(
            f"/accounts/{self._account_id}/orders",
            payload,
        )

        # OANDA returns the created order under different keys depending on type
        created = (
            resp_data.get("orderCreateTransaction")
            or resp_data.get("orderFillTransaction")
        )
        if not created:
            raise BrokerTemporaryError(f"Unexpected OANDA response: {resp_data}")

        order.broker_ref   = str(created.get("orderID") or created.get("id", ""))
        order.status       = OrderStatus.SUBMITTED
        order.submitted_at = datetime.now(timezone.utc)

        # Market orders may fill immediately
        if "orderFillTransaction" in resp_data:
            fill_data = resp_data["orderFillTransaction"]
            fill = self._parse_fill(fill_data, order)
            order.apply_fill(fill)

        logger.info(
            "oanda_order_placed | order_id=%s broker_ref=%s",
            order.order_id, order.broker_ref,
        )
        return order

    async def cancel_order(self, broker_ref: str) -> bool:
        self._require_connection()
        try:
            await self._delete(f"/accounts/{self._account_id}/orders/{broker_ref}")
            return True
        except BrokerRejectedError:
            return False  # already filled / cancelled

    async def get_order_status(self, broker_ref: str) -> BrokerOrderSnapshot:
        self._require_connection()
        data = await self._get(f"/accounts/{self._account_id}/orders/{broker_ref}")
        order_data = data.get("order", data)
        return self._parse_order_snapshot(order_data)

    async def get_open_orders(self) -> list[BrokerOrderSnapshot]:
        self._require_connection()
        data = await self._get(f"/accounts/{self._account_id}/pendingOrders")
        return [self._parse_order_snapshot(o) for o in data.get("orders", [])]

    async def get_positions(self) -> list[BrokerPositionSnapshot]:
        self._require_connection()
        data = await self._get(f"/accounts/{self._account_id}/openPositions")
        snapshots = []
        for pos in data.get("positions", []):
            long_units  = Decimal(pos["long"]["units"])
            short_units = abs(Decimal(pos["short"]["units"]))
            symbol      = pos["instrument"].replace("_", "")  # "XAU_USD" → "XAUUSD"

            if long_units > 0:
                snapshots.append(BrokerPositionSnapshot(
                    symbol    = symbol,
                    side      = OrderSide.BUY,
                    quantity  = long_units,
                    avg_price = Decimal(pos["long"].get("averagePrice", "0")),
                ))
            if short_units > 0:
                snapshots.append(BrokerPositionSnapshot(
                    symbol    = symbol,
                    side      = OrderSide.SELL,
                    quantity  = short_units,
                    avg_price = Decimal(pos["short"].get("averagePrice", "0")),
                ))
        return snapshots

    async def get_account_equity(self) -> Decimal:
        self._require_connection()
        data = await self._get(f"/accounts/{self._account_id}/summary")
        return Decimal(data["account"]["NAV"])

    # ── HTTP helpers ─────────────────────────────────────────────────────────

    async def _get(self, path: str) -> dict:
        try:
            async with self._session.get(f"{self._base_url}{path}") as resp:
                return await self._handle_response(resp)
        except aiohttp.ClientError as exc:
            raise BrokerConnectionError(str(exc)) from exc

    async def _post(self, path: str, payload: dict) -> dict:
        try:
            async with self._session.post(
                f"{self._base_url}{path}", json=payload
            ) as resp:
                return await self._handle_response(resp)
        except aiohttp.ClientError as exc:
            raise BrokerConnectionError(str(exc)) from exc

    async def _delete(self, path: str) -> dict:
        try:
            async with self._session.delete(f"{self._base_url}{path}") as resp:
                return await self._handle_response(resp)
        except aiohttp.ClientError as exc:
            raise BrokerConnectionError(str(exc)) from exc

    async def _handle_response(self, resp) -> dict:
        if resp.status == 429:
            retry_after = float(resp.headers.get("Retry-After", "1"))
            raise RateLimitError("OANDA rate limit", retry_after)
        if resp.status in (500, 502, 503, 504):
            text = await resp.text()
            raise BrokerTemporaryError(f"OANDA server error {resp.status}: {text}", str(resp.status))
        if resp.status in (400, 404):
            text = await resp.text()
            raise BrokerRejectedError(f"OANDA rejected {resp.status}: {text}", str(resp.status))
        if resp.status == 401:
            raise BrokerConnectionError("OANDA authentication failed")

        try:
            return await resp.json()
        except Exception as exc:
            raise BrokerTemporaryError(f"Failed to parse OANDA response: {exc}") from exc

    # ── Payload builders ─────────────────────────────────────────────────────

    def _build_order_payload(self, order: ExecutionOrder) -> dict:
        # OANDA units: positive = buy, negative = sell
        units = str(order.quantity if order.side == OrderSide.BUY else -order.quantity)
        instrument = self._symbol_to_oanda(order.symbol)

        base: dict = {"units": units, "instrument": instrument}

        if order.order_type == OrderType.MARKET:
            base["type"] = "MARKET"
        elif order.order_type == OrderType.LIMIT:
            base["type"]  = "LIMIT"
            base["price"] = str(order.limit_price)
        elif order.order_type == OrderType.STOP_MARKET:
            base["type"]  = "STOP"
            base["price"] = str(order.stop_price)

        if order.stop_price and order.order_type == OrderType.MARKET:
            base["stopLossOnFill"] = {"price": str(order.stop_price)}

        return {"order": base}

    # ── Response parsers ─────────────────────────────────────────────────────

    def _parse_order_snapshot(self, data: dict) -> BrokerOrderSnapshot:
        raw_units  = Decimal(data.get("units", "0"))
        side       = OrderSide.BUY if raw_units >= 0 else OrderSide.SELL
        qty        = abs(raw_units)
        filled_qty = abs(Decimal(data.get("filledUnits", "0")))
        status_raw = data.get("state", "PENDING")
        status     = self._STATUS_MAP.get(status_raw, OrderStatus.UNKNOWN)

        return BrokerOrderSnapshot(
            broker_ref  = str(data.get("id", "")),
            symbol      = self._oanda_to_symbol(data.get("instrument", "")),
            side        = side,
            order_type  = OrderType.MARKET,
            quantity    = qty,
            filled_qty  = filled_qty,
            status      = status,
            avg_price   = Decimal(data["averagePrice"]) if "averagePrice" in data else None,
            timestamp   = self._parse_dt(data.get("createTime", "")),
        )

    def _parse_fill(self, data: dict, order: ExecutionOrder) -> Fill:
        return Fill(
            fill_id    = str(data.get("id", "")),
            order_id   = order.order_id,
            broker_ref = order.broker_ref,
            symbol     = order.symbol,
            side       = order.side,
            quantity   = abs(Decimal(data.get("units", str(order.quantity)))),
            price      = Decimal(data.get("price", "0")),
            commission = abs(Decimal(data.get("commission", "0"))),
            timestamp  = self._parse_dt(data.get("time", "")),
            is_partial = False,
        )

    @staticmethod
    def _symbol_to_oanda(symbol: str) -> str:
        if len(symbol) == 6:
            return f"{symbol[:3]}_{symbol[3:]}"
        return symbol

    @staticmethod
    def _oanda_to_symbol(instrument: str) -> str:
        return instrument.replace("_", "")

    @staticmethod
    def _parse_dt(s: str) -> datetime:
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return datetime.now(timezone.utc)

    def _require_connection(self) -> None:
        if not self._connected or self._session is None:
            raise BrokerConnectionError("OandaAdapter not connected — call connect() first")
