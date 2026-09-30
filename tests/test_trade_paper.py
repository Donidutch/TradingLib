"""Paper broker fills, cash accounting and P&L against synthetic quotes.

Every test here drives :class:`~tradinglib.trade.paper.PaperBroker` from a
deterministic in-test quote source, so the expected fill prices and balances
are arithmetic rather than whatever a vendor happened to print. No test in this
file - or anywhere else in the suite - places an order at a real venue.
"""

from __future__ import annotations

import pytest

from tradinglib.config import Settings
from tradinglib.data.base import DataProvider, ProviderStatus, Quote, SymbolNotFound, utcnow
from tradinglib.trade.alpaca_broker import LIVE_OPT_IN_ENV, AlpacaBroker
from tradinglib.trade.ibkr_broker import IBKRBroker
from tradinglib.trade.paper import PaperBroker
from tradinglib.trade.portfolio import (
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Portfolio,
    PortfolioStore,
)


class SyntheticQuoteProvider(DataProvider):
    """Deterministic quote source. Test-only - it reaches no vendor at all."""

    name = "synthetic"

    def __init__(self, prices: dict[str, float] | None = None) -> None:
        """Create the provider seeded with ``{symbol: price}``."""
        self._quotes: dict[str, Quote] = {}
        for symbol, price in (prices or {}).items():
            self.set(symbol, price)

    def set(
        self, symbol: str, price: float, *, bid: float | None = None, ask: float | None = None
    ) -> None:
        """Set (or replace) the quote a symbol returns."""
        key = symbol.strip().upper()
        self._quotes[key] = Quote(
            symbol=key, price=price, ts=utcnow(), bid=bid, ask=ask, source=self.name
        )

    def drop(self, symbol: str) -> None:
        """Forget a symbol, so quoting it fails the way an unknown ticker does."""
        self._quotes.pop(symbol.strip().upper(), None)

    async def status(self) -> ProviderStatus:
        """Report the synthetic provider as configured and reachable."""
        return ProviderStatus(name=self.name, configured=True, reachable=True)

    async def get_quote(self, symbol: str) -> Quote:
        """Return the seeded quote for ``symbol``."""
        key = symbol.strip().upper()
        if key not in self._quotes:
            raise SymbolNotFound(self.name, f"no synthetic quote for {key}")
        return self._quotes[key]

    async def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """Return the seeded quotes for the symbols that have one."""
        return {s.strip().upper(): self._quotes[s.strip().upper()] for s in symbols
                if s.strip().upper() in self._quotes}


def make_broker(
    provider: SyntheticQuoteProvider,
    *,
    cash: float = 100_000.0,
    slippage_bps: float = 0.0,
    account_id: str = "test-paper",
    quote_ttl: float = 0.0,
    **kwargs: object,
) -> PaperBroker:
    """Build a paper broker bound to ``provider`` and the temporary database.

    ``quote_ttl`` defaults to zero so a test that moves the synthetic price is
    never served the broker's cached quote - the tests, not a cache timer,
    decide what the market is doing.
    """
    return PaperBroker(
        starting_cash=cash,
        slippage_bps=slippage_bps,
        account_id=account_id,
        provider=provider,
        quote_ttl=quote_ttl,
        **kwargs,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------
# portfolio arithmetic (no broker involved)
# --------------------------------------------------------------------------
def test_average_cost_blends_on_add_and_holds_on_partial_close() -> None:
    """Adding blends the average; a partial close realises and keeps the average."""
    portfolio = Portfolio(account_id="unit", cash=10_000.0, starting_cash=10_000.0)
    buy1 = Order(id="o1", symbol="AAPL", side=OrderSide.BUY, qty=10)
    portfolio.apply_fill(buy1, 10, 100.0)
    buy2 = Order(id="o2", symbol="AAPL", side=OrderSide.BUY, qty=10)
    portfolio.apply_fill(buy2, 10, 120.0)

    position = portfolio.positions["AAPL"]
    assert position.qty == 20
    assert position.avg_price == 110.0
    assert portfolio.cash == 10_000.0 - 1_000.0 - 1_200.0

    sell = Order(id="o3", symbol="AAPL", side=OrderSide.SELL, qty=5)
    portfolio.apply_fill(sell, 5, 130.0)
    position = portfolio.positions["AAPL"]
    assert position.qty == 15
    assert position.avg_price == 110.0, "a partial close must not move the average"
    assert portfolio.realized_pnl == pytest.approx((130.0 - 110.0) * 5)


def test_flip_realises_the_close_then_reopens_at_the_fill_price() -> None:
    """A sell larger than the long realises the whole close and opens a short."""
    portfolio = Portfolio(account_id="unit", cash=100_000.0, starting_cash=100_000.0)
    buy = Order(id="o1", symbol="MSFT", side=OrderSide.BUY, qty=10)
    portfolio.apply_fill(buy, 10, 100.0)
    sell = Order(id="o2", symbol="MSFT", side=OrderSide.SELL, qty=25)
    portfolio.apply_fill(sell, 25, 110.0)

    position = portfolio.positions["MSFT"]
    assert position.qty == -15
    assert position.avg_price == 110.0
    assert portfolio.realized_pnl == pytest.approx((110.0 - 100.0) * 10)
    assert portfolio.cash == pytest.approx(100_000.0 - 1_000.0 + 25 * 110.0)


def test_equity_is_null_until_every_position_is_marked() -> None:
    """An unmarked holding makes equity unknown rather than wrong."""
    portfolio = Portfolio(account_id="unit", cash=1_000.0, starting_cash=1_000.0)
    buy = Order(id="o1", symbol="KO", side=OrderSide.BUY, qty=5)
    portfolio.apply_fill(buy, 5, 60.0)
    assert portfolio.equity is None
    assert portfolio.unmarked_symbols == ["KO"]

    portfolio.mark({"KO": 65.0})
    assert portfolio.equity == pytest.approx(1_000.0 - 300.0 + 5 * 65.0)
    assert portfolio.unrealized_pnl == pytest.approx(25.0)
    assert portfolio.total_pnl == pytest.approx(25.0)


def test_apply_fill_refuses_to_overfill_an_order() -> None:
    """A fill bigger than the order's remaining quantity is a programming error."""
    portfolio = Portfolio(account_id="unit", cash=10_000.0)
    order = Order(id="o1", symbol="AAPL", side=OrderSide.BUY, qty=5)
    with pytest.raises(ValueError, match="exceeds"):
        portfolio.apply_fill(order, 6, 100.0)


# --------------------------------------------------------------------------
# paper broker fills
# --------------------------------------------------------------------------
async def test_market_buy_fills_at_the_slipped_ask(settings: Settings) -> None:
    """A buy crosses the spread and then pays the slippage assumption."""
    provider = SyntheticQuoteProvider()
    provider.set("AAPL", 100.0, bid=99.9, ask=100.1)
    broker = make_broker(provider, slippage_bps=10.0)

    order = await broker.submit_order("aapl", OrderSide.BUY, 10)

    assert order.status is OrderStatus.FILLED
    expected = round(100.1 * 1.001, 6)
    assert order.avg_fill_price == pytest.approx(expected)
    fill = order.fills[0]
    assert fill.reference_price == pytest.approx(100.1), "the ask is the reference for a buy"
    assert fill.slippage_bps == 10.0

    portfolio = await broker.get_portfolio()
    assert portfolio.cash == pytest.approx(100_000.0 - 10 * expected)
    assert portfolio.positions["AAPL"].qty == 10
    assert portfolio.positions["AAPL"].market_price == pytest.approx(100.0)


async def test_market_sell_realises_pnl_and_credits_cash(settings: Settings) -> None:
    """Selling a long realises P&L against the average cost."""
    provider = SyntheticQuoteProvider({"MSFT": 100.0})
    broker = make_broker(provider, slippage_bps=10.0)

    await broker.submit_order("MSFT", OrderSide.BUY, 10)
    provider.set("MSFT", 110.0)
    sell = await broker.submit_order("MSFT", OrderSide.SELL, 10)

    buy_price = round(100.0 * 1.001, 6)
    sell_price = round(110.0 * 0.999, 6)
    assert sell.avg_fill_price == pytest.approx(sell_price)

    portfolio = await broker.get_portfolio()
    assert portfolio.positions == {}
    assert portfolio.realized_pnl == pytest.approx((sell_price - buy_price) * 10)
    assert portfolio.cash == pytest.approx(100_000.0 - 10 * buy_price + 10 * sell_price)
    assert portfolio.equity == pytest.approx(portfolio.cash)


async def test_short_sell_then_cover(settings: Settings) -> None:
    """A short credits the proceeds and covering realises the gain."""
    provider = SyntheticQuoteProvider({"XOM": 100.0})
    broker = make_broker(provider)

    await broker.submit_order("XOM", OrderSide.SELL, 5)
    portfolio = await broker.get_portfolio()
    assert portfolio.positions["XOM"].qty == -5
    assert portfolio.cash == pytest.approx(100_500.0)

    provider.set("XOM", 90.0)
    await broker.submit_order("XOM", OrderSide.BUY, 5)
    portfolio = await broker.get_portfolio()
    assert portfolio.positions == {}
    assert portfolio.realized_pnl == pytest.approx(50.0)
    assert portfolio.cash == pytest.approx(100_050.0)
    assert portfolio.total_pnl == pytest.approx(50.0)


async def test_commission_is_charged_as_a_fee(settings: Settings) -> None:
    """Commission leaves cash and shows up as a realised cost."""
    provider = SyntheticQuoteProvider({"PG": 100.0})
    broker = make_broker(provider, commission_per_share=0.01)

    order = await broker.submit_order("PG", OrderSide.BUY, 10)

    assert order.fills[0].fee == pytest.approx(0.1)
    portfolio = await broker.get_portfolio()
    assert portfolio.cash == pytest.approx(100_000.0 - 1_000.0 - 0.1)
    assert portfolio.realized_pnl == pytest.approx(-0.1)


async def test_limit_buy_rests_until_the_tape_reaches_it(settings: Settings) -> None:
    """An unmarketable limit rests and fills when a later quote satisfies it."""
    provider = SyntheticQuoteProvider({"JNJ": 100.0})
    broker = make_broker(provider)

    order = await broker.submit_order(
        "JNJ", OrderSide.BUY, 10, type=OrderType.LIMIT, limit_price=95.0
    )
    assert order.status is OrderStatus.NEW
    assert order.is_open

    filled = await broker.on_quote("JNJ", 94.0)
    assert [o.id for o in filled] == [order.id]
    assert order.status is OrderStatus.FILLED
    assert order.avg_fill_price == pytest.approx(94.0)


async def test_limit_never_trades_through_its_price(settings: Settings) -> None:
    """Slippage may not push a limit fill past the limit."""
    provider = SyntheticQuoteProvider({"KO": 100.0})
    broker = make_broker(provider, slippage_bps=100.0)  # 1%

    order = await broker.submit_order(
        "KO", OrderSide.BUY, 10, type=OrderType.LIMIT, limit_price=100.0
    )

    assert order.status is OrderStatus.FILLED
    assert order.avg_fill_price == pytest.approx(100.0), "1% slippage would breach the limit"


async def test_sell_stop_triggers_on_a_falling_quote(settings: Settings) -> None:
    """A sell stop rests above the trigger and fills once price trades through."""
    provider = SyntheticQuoteProvider({"HD": 100.0})
    broker = make_broker(provider)

    await broker.submit_order("HD", OrderSide.BUY, 10)
    stop = await broker.submit_order(
        "HD", OrderSide.SELL, 10, type=OrderType.STOP, stop_price=95.0
    )
    assert stop.status is OrderStatus.NEW

    assert await broker.on_quote("HD", 96.0) == [], "96 has not reached the stop"
    filled = await broker.on_quote("HD", 94.0)

    assert [o.id for o in filled] == [stop.id]
    assert stop.avg_fill_price == pytest.approx(94.0)
    portfolio = await broker.get_portfolio()
    assert portfolio.positions == {}
    assert portfolio.realized_pnl == pytest.approx(-60.0)


async def test_unaffordable_buy_is_rejected_and_recorded(settings: Settings) -> None:
    """A buy the account cannot fund is refused, and the refusal is kept."""
    provider = SyntheticQuoteProvider({"NVDA": 100.0})
    broker = make_broker(provider, cash=1_000.0, account_id="test-poor")

    order = await broker.submit_order("NVDA", OrderSide.BUY, 100)

    assert order.status is OrderStatus.REJECTED
    assert order.note is not None and "insufficient cash" in order.note
    portfolio = await broker.get_portfolio()
    assert portfolio.cash == 1_000.0
    assert portfolio.positions == {}
    assert [o.id for o in portfolio.orders] == [order.id]


async def test_short_selling_can_be_disabled(settings: Settings) -> None:
    """With shorting off, a naked sell is rejected rather than silently allowed."""
    provider = SyntheticQuoteProvider({"TSLA": 100.0})
    broker = make_broker(provider, allow_short=False, account_id="test-noshort")

    order = await broker.submit_order("TSLA", OrderSide.SELL, 5)

    assert order.status is OrderStatus.REJECTED
    assert order.note is not None and "short selling is disabled" in order.note


async def test_cancel_a_resting_order(settings: Settings) -> None:
    """Cancelling closes the order; cancelling twice is an error, not a no-op."""
    provider = SyntheticQuoteProvider({"PEP": 100.0})
    broker = make_broker(provider)

    order = await broker.submit_order(
        "PEP", OrderSide.BUY, 10, type=OrderType.LIMIT, limit_price=90.0
    )
    cancelled = await broker.cancel_order(order.id)
    assert cancelled.status is OrderStatus.CANCELLED

    with pytest.raises(ValueError, match="cannot be cancelled"):
        await broker.cancel_order(order.id)
    assert await broker.on_quote("PEP", 80.0) == [], "a cancelled order must not fill"


async def test_unknown_order_cancellation_raises(settings: Settings) -> None:
    """Cancelling an order that never existed is a KeyError (a 404 at the API)."""
    broker = make_broker(SyntheticQuoteProvider({"AAPL": 100.0}))
    await broker.connect()
    with pytest.raises(KeyError):
        await broker.cancel_order("ord-does-not-exist")


async def test_quote_failure_leaves_the_position_unmarked(settings: Settings) -> None:
    """A symbol that cannot be quoted is reported unmarked, not stale-marked."""
    provider = SyntheticQuoteProvider({"WMT": 100.0})
    broker = make_broker(provider)

    await broker.submit_order("WMT", OrderSide.BUY, 10)
    provider.drop("WMT")

    portfolio = await broker.get_portfolio()
    assert portfolio.unmarked_symbols == ["WMT"]
    assert portfolio.equity is None
    assert portfolio.positions["WMT"].qty == 10


async def test_state_survives_a_new_broker_instance(settings: Settings) -> None:
    """Cash, positions and resting orders are persisted, not held in memory."""
    provider = SyntheticQuoteProvider({"SPY": 400.0})
    first = make_broker(provider, account_id="test-persist")
    await first.submit_order("SPY", OrderSide.BUY, 2)
    resting = await first.submit_order(
        "SPY", OrderSide.BUY, 1, type=OrderType.LIMIT, limit_price=300.0
    )
    await first.close()

    second = make_broker(provider, account_id="test-persist")
    portfolio = await second.get_portfolio()
    assert portfolio.cash == pytest.approx(100_000.0 - 800.0)
    assert portfolio.positions["SPY"].qty == 2

    filled = await second.on_quote("SPY", 299.0)
    assert [o.id for o in filled] == [resting.id], "the resting order was reloaded"


async def test_reset_restores_the_opening_balance(settings: Settings) -> None:
    """Resetting wipes positions, orders and fills and restores cash."""
    provider = SyntheticQuoteProvider({"JPM": 100.0})
    broker = make_broker(provider, account_id="test-reset")
    await broker.submit_order("JPM", OrderSide.BUY, 10)

    portfolio = await broker.reset()

    assert portfolio.cash == 100_000.0
    assert portfolio.positions == {}
    assert portfolio.orders == []
    assert await broker.list_fills() == []


async def test_fills_record_the_price_they_were_derived_from(settings: Settings) -> None:
    """Every fill carries its reference price, so a track record is re-derivable."""
    provider = SyntheticQuoteProvider()
    provider.set("AVGO", 200.0, bid=199.5, ask=200.5)
    broker = make_broker(provider, slippage_bps=25.0, account_id="test-audit")

    await broker.submit_order("AVGO", OrderSide.BUY, 3)
    fills = await broker.list_fills()

    assert len(fills) == 1
    fill = fills[0]
    assert fill.reference_price == pytest.approx(200.5)
    assert fill.price == pytest.approx(round(200.5 * 1.0025, 6))
    assert fill.qty == 3
    assert fill.notional == pytest.approx(3 * fill.price)


async def test_the_tape_channel_drives_resting_orders(settings: Settings, bus) -> None:  # type: ignore[no-untyped-def]
    """Attached to the event bus, a live tape print fills a resting order."""
    provider = SyntheticQuoteProvider({"META": 500.0})
    broker = make_broker(provider, account_id="test-tape")
    broker.attach(bus)

    order = await broker.submit_order(
        "META", OrderSide.BUY, 1, type=OrderType.LIMIT, limit_price=450.0
    )
    assert order.status is OrderStatus.NEW

    await bus.publish("tape.META", {"kind": "trade", "symbol": "META", "price": 449.0})

    assert order.status is OrderStatus.FILLED
    assert broker.tape_events == 1
    broker.detach()
    assert broker.status()["tape_attached"] is False


async def test_paper_status_is_honest_before_and_after_connecting(settings: Settings) -> None:
    """The status payload never claims a session that does not exist."""
    broker = make_broker(SyntheticQuoteProvider({"AAPL": 100.0}), account_id="test-status")

    idle = broker.status()
    assert idle["status"] == "idle"
    assert idle["connected"] is False
    assert idle["paper"] is True and idle["live_capable"] is False
    assert idle["cash"] is None

    await broker.connect()
    live = broker.status()
    assert live["status"] == "up"
    assert live["cash"] == 100_000.0
    assert live["quote_source"] == "synthetic"


async def test_store_load_rejects_an_unknown_account(settings: Settings) -> None:
    """Loading an account that was never created is a KeyError (a 404 at the API)."""
    store = PortfolioStore()
    with pytest.raises(KeyError):
        store.load("no-such-account")


# --------------------------------------------------------------------------
# live-trading guards
# --------------------------------------------------------------------------
def test_alpaca_refuses_live_without_the_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """Constructing a live Alpaca session is impossible without the env opt-in."""
    monkeypatch.delenv(LIVE_OPT_IN_ENV, raising=False)
    with pytest.raises(PermissionError, match=LIVE_OPT_IN_ENV):
        AlpacaBroker(api_key="k", secret_key="s", paper=False)


def test_alpaca_paper_adapter_points_at_the_paper_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default adapter talks to paper-api, and says so in its status."""
    monkeypatch.delenv(LIVE_OPT_IN_ENV, raising=False)
    broker = AlpacaBroker(api_key="k", secret_key="s")
    assert broker.base_url == "https://paper-api.alpaca.markets"
    assert broker.status()["paper"] is True


def test_alpaca_without_credentials_reports_not_configured(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No keys means an honest not_configured status, not a hopeful default."""
    monkeypatch.delenv(LIVE_OPT_IN_ENV, raising=False)
    broker = AlpacaBroker(api_key=None, secret_key=None)
    broker.api_key = None
    broker.secret_key = None
    payload = broker.status()
    assert payload["configured"] is False
    assert payload["status"] == "not_configured"
    assert "not configured" in payload["detail"]


def test_ibkr_refuses_a_live_port_in_paper_mode() -> None:
    """A live TWS port cannot be used while claiming paper mode."""
    with pytest.raises(PermissionError, match="live port"):
        IBKRBroker(port=7496)


def test_ibkr_reports_a_missing_gateway_instead_of_pretending() -> None:
    """With nothing listening, status says unavailable and names host and port."""
    broker = IBKRBroker(port=1)
    payload = broker.status()
    assert payload["connected"] is False
    assert payload["gateway_reachable"] is False
    assert payload["status"] == "unavailable"
    assert "127.0.0.1:1" in payload["detail"]


async def test_ibkr_connect_fails_gracefully_without_a_gateway() -> None:
    """Connecting with no gateway raises a provider error, not a raw socket error."""
    from tradinglib.data.base import ProviderUnavailable

    broker = IBKRBroker(port=1, timeout=0.5)
    with pytest.raises(ProviderUnavailable, match="127.0.0.1:1"):
        await broker.connect()
    assert broker.connected is False
