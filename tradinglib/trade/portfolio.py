"""Portfolio accounting shared by every broker adapter.

This module is the vocabulary of the execution layer: orders, fills, positions
and the cash/P&L arithmetic that turns a stream of fills into a portfolio. It
is deliberately broker-agnostic - :mod:`tradinglib.trade.paper`,
:mod:`tradinglib.trade.alpaca_broker` and :mod:`tradinglib.trade.ibkr_broker` all
translate their own wire formats into these dataclasses, so callers (and the
``/api/trade`` routes) see one shape regardless of where the order went.

Accounting model
----------------

* **Average cost.** A position carries a signed quantity and an average price.
  Adding to a position blends the average; reducing one realises
  ``(exit - avg) * closed_qty * sign(position)``. A fill large enough to flip
  the sign realises the full close first and re-opens the remainder at the fill
  price. Shorts are supported and use the same arithmetic with a negative
  quantity.
* **Cash.** A buy debits ``qty * price + fee``; a sell credits
  ``qty * price - fee``. Short sales credit the proceeds, which is how a cash
  paper account is normally simulated - it is not a margin model and does not
  pretend to be one.
* **Unknown is null.** Market value, unrealised P&L and equity are ``None``
  while any held symbol has no mark. Nothing is zero-filled to make a number
  appear.

State is persisted through :class:`PortfolioStore` into the SQLite application
database (``docs/SPEC.md`` decision D4), so a paper account survives a restart.
"""

from __future__ import annotations

import abc
import logging
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    delete,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from tradinglib.data.base import to_utc, utcnow
from tradinglib.db import Base, get_engine, get_session_factory

logger = logging.getLogger(__name__)

#: Quantities smaller than this are treated as flat (float noise guard).
QTY_EPSILON: float = 1e-9

#: Decimal places prices and cash are rounded to. Six is well past a
#: sub-penny tick and keeps repeated float arithmetic from drifting.
MONEY_DP: int = 6


def _round(value: float) -> float:
    """Round a price or cash amount to :data:`MONEY_DP` places."""
    return round(value, MONEY_DP)


def _iso(value: datetime | None) -> str | None:
    """Serialise a datetime as UTC ISO-8601, or ``None``."""
    return None if value is None else to_utc(value).isoformat()


def new_id(prefix: str) -> str:
    """Return a short unique identifier such as ``ord-3f9c1a2b7d40``."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class OrderSide(StrEnum):
    """Order side."""

    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> int:
        """``+1`` for a buy, ``-1`` for a sell."""
        return 1 if self is OrderSide.BUY else -1


class OrderType(StrEnum):
    """Supported order types."""

    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class TimeInForce(StrEnum):
    """Order lifetime.

    The paper broker treats every resting order as good-til-cancelled: it has
    no session calendar of its own, and expiring orders on a guessed close time
    would be a fiction. ``DAY`` is carried through to live brokers, which do
    have a real session.
    """

    DAY = "day"
    GTC = "gtc"


class OrderStatus(StrEnum):
    """Order lifecycle states."""

    NEW = "new"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"

    @property
    def open(self) -> bool:
        """Whether an order in this state can still trade."""
        return self in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED)


@dataclass(slots=True)
class Fill:
    """A single execution against an order."""

    id: str
    order_id: str
    symbol: str
    side: OrderSide
    qty: float
    price: float
    ts: datetime
    fee: float = 0.0
    slippage_bps: float = 0.0
    reference_price: float | None = None
    broker: str = "paper"

    @property
    def notional(self) -> float:
        """Absolute traded value, before fees."""
        return abs(self.qty) * self.price

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of the fill."""
        return {
            "id": self.id,
            "order_id": self.order_id,
            "symbol": self.symbol,
            "side": str(self.side),
            "qty": self.qty,
            "price": self.price,
            "ts": _iso(self.ts),
            "fee": self.fee,
            "slippage_bps": self.slippage_bps,
            "reference_price": self.reference_price,
            "notional": _round(self.notional),
            "broker": self.broker,
        }


@dataclass(slots=True)
class Order:
    """An order as TradingLib tracks it, independent of the broker."""

    id: str
    symbol: str
    side: OrderSide
    qty: float
    type: OrderType = OrderType.MARKET
    limit_price: float | None = None
    stop_price: float | None = None
    status: OrderStatus = OrderStatus.NEW
    filled_qty: float = 0.0
    avg_fill_price: float | None = None
    submitted_at: datetime | None = None
    updated_at: datetime | None = None
    broker: str = "paper"
    broker_order_id: str | None = None
    account_id: str = "paper"
    time_in_force: TimeInForce = TimeInForce.GTC
    fills: list[Fill] = field(default_factory=list)
    note: str | None = None

    def __post_init__(self) -> None:
        """Validate the order's own consistency."""
        self.symbol = self.symbol.strip().upper()
        if not self.symbol:
            raise ValueError("order symbol must not be empty")
        if self.qty <= 0:
            raise ValueError("order qty must be positive")
        if self.type in (OrderType.LIMIT, OrderType.STOP_LIMIT) and self.limit_price is None:
            raise ValueError(f"a {self.type} order requires a limit price")
        if self.type in (OrderType.STOP, OrderType.STOP_LIMIT) and self.stop_price is None:
            raise ValueError(f"a {self.type} order requires a stop price")
        if self.limit_price is not None and self.limit_price <= 0:
            raise ValueError("limit price must be positive")
        if self.stop_price is not None and self.stop_price <= 0:
            raise ValueError("stop price must be positive")

    @property
    def remaining_qty(self) -> float:
        """Quantity still to be executed."""
        return max(0.0, self.qty - self.filled_qty)

    @property
    def is_open(self) -> bool:
        """Whether the order can still trade."""
        return self.status.open and self.remaining_qty > QTY_EPSILON

    def record_fill(self, fill: Fill) -> None:
        """Attach ``fill``, updating the filled quantity, average and status."""
        prior_qty = self.filled_qty
        prior_avg = self.avg_fill_price or 0.0
        self.filled_qty = _round(prior_qty + fill.qty)
        total = prior_qty + fill.qty
        self.avg_fill_price = _round((prior_avg * prior_qty + fill.price * fill.qty) / total)
        self.fills.append(fill)
        self.status = (
            OrderStatus.FILLED
            if self.remaining_qty <= QTY_EPSILON
            else OrderStatus.PARTIALLY_FILLED
        )
        self.updated_at = fill.ts

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of the order and its fills."""
        return {
            "id": self.id,
            "account_id": self.account_id,
            "symbol": self.symbol,
            "side": str(self.side),
            "qty": self.qty,
            "type": str(self.type),
            "limit_price": self.limit_price,
            "stop_price": self.stop_price,
            "status": str(self.status),
            "filled_qty": self.filled_qty,
            "remaining_qty": _round(self.remaining_qty),
            "avg_fill_price": self.avg_fill_price,
            "time_in_force": str(self.time_in_force),
            "submitted_at": _iso(self.submitted_at),
            "updated_at": _iso(self.updated_at),
            "broker": self.broker,
            "broker_order_id": self.broker_order_id,
            "note": self.note,
            "fills": [f.as_dict() for f in self.fills],
        }


@dataclass(slots=True)
class Position:
    """An open position, held at average cost.

    ``qty`` is signed: positive is long, negative is short.
    """

    symbol: str
    qty: float
    avg_price: float
    market_price: float | None = None
    realized_pnl: float = 0.0

    @property
    def market_value(self) -> float | None:
        """Current market value, when a market price is known."""
        return None if self.market_price is None else _round(self.qty * self.market_price)

    @property
    def cost_basis(self) -> float:
        """Signed cost of the position at its average price."""
        return _round(self.qty * self.avg_price)

    @property
    def unrealized_pnl(self) -> float | None:
        """Unrealised profit and loss, when a market price is known."""
        if self.market_price is None:
            return None
        return _round((self.market_price - self.avg_price) * self.qty)

    @property
    def unrealized_pnl_pct(self) -> float | None:
        """Unrealised P&L as a fraction of cost, when both are known."""
        pnl = self.unrealized_pnl
        basis = abs(self.cost_basis)
        if pnl is None or basis <= 0:
            return None
        return pnl / basis

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of the position."""
        return {
            "symbol": self.symbol,
            "qty": self.qty,
            "side": "long" if self.qty >= 0 else "short",
            "avg_price": self.avg_price,
            "market_price": self.market_price,
            "market_value": self.market_value,
            "cost_basis": self.cost_basis,
            "unrealized_pnl": self.unrealized_pnl,
            "unrealized_pnl_pct": self.unrealized_pnl_pct,
            "realized_pnl": self.realized_pnl,
        }


@dataclass(slots=True)
class Account:
    """Identity and opening balance of one trading account."""

    id: str
    broker: str = "paper"
    name: str = "Paper account"
    currency: str = "USD"
    starting_cash: float = 0.0
    created_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of the account."""
        return {
            "id": self.id,
            "broker": self.broker,
            "name": self.name,
            "currency": self.currency,
            "starting_cash": self.starting_cash,
            "created_at": _iso(self.created_at),
        }


@dataclass(slots=True)
class Portfolio:
    """Cash, positions and derived equity for one account."""

    account_id: str
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    orders: list[Order] = field(default_factory=list)
    updated_at: datetime | None = None
    broker: str = "paper"
    currency: str = "USD"
    starting_cash: float | None = None
    realized_pnl: float = 0.0

    # --- derived figures --------------------------------------------------
    @property
    def market_value(self) -> float | None:
        """Total market value of the positions; ``None`` if any mark is missing."""
        values = [p.market_value for p in self.positions.values()]
        if any(v is None for v in values):
            return None
        return _round(sum(v for v in values if v is not None))

    @property
    def equity(self) -> float | None:
        """Cash plus market value of all positions; ``None`` if any price is unknown."""
        value = self.market_value
        return None if value is None else _round(self.cash + value)

    @property
    def unrealized_pnl(self) -> float | None:
        """Total unrealised P&L; ``None`` if any mark is missing."""
        values = [p.unrealized_pnl for p in self.positions.values()]
        if any(v is None for v in values):
            return None
        return _round(sum(v for v in values if v is not None))

    @property
    def total_pnl(self) -> float | None:
        """Equity minus the opening balance, when both are known."""
        equity = self.equity
        if equity is None or self.starting_cash is None:
            return None
        return _round(equity - self.starting_cash)

    @property
    def unmarked_symbols(self) -> list[str]:
        """Held symbols with no current mark - why equity may be ``None``."""
        return sorted(s for s, p in self.positions.items() if p.market_price is None)

    # --- mutation ---------------------------------------------------------
    def mark(self, prices: Mapping[str, float]) -> None:
        """Apply marks to the held positions. Symbols not present keep their mark."""
        for symbol, price in prices.items():
            position = self.positions.get(symbol.strip().upper())
            if position is not None and price is not None:
                position.market_price = float(price)

    def apply_fill(
        self,
        order: Order,
        qty: float,
        price: float,
        *,
        fee: float = 0.0,
        ts: datetime | None = None,
        slippage_bps: float = 0.0,
        reference_price: float | None = None,
    ) -> Fill:
        """Apply a fill to cash and positions, updating the average price.

        Args:
            order: the order being executed; its filled quantity, average price
                and status are updated in place.
            qty: executed quantity, always positive - direction comes from
                ``order.side``.
            price: execution price per share.
            fee: commission charged on this execution.
            ts: execution time; defaults to now (UTC).
            slippage_bps: slippage assumption recorded on the fill, for audit.
            reference_price: the quote the fill price was derived from.

        Returns:
            The :class:`Fill` that was applied and attached to ``order``.

        Raises:
            ValueError: if ``qty`` or ``price`` is not positive, or the fill
                exceeds the order's remaining quantity.
        """
        if qty <= 0:
            raise ValueError("fill qty must be positive")
        if price <= 0:
            raise ValueError("fill price must be positive")
        if qty - order.remaining_qty > QTY_EPSILON:
            raise ValueError(
                f"fill of {qty} exceeds the {order.remaining_qty} remaining on order {order.id}"
            )

        when = to_utc(ts) if ts is not None else utcnow()
        signed = qty * order.side.sign
        realized = self._apply_to_position(order.symbol, signed, price)

        self.cash = _round(self.cash - signed * price - fee)
        self.realized_pnl = _round(self.realized_pnl + realized - fee)
        self.updated_at = when

        fill = Fill(
            id=new_id("fil"),
            order_id=order.id,
            symbol=order.symbol,
            side=order.side,
            qty=qty,
            price=_round(price),
            ts=when,
            fee=_round(fee),
            slippage_bps=slippage_bps,
            reference_price=None if reference_price is None else _round(reference_price),
            broker=order.broker,
        )
        order.record_fill(fill)
        return fill

    def _apply_to_position(self, symbol: str, signed_qty: float, price: float) -> float:
        """Update the position for ``symbol`` and return the realised P&L.

        Average-cost arithmetic, sign-aware: adding blends the average,
        reducing realises, and crossing zero realises the close and re-opens
        the remainder at ``price``.
        """
        existing = self.positions.get(symbol)
        prior_realized = existing.realized_pnl if existing is not None else 0.0
        mark = existing.market_price if existing is not None else None
        q0 = existing.qty if existing is not None else 0.0
        avg = existing.avg_price if existing is not None else 0.0
        realized = 0.0

        if abs(q0) <= QTY_EPSILON:
            new_qty, new_avg = signed_qty, price
        elif (q0 > 0) == (signed_qty > 0):
            # Same direction: blend the average cost.
            new_qty = q0 + signed_qty
            new_avg = (avg * abs(q0) + price * abs(signed_qty)) / (abs(q0) + abs(signed_qty))
        else:
            closed = min(abs(signed_qty), abs(q0))
            direction = 1.0 if q0 > 0 else -1.0
            realized = (price - avg) * closed * direction
            new_qty = q0 + signed_qty
            # A flip re-opens at the fill price; a partial close keeps the average.
            new_avg = price if abs(new_qty) > QTY_EPSILON and (new_qty > 0) != (q0 > 0) else avg

        new_qty = _round(new_qty)
        if abs(new_qty) <= QTY_EPSILON:
            # Flat: forget the position but keep the realised P&L on the book.
            self.positions.pop(symbol, None)
            return _round(realized)

        self.positions[symbol] = Position(
            symbol=symbol,
            qty=new_qty,
            avg_price=_round(new_avg),
            market_price=mark,
            realized_pnl=_round(prior_realized + realized),
        )
        return _round(realized)

    def to_dict(self) -> dict[str, Any]:
        """Serialise for ``GET /api/trade/portfolio``."""
        return {
            "account_id": self.account_id,
            "broker": self.broker,
            "currency": self.currency,
            "cash": self.cash,
            "starting_cash": self.starting_cash,
            "market_value": self.market_value,
            "equity": self.equity,
            "realized_pnl": self.realized_pnl,
            "unrealized_pnl": self.unrealized_pnl,
            "total_pnl": self.total_pnl,
            "unmarked_symbols": self.unmarked_symbols,
            "positions": [p.as_dict() for p in sorted(self.positions.values(), key=_by_symbol)],
            "orders": [o.as_dict() for o in self.orders],
            "updated_at": _iso(self.updated_at),
        }


def _by_symbol(position: Position) -> str:
    """Sort key placing positions in symbol order."""
    return position.symbol


# --------------------------------------------------------------------------
# Broker interface
# --------------------------------------------------------------------------
class Broker(abc.ABC):
    """The interface every execution adapter implements.

    Implementations are interchangeable: the paper simulator, Alpaca and IBKR
    all speak the dataclasses above. Two rules bind every implementation:

    * :meth:`status` must never lie - an adapter with no credentials, or no
      reachable gateway, reports that instead of an optimistic default;
    * live trading is opt-in. Adapters default to paper/sandbox endpoints and
      refuse to construct a live session without a deliberate human override
      (``docs/SPEC.md`` section 2, decision D5).
    """

    #: Stable adapter identifier, e.g. ``"paper"``.
    name: str = "broker"

    @abc.abstractmethod
    async def connect(self) -> None:
        """Establish the session (or load local state) and validate the account."""

    @abc.abstractmethod
    async def close(self) -> None:
        """Release sessions, clients and background tasks."""

    @abc.abstractmethod
    async def get_portfolio(self) -> Portfolio:
        """Return cash and positions, marked where a price is available."""

    @abc.abstractmethod
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
        """Submit an order and return TradingLib's record of it."""

    @abc.abstractmethod
    async def cancel_order(self, order_id: str) -> Order:
        """Cancel an open order and return its updated record."""

    @abc.abstractmethod
    async def list_orders(self, *, limit: int = 100) -> list[Order]:
        """Return recent orders, newest first."""

    @abc.abstractmethod
    def status(self) -> dict[str, Any]:
        """Return an honest configuration/connection status (no network calls)."""


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------
class TradeAccountRow(Base):
    """An account's identity and running cash balance."""

    __tablename__ = "trade_accounts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    broker: Mapped[str] = mapped_column(String(32), nullable=False, default="paper")
    name: Mapped[str] = mapped_column(String(128), nullable=False, default="Paper account")
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    starting_cash: Mapped[float] = mapped_column(Float(), nullable=False, default=0.0)
    cash: Mapped[float] = mapped_column(Float(), nullable=False, default=0.0)
    realized_pnl: Mapped[float] = mapped_column(Float(), nullable=False, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)


class TradePositionRow(Base):
    """One open position of one account."""

    __tablename__ = "trade_positions"
    __table_args__ = (UniqueConstraint("account_id", "symbol", name="uq_trade_position"),)

    id: Mapped[int] = mapped_column(Integer(), primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("trade_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    qty: Mapped[float] = mapped_column(Float(), nullable=False)
    avg_price: Mapped[float] = mapped_column(Float(), nullable=False)
    realized_pnl: Mapped[float] = mapped_column(Float(), nullable=False, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)


class TradeOrderRow(Base):
    """One order of one account, in whatever state it reached."""

    __tablename__ = "trade_orders"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("trade_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    symbol: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    qty: Mapped[float] = mapped_column(Float(), nullable=False)
    order_type: Mapped[str] = mapped_column(String(16), nullable=False)
    limit_price: Mapped[float | None] = mapped_column(Float(), default=None)
    stop_price: Mapped[float | None] = mapped_column(Float(), default=None)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    filled_qty: Mapped[float] = mapped_column(Float(), nullable=False, default=0.0)
    avg_fill_price: Mapped[float | None] = mapped_column(Float(), default=None)
    time_in_force: Mapped[str] = mapped_column(String(8), nullable=False, default="gtc")
    submitted_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    broker: Mapped[str] = mapped_column(String(32), nullable=False, default="paper")
    broker_order_id: Mapped[str | None] = mapped_column(String(64), default=None)
    note: Mapped[str | None] = mapped_column(Text(), default=None)


class TradeFillRow(Base):
    """One execution against an order."""

    __tablename__ = "trade_fills"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    order_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("trade_orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    account_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    qty: Mapped[float] = mapped_column(Float(), nullable=False)
    price: Mapped[float] = mapped_column(Float(), nullable=False)
    fee: Mapped[float] = mapped_column(Float(), nullable=False, default=0.0)
    slippage_bps: Mapped[float] = mapped_column(Float(), nullable=False, default=0.0)
    reference_price: Mapped[float | None] = mapped_column(Float(), default=None)
    ts: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    broker: Mapped[str] = mapped_column(String(32), nullable=False, default="paper")


_OWNED_TABLES = (
    TradeAccountRow.__table__,
    TradePositionRow.__table__,
    TradeOrderRow.__table__,
    TradeFillRow.__table__,
)


def ensure_schema() -> None:
    """Create the trade tables if they do not exist yet."""
    Base.metadata.create_all(get_engine(), tables=list(_OWNED_TABLES))


class PortfolioStore:
    """SQLite persistence for accounts, positions, orders and fills.

    Every method is synchronous (SQLAlchemy's sync API); async callers dispatch
    them with :func:`asyncio.to_thread` so the event loop stays free.
    """

    def __init__(self, session_factory: sessionmaker[Session] | None = None) -> None:
        """Create the store, ensuring its tables exist.

        Args:
            session_factory: SQLAlchemy session factory; defaults to the
                process-wide one from :mod:`tradinglib.db`.
        """
        self._session_factory = session_factory
        ensure_schema()

    @contextmanager
    def _session(self) -> Iterator[Session]:
        """Transactional session scope bound to this store."""
        factory = self._session_factory or get_session_factory()
        session = factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # --- accounts ---------------------------------------------------------
    def ensure_account(
        self,
        account_id: str,
        *,
        broker: str = "paper",
        name: str = "Paper account",
        starting_cash: float = 0.0,
        currency: str = "USD",
    ) -> Account:
        """Return the account, creating it with ``starting_cash`` on first use."""
        now = utcnow()
        with self._session() as session:
            row = session.get(TradeAccountRow, account_id)
            if row is None:
                row = TradeAccountRow(
                    id=account_id,
                    broker=broker,
                    name=name,
                    currency=currency,
                    starting_cash=_round(starting_cash),
                    cash=_round(starting_cash),
                    realized_pnl=0.0,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                logger.info(
                    "created %s account %s with %.2f %s",
                    broker,
                    account_id,
                    starting_cash,
                    currency,
                )
            return _account_from_row(row)

    def get_account(self, account_id: str) -> Account:
        """Return an existing account.

        Raises:
            KeyError: if the account does not exist.
        """
        with self._session() as session:
            row = session.get(TradeAccountRow, account_id)
            if row is None:
                raise KeyError(f"unknown trading account: {account_id}")
            return _account_from_row(row)

    def list_accounts(self) -> list[Account]:
        """Return every account, oldest first."""
        with self._session() as session:
            rows = session.scalars(
                select(TradeAccountRow).order_by(TradeAccountRow.created_at)
            ).all()
            return [_account_from_row(row) for row in rows]

    def reset_account(self, account_id: str, *, starting_cash: float | None = None) -> Account:
        """Delete an account's positions, orders and fills and restore its cash.

        Raises:
            KeyError: if the account does not exist.
        """
        with self._session() as session:
            row = session.get(TradeAccountRow, account_id)
            if row is None:
                raise KeyError(f"unknown trading account: {account_id}")
            session.execute(delete(TradeFillRow).where(TradeFillRow.account_id == account_id))
            session.execute(delete(TradeOrderRow).where(TradeOrderRow.account_id == account_id))
            session.execute(
                delete(TradePositionRow).where(TradePositionRow.account_id == account_id)
            )
            if starting_cash is not None:
                row.starting_cash = _round(starting_cash)
            row.cash = row.starting_cash
            row.realized_pnl = 0.0
            row.updated_at = utcnow()
            logger.info("reset trading account %s to %.2f", account_id, row.cash)
            return _account_from_row(row)

    # --- portfolio --------------------------------------------------------
    def load(self, account_id: str, *, order_limit: int = 200) -> Portfolio:
        """Rebuild a :class:`Portfolio` from the stored rows.

        Args:
            account_id: account to load.
            order_limit: how many recent orders to attach (newest first).

        Raises:
            KeyError: if the account does not exist.
        """
        with self._session() as session:
            account_row = session.get(TradeAccountRow, account_id)
            if account_row is None:
                raise KeyError(f"unknown trading account: {account_id}")
            position_rows = session.scalars(
                select(TradePositionRow)
                .where(TradePositionRow.account_id == account_id)
                .order_by(TradePositionRow.symbol)
            ).all()
            order_rows = session.scalars(
                select(TradeOrderRow)
                .where(TradeOrderRow.account_id == account_id)
                .order_by(TradeOrderRow.submitted_at.desc(), TradeOrderRow.id.desc())
                .limit(order_limit)
            ).all()
            order_ids = [row.id for row in order_rows]
            fills_by_order: dict[str, list[Fill]] = {}
            if order_ids:
                fill_rows = session.scalars(
                    select(TradeFillRow)
                    .where(TradeFillRow.order_id.in_(order_ids))
                    .order_by(TradeFillRow.ts)
                ).all()
                for fill_row in fill_rows:
                    fills_by_order.setdefault(fill_row.order_id, []).append(
                        _fill_from_row(fill_row)
                    )

            portfolio = Portfolio(
                account_id=account_id,
                cash=account_row.cash,
                broker=account_row.broker,
                currency=account_row.currency,
                starting_cash=account_row.starting_cash,
                realized_pnl=account_row.realized_pnl,
                updated_at=_aware(account_row.updated_at),
            )
            for row in position_rows:
                portfolio.positions[row.symbol] = Position(
                    symbol=row.symbol,
                    qty=row.qty,
                    avg_price=row.avg_price,
                    realized_pnl=row.realized_pnl,
                )
            portfolio.orders = [
                _order_from_row(row, fills_by_order.get(row.id, [])) for row in order_rows
            ]
            return portfolio

    def save_state(self, portfolio: Portfolio) -> None:
        """Write cash, realised P&L and the full position set back to SQLite."""
        now = portfolio.updated_at or utcnow()
        with self._session() as session:
            account_row = session.get(TradeAccountRow, portfolio.account_id)
            if account_row is None:
                raise KeyError(f"unknown trading account: {portfolio.account_id}")
            account_row.cash = _round(portfolio.cash)
            account_row.realized_pnl = _round(portfolio.realized_pnl)
            account_row.updated_at = now

            existing = {
                row.symbol: row
                for row in session.scalars(
                    select(TradePositionRow).where(
                        TradePositionRow.account_id == portfolio.account_id
                    )
                ).all()
            }
            for symbol, position in portfolio.positions.items():
                row = existing.pop(symbol, None)
                if row is None:
                    session.add(
                        TradePositionRow(
                            account_id=portfolio.account_id,
                            symbol=symbol,
                            qty=position.qty,
                            avg_price=position.avg_price,
                            realized_pnl=position.realized_pnl,
                            updated_at=now,
                        )
                    )
                else:
                    row.qty = position.qty
                    row.avg_price = position.avg_price
                    row.realized_pnl = position.realized_pnl
                    row.updated_at = now
            for row in existing.values():
                session.delete(row)

    def save_order(self, order: Order) -> None:
        """Insert or update an order row."""
        with self._session() as session:
            row = session.get(TradeOrderRow, order.id)
            submitted = order.submitted_at or utcnow()
            if row is None:
                session.add(
                    TradeOrderRow(
                        id=order.id,
                        account_id=order.account_id,
                        symbol=order.symbol,
                        side=str(order.side),
                        qty=order.qty,
                        order_type=str(order.type),
                        limit_price=order.limit_price,
                        stop_price=order.stop_price,
                        status=str(order.status),
                        filled_qty=order.filled_qty,
                        avg_fill_price=order.avg_fill_price,
                        time_in_force=str(order.time_in_force),
                        submitted_at=submitted,
                        updated_at=order.updated_at or submitted,
                        broker=order.broker,
                        broker_order_id=order.broker_order_id,
                        note=order.note,
                    )
                )
                return
            row.status = str(order.status)
            row.filled_qty = order.filled_qty
            row.avg_fill_price = order.avg_fill_price
            row.limit_price = order.limit_price
            row.stop_price = order.stop_price
            row.updated_at = order.updated_at or utcnow()
            row.broker_order_id = order.broker_order_id
            row.note = order.note

    def save_fill(self, account_id: str, fill: Fill) -> None:
        """Insert a fill row."""
        with self._session() as session:
            session.add(
                TradeFillRow(
                    id=fill.id,
                    order_id=fill.order_id,
                    account_id=account_id,
                    symbol=fill.symbol,
                    side=str(fill.side),
                    qty=fill.qty,
                    price=fill.price,
                    fee=fill.fee,
                    slippage_bps=fill.slippage_bps,
                    reference_price=fill.reference_price,
                    ts=fill.ts,
                    broker=fill.broker,
                )
            )

    def get_order(self, account_id: str, order_id: str) -> Order:
        """Return one order with its fills.

        Raises:
            KeyError: if the order does not belong to the account or is unknown.
        """
        with self._session() as session:
            row = session.get(TradeOrderRow, order_id)
            if row is None or row.account_id != account_id:
                raise KeyError(f"unknown order: {order_id}")
            fill_rows = session.scalars(
                select(TradeFillRow)
                .where(TradeFillRow.order_id == order_id)
                .order_by(TradeFillRow.ts)
            ).all()
            return _order_from_row(row, [_fill_from_row(f) for f in fill_rows])

    def list_orders(
        self,
        account_id: str,
        *,
        limit: int = 100,
        open_only: bool = False,
        symbol: str | None = None,
    ) -> list[Order]:
        """Return recent orders, newest first."""
        with self._session() as session:
            stmt = select(TradeOrderRow).where(TradeOrderRow.account_id == account_id)
            if open_only:
                stmt = stmt.where(
                    TradeOrderRow.status.in_(
                        (str(OrderStatus.NEW), str(OrderStatus.PARTIALLY_FILLED))
                    )
                )
            if symbol:
                stmt = stmt.where(TradeOrderRow.symbol == symbol.strip().upper())
            stmt = stmt.order_by(
                TradeOrderRow.submitted_at.desc(), TradeOrderRow.id.desc()
            ).limit(limit)
            rows = session.scalars(stmt).all()
            order_ids = [row.id for row in rows]
            fills_by_order: dict[str, list[Fill]] = {}
            if order_ids:
                for fill_row in session.scalars(
                    select(TradeFillRow)
                    .where(TradeFillRow.order_id.in_(order_ids))
                    .order_by(TradeFillRow.ts)
                ).all():
                    fills_by_order.setdefault(fill_row.order_id, []).append(
                        _fill_from_row(fill_row)
                    )
            return [_order_from_row(row, fills_by_order.get(row.id, [])) for row in rows]

    def list_fills(self, account_id: str, *, limit: int = 200) -> list[Fill]:
        """Return recent fills, newest first."""
        with self._session() as session:
            rows = session.scalars(
                select(TradeFillRow)
                .where(TradeFillRow.account_id == account_id)
                .order_by(TradeFillRow.ts.desc(), TradeFillRow.id.desc())
                .limit(limit)
            ).all()
            return [_fill_from_row(row) for row in rows]


def _aware(value: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; restore the UTC tzinfo TradingLib stores in."""
    return None if value is None else to_utc(value)


def _account_from_row(row: TradeAccountRow) -> Account:
    """Map an account row onto :class:`Account`."""
    return Account(
        id=row.id,
        broker=row.broker,
        name=row.name,
        currency=row.currency,
        starting_cash=row.starting_cash,
        created_at=_aware(row.created_at),
    )


def _order_from_row(row: TradeOrderRow, fills: Sequence[Fill]) -> Order:
    """Map an order row and its fills onto :class:`Order`."""
    order = Order(
        id=row.id,
        symbol=row.symbol,
        side=OrderSide(row.side),
        qty=row.qty,
        type=OrderType(row.order_type),
        limit_price=row.limit_price,
        stop_price=row.stop_price,
        status=OrderStatus(row.status),
        filled_qty=row.filled_qty,
        avg_fill_price=row.avg_fill_price,
        submitted_at=_aware(row.submitted_at),
        updated_at=_aware(row.updated_at),
        broker=row.broker,
        broker_order_id=row.broker_order_id,
        account_id=row.account_id,
        time_in_force=TimeInForce(row.time_in_force),
        note=row.note,
    )
    order.fills = list(fills)
    return order


def _fill_from_row(row: TradeFillRow) -> Fill:
    """Map a fill row onto :class:`Fill`."""
    ts = _aware(row.ts)
    assert ts is not None  # noqa: S101 - the column is NOT NULL
    return Fill(
        id=row.id,
        order_id=row.order_id,
        symbol=row.symbol,
        side=OrderSide(row.side),
        qty=row.qty,
        price=row.price,
        ts=ts,
        fee=row.fee,
        slippage_bps=row.slippage_bps,
        reference_price=row.reference_price,
        broker=row.broker,
    )


__all__ = [
    "MONEY_DP",
    "QTY_EPSILON",
    "Account",
    "Broker",
    "Fill",
    "Order",
    "OrderSide",
    "OrderStatus",
    "OrderType",
    "Portfolio",
    "PortfolioStore",
    "Position",
    "TimeInForce",
    "TradeAccountRow",
    "TradeFillRow",
    "TradeOrderRow",
    "TradePositionRow",
    "ensure_schema",
    "new_id",
]
