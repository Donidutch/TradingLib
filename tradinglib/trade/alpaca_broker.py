"""Alpaca broker adapter. Paper endpoints unless live is explicitly opted into.

Wraps ``alpaca-py``'s :class:`~alpaca.trading.client.TradingClient`. The SDK is
synchronous, so every call is dispatched with :func:`asyncio.to_thread` and the
FastAPI event loop is never blocked.

Safety (``docs/SPEC.md`` section 2, decision D5)
-------------------------------------------

``paper`` defaults to ``True``, which points the SDK at
``https://paper-api.alpaca.markets``. Constructing the adapter with
``paper=False`` additionally requires the environment variable
``TRADINGLIB_ALLOW_LIVE_TRADING`` to be set - without it the constructor raises
:class:`PermissionError`. Agents, the API routes and the test suite never set
it, so no code path in TradingLib can reach a live venue by accident.

With no credentials configured the adapter does not pretend: :meth:`status`
reports ``not_configured`` and every operation raises
:class:`~tradinglib.data.base.ProviderNotConfigured`.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from typing import Any

from tradinglib.config import get_settings
from tradinglib.data.base import (
    ProviderNotConfigured,
    ProviderUnavailable,
    to_utc,
    utcnow,
)
from tradinglib.trade.portfolio import (
    Broker,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Portfolio,
    Position,
    TimeInForce,
)

logger = logging.getLogger(__name__)

#: Environment variable a human must set before a live session may be built.
LIVE_OPT_IN_ENV: str = "TRADINGLIB_ALLOW_LIVE_TRADING"

#: Alpaca order states mapped onto TradingLib's smaller lifecycle.
_STATUS_MAP: dict[str, OrderStatus] = {
    "new": OrderStatus.NEW,
    "accepted": OrderStatus.NEW,
    "pending_new": OrderStatus.NEW,
    "accepted_for_bidding": OrderStatus.NEW,
    "held": OrderStatus.NEW,
    "calculated": OrderStatus.NEW,
    "pending_cancel": OrderStatus.NEW,
    "pending_replace": OrderStatus.NEW,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "filled": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELLED,
    "cancelled": OrderStatus.CANCELLED,
    "expired": OrderStatus.CANCELLED,
    "replaced": OrderStatus.CANCELLED,
    "done_for_day": OrderStatus.CANCELLED,
    "stopped": OrderStatus.CANCELLED,
    "rejected": OrderStatus.REJECTED,
    "suspended": OrderStatus.REJECTED,
}


def live_trading_opted_in() -> bool:
    """Whether the human opt-in for live trading is present in the environment."""
    raw = os.environ.get(LIVE_OPT_IN_ENV)
    return bool(raw) and raw.strip().lower() in {"1", "true", "yes", "on"}


class AlpacaBroker(Broker):
    """Adapter over ``alpaca-py``'s trading client."""

    name = "alpaca"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        secret_key: str | None = None,
        paper: bool = True,
    ) -> None:
        """Create the adapter.

        Args:
            api_key: Alpaca key id; falls back to configured settings.
            secret_key: Alpaca secret; falls back to configured settings.
            paper: keep ``True`` unless a human deliberately enables live
                trading. Agents and tests must never pass ``False``.

        Raises:
            PermissionError: if ``paper`` is ``False`` and the
                :data:`LIVE_OPT_IN_ENV` opt-in is absent.
        """
        if not paper and not live_trading_opted_in():
            raise PermissionError(
                "live Alpaca trading is disabled: set "
                f"{LIVE_OPT_IN_ENV}=1 to allow a live session (TradingLib's own code never does)"
            )
        settings = get_settings()
        self.api_key = api_key or settings.alpaca_api_key
        self.secret_key = secret_key or settings.alpaca_secret_key
        self.paper = paper
        self._client: Any | None = None
        self._account: Any | None = None
        self._connected_at: datetime | None = None
        self.last_error: str | None = None

    @property
    def configured(self) -> bool:
        """Whether credentials are present."""
        return bool(self.api_key and self.secret_key)

    @property
    def base_url(self) -> str:
        """The REST endpoint this adapter talks to."""
        return (
            "https://paper-api.alpaca.markets"
            if self.paper
            else "https://api.alpaca.markets"
        )

    def _require_credentials(self) -> None:
        """Raise when the adapter has no credentials to work with."""
        if not self.configured:
            raise ProviderNotConfigured(
                self.name,
                "ALPACA_API_KEY / ALPACA_SECRET_KEY are not configured",
            )

    def _require_client(self) -> Any:
        """Return the trading client, raising when it has not connected."""
        if self._client is None:
            raise ProviderUnavailable(self.name, "not connected; call connect() first")
        return self._client

    async def connect(self) -> None:
        """Create the trading client and verify the account. Idempotent.

        Raises:
            tradinglib.data.base.ProviderNotConfigured: if credentials are absent.
            tradinglib.data.base.ProviderUnavailable: if the SDK is missing or
                Alpaca refused the credentials.
        """
        if self._client is not None:
            return
        self._require_credentials()
        try:
            from alpaca.trading.client import TradingClient
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise ProviderUnavailable(self.name, f"alpaca-py is not installed: {exc}") from exc

        def _build() -> tuple[Any, Any]:
            client = TradingClient(
                api_key=self.api_key,
                secret_key=self.secret_key,
                paper=self.paper,
            )
            return client, client.get_account()

        try:
            client, account = await asyncio.to_thread(_build)
        except Exception as exc:  # noqa: BLE001 - the vendor SDK raises its own types
            self.last_error = f"{type(exc).__name__}: {exc}"
            raise ProviderUnavailable(self.name, self.last_error) from exc
        self._client = client
        self._account = account
        self._connected_at = utcnow()
        self.last_error = None
        logger.info(
            "alpaca broker connected (%s, account %s)",
            "paper" if self.paper else "LIVE",
            getattr(account, "account_number", "?"),
        )

    async def close(self) -> None:
        """Release the trading client."""
        self._client = None
        self._account = None
        self._connected_at = None

    async def get_portfolio(self) -> Portfolio:
        """Return account cash and positions from Alpaca."""
        client = self._require_client()

        def _fetch() -> tuple[Any, list[Any]]:
            return client.get_account(), list(client.get_all_positions())

        try:
            account, positions = await asyncio.to_thread(_fetch)
        except Exception as exc:  # noqa: BLE001 - vendor SDK
            self.last_error = f"{type(exc).__name__}: {exc}"
            raise ProviderUnavailable(self.name, self.last_error) from exc
        self._account = account

        portfolio = Portfolio(
            account_id=str(getattr(account, "account_number", "") or getattr(account, "id", "")),
            cash=_float(getattr(account, "cash", None)) or 0.0,
            broker=self.name,
            currency=str(getattr(account, "currency", None) or "USD"),
            updated_at=utcnow(),
        )
        for raw in positions:
            symbol = str(raw.symbol).upper()
            qty = _float(raw.qty) or 0.0
            if str(getattr(raw, "side", "long")).endswith("short") and qty > 0:
                qty = -qty
            portfolio.positions[symbol] = Position(
                symbol=symbol,
                qty=qty,
                avg_price=_float(raw.avg_entry_price) or 0.0,
                market_price=_float(getattr(raw, "current_price", None)),
            )
        return portfolio

    async def submit_order(
        self,
        symbol: str,
        side: OrderSide,
        qty: float,
        *,
        type: OrderType = OrderType.MARKET,
        limit_price: float | None = None,
        stop_price: float | None = None,
        time_in_force: TimeInForce = TimeInForce.DAY,
    ) -> Order:
        """Submit an order to Alpaca (paper unless live was opted into).

        Raises:
            ValueError: if the order is not valid for its type.
            tradinglib.data.base.ProviderUnavailable: if Alpaca refused the order.
        """
        client = self._require_client()
        request = _build_order_request(
            symbol=symbol.strip().upper(),
            side=OrderSide(side),
            qty=float(qty),
            order_type=OrderType(type),
            limit_price=limit_price,
            stop_price=stop_price,
            time_in_force=TimeInForce(time_in_force),
        )
        logger.info(
            "submitting %s alpaca order: %s %g %s",
            "paper" if self.paper else "LIVE",
            side,
            qty,
            symbol,
        )
        try:
            raw = await asyncio.to_thread(client.submit_order, request)
        except Exception as exc:  # noqa: BLE001 - vendor SDK
            self.last_error = f"{type(exc).__name__}: {exc}"
            raise ProviderUnavailable(self.name, self.last_error) from exc
        return self._to_order(raw)

    async def cancel_order(self, order_id: str) -> Order:
        """Cancel an open Alpaca order and return its updated record."""
        client = self._require_client()

        def _cancel() -> Any:
            client.cancel_order_by_id(order_id)
            return client.get_order_by_id(order_id)

        try:
            raw = await asyncio.to_thread(_cancel)
        except Exception as exc:  # noqa: BLE001 - vendor SDK
            self.last_error = f"{type(exc).__name__}: {exc}"
            raise ProviderUnavailable(self.name, self.last_error) from exc
        return self._to_order(raw)

    async def list_orders(self, *, limit: int = 100) -> list[Order]:
        """Return recent Alpaca orders, newest first."""
        client = self._require_client()
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        request = GetOrdersRequest(status=QueryOrderStatus.ALL, limit=limit, direction="desc")
        try:
            raw_orders = await asyncio.to_thread(client.get_orders, request)
        except Exception as exc:  # noqa: BLE001 - vendor SDK
            self.last_error = f"{type(exc).__name__}: {exc}"
            raise ProviderUnavailable(self.name, self.last_error) from exc
        return [self._to_order(raw) for raw in raw_orders]

    def _to_order(self, raw: Any) -> Order:
        """Map an ``alpaca.trading.models.Order`` onto TradingLib's :class:`Order`."""
        account_id = str(getattr(self._account, "account_number", "") or "alpaca")
        qty = _float(getattr(raw, "qty", None)) or 0.0
        order = Order(
            id=str(raw.id),
            symbol=str(raw.symbol).upper(),
            side=OrderSide(str(getattr(raw.side, "value", raw.side))),
            qty=qty if qty > 0 else 1.0,
            type=_map_order_type(getattr(raw, "order_type", None) or getattr(raw, "type", None)),
            limit_price=_float(getattr(raw, "limit_price", None)),
            stop_price=_float(getattr(raw, "stop_price", None)),
            status=_STATUS_MAP.get(
                str(getattr(raw.status, "value", raw.status)).lower(), OrderStatus.NEW
            ),
            filled_qty=_float(getattr(raw, "filled_qty", None)) or 0.0,
            avg_fill_price=_float(getattr(raw, "filled_avg_price", None)),
            submitted_at=_dt(
                getattr(raw, "submitted_at", None) or getattr(raw, "created_at", None)
            ),
            updated_at=_dt(getattr(raw, "updated_at", None)),
            broker=self.name,
            broker_order_id=str(raw.id),
            account_id=account_id,
            time_in_force=_map_tif(getattr(raw, "time_in_force", None)),
        )
        return order

    def status(self) -> dict[str, Any]:
        """Return honest configuration/connection status. Makes no network call."""
        account = self._account
        return {
            "broker": self.name,
            "status": (
                "up"
                if self._client is not None
                else ("not_configured" if not self.configured else "idle")
            ),
            "configured": self.configured,
            "connected": self._client is not None,
            "paper": self.paper,
            "live_capable": live_trading_opted_in(),
            "base_url": self.base_url,
            "account_id": None if account is None else str(getattr(account, "account_number", "")),
            "account_status": (
                None
                if account is None
                else str(getattr(getattr(account, "status", None), "value", account.status))
            ),
            "cash": None if account is None else _float(getattr(account, "cash", None)),
            "equity": None if account is None else _float(getattr(account, "equity", None)),
            "trading_blocked": None
            if account is None
            else bool(getattr(account, "trading_blocked", False)),
            "connected_at": None if self._connected_at is None else self._connected_at.isoformat(),
            "last_error": self.last_error,
            "detail": (
                "ALPACA_API_KEY / ALPACA_SECRET_KEY are not configured"
                if not self.configured
                else (
                    "connected"
                    if self._client is not None
                    else "credentials present; connect() has not run"
                )
            ),
        }


def _build_order_request(
    *,
    symbol: str,
    side: OrderSide,
    qty: float,
    order_type: OrderType,
    limit_price: float | None,
    stop_price: float | None,
    time_in_force: TimeInForce,
) -> Any:
    """Build the alpaca-py request object matching ``order_type``.

    Raises:
        ValueError: if a price required by the order type is missing.
    """
    from alpaca.trading.enums import OrderSide as AlpacaSide
    from alpaca.trading.enums import TimeInForce as AlpacaTIF
    from alpaca.trading.requests import (
        LimitOrderRequest,
        MarketOrderRequest,
        StopLimitOrderRequest,
        StopOrderRequest,
    )

    if qty <= 0:
        raise ValueError("order qty must be positive")
    common: dict[str, Any] = {
        "symbol": symbol,
        "qty": qty,
        "side": AlpacaSide(str(side)),
        "time_in_force": AlpacaTIF(str(time_in_force)),
    }
    if order_type is OrderType.MARKET:
        return MarketOrderRequest(**common)
    if order_type is OrderType.LIMIT:
        if limit_price is None:
            raise ValueError("a limit order requires a limit price")
        return LimitOrderRequest(limit_price=limit_price, **common)
    if order_type is OrderType.STOP:
        if stop_price is None:
            raise ValueError("a stop order requires a stop price")
        return StopOrderRequest(stop_price=stop_price, **common)
    if limit_price is None or stop_price is None:
        raise ValueError("a stop-limit order requires both a stop and a limit price")
    return StopLimitOrderRequest(stop_price=stop_price, limit_price=limit_price, **common)


def _map_order_type(raw: Any) -> OrderType:
    """Map an Alpaca order type onto :class:`OrderType`, defaulting to market."""
    value = str(getattr(raw, "value", raw) or "market").lower()
    try:
        return OrderType(value)
    except ValueError:
        # trailing_stop and anything Alpaca adds later: report the closest
        # supported shape rather than crashing the portfolio view.
        return OrderType.STOP if "stop" in value else OrderType.MARKET


def _map_tif(raw: Any) -> TimeInForce:
    """Map an Alpaca time-in-force onto :class:`TimeInForce`."""
    value = str(getattr(raw, "value", raw) or "day").lower()
    return TimeInForce.GTC if value == "gtc" else TimeInForce.DAY


def _float(value: Any) -> float | None:
    """Coerce an Alpaca string/decimal field to ``float``, or ``None``."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _dt(value: Any) -> datetime | None:
    """Coerce an Alpaca timestamp to an aware UTC datetime, or ``None``."""
    if isinstance(value, datetime):
        return to_utc(value)
    return None


__all__ = ["LIVE_OPT_IN_ENV", "AlpacaBroker", "live_trading_opted_in"]
