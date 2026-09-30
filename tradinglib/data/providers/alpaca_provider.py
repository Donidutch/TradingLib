"""Alpaca market data: REST snapshots/bars plus a WebSocket streamer.

Requires credentials; without them :meth:`AlpacaProvider.status` reports
``configured=False`` and callers must show "not configured" rather than
substituting values from elsewhere. Every data call raises
:class:`~tradinglib.data.base.ProviderNotConfigured` in that state - it never
silently degrades to another vendor.

``alpaca-py``'s historical client is blocking, so REST calls run in a worker
thread. The live client is asyncio-native: :class:`AlpacaStream` drives its
event loop as a task on the running loop and republishes every message onto the
:class:`~tradinglib.events.EventBus`, because the data layer must not import
``tradinglib.api`` (``docs/SPEC.md`` section 4).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any, Final

from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.live import StockDataStream
from alpaca.data.models.bars import Bar as AlpacaBar
from alpaca.data.models.quotes import Quote as AlpacaQuote
from alpaca.data.models.trades import Trade as AlpacaTrade
from alpaca.data.requests import StockBarsRequest, StockSnapshotRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from tradinglib.data.base import (
    Bar,
    BarInterval,
    DataProvider,
    ProviderNotConfigured,
    ProviderStatus,
    ProviderUnavailable,
    Quote,
    SymbolNotFound,
    to_utc,
    utcnow,
)
from tradinglib.events import EventBus

logger = logging.getLogger(__name__)

#: Symbol used for the reachability probe in :meth:`AlpacaProvider.status`.
PROBE_SYMBOL: Final[str] = "SPY"

#: Event-bus channel family the live tape is published on (``tape.<SYMBOL>``).
TAPE_CHANNEL: Final[str] = "tape"

#: ``BarInterval`` -> ``alpaca-py`` timeframe.
TIMEFRAMES: Final[dict[BarInterval, tuple[int, TimeFrameUnit]]] = {
    BarInterval.MIN_1: (1, TimeFrameUnit.Minute),
    BarInterval.MIN_5: (5, TimeFrameUnit.Minute),
    BarInterval.MIN_15: (15, TimeFrameUnit.Minute),
    BarInterval.MIN_30: (30, TimeFrameUnit.Minute),
    BarInterval.HOUR_1: (1, TimeFrameUnit.Hour),
    BarInterval.DAY_1: (1, TimeFrameUnit.Day),
    BarInterval.WEEK_1: (1, TimeFrameUnit.Week),
    BarInterval.MONTH_1: (1, TimeFrameUnit.Month),
}


def _resolve_feed(feed: str) -> DataFeed:
    """Map a configured feed name onto an Alpaca ``DataFeed``."""
    try:
        return DataFeed(feed.strip().lower())
    except ValueError:
        logger.warning("unknown Alpaca feed %r; falling back to IEX", feed)
        return DataFeed.IEX


def _credentials(
    api_key: str | None, secret_key: str | None, feed: str | None
) -> tuple[str | None, str | None, str]:
    """Fill missing credentials from settings without overriding explicit ones."""
    if api_key is None or secret_key is None or feed is None:
        from tradinglib.config import get_settings

        settings = get_settings()
        api_key = api_key if api_key is not None else settings.alpaca_api_key
        secret_key = secret_key if secret_key is not None else settings.alpaca_secret_key
        feed = feed if feed is not None else settings.alpaca_data_feed
    return api_key, secret_key, feed or "iex"


class AlpacaProvider(DataProvider):
    """REST adapter over ``alpaca-py`` historical and snapshot endpoints."""

    name = "alpaca"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        secret_key: str | None = None,
        feed: str | None = "iex",
    ) -> None:
        """Create the provider; credentials default to configured settings.

        Pass ``feed=None`` to take the configured feed as well.
        """
        self.api_key, self.secret_key, self.feed = _credentials(api_key, secret_key, feed)
        self._client: StockHistoricalDataClient | None = None
        self._last_success: datetime | None = None

    @property
    def configured(self) -> bool:
        """Whether both credentials are present."""
        return bool(self.api_key and self.secret_key)

    def _require_client(self) -> StockHistoricalDataClient:
        """Return the REST client, raising when credentials are missing."""
        if not self.configured:
            raise ProviderNotConfigured(
                self.name,
                "ALPACA_API_KEY / ALPACA_SECRET_KEY are not configured",
            )
        if self._client is None:
            self._client = StockHistoricalDataClient(
                api_key=self.api_key, secret_key=self.secret_key
            )
        return self._client

    async def _call(self, label: str, fn: Any) -> Any:
        """Run a blocking alpaca-py call in a thread, mapping vendor errors."""
        try:
            result = await asyncio.to_thread(fn)
        except ProviderNotConfigured:
            raise
        except Exception as exc:  # noqa: BLE001 - alpaca raises APIError/ValueError/OSError
            raise ProviderUnavailable(
                self.name, f"{label}: {type(exc).__name__}: {exc}"
            ) from exc
        self._last_success = utcnow()
        return result

    # --- status -----------------------------------------------------------
    async def status(self) -> ProviderStatus:
        """Report credential presence and, when configured, live reachability."""
        if not self.configured:
            return ProviderStatus(
                name=self.name,
                configured=False,
                reachable=None,
                detail="not configured: set ALPACA_API_KEY and ALPACA_SECRET_KEY in Settings",
            )
        started = utcnow()
        try:
            await self.get_quote(PROBE_SYMBOL)
        except ProviderUnavailable as exc:
            return ProviderStatus(
                name=self.name,
                configured=True,
                reachable=False,
                last_success=self._last_success,
                detail=exc.message,
            )
        except SymbolNotFound as exc:
            return ProviderStatus(
                name=self.name,
                configured=True,
                reachable=True,
                last_success=self._last_success,
                detail=exc.message,
            )
        latency_ms = (utcnow() - started).total_seconds() * 1000.0
        return ProviderStatus(
            name=self.name,
            configured=True,
            reachable=True,
            latency_ms=latency_ms,
            last_success=self._last_success,
            detail=f"feed={self.feed}",
        )

    # --- quotes -----------------------------------------------------------
    def _snapshot_to_quote(self, symbol: str, snapshot: Any) -> Quote | None:
        """Build a :class:`Quote` from an Alpaca snapshot, or ``None`` if empty."""
        if snapshot is None:
            return None
        trade: AlpacaTrade | None = getattr(snapshot, "latest_trade", None)
        quote: AlpacaQuote | None = getattr(snapshot, "latest_quote", None)
        daily: AlpacaBar | None = getattr(snapshot, "daily_bar", None)
        previous: AlpacaBar | None = getattr(snapshot, "previous_daily_bar", None)

        price = None
        ts = None
        if trade is not None:
            price = float(trade.price)
            ts = to_utc(trade.timestamp)
        elif daily is not None:
            price = float(daily.close)
            ts = to_utc(daily.timestamp)
        if price is None or ts is None:
            return None
        return Quote(
            symbol=symbol.upper(),
            price=price,
            ts=ts,
            bid=float(quote.bid_price) if quote and quote.bid_price else None,
            ask=float(quote.ask_price) if quote and quote.ask_price else None,
            bid_size=int(quote.bid_size) if quote and quote.bid_size else None,
            ask_size=int(quote.ask_size) if quote and quote.ask_size else None,
            volume=int(daily.volume) if daily and daily.volume is not None else None,
            prev_close=float(previous.close) if previous is not None else None,
            source=self.name,
        )

    async def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """Return latest snapshot quotes for ``symbols``.

        Symbols the feed does not cover are omitted rather than faked.
        """
        wanted = [s.strip().upper() for s in symbols if s.strip()]
        if not wanted:
            return {}
        client = self._require_client()
        request = StockSnapshotRequest(symbol_or_symbols=wanted, feed=_resolve_feed(self.feed))
        raw = await self._call(
            f"snapshots({len(wanted)} symbols)", lambda: client.get_stock_snapshot(request)
        )
        snapshots = raw if isinstance(raw, dict) else {wanted[0]: raw}
        out: dict[str, Quote] = {}
        for symbol in wanted:
            quote = self._snapshot_to_quote(symbol, snapshots.get(symbol))
            if quote is not None:
                out[symbol] = quote
        return out

    async def get_quote(self, symbol: str) -> Quote:
        """Return the latest Alpaca snapshot quote for ``symbol``."""
        quotes = await self.get_quotes([symbol])
        quote = quotes.get(symbol.strip().upper())
        if quote is None:
            raise SymbolNotFound(self.name, f"{symbol}: no snapshot on feed {self.feed}")
        return quote

    # --- bars -------------------------------------------------------------
    async def get_bars(
        self,
        symbol: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        interval: BarInterval = BarInterval.DAY_1,
        limit: int | None = None,
    ) -> list[Bar]:
        """Return historical bars from the Alpaca data API."""
        client = self._require_client()
        amount, unit = TIMEFRAMES[interval]
        window = timedelta(seconds=interval.approx_seconds * (limit or 500))
        request = StockBarsRequest(
            symbol_or_symbols=symbol.strip().upper(),
            timeframe=TimeFrame(amount, unit),
            start=to_utc(start) if start else utcnow() - window,
            end=to_utc(end) if end else None,
            limit=limit,
            feed=_resolve_feed(self.feed),
        )
        raw = await self._call(f"bars({symbol})", lambda: client.get_stock_bars(request))
        rows = raw.data.get(symbol.strip().upper(), []) if hasattr(raw, "data") else []
        return [
            Bar(
                symbol=symbol.strip().upper(),
                ts=to_utc(row.timestamp),
                open=float(row.open),
                high=float(row.high),
                low=float(row.low),
                close=float(row.close),
                volume=float(row.volume or 0.0),
                interval=interval,
                source=self.name,
            )
            for row in rows
        ]


class AlpacaStream:
    """WebSocket streamer publishing live trades/quotes onto the event bus.

    Publishes to the ``tape`` channel family (``tape.<SYMBOL>``) so
    ``tradinglib.api.ws`` can fan out to browsers without the data layer importing
    the API package. Payloads carry ``kind`` (``"trade"``/``"quote"``), the
    symbol, the vendor timestamp in UTC ISO-8601 and the numeric fields the
    message actually contained - absent fields stay ``null``.
    """

    def __init__(
        self,
        bus: EventBus,
        *,
        api_key: str | None = None,
        secret_key: str | None = None,
        feed: str | None = "iex",
    ) -> None:
        """Create the streamer bound to ``bus``."""
        self.bus = bus
        self.api_key, self.secret_key, self.feed = _credentials(api_key, secret_key, feed)
        self._stream: StockDataStream | None = None
        self._task: asyncio.Task[None] | None = None
        self._symbols: set[str] = set()
        self._lock = asyncio.Lock()
        self.messages_published = 0
        self.last_message_at: datetime | None = None

    @property
    def configured(self) -> bool:
        """Whether both credentials are present."""
        return bool(self.api_key and self.secret_key)

    @property
    def running(self) -> bool:
        """Whether the stream task is alive."""
        return self._task is not None and not self._task.done()

    @property
    def symbols(self) -> list[str]:
        """Symbols currently subscribed, sorted."""
        return sorted(self._symbols)

    def status(self) -> dict[str, Any]:
        """Return an honest snapshot of the streamer's state."""
        return {
            "configured": self.configured,
            "running": self.running,
            "feed": self.feed,
            "symbols": self.symbols,
            "messages_published": self.messages_published,
            "last_message_at": self.last_message_at.isoformat()
            if self.last_message_at
            else None,
        }

    # --- lifecycle --------------------------------------------------------
    async def start(self) -> None:
        """Open the stream and begin publishing events.

        The underlying client does not connect until at least one subscription
        exists, so this returns as soon as its loop is scheduled; call
        :meth:`subscribe` to make it connect.
        """
        if not self.configured:
            raise ProviderNotConfigured(
                "alpaca", "ALPACA_API_KEY / ALPACA_SECRET_KEY are not configured"
            )
        async with self._lock:
            if self.running:
                return
            self._stream = StockDataStream(
                api_key=str(self.api_key),
                secret_key=str(self.secret_key),
                feed=_resolve_feed(self.feed),
            )
            # ``_run_forever`` is the client's own asyncio entry point;
            # ``run()`` merely wraps it in ``asyncio.run``, which cannot be used
            # from inside the API's already-running event loop.
            self._task = asyncio.create_task(
                self._stream._run_forever(), name="alpaca-stream"
            )

    async def stop(self) -> None:
        """Close the stream and cancel background tasks."""
        async with self._lock:
            stream, task = self._stream, self._task
            self._stream, self._task = None, None
            self._symbols.clear()
        if stream is not None:
            try:
                await stream.stop_ws()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                logger.debug("alpaca stream stop_ws failed", exc_info=True)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    # --- subscriptions ----------------------------------------------------
    async def subscribe(self, symbols: Iterable[str]) -> None:
        """Add ``symbols`` to the live subscription set."""
        wanted = sorted({s.strip().upper() for s in symbols if s.strip()})
        if not wanted:
            return
        if not self.running:
            await self.start()
        stream = self._stream
        if stream is None:  # pragma: no cover - start() guarantees this
            raise ProviderUnavailable("alpaca", "stream is not running")
        stream.subscribe_trades(self._on_trade, *wanted)
        stream.subscribe_quotes(self._on_quote, *wanted)
        self._symbols.update(wanted)

    async def unsubscribe(self, symbols: Iterable[str]) -> None:
        """Remove ``symbols`` from the live subscription set."""
        wanted = sorted({s.strip().upper() for s in symbols if s.strip()})
        stream = self._stream
        if not wanted or stream is None:
            return
        stream.unsubscribe_trades(*wanted)
        stream.unsubscribe_quotes(*wanted)
        self._symbols.difference_update(wanted)

    # --- handlers ---------------------------------------------------------
    async def _publish(self, symbol: str, payload: dict[str, Any]) -> None:
        """Publish one tape payload and update the counters."""
        self.messages_published += 1
        self.last_message_at = utcnow()
        await self.bus.publish(f"{TAPE_CHANNEL}.{symbol}", payload)

    async def _on_trade(self, trade: AlpacaTrade) -> None:
        """Publish a live trade print."""
        symbol = str(trade.symbol).upper()
        await self._publish(
            symbol,
            {
                "kind": "trade",
                "symbol": symbol,
                "price": float(trade.price),
                "size": float(trade.size) if trade.size is not None else None,
                "ts": to_utc(trade.timestamp).isoformat(),
                "exchange": str(trade.exchange) if trade.exchange else None,
                "source": "alpaca",
                "feed": self.feed,
            },
        )

    async def _on_quote(self, quote: AlpacaQuote) -> None:
        """Publish a live NBBO update."""
        symbol = str(quote.symbol).upper()
        await self._publish(
            symbol,
            {
                "kind": "quote",
                "symbol": symbol,
                "bid": float(quote.bid_price) if quote.bid_price else None,
                "ask": float(quote.ask_price) if quote.ask_price else None,
                "bid_size": float(quote.bid_size) if quote.bid_size else None,
                "ask_size": float(quote.ask_size) if quote.ask_size else None,
                "ts": to_utc(quote.timestamp).isoformat(),
                "source": "alpaca",
                "feed": self.feed,
            },
        )
