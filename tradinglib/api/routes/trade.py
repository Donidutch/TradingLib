"""Trading endpoints (``docs/SPEC.md`` section 5: ``/api/trade/*``, paper default).

There is no UI surface for trading yet - these endpoints exist so the execution
layer is reachable and testable from outside the process, and so the honest
status of each adapter can be inspected.

Three rules shape this router:

* **Paper is the default.** ``broker`` defaults to ``paper``; every adapter is
  constructed in paper/sandbox mode and :func:`_require_paper` refuses to place
  or cancel an order on any broker that does not report ``paper: true``. Going
  live needs a deliberate human opt-in outside TradingLib's own code
  (``TRADINGLIB_ALLOW_LIVE_TRADING``), which this router never sets.
* **Status never lies.** ``GET /api/trade/brokers`` opens no session: it reports
  what each adapter can currently say for itself - Alpaca without credentials
  reads ``not_configured``, IBKR with no gateway listening reads
  ``unavailable``.
* **Adapters are interchangeable.** Every response is built from the
  :class:`~tradinglib.trade.portfolio.Broker` interface, so the payload shape is the
  same whichever venue answered.

Brokers are created on first use and cached on ``app.state.brokers``;
:func:`close_brokers` releases them when the application shuts down.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from tradinglib.api.deps import translate_errors
from tradinglib.trade.alpaca_broker import AlpacaBroker
from tradinglib.trade.ibkr_broker import IBKRBroker
from tradinglib.trade.paper import DEFAULT_ACCOUNT_ID, PaperBroker
from tradinglib.trade.portfolio import (
    Broker,
    OrderSide,
    OrderType,
    PortfolioStore,
    TimeInForce,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/trade", tags=["trade"])

#: Adapters this router can build, in the order the brokers list reports them.
BROKER_NAMES: tuple[str, ...] = ("paper", "alpaca", "ibkr")

#: The adapter used when a request does not name one.
DEFAULT_BROKER: str = "paper"

BrokerName = Literal["paper", "alpaca", "ibkr"]


def _env_float(name: str, default: float) -> float:
    """Read a float environment setting, falling back to ``default``."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number; using %s", name, raw, default)
        return default


def _env_int(name: str, default: int) -> int:
    """Read an int environment setting, falling back to ``default``."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer; using %s", name, raw, default)
        return default


# --------------------------------------------------------------------------
# broker registry
# --------------------------------------------------------------------------
def _registry(request: Request) -> dict[str, Broker]:
    """Return (creating if needed) the per-application broker cache."""
    brokers = getattr(request.app.state, "brokers", None)
    if not isinstance(brokers, dict):
        brokers = {}
        request.app.state.brokers = brokers
    return brokers


def _build_paper(request: Request) -> PaperBroker:
    """Construct the paper broker for this application.

    The quote source is ``app.state.trade_quote_provider`` when one is
    installed (tests inject a deterministic provider there) and otherwise the
    library default, which is real yfinance data. When the application has an
    event bus, the broker is attached to the ``tape`` channel so resting orders
    are re-evaluated against the same live prices the UI receives.
    """
    broker = PaperBroker(
        account_id=os.environ.get("TRADINGLIB_PAPER_ACCOUNT", DEFAULT_ACCOUNT_ID),
        starting_cash=_env_float("TRADINGLIB_PAPER_CASH", 1_000_000.0),
        slippage_bps=_env_float("TRADINGLIB_PAPER_SLIPPAGE_BPS", 5.0),
        commission_per_share=_env_float("TRADINGLIB_PAPER_COMMISSION_PER_SHARE", 0.0),
        commission_min=_env_float("TRADINGLIB_PAPER_COMMISSION_MIN", 0.0),
        provider=getattr(request.app.state, "trade_quote_provider", None),
    )
    bus = getattr(request.app.state, "bus", None)
    if bus is not None:
        broker.attach(bus)
    return broker


def _build_broker(request: Request, name: str) -> Broker:
    """Construct one adapter, always in paper/sandbox mode."""
    if name == "paper":
        return _build_paper(request)
    if name == "alpaca":
        return AlpacaBroker(paper=True)
    return IBKRBroker(
        host=os.environ.get("TRADINGLIB_IBKR_HOST", "127.0.0.1"),
        port=_env_int("TRADINGLIB_IBKR_PORT", 7497),
        client_id=_env_int("TRADINGLIB_IBKR_CLIENT_ID", 17),
        paper=True,
    )


def get_broker(request: Request, name: str = DEFAULT_BROKER) -> Broker:
    """Return the cached adapter called ``name``, building it on first use.

    Raises:
        HTTPException: 400 when ``name`` is not one of :data:`BROKER_NAMES`.
    """
    if name not in BROKER_NAMES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "unknown_broker",
                "entity": "broker",
                "detail": f"unknown broker {name!r}; known brokers: {', '.join(BROKER_NAMES)}",
            },
        )
    brokers = _registry(request)
    broker = brokers.get(name)
    if broker is None:
        broker = _build_broker(request, name)
        brokers[name] = broker
    return broker


def _require_paper(broker: Broker) -> None:
    """Refuse an order-placing action on an adapter that is not in paper mode.

    Raises:
        HTTPException: 409 if the adapter reports ``paper: false``.
    """
    if broker.status().get("paper") is not True:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "live_trading_refused",
                "entity": "order",
                "detail": (
                    f"the {broker.name} adapter is not in paper mode; "
                    "the API refuses to route live orders"
                ),
            },
        )


async def _connected(broker: Broker) -> Broker:
    """Connect the adapter if it is not connected yet, translating failures."""
    with translate_errors("broker"):
        await broker.connect()
    return broker


async def close_brokers(app: Any) -> None:
    """Close every cached adapter. Called from the application lifespan."""
    brokers: dict[str, Broker] = getattr(app.state, "brokers", None) or {}
    for name, broker in list(brokers.items()):
        try:
            await broker.close()
        except Exception:  # noqa: BLE001 - shutdown must not raise
            logger.warning("closing the %s broker failed", name, exc_info=True)
    brokers.clear()


# --------------------------------------------------------------------------
# request models
# --------------------------------------------------------------------------
class OrderRequest(BaseModel):
    """Body of ``POST /api/trade/orders``."""

    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1, max_length=16, description="ticker, e.g. AAPL")
    side: Literal["buy", "sell"]
    qty: float = Field(gt=0, description="quantity in shares")
    type: Literal["market", "limit", "stop", "stop_limit"] = "market"
    limit_price: float | None = Field(default=None, gt=0)
    stop_price: float | None = Field(default=None, gt=0)
    time_in_force: Literal["day", "gtc"] | None = Field(
        default=None,
        description="defaults to gtc on the paper broker (it has no session calendar) "
        "and day on a live venue",
    )
    broker: BrokerName = DEFAULT_BROKER


class QuoteRequest(BaseModel):
    """Body of ``POST /api/trade/paper/quote``."""

    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1, max_length=16)
    price: float = Field(gt=0, description="last trade price")
    bid: float | None = Field(default=None, gt=0)
    ask: float | None = Field(default=None, gt=0)


class ResetRequest(BaseModel):
    """Body of ``POST /api/trade/paper/reset``."""

    model_config = ConfigDict(extra="ignore")

    starting_cash: float | None = Field(
        default=None, gt=0, description="new opening balance; omit to keep the current one"
    )


BrokerQuery = Annotated[BrokerName, Query(description="execution adapter; paper by default")]


# --------------------------------------------------------------------------
# endpoints
# --------------------------------------------------------------------------
@router.get("/brokers", summary="Adapter status")
async def list_brokers(request: Request) -> dict[str, Any]:
    """Report every adapter's real status without opening a session.

    Nothing is fabricated: an adapter with no credentials says
    ``not_configured``, and an IBKR gateway that is not running says
    ``unavailable`` with the host and port it looked at.
    """
    statuses = [get_broker(request, name).status() for name in BROKER_NAMES]
    return {
        "brokers": statuses,
        "default": DEFAULT_BROKER,
        "count": len(statuses),
        "live_trading_enabled": any(s.get("paper") is False for s in statuses),
    }


@router.get("/accounts", summary="Locally tracked accounts")
async def list_accounts() -> dict[str, Any]:
    """List the accounts the local portfolio store holds (paper accounts)."""
    store = PortfolioStore()
    accounts = await asyncio.to_thread(store.list_accounts)
    return {"accounts": [a.as_dict() for a in accounts], "count": len(accounts)}


@router.get("/portfolio", summary="Cash, positions and P&L")
async def get_portfolio(request: Request, broker: BrokerQuery = DEFAULT_BROKER) -> dict[str, Any]:
    """Return the account's cash, positions and P&L from the selected adapter.

    Positions are marked to the freshest quote available. A symbol that cannot
    be marked is listed in ``unmarked_symbols`` and leaves ``equity`` ``null``
    rather than being valued at a stale or invented price.
    """
    adapter = await _connected(get_broker(request, broker))
    with translate_errors("portfolio"):
        portfolio = await adapter.get_portfolio()
    payload = portfolio.to_dict()
    payload["broker_status"] = adapter.status()
    return payload


@router.get("/orders", summary="Recent orders")
async def list_orders(
    request: Request,
    broker: BrokerQuery = DEFAULT_BROKER,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    """Return the selected adapter's recent orders, newest first."""
    adapter = await _connected(get_broker(request, broker))
    with translate_errors("order"):
        orders = await adapter.list_orders(limit=limit)
    return {
        "orders": [order.as_dict() for order in orders],
        "count": len(orders),
        "open": sum(1 for order in orders if order.is_open),
        "broker": adapter.name,
    }


@router.post("/orders", summary="Place an order (paper only)")
async def submit_order(
    request: Request, body: Annotated[OrderRequest, Body()]
) -> dict[str, Any]:
    """Place an order on the selected adapter, which must be in paper mode.

    Returns the order record. A paper market order comes back ``filled``; a
    limit or stop order whose condition is not met yet comes back ``new`` and
    rests until the tape satisfies it. An order the account cannot afford comes
    back ``rejected`` with the reason in ``note`` - it is recorded, not hidden.
    """
    adapter = get_broker(request, body.broker)
    _require_paper(adapter)
    await _connected(adapter)
    tif = TimeInForce(body.time_in_force) if body.time_in_force else _default_tif(adapter)
    with translate_errors("order"):
        order = await adapter.submit_order(
            body.symbol,
            OrderSide(body.side),
            body.qty,
            type=OrderType(body.type),
            limit_price=body.limit_price,
            stop_price=body.stop_price,
            time_in_force=tif,
        )
    return {"order": order.as_dict(), "broker": adapter.name}


def _default_tif(broker: Broker) -> TimeInForce:
    """Paper orders rest until cancelled; live venues default to the session."""
    return TimeInForce.GTC if broker.name == "paper" else TimeInForce.DAY


@router.delete("/orders/{order_id}", summary="Cancel an order (paper only)")
async def cancel_order(
    request: Request, order_id: str, broker: BrokerQuery = DEFAULT_BROKER
) -> dict[str, Any]:
    """Cancel an open order on the selected adapter, which must be in paper mode."""
    adapter = get_broker(request, broker)
    _require_paper(adapter)
    await _connected(adapter)
    with translate_errors("order"):
        order = await adapter.cancel_order(order_id)
    return {"order": order.as_dict(), "broker": adapter.name}


@router.get("/fills", summary="Recent paper fills")
async def list_fills(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> dict[str, Any]:
    """Return the paper account's executions, newest first.

    Each fill carries the reference price and slippage assumption that produced
    it, so a simulated track record can be re-derived rather than trusted.
    """
    adapter = get_broker(request, "paper")
    assert isinstance(adapter, PaperBroker)  # noqa: S101 - "paper" always builds one
    await _connected(adapter)
    with translate_errors("fill"):
        fills = await adapter.list_fills(limit=limit)
    return {"fills": [fill.as_dict() for fill in fills], "count": len(fills)}


@router.post("/paper/quote", summary="Feed a quote to the paper simulator")
async def push_quote(
    request: Request, body: Annotated[QuoteRequest, Body()]
) -> dict[str, Any]:
    """Push one quote into the paper broker and re-evaluate its resting orders.

    The broker normally consumes the live ``tape`` channel; this endpoint is the
    explicit path for a strategy loop (or an operator) that wants to drive the
    simulation from its own price source.
    """
    adapter = get_broker(request, "paper")
    assert isinstance(adapter, PaperBroker)  # noqa: S101 - "paper" always builds one
    await _connected(adapter)
    with translate_errors("quote"):
        filled = await adapter.on_quote(body.symbol, body.price, bid=body.bid, ask=body.ask)
    return {
        "symbol": body.symbol.strip().upper(),
        "price": body.price,
        "filled": [order.as_dict() for order in filled],
        "filled_count": len(filled),
    }


@router.post("/paper/reset", summary="Reset the paper account")
async def reset_paper(
    request: Request, body: Annotated[ResetRequest, Body()] = ResetRequest()
) -> dict[str, Any]:
    """Delete the paper account's positions, orders and fills and restore cash."""
    adapter = get_broker(request, "paper")
    assert isinstance(adapter, PaperBroker)  # noqa: S101 - "paper" always builds one
    with translate_errors("account"):
        portfolio = await adapter.reset(starting_cash=body.starting_cash)
    return portfolio.to_dict()
