"""Live publishers behind the ``status`` and ``tape`` WebSocket channels.

The other two live channels need no publisher: ``ingest`` is fed by the source
catalog, scheduler and gateway, and ``quality`` by the quality engine - both
publish onto :class:`tradinglib.events.EventBus` themselves, and
:class:`tradinglib.api.ws.WsHub` fans them out. What is missing is a producer for

* ``status`` - the datastrip payload, republished on a cadence
  (:class:`StatusBroadcaster`);
* ``tape`` - live quotes for the symbols browsers are watching
  (:class:`TapePublisher`);
* ``runs`` - :class:`LabRunBridge` mirrors the Strategy Lab's ``lab`` progress
  events onto it, so the spec's run channel carries research runs too.

The tape has two real sources and no third one: when Alpaca credentials are
configured the process runs :class:`~tradinglib.data.providers.alpaca_provider.AlpacaStream`,
which publishes genuine trade/quote messages onto ``tape.<SYMBOL>`` itself, and
this publisher only manages its subscription set. Without credentials the tape
falls back to polling Yahoo quotes through
:class:`~tradinglib.data.providers.yfinance_provider.YFinanceProvider`; those are
delayed, and every payload says so (``delayed: true``) rather than pretending
to be a live NBBO feed. Both publishers do nothing at all while no browser is
subscribed, so a headless server issues no vendor requests.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI

from tradinglib.api.ws import WsHub
from tradinglib.data.base import ProviderError, ProviderRateLimited
from tradinglib.events import EventBus

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tradinglib.data.base import DataProvider
    from tradinglib.data.providers.alpaca_provider import AlpacaStream
    from tradinglib.data.universe import UniverseRegistry, WatchlistRegistry

logger = logging.getLogger(__name__)

#: Channel names (``docs/SPEC.md`` section 5).
STATUS_CHANNEL = "status"
TAPE_CHANNEL = "tape"
RUNS_CHANNEL = "runs"

#: Channel the research engines publish Strategy Lab progress on.
LAB_CHANNEL = "lab"

#: Default cadences, in seconds.
DEFAULT_STATUS_INTERVAL = 5.0
DEFAULT_TAPE_INTERVAL = 15.0

#: Upper bound on symbols polled per tape cycle. Yahoo's free endpoint is one
#: request per symbol, so an unbounded active universe would be abusive.
DEFAULT_MAX_TAPE_SYMBOLS = 30

#: Fields that change on every build and therefore never count as a "delta".
_VOLATILE_STATUS_FIELDS = frozenset({"server_time_utc", "uptime_seconds", "ws_connections"})


class StatusBroadcaster:
    """Republishes the ``/api/status`` payload on the ``status`` channel.

    The payload is built by the same function the REST endpoint uses, so the
    datastrip over WebSocket can never disagree with a page reload. Each frame
    carries ``changed``: the keys whose value differs from the previous frame
    (volatile clock fields excluded), which is the "datastrip deltas" the UI
    needs to animate only what actually moved.
    """

    def __init__(
        self,
        app: FastAPI,
        bus: EventBus,
        hub: WsHub,
        *,
        interval: float = DEFAULT_STATUS_INTERVAL,
        only_when_watched: bool = True,
    ) -> None:
        """Create the broadcaster.

        Args:
            app: application whose ``state`` the payload is built from.
            bus: event bus to publish on.
            hub: WebSocket hub, consulted for live subscribers.
            interval: seconds between frames.
            only_when_watched: skip work entirely while nobody is subscribed.
        """
        self.app = app
        self.bus = bus
        self.hub = hub
        self.interval = interval
        self.only_when_watched = only_when_watched
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._previous: dict[str, Any] | None = None
        self.published = 0

    @property
    def running(self) -> bool:
        """Whether the broadcast loop is alive."""
        return self._running and self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Start the broadcast loop (idempotent)."""
        if self.running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="status-broadcaster")
        logger.info("status broadcaster started (every %.1fs)", self.interval)

    async def stop(self) -> None:
        """Stop the broadcast loop."""
        self._running = False
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        logger.info("status broadcaster stopped after %d frame(s)", self.published)

    async def _loop(self) -> None:
        """Publish one frame per interval until cancelled."""
        while self._running:
            try:
                await self.publish_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad frame must not stop the loop
                logger.exception("status broadcast failed")
            await asyncio.sleep(self.interval)

    async def publish_once(self) -> dict[str, Any] | None:
        """Build and publish one status frame. Returns it, or ``None`` if skipped."""
        if self.only_when_watched and self.hub.subscriber_count(STATUS_CHANNEL) == 0:
            return None
        from tradinglib.api.routes.status import build_status

        payload = await build_status(self.app)
        changed = self._changed_keys(payload)
        frame = {"event": "datastrip", "changed": changed, **payload}
        await self.bus.publish(STATUS_CHANNEL, frame)
        self._previous = payload
        self.published += 1
        return frame

    def _changed_keys(self, payload: dict[str, Any]) -> list[str]:
        """Return the keys that differ from the previous frame."""
        if self._previous is None:
            return sorted(key for key in payload if key not in _VOLATILE_STATUS_FIELDS)
        return sorted(
            key
            for key, value in payload.items()
            if key not in _VOLATILE_STATUS_FIELDS and self._previous.get(key) != value
        )


class TapePublisher:
    """Publishes live quotes for the symbols browsers are watching.

    Symbol selection, in order: the ``symbols`` parameter a client sent with
    its ``tape`` subscription, then any ``tape.<SYMBOL>`` channel it subscribed
    to, then the active universe, then the default watchlist. The set is capped
    at ``max_symbols`` and the cap is reported in the summary frame rather than
    silently truncating the tape.
    """

    def __init__(
        self,
        bus: EventBus,
        hub: WsHub,
        *,
        provider: DataProvider | None = None,
        universes: UniverseRegistry | None = None,
        watchlists: WatchlistRegistry | None = None,
        stream_getter: Callable[[], AlpacaStream | None] | None = None,
        interval: float = DEFAULT_TAPE_INTERVAL,
        max_symbols: int = DEFAULT_MAX_TAPE_SYMBOLS,
    ) -> None:
        """Create the tape publisher.

        Args:
            bus: event bus to publish on.
            hub: WebSocket hub, consulted for the watched symbol set.
            provider: quote provider used for the polling fallback; defaults to
                :class:`YFinanceProvider` on first use.
            universes: universe registry, for the active-universe fallback.
            watchlists: watchlist registry, for the default-watchlist fallback.
            stream_getter: returns the Alpaca stream when one is running, so a
                credentialed install uses the real push feed instead of polling.
            interval: seconds between polling cycles.
            max_symbols: maximum symbols polled per cycle.
        """
        self.bus = bus
        self.hub = hub
        self._provider = provider
        self.universes = universes
        self.watchlists = watchlists
        self.stream_getter = stream_getter
        self.interval = interval
        self.max_symbols = max_symbols
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._streamed: set[str] = set()
        self.published = 0
        self.last_error: str | None = None

    @property
    def provider(self) -> DataProvider:
        """The quote provider used when no push stream is available."""
        if self._provider is None:
            from tradinglib.data.providers.yfinance_provider import YFinanceProvider

            self._provider = YFinanceProvider()
        return self._provider

    @property
    def running(self) -> bool:
        """Whether the polling loop is alive."""
        return self._running and self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Start the tape loop (idempotent)."""
        if self.running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="tape-publisher")
        logger.info("tape publisher started (every %.1fs)", self.interval)

    async def stop(self) -> None:
        """Stop the tape loop and drop any stream subscriptions it made."""
        self._running = False
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        stream = self.stream_getter() if self.stream_getter else None
        if stream is not None and self._streamed:
            with contextlib.suppress(Exception):
                await stream.unsubscribe(sorted(self._streamed))
        self._streamed.clear()
        logger.info("tape publisher stopped after %d quote(s)", self.published)

    # --- symbol selection -------------------------------------------------
    def watched_symbols(self) -> list[str]:
        """Return the symbols the connected browsers are asking for."""
        wanted: list[str] = []
        for channel, params in self.hub.subscriptions(TAPE_CHANNEL):
            for raw in params.get("symbols") or []:
                self._add(wanted, str(raw))
            if "." in channel:
                suffix = channel.split(".", 1)[1]
                if suffix and "*" not in suffix:
                    self._add(wanted, suffix)
        if wanted:
            return wanted[: self.max_symbols]
        return self._fallback_symbols()[: self.max_symbols]

    def _fallback_symbols(self) -> list[str]:
        """Symbols from the active scope, then the default watchlist.

        The active scope is whatever the topbar selector points at - a universe
        or a watchlist - so choosing "Watchlist: ..." really does re-point the
        tape rather than leaving it on the last universe.
        """
        out: list[str] = []
        if self.universes is not None:
            try:
                scope = self.universes.active_scope()
            except Exception:  # noqa: BLE001 - a storage fault must not kill the tape
                logger.exception("tape: could not read the active scope")
                scope = None
            if scope is not None:
                for symbol in scope.get("symbols") or []:
                    self._add(out, symbol)
        if not out and self.watchlists is not None:
            try:
                watchlist = self.watchlists.default_watchlist()
            except Exception:  # noqa: BLE001
                logger.exception("tape: could not read the default watchlist")
                watchlist = None
            if watchlist is not None:
                for symbol in watchlist.symbols:
                    self._add(out, symbol)
        return out

    @staticmethod
    def _add(target: list[str], raw: str) -> None:
        """Append a normalised, deduplicated symbol to ``target``."""
        symbol = raw.strip().upper()
        if symbol and symbol not in target:
            target.append(symbol)

    # --- loop -------------------------------------------------------------
    async def _loop(self) -> None:
        """Publish one tape cycle per interval until cancelled."""
        while self._running:
            delay = self.interval
            try:
                await self.publish_once()
            except asyncio.CancelledError:
                raise
            except ProviderRateLimited as exc:
                self.last_error = str(exc)
                delay = max(self.interval * 4, 60.0)
                logger.warning("tape: rate limited, backing off %.0fs", delay)
                await self._publish_error(str(exc), retry_in=delay)
            except Exception as exc:  # noqa: BLE001 - one bad cycle must not stop the tape
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.exception("tape cycle failed")
                await self._publish_error(self.last_error, retry_in=delay)
            await asyncio.sleep(delay)

    async def publish_once(self) -> list[str]:
        """Run one tape cycle. Returns the symbols published this cycle."""
        if self.hub.subscriber_count(TAPE_CHANNEL) == 0:
            return []
        symbols = self.watched_symbols()
        if not symbols:
            return []

        stream = self.stream_getter() if self.stream_getter else None
        if stream is not None and stream.configured:
            await self._sync_stream(stream, symbols)
            return symbols

        quotes = await self.provider.get_quotes(list(symbols))
        published: list[str] = []
        for symbol, quote in quotes.items():
            payload = quote.as_dict()
            payload.update({"kind": "quote", "delayed": True, "feed": self.provider.name})
            await self.bus.publish(f"{TAPE_CHANNEL}.{symbol}", payload)
            published.append(symbol)
        self.published += len(published)
        self.last_error = None
        missing = [s for s in symbols if s not in quotes]
        await self.bus.publish(
            TAPE_CHANNEL,
            {
                "event": "tape_cycle",
                "symbols": published,
                "unavailable": missing,
                "requested": len(symbols),
                "capped": len(symbols) >= self.max_symbols,
                "feed": self.provider.name,
                "delayed": True,
                "interval_seconds": self.interval,
            },
        )
        return published

    async def _sync_stream(self, stream: AlpacaStream, symbols: Sequence[str]) -> None:
        """Point the Alpaca stream at exactly the symbols being watched."""
        wanted = set(symbols)
        added = sorted(wanted - self._streamed)
        removed = sorted(self._streamed - wanted)
        try:
            if added:
                await stream.subscribe(added)
            if removed:
                await stream.unsubscribe(removed)
        except ProviderError as exc:
            self.last_error = str(exc)
            await self._publish_error(str(exc), retry_in=self.interval)
            return
        self._streamed = wanted
        self.last_error = None
        await self.bus.publish(
            TAPE_CHANNEL,
            {
                "event": "tape_stream",
                "symbols": sorted(wanted),
                "added": added,
                "removed": removed,
                "feed": f"alpaca:{stream.feed}",
                "delayed": False,
            },
        )

    async def _publish_error(self, detail: str, *, retry_in: float) -> None:
        """Tell subscribers the tape is degraded, and why."""
        await self.bus.publish(
            TAPE_CHANNEL,
            {
                "event": "tape_error",
                "detail": detail,
                "retry_in_seconds": retry_in,
            },
        )

    def status(self) -> dict[str, Any]:
        """Return an honest snapshot of the publisher's state."""
        stream = self.stream_getter() if self.stream_getter else None
        return {
            "running": self.running,
            "interval_seconds": self.interval,
            "max_symbols": self.max_symbols,
            "subscribers": self.hub.subscriber_count(TAPE_CHANNEL),
            "symbols": self.watched_symbols(),
            "published": self.published,
            "last_error": self.last_error,
            "feed": f"alpaca:{stream.feed}" if stream is not None and stream.configured else None,
        }


class LabRunBridge:
    """Mirrors Strategy Lab progress from the ``lab`` channel onto ``runs``.

    :mod:`tradinglib.research` publishes backtest, experiment, validation and
    promotion progress on ``lab``. ``docs/SPEC.md`` section 5 names ``runs`` as the
    channel carrying "run/stage progress + logs", so every lab frame is
    re-published there with ``source: "lab"`` and the originating channel
    attached. Nothing is invented: the payload is the domain event verbatim,
    only wrapped. A browser may subscribe to either channel (``lab`` for
    research only, ``runs`` for every kind of run) and will not be told
    anything different on the two.
    """

    def __init__(self, bus: EventBus, *, source: str = LAB_CHANNEL, target: str = RUNS_CHANNEL) -> None:
        """Create the bridge over an event bus. Call :meth:`attach` to start it."""
        self.bus = bus
        self.source = source
        self.target = target
        self.forwarded = 0
        self._subscription: Any | None = None

    @property
    def attached(self) -> bool:
        """Whether the bridge is currently subscribed."""
        return self._subscription is not None

    def attach(self) -> None:
        """Subscribe to the lab channel. Idempotent."""
        if self._subscription is None:
            self._subscription = self.bus.subscribe(self.source, self._forward)
            logger.info("lab->%s bridge attached", self.target)

    def detach(self) -> None:
        """Stop forwarding. Idempotent."""
        if self._subscription is not None:
            self._subscription.cancel()
            self._subscription = None
            logger.info("lab->%s bridge detached after %d frame(s)", self.target, self.forwarded)

    async def _forward(self, event: Any) -> None:
        """Re-publish one lab event on the target channel."""
        payload = event.payload
        frame: dict[str, Any]
        if isinstance(payload, dict):
            frame = {"source": "lab", "channel": event.channel, **payload}
        else:  # pragma: no cover - the lab always publishes mappings
            frame = {"source": "lab", "channel": event.channel, "payload": payload}
        self.forwarded += 1
        await self.bus.publish(self.target, frame)


__all__ = [
    "DEFAULT_MAX_TAPE_SYMBOLS",
    "DEFAULT_STATUS_INTERVAL",
    "DEFAULT_TAPE_INTERVAL",
    "LAB_CHANNEL",
    "RUNS_CHANNEL",
    "STATUS_CHANNEL",
    "TAPE_CHANNEL",
    "LabRunBridge",
    "StatusBroadcaster",
    "TapePublisher",
]
