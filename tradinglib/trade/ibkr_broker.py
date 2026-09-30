"""Interactive Brokers adapter via ``ib_async`` (successor of ib_insync).

Requires a running TWS or IB Gateway. Paper ports (7497 / 4002) are the
default; a live port must be supplied deliberately by a human.

Safety (``docs/SPEC.md`` section 2, decision D5)
-------------------------------------------

Three independent guards stand between TradingLib and a live IB account:

1. the constructor refuses a live port while ``paper=True``;
2. ``paper=False`` requires the ``TRADINGLIB_ALLOW_LIVE_TRADING`` opt-in that the
   Alpaca adapter uses, which nothing in TradingLib ever sets;
3. after connecting in paper mode the adapter reads the managed account codes
   and disconnects unless they are IB paper accounts (paper accounts are the
   ``D``-prefixed ``DU``/``DF`` codes; live accounts are ``U``-prefixed).

Absence of a gateway is not an error condition to hide: with nothing listening
on the configured port :meth:`IBKRBroker.status` says so and :meth:`connect`
raises :class:`~tradinglib.data.base.ProviderUnavailable` naming the host and port.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from datetime import datetime
from typing import Any, Final

from tradinglib.data.base import ProviderUnavailable, to_utc, utcnow
from tradinglib.trade.alpaca_broker import LIVE_OPT_IN_ENV, live_trading_opted_in
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

#: TWS/Gateway ports that reach a *live* account.
LIVE_PORTS: Final[frozenset[int]] = frozenset({7496, 4001})

#: TWS/Gateway ports that reach a *paper* account.
PAPER_PORTS: Final[frozenset[int]] = frozenset({7497, 4002})

#: IB order states mapped onto TradingLib's smaller lifecycle.
_STATUS_MAP: dict[str, OrderStatus] = {
    "pendingsubmit": OrderStatus.NEW,
    "pendingcancel": OrderStatus.NEW,
    "presubmitted": OrderStatus.NEW,
    "submitted": OrderStatus.NEW,
    "apipending": OrderStatus.NEW,
    "filled": OrderStatus.FILLED,
    "cancelled": OrderStatus.CANCELLED,
    "apicancelled": OrderStatus.CANCELLED,
    "inactive": OrderStatus.REJECTED,
}


def port_is_open(host: str, port: int, *, timeout: float = 0.4) -> bool:
    """Return whether something is listening on ``host:port``.

    Used by :meth:`IBKRBroker.status` so the trade endpoints can report "no
    gateway running" without opening an IB API session.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class IBKRBroker(Broker):
    """Adapter over ``ib_async``'s ``IB`` client."""

    name = "ibkr"

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 7497,
        client_id: int = 17,
        paper: bool = True,
        timeout: float = 5.0,
        account: str = "",
    ) -> None:
        """Create the adapter.

        Args:
            host: TWS/Gateway host.
            port: 7497/4002 are paper, 7496/4001 are live.
            client_id: IB API client id.
            paper: guard flag; agents and tests must never pass ``False``.
            timeout: connection timeout in seconds.
            account: IB account code to act on; empty means the only managed
                account, which is the usual single-account setup.

        Raises:
            PermissionError: if ``paper`` is ``True`` but ``port`` is a live
                port, or if ``paper`` is ``False`` without the
                ``TRADINGLIB_ALLOW_LIVE_TRADING`` opt-in.
        """
        if paper and port in LIVE_PORTS:
            raise PermissionError(
                f"port {port} is an Interactive Brokers live port; "
                "use 7497 (TWS paper) or 4002 (Gateway paper)"
            )
        if not paper and not live_trading_opted_in():
            raise PermissionError(
                "live IBKR trading is disabled: set "
                f"{LIVE_OPT_IN_ENV}=1 to allow a live session (TradingLib's own code never does)"
            )
        self.host = host
        self.port = port
        self.client_id = client_id
        self.paper = paper
        self.timeout = timeout
        self.account = account
        self._ib: Any | None = None
        self._accounts: list[str] = []
        self._connected_at: datetime | None = None
        self.last_error: str | None = None

    # --- plumbing ---------------------------------------------------------
    @property
    def connected(self) -> bool:
        """Whether an IB API session is currently up."""
        return self._ib is not None and bool(self._ib.isConnected())

    def _require_ib(self) -> Any:
        """Return the connected ``IB`` client, or raise honestly."""
        if not self.connected:
            raise ProviderUnavailable(
                self.name,
                f"not connected to TWS/Gateway at {self.host}:{self.port}; call connect() first",
            )
        assert self._ib is not None  # noqa: S101 - established by self.connected
        return self._ib

    def _resolve_account(self) -> str:
        """Return the account code to act on (explicit, or the only managed one)."""
        if self.account:
            return self.account
        return self._accounts[0] if self._accounts else ""

    async def connect(self) -> None:
        """Connect to TWS/Gateway and confirm the account is a paper account.

        Raises:
            tradinglib.data.base.ProviderUnavailable: if ``ib_async`` is missing or
                nothing is listening on the configured host and port.
            PermissionError: if paper mode was requested but the gateway is
                logged into a live account.
        """
        if self.connected:
            return
        try:
            from ib_async import IB
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise ProviderUnavailable(self.name, f"ib_async is not installed: {exc}") from exc

        ib = IB()
        try:
            await ib.connectAsync(
                host=self.host,
                port=self.port,
                clientId=self.client_id,
                timeout=self.timeout,
                account=self.account,
            )
        except (OSError, TimeoutError) as exc:
            ib.disconnect()
            self.last_error = f"{type(exc).__name__}: {exc}"
            raise ProviderUnavailable(
                self.name,
                f"no TWS/IB Gateway reachable at {self.host}:{self.port} ({self.last_error})",
            ) from exc
        except Exception as exc:  # noqa: BLE001 - the IB API raises its own types
            ib.disconnect()
            self.last_error = f"{type(exc).__name__}: {exc}"
            raise ProviderUnavailable(self.name, self.last_error) from exc

        accounts = [str(a) for a in ib.managedAccounts()]
        if self.paper and accounts and not all(a.upper().startswith("D") for a in accounts):
            ib.disconnect()
            self.last_error = "gateway is logged into a live account"
            raise PermissionError(
                "refusing to trade: paper mode was requested but TWS/Gateway at "
                f"{self.host}:{self.port} is logged into account(s) {', '.join(accounts)}, "
                "which are not IB paper accounts"
            )
        self._ib = ib
        self._accounts = accounts
        self._connected_at = utcnow()
        self.last_error = None
        logger.info(
            "ibkr broker connected to %s:%s (%s, accounts=%s)",
            self.host,
            self.port,
            "paper" if self.paper else "LIVE",
            ",".join(accounts) or "none reported",
        )

    async def close(self) -> None:
        """Disconnect from TWS/Gateway."""
        ib, self._ib = self._ib, None
        self._accounts = []
        self._connected_at = None
        if ib is not None:
            try:
                ib.disconnect()
            except Exception:  # noqa: BLE001 - a disconnect must never raise upward
                logger.debug("ibkr disconnect raised", exc_info=True)

    # --- portfolio --------------------------------------------------------
    async def get_portfolio(self) -> Portfolio:
        """Return account cash and positions from IBKR."""
        ib = self._require_ib()
        account = self._resolve_account()
        cash: float | None = None
        for value in ib.accountValues(account):
            if value.tag == "TotalCashValue" and value.currency in ("USD", "BASE", ""):
                cash = _float(value.value)
                if value.currency == "USD":
                    break
        portfolio = Portfolio(
            account_id=account or "ibkr",
            cash=cash if cash is not None else 0.0,
            broker=self.name,
            updated_at=utcnow(),
        )
        for item in ib.portfolio(account):
            symbol = str(getattr(item.contract, "symbol", "") or "").upper()
            if not symbol:
                continue
            portfolio.positions[symbol] = Position(
                symbol=symbol,
                qty=float(item.position),
                avg_price=float(item.averageCost),
                market_price=_float(item.marketPrice),
                realized_pnl=_float(item.realizedPNL) or 0.0,
            )
        if cash is None:
            logger.warning("ibkr: TotalCashValue was not reported by the gateway")
        return portfolio

    # --- orders -----------------------------------------------------------
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
        """Submit an order through the IB API.

        Raises:
            ValueError: if the order is invalid or the symbol cannot be
                qualified into an IB contract.
        """
        ib = self._require_ib()
        from ib_async import LimitOrder, MarketOrder, Stock, StopLimitOrder, StopOrder

        normalised = symbol.strip().upper()
        if qty <= 0:
            raise ValueError("order qty must be positive")
        action = "BUY" if OrderSide(side) is OrderSide.BUY else "SELL"
        tif = "GTC" if TimeInForce(time_in_force) is TimeInForce.GTC else "DAY"

        contracts = await ib.qualifyContractsAsync(Stock(normalised, "SMART", "USD"))
        if not contracts:
            raise ValueError(f"IBKR could not resolve a US equity contract for {normalised}")
        contract = contracts[0]

        order_type = OrderType(type)
        if order_type is OrderType.MARKET:
            ib_order = MarketOrder(action, qty, tif=tif)
        elif order_type is OrderType.LIMIT:
            if limit_price is None:
                raise ValueError("a limit order requires a limit price")
            ib_order = LimitOrder(action, qty, limit_price, tif=tif)
        elif order_type is OrderType.STOP:
            if stop_price is None:
                raise ValueError("a stop order requires a stop price")
            ib_order = StopOrder(action, qty, stop_price, tif=tif)
        else:
            if limit_price is None or stop_price is None:
                raise ValueError("a stop-limit order requires both a stop and a limit price")
            ib_order = StopLimitOrder(action, qty, limit_price, stop_price, tif=tif)

        logger.info(
            "submitting %s ibkr order: %s %g %s",
            "paper" if self.paper else "LIVE",
            action,
            qty,
            normalised,
        )
        trade = ib.placeOrder(contract, ib_order)
        # Let the gateway acknowledge before reporting a status back.
        await asyncio.sleep(0)
        return self._to_order(trade)

    async def cancel_order(self, order_id: str) -> Order:
        """Cancel an open IB order.

        Raises:
            KeyError: if no live trade carries that order id.
        """
        ib = self._require_ib()
        trade = self._find_trade(ib, order_id)
        ib.cancelOrder(trade.order)
        await asyncio.sleep(0)
        return self._to_order(trade)

    async def list_orders(self, *, limit: int = 100) -> list[Order]:
        """Return recent IB orders, newest first."""
        ib = self._require_ib()
        try:
            await ib.reqAllOpenOrdersAsync()
        except Exception as exc:  # noqa: BLE001 - IB API
            logger.debug("ibkr reqAllOpenOrders failed: %s", exc, exc_info=True)
        orders = [self._to_order(trade) for trade in ib.trades()]
        orders.sort(key=lambda o: (o.submitted_at or utcnow()), reverse=True)
        return orders[:limit]

    def _find_trade(self, ib: Any, order_id: str) -> Any:
        """Return the ``Trade`` whose order matches ``order_id``.

        Raises:
            KeyError: when no trade matches.
        """
        for trade in ib.trades():
            if _order_key(trade.order) == str(order_id):
                return trade
        raise KeyError(f"unknown IBKR order: {order_id}")

    def _to_order(self, trade: Any) -> Order:
        """Map an ``ib_async`` ``Trade`` onto TradingLib's :class:`Order`."""
        ib_order = trade.order
        state = trade.orderStatus
        raw_status = str(getattr(state, "status", "") or "").lower()
        filled = float(getattr(state, "filled", 0.0) or 0.0)
        status = _STATUS_MAP.get(raw_status, OrderStatus.NEW)
        if status is OrderStatus.NEW and filled > 0:
            status = OrderStatus.PARTIALLY_FILLED
        qty = float(getattr(ib_order, "totalQuantity", 0.0) or 0.0)
        limit_price = _positive(getattr(ib_order, "lmtPrice", None))
        stop_price = _positive(getattr(ib_order, "auxPrice", None))
        return Order(
            id=_order_key(ib_order),
            symbol=str(getattr(trade.contract, "symbol", "") or "?").upper(),
            side=OrderSide.BUY if str(ib_order.action).upper() == "BUY" else OrderSide.SELL,
            qty=qty if qty > 0 else 1.0,
            type=_map_order_type(str(getattr(ib_order, "orderType", "MKT"))),
            limit_price=limit_price,
            stop_price=stop_price,
            status=status,
            filled_qty=filled,
            avg_fill_price=_positive(getattr(state, "avgFillPrice", None)),
            submitted_at=_first_log_time(trade),
            updated_at=utcnow(),
            broker=self.name,
            broker_order_id=_order_key(ib_order),
            account_id=self._resolve_account() or "ibkr",
            time_in_force=(
                TimeInForce.GTC
                if str(getattr(ib_order, "tif", "DAY")).upper() == "GTC"
                else TimeInForce.DAY
            ),
            note=str(getattr(state, "whyHeld", "") or "") or None,
        )

    # --- status -----------------------------------------------------------
    def status(self) -> dict[str, Any]:
        """Return honest connection status (gateway reachable, account mode)."""
        connected = self.connected
        gateway_up = connected or port_is_open(self.host, self.port)
        if connected:
            state = "up"
            detail = f"connected to TWS/Gateway at {self.host}:{self.port}"
        elif gateway_up:
            state = "idle"
            detail = (
                f"TWS/Gateway is listening on {self.host}:{self.port}; connect() has not run"
            )
        else:
            state = "unavailable"
            detail = (
                f"no TWS or IB Gateway is listening on {self.host}:{self.port} - "
                "start it and enable API connections"
            )
        return {
            "broker": self.name,
            "status": state,
            # IBKR needs no API keys: the gateway itself carries the login, so
            # "configured" means "a gateway is reachable", not "a key is set".
            "configured": gateway_up,
            "connected": connected,
            "paper": self.paper,
            "live_capable": live_trading_opted_in(),
            "host": self.host,
            "port": self.port,
            "port_kind": _port_kind(self.port),
            "client_id": self.client_id,
            "gateway_reachable": gateway_up,
            "accounts": list(self._accounts),
            "account_id": self._resolve_account() or None,
            "connected_at": None if self._connected_at is None else self._connected_at.isoformat(),
            "last_error": self.last_error,
            "detail": detail,
        }


def _port_kind(port: int) -> str:
    """Describe a TWS/Gateway port as ``paper``, ``live`` or ``custom``."""
    if port in PAPER_PORTS:
        return "paper"
    return "live" if port in LIVE_PORTS else "custom"


def _order_key(ib_order: Any) -> str:
    """Return a stable string id for an IB order (permId, else orderId)."""
    perm = getattr(ib_order, "permId", 0)
    if perm:
        return str(perm)
    return str(getattr(ib_order, "orderId", "") or "0")


def _map_order_type(raw: str) -> OrderType:
    """Map an IB order type code onto :class:`OrderType`."""
    return {
        "MKT": OrderType.MARKET,
        "LMT": OrderType.LIMIT,
        "STP": OrderType.STOP,
        "STP LMT": OrderType.STOP_LIMIT,
    }.get(raw.upper(), OrderType.MARKET)


def _first_log_time(trade: Any) -> datetime | None:
    """Return the time of a trade's first log entry, when IB supplied one."""
    log = list(getattr(trade, "log", []) or [])
    if not log:
        return None
    stamp = getattr(log[0], "time", None)
    return to_utc(stamp) if isinstance(stamp, datetime) else None


def _float(value: Any) -> float | None:
    """Coerce an IB string/decimal field to ``float``, or ``None``."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _positive(value: Any) -> float | None:
    """Return ``value`` as a float when it is a real price; IB uses 0.0 for none."""
    number = _float(value)
    return number if number and number > 0 else None


__all__ = ["LIVE_PORTS", "PAPER_PORTS", "IBKRBroker", "port_is_open"]
