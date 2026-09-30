"""Paper broker: simulated fills against live quotes.

Fills are modelled against real quotes from ``tradinglib.data`` with configurable
slippage - the simulation is honest about what price it assumed.

How a fill price is produced
----------------------------

1. A **reference price** is taken from the current quote: the ask for a buy and
   the bid for a sell when the vendor supplies a two-sided quote, otherwise the
   last trade price. Crossing the spread is the first, and usually largest,
   real execution cost, so it is modelled before anything else.
2. **Slippage** of ``slippage_bps`` basis points is then applied against the
   order: a buy pays ``ref * (1 + bps/10_000)``, a sell receives
   ``ref * (1 - bps/10_000)``.
3. A limit order never trades through its limit: the slipped price is capped at
   ``limit_price``.
4. **Commission** of ``commission_per_share`` per share (with a
   ``commission_min`` floor per fill) is charged as a fee, and is zero by
   default because a zero-commission US equity broker is the common case.

Every fill records the reference price and the slippage assumption that
produced it, so a paper track record can always be re-derived rather than
taken on faith.

Order handling
--------------

Market orders fill immediately in full at the price above. Limit, stop and
stop-limit orders fill immediately when the current quote already satisfies
them; otherwise they **rest** and are re-evaluated by :meth:`PaperBroker.on_quote`
as new prices arrive. There is no partial-fill or queue-position model: the
simulator assumes the full size trades once the price condition is met, which
is a documented simplification rather than a hidden one.

A buy that the account cannot afford is *rejected and recorded* as rejected -
the order stays on the book with the reason in its ``note`` rather than
silently vanishing.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from tradinglib.data.base import DataProvider, ProviderError, Quote, to_utc, utcnow
from tradinglib.events import Event, EventBus, Subscription
from tradinglib.trade.portfolio import (
    QTY_EPSILON,
    Broker,
    Fill,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Portfolio,
    PortfolioStore,
    TimeInForce,
    new_id,
)

logger = logging.getLogger(__name__)

#: Default identifier of the account the paper broker trades.
DEFAULT_ACCOUNT_ID: str = "paper"

#: How long a cached quote is considered current, in seconds.
DEFAULT_QUOTE_TTL: float = 5.0

#: Event-bus channel family the live tape is published on (``tape.<SYMBOL>``).
TAPE_CHANNEL: str = "tape"


class PaperBroker(Broker):
    """In-process broker simulating execution against live market data."""

    name = "paper"

    def __init__(
        self,
        *,
        starting_cash: float = 1_000_000.0,
        slippage_bps: float = 5.0,
        account_id: str = DEFAULT_ACCOUNT_ID,
        provider: DataProvider | None = None,
        store: PortfolioStore | None = None,
        commission_per_share: float = 0.0,
        commission_min: float = 0.0,
        allow_short: bool = True,
        quote_ttl: float = DEFAULT_QUOTE_TTL,
    ) -> None:
        """Create the broker with an opening balance and a slippage assumption.

        Args:
            starting_cash: opening balance, used only when the account is
                created for the first time.
            slippage_bps: slippage applied against the order, in basis points.
            account_id: account these simulated trades belong to.
            provider: quote source; defaults to
                :class:`~tradinglib.data.providers.yfinance_provider.YFinanceProvider`
                on first use, so a keyless install still gets real prices.
            store: portfolio persistence; defaults to a
                :class:`~tradinglib.trade.portfolio.PortfolioStore` on the
                process-wide database.
            commission_per_share: commission charged per share filled.
            commission_min: minimum commission per fill.
            allow_short: whether a sell may open or extend a short position.
            quote_ttl: how long a fetched or streamed quote stays usable.

        Raises:
            ValueError: if slippage or commission is negative.
        """
        if slippage_bps < 0:
            raise ValueError("slippage_bps must not be negative")
        if commission_per_share < 0 or commission_min < 0:
            raise ValueError("commission must not be negative")
        self.starting_cash = starting_cash
        self.slippage_bps = slippage_bps
        self.account_id = account_id
        self.commission_per_share = commission_per_share
        self.commission_min = commission_min
        self.allow_short = allow_short
        self.quote_ttl = quote_ttl
        self._provider = provider
        self._store = store
        self._portfolio: Portfolio | None = None
        self._open_orders: dict[str, Order] = {}
        self._quotes: dict[str, tuple[Quote, datetime]] = {}
        self._lock = asyncio.Lock()
        self._connected = False
        self._tape_sub: Subscription | None = None
        self.tape_events = 0
        self.last_quote_error: str | None = None

    # --- plumbing ---------------------------------------------------------
    @property
    def provider(self) -> DataProvider:
        """The quote provider, created on first use."""
        if self._provider is None:
            from tradinglib.data.providers.yfinance_provider import YFinanceProvider

            self._provider = YFinanceProvider()
        return self._provider

    @property
    def store(self) -> PortfolioStore:
        """The portfolio store, created on first use."""
        if self._store is None:
            self._store = PortfolioStore()
        return self._store

    @property
    def connected(self) -> bool:
        """Whether :meth:`connect` has loaded the account state."""
        return self._connected

    async def connect(self) -> None:
        """Load or create the paper account state."""
        async with self._lock:
            await self._ensure_loaded()

    async def _ensure_loaded(self) -> None:
        """Load the account and its resting orders; create the account if new."""
        if self._connected and self._portfolio is not None:
            return
        await asyncio.to_thread(
            lambda: self.store.ensure_account(
                self.account_id,
                broker=self.name,
                name="TradingLib paper account",
                starting_cash=self.starting_cash,
            )
        )
        self._portfolio = await asyncio.to_thread(self.store.load, self.account_id)
        resting = await asyncio.to_thread(
            lambda: self.store.list_orders(self.account_id, limit=500, open_only=True)
        )
        self._open_orders = {order.id: order for order in resting if order.is_open}
        self._connected = True
        logger.info(
            "paper broker ready: account=%s cash=%.2f positions=%d resting_orders=%d",
            self.account_id,
            self._portfolio.cash,
            len(self._portfolio.positions),
            len(self._open_orders),
        )

    # --- live tape --------------------------------------------------------
    def attach(self, bus: EventBus) -> None:
        """Drive resting orders from the live tape published on ``bus``.

        Subscribes to the ``tape`` channel family, so every trade print or NBBO
        update the data layer publishes re-evaluates this account's resting
        orders. Without it, a limit order only sees the quote taken when it was
        submitted; with it, the simulator reacts to the same prices the UI does.
        Idempotent.
        """
        if self._tape_sub is not None:
            return
        self._tape_sub = bus.subscribe(TAPE_CHANNEL, self._on_tape_event)
        logger.info("paper broker attached to the %s channel", TAPE_CHANNEL)

    def detach(self) -> None:
        """Stop consuming the live tape. Idempotent."""
        sub, self._tape_sub = self._tape_sub, None
        if sub is not None:
            sub.cancel()

    async def _on_tape_event(self, event: Event) -> None:
        """Feed one tape payload into :meth:`on_quote` when it carries a price."""
        payload = event.payload
        if not isinstance(payload, dict):
            return
        symbol = str(payload.get("symbol") or "").strip().upper()
        if not symbol:
            return
        bid = _as_float(payload.get("bid"))
        ask = _as_float(payload.get("ask"))
        price = _as_float(payload.get("price"))
        if price is None and bid is not None and ask is not None:
            price = (bid + ask) / 2.0
        if price is None or price <= 0:
            return
        self.tape_events += 1
        await self.on_quote(symbol, price, bid=bid, ask=ask)

    async def close(self) -> None:
        """Persist state and release the quote provider."""
        self.detach()
        async with self._lock:
            if self._portfolio is not None:
                await asyncio.to_thread(self.store.save_state, self._portfolio)
            self._connected = False
            self._quotes.clear()
        if self._provider is not None:
            await self._provider.close()

    # --- quotes -----------------------------------------------------------
    async def _reference_quote(self, symbol: str) -> Quote:
        """Return a current quote for ``symbol``, from cache or the provider."""
        cached = self._quotes.get(symbol)
        if cached is not None:
            quote, fetched = cached
            if (utcnow() - fetched).total_seconds() <= self.quote_ttl:
                return quote
        try:
            quote = await self.provider.get_quote(symbol)
        except ProviderError as exc:
            self.last_quote_error = str(exc)
            raise
        self._quotes[symbol] = (quote, utcnow())
        self.last_quote_error = None
        return quote

    def _reference_price(self, quote: Quote, side: OrderSide) -> float:
        """Return the side-appropriate reference price from ``quote``.

        A buy references the ask and a sell the bid when the vendor supplies
        them; otherwise both reference the last trade price.
        """
        candidate = quote.ask if side is OrderSide.BUY else quote.bid
        if candidate is not None and candidate > 0:
            return float(candidate)
        if quote.price <= 0:
            raise ValueError(f"{quote.symbol}: quote price {quote.price} is not tradable")
        return float(quote.price)

    def _slipped_price(self, reference: float, order: Order) -> float:
        """Apply slippage to ``reference``, capped at a limit price if present."""
        factor = 1.0 + (self.slippage_bps / 10_000.0) * order.side.sign
        price = reference * factor
        if order.limit_price is not None and order.type in (
            OrderType.LIMIT,
            OrderType.STOP_LIMIT,
        ):
            # A limit order never trades through its limit.
            price = (
                min(price, order.limit_price)
                if order.side is OrderSide.BUY
                else max(price, order.limit_price)
            )
        return price

    def _commission(self, qty: float) -> float:
        """Commission charged on a fill of ``qty`` shares."""
        if self.commission_per_share <= 0 and self.commission_min <= 0:
            return 0.0
        return max(self.commission_min, self.commission_per_share * abs(qty))

    # --- order lifecycle --------------------------------------------------
    async def submit_order(
        self,
        symbol: str,
        side: OrderSide,
        qty: float,
        *,
        type: OrderType = OrderType.MARKET,
        limit_price: float | None = None,
        stop_price: float | None = None,
        time_in_force: TimeInForce = TimeInForce.GTC,
    ) -> Order:
        """Submit a simulated order and attempt an immediate fill where valid.

        Returns:
            The order record. A market order that could be priced comes back
            ``filled``; a limit or stop order whose condition is not yet met
            comes back ``new`` and rests until :meth:`on_quote` (or a later
            submission) re-evaluates it. An order the account cannot afford
            comes back ``rejected`` with the reason in ``note``.

        Raises:
            ValueError: if the order itself is invalid (empty symbol,
                non-positive quantity, missing limit or stop price).
            tradinglib.data.base.ProviderError: if no quote could be obtained, so
                no honest fill price exists.
        """
        async with self._lock:
            await self._ensure_loaded()
            order = Order(
                id=new_id("ord"),
                symbol=symbol,
                side=OrderSide(side),
                qty=float(qty),
                type=OrderType(type),
                limit_price=limit_price,
                stop_price=stop_price,
                submitted_at=utcnow(),
                broker=self.name,
                account_id=self.account_id,
                time_in_force=TimeInForce(time_in_force),
            )
            order.updated_at = order.submitted_at
            await asyncio.to_thread(self.store.save_order, order)

            quote = await self._reference_quote(order.symbol)
            filled = await self._evaluate(order, quote)
            if not filled and order.is_open:
                self._open_orders[order.id] = order
                logger.info(
                    "paper order %s rests: %s %s %g (%s)",
                    order.id,
                    order.side,
                    order.symbol,
                    order.qty,
                    order.type,
                )
            return order

    async def cancel_order(self, order_id: str) -> Order:
        """Cancel a resting simulated order.

        Raises:
            KeyError: if the order is unknown.
            ValueError: if the order is no longer open.
        """
        async with self._lock:
            await self._ensure_loaded()
            order = self._open_orders.get(order_id)
            if order is None:
                order = await asyncio.to_thread(self.store.get_order, self.account_id, order_id)
                if order.is_open:
                    # Loaded from storage rather than memory: adopt it.
                    self._open_orders[order.id] = order
            if not order.is_open:
                raise ValueError(f"order {order_id} is {order.status} and cannot be cancelled")
            order.status = OrderStatus.CANCELLED
            order.updated_at = utcnow()
            order.note = "cancelled by request"
            self._open_orders.pop(order_id, None)
            await asyncio.to_thread(self.store.save_order, order)
            logger.info("paper order %s cancelled", order_id)
            return order

    async def list_orders(self, *, limit: int = 100) -> list[Order]:
        """Return simulated orders, newest first."""
        async with self._lock:
            await self._ensure_loaded()
        return await asyncio.to_thread(
            lambda: self.store.list_orders(self.account_id, limit=limit)
        )

    async def list_fills(self, *, limit: int = 200) -> list[Fill]:
        """Return simulated fills, newest first."""
        async with self._lock:
            await self._ensure_loaded()
        return await asyncio.to_thread(
            lambda: self.store.list_fills(self.account_id, limit=limit)
        )

    async def on_quote(
        self,
        symbol: str,
        price: float,
        *,
        bid: float | None = None,
        ask: float | None = None,
        ts: datetime | None = None,
    ) -> list[Order]:
        """Re-evaluate resting orders against a new quote; returns filled orders.

        Args:
            symbol: symbol the quote is for.
            price: last trade price.
            bid: bid price, when the feed supplies one.
            ask: ask price, when the feed supplies one.
            ts: quote timestamp; defaults to now (UTC).

        Returns:
            The orders that filled on this quote, in the order they filled.
        """
        normalised = symbol.strip().upper()
        quote = Quote(
            symbol=normalised,
            price=float(price),
            ts=to_utc(ts) if ts is not None else utcnow(),
            bid=bid,
            ask=ask,
            source="paper-feed",
        )
        async with self._lock:
            await self._ensure_loaded()
            self._quotes[normalised] = (quote, utcnow())
            filled: list[Order] = []
            for order in [o for o in self._open_orders.values() if o.symbol == normalised]:
                if await self._evaluate(order, quote):
                    filled.append(order)
            return filled

    async def _evaluate(self, order: Order, quote: Quote) -> bool:
        """Try to fill ``order`` against ``quote``. Returns whether it filled.

        The caller must hold :attr:`_lock`.
        """
        assert self._portfolio is not None  # noqa: S101 - _ensure_loaded ran first
        if not order.is_open:
            return False
        reference = self._reference_price(quote, order.side)
        if not self._triggered(order, reference):
            return False

        qty = order.remaining_qty
        price = self._slipped_price(reference, order)
        fee = self._commission(qty)
        rejection = self._rejection_reason(order, qty, price, fee)
        if rejection is not None:
            order.status = OrderStatus.REJECTED
            order.note = rejection
            order.updated_at = utcnow()
            self._open_orders.pop(order.id, None)
            await asyncio.to_thread(self.store.save_order, order)
            logger.warning("paper order %s rejected: %s", order.id, rejection)
            return False

        fill = self._portfolio.apply_fill(
            order,
            qty,
            price,
            fee=fee,
            ts=quote.ts,
            slippage_bps=self.slippage_bps,
            reference_price=reference,
        )
        self._open_orders.pop(order.id, None)
        await asyncio.to_thread(self.store.save_fill, self.account_id, fill)
        await asyncio.to_thread(self.store.save_order, order)
        await asyncio.to_thread(self.store.save_state, self._portfolio)
        logger.info(
            "paper fill %s: %s %g %s @ %.4f (ref %.4f, fee %.4f)",
            fill.id,
            order.side,
            qty,
            order.symbol,
            fill.price,
            reference,
            fee,
        )
        return True

    def _triggered(self, order: Order, reference: float) -> bool:
        """Whether ``order`` may execute at ``reference``."""
        if order.type is OrderType.MARKET:
            return True
        if order.type is OrderType.LIMIT:
            assert order.limit_price is not None  # noqa: S101 - validated in Order
            return (
                reference <= order.limit_price
                if order.side is OrderSide.BUY
                else reference >= order.limit_price
            )
        assert order.stop_price is not None  # noqa: S101 - validated in Order
        stopped = (
            reference >= order.stop_price
            if order.side is OrderSide.BUY
            else reference <= order.stop_price
        )
        if not stopped:
            return False
        if order.type is OrderType.STOP:
            return True
        assert order.limit_price is not None  # noqa: S101 - validated in Order
        return (
            reference <= order.limit_price
            if order.side is OrderSide.BUY
            else reference >= order.limit_price
        )

    def _rejection_reason(self, order: Order, qty: float, price: float, fee: float) -> str | None:
        """Return why the fill must be refused, or ``None`` when it is allowed."""
        assert self._portfolio is not None  # noqa: S101 - _ensure_loaded ran first
        if order.side is OrderSide.BUY:
            required = qty * price + fee
            if required > self._portfolio.cash + QTY_EPSILON:
                return (
                    f"insufficient cash: {required:.2f} required, "
                    f"{self._portfolio.cash:.2f} available"
                )
            return None
        if not self.allow_short:
            held = self._portfolio.positions.get(order.symbol)
            available = held.qty if held is not None else 0.0
            if qty - available > QTY_EPSILON:
                return (
                    f"short selling is disabled: {qty:g} requested, {available:g} held"
                )
        return None

    # --- portfolio --------------------------------------------------------
    async def get_portfolio(self) -> Portfolio:
        """Return the paper portfolio, marked to the latest live quotes.

        A symbol whose quote cannot be fetched is left unmarked, which makes
        ``equity`` ``None`` and lists the symbol in ``unmarked_symbols`` -
        the page then says what it does not know instead of showing a stale
        number as if it were current.
        """
        async with self._lock:
            await self._ensure_loaded()
            portfolio = await asyncio.to_thread(self.store.load, self.account_id)
            symbols = sorted(portfolio.positions)
            if symbols:
                await self._mark(portfolio, symbols)
            return portfolio

    async def _mark(self, portfolio: Portfolio, symbols: list[str]) -> None:
        """Mark ``portfolio`` to the freshest quotes available for ``symbols``."""
        now = utcnow()
        needed: list[str] = []
        for symbol in symbols:
            cached = self._quotes.get(symbol)
            if cached is not None and (now - cached[1]).total_seconds() <= self.quote_ttl:
                portfolio.mark({symbol: cached[0].price})
            else:
                needed.append(symbol)
        if not needed:
            return
        try:
            quotes = await self.provider.get_quotes(needed)
        except ProviderError as exc:
            self.last_quote_error = str(exc)
            logger.warning("paper broker could not mark %s: %s", ",".join(needed), exc)
            return
        except NotImplementedError:
            self.last_quote_error = f"{self.provider.name} does not provide quotes"
            return
        self.last_quote_error = None
        for symbol, quote in quotes.items():
            self._quotes[symbol] = (quote, now)
            portfolio.mark({symbol: quote.price})

    async def reset(self, *, starting_cash: float | None = None) -> Portfolio:
        """Wipe the account's positions, orders and fills and restore its cash."""
        async with self._lock:
            await asyncio.to_thread(
                lambda: self.store.ensure_account(
                    self.account_id,
                    broker=self.name,
                    name="TradingLib paper account",
                    starting_cash=self.starting_cash,
                )
            )
            await asyncio.to_thread(
                lambda: self.store.reset_account(self.account_id, starting_cash=starting_cash)
            )
            self._open_orders.clear()
            self._quotes.clear()
            self._portfolio = await asyncio.to_thread(self.store.load, self.account_id)
            self._connected = True
            logger.info("paper account %s reset", self.account_id)
            return self._portfolio

    # --- status -----------------------------------------------------------
    def status(self) -> dict[str, Any]:
        """Return broker status for the trade endpoints."""
        portfolio = self._portfolio
        provider_name = self._provider.name if self._provider is not None else "yfinance"
        return {
            "broker": self.name,
            "status": "up" if self._connected else "idle",
            "configured": True,
            "connected": self._connected,
            "paper": True,
            "live_capable": False,
            "account_id": self.account_id,
            "starting_cash": self.starting_cash,
            "cash": None if portfolio is None else portfolio.cash,
            "positions": 0 if portfolio is None else len(portfolio.positions),
            "open_orders": len(self._open_orders),
            "slippage_bps": self.slippage_bps,
            "commission_per_share": self.commission_per_share,
            "commission_min": self.commission_min,
            "allow_short": self.allow_short,
            "quote_source": provider_name,
            "quote_ttl_seconds": self.quote_ttl,
            "cached_quotes": len(self._quotes),
            "tape_attached": self._tape_sub is not None,
            "tape_events": self.tape_events,
            "last_quote_error": self.last_quote_error,
            "detail": (
                "simulated fills against live quotes; no order ever reaches a venue"
            ),
        }


def _as_float(value: Any) -> float | None:
    """Coerce a tape payload field to ``float``, or ``None`` when it is absent."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


__all__ = ["DEFAULT_ACCOUNT_ID", "DEFAULT_QUOTE_TTL", "TAPE_CHANNEL", "PaperBroker"]
