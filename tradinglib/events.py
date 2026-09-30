"""In-process asynchronous publish/subscribe bus.

Domain modules never import from :mod:`tradinglib.api`; they publish events here
and ``tradinglib.api.ws`` fans them out to browsers. That inversion is an
architecture rule of ``docs/SPEC.md`` section 4.

Channels are dot-separated names (``runs``, ``runs.42``, ``ingest.yfinance``).
Subscribers may use a literal channel, a dotted prefix (a subscription to
``runs`` also receives ``runs.42``) or a glob pattern (``runs.*``, ``*``).
"""

from __future__ import annotations

import asyncio
import fnmatch
import inspect
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: A subscriber callback. May be a plain function or a coroutine function.
EventCallback = Callable[["Event"], Awaitable[None] | None]


@dataclass(frozen=True, slots=True)
class Event:
    """A single published event.

    Attributes:
        channel: the concrete channel the event was published on.
        payload: JSON-serialisable event body.
        ts: UNIX epoch seconds (UTC) at publication time.
        id: unique event identifier, useful for de-duplication and logs.
    """

    channel: str
    payload: Any
    ts: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def as_dict(self) -> dict[str, Any]:
        """Return the wire representation used by the ``/ws`` endpoint."""
        return {
            "type": "event",
            "channel": self.channel,
            "payload": self.payload,
            "ts": self.ts,
            "id": self.id,
        }


def channel_matches(pattern: str, channel: str) -> bool:
    """Return whether ``channel`` is covered by subscription ``pattern``.

    Matching rules, in order: exact equality, dotted-prefix containment
    (``runs`` covers ``runs.42``), then glob (:mod:`fnmatch`) semantics.
    """
    if pattern == channel:
        return True
    if channel.startswith(pattern + "."):
        return True
    if any(ch in pattern for ch in "*?["):
        return fnmatch.fnmatchcase(channel, pattern)
    return False


@dataclass(slots=True)
class Subscription:
    """Handle returned by :meth:`EventBus.subscribe`; cancel it to unsubscribe."""

    channel: str
    callback: EventCallback
    bus: EventBus
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    active: bool = True

    def cancel(self) -> None:
        """Detach this subscription from its bus. Safe to call twice."""
        self.bus.unsubscribe(self)

    def __enter__(self) -> Subscription:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.cancel()


class EventBus:
    """Asynchronous, in-process fan-out bus.

    Callbacks are invoked in subscription order. A failing callback is logged
    and skipped: one bad subscriber must never break a publish for the others.
    """

    def __init__(self) -> None:
        self._subs: list[Subscription] = []
        self._lock = asyncio.Lock()

    # --- subscription management ----------------------------------------
    def subscribe(self, channel: str, cb: EventCallback) -> Subscription:
        """Subscribe ``cb`` to ``channel`` (literal, dotted prefix or glob)."""
        sub = Subscription(channel=channel, callback=cb, bus=self)
        self._subs.append(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        """Remove a subscription. Idempotent."""
        sub.active = False
        try:
            self._subs.remove(sub)
        except ValueError:
            pass

    @property
    def subscription_count(self) -> int:
        """Number of live subscriptions (used by health reporting and tests)."""
        return len(self._subs)

    def channels(self) -> list[str]:
        """Distinct channel patterns currently subscribed to."""
        return sorted({sub.channel for sub in self._subs})

    def clear(self) -> None:
        """Drop every subscription. Intended for tests and shutdown."""
        for sub in list(self._subs):
            sub.active = False
        self._subs.clear()

    # --- publishing -------------------------------------------------------
    async def publish(self, channel: str, payload: Any) -> Event:
        """Publish ``payload`` on ``channel`` and await every matching callback.

        Returns:
            The :class:`Event` that was delivered.
        """
        event = Event(channel=channel, payload=payload)
        await self.publish_event(event)
        return event

    async def publish_event(self, event: Event) -> None:
        """Deliver an already-constructed :class:`Event` to matching subscribers."""
        for sub in list(self._subs):
            if not sub.active or not channel_matches(sub.channel, event.channel):
                continue
            try:
                result = sub.callback(event)
                if inspect.isawaitable(result):
                    await result
            except Exception:  # noqa: BLE001 - one subscriber must not break others
                logger.exception(
                    "event subscriber failed (channel=%s pattern=%s)",
                    event.channel,
                    sub.channel,
                )

    def publish_nowait(self, channel: str, payload: Any) -> Event:
        """Schedule a publish on the running loop without awaiting delivery.

        Useful from synchronous code that runs inside an event loop's thread
        executor. Raises :class:`RuntimeError` if no loop is running.
        """
        event = Event(channel=channel, payload=payload)
        loop = asyncio.get_running_loop()
        loop.create_task(self.publish_event(event))
        return event

    def publish_threadsafe(self, channel: str, payload: Any, loop: asyncio.AbstractEventLoop) -> Event:
        """Publish from another thread onto ``loop``. Used by blocking SDK callbacks."""
        event = Event(channel=channel, payload=payload)
        asyncio.run_coroutine_threadsafe(self.publish_event(event), loop)
        return event

    # --- queue-based consumption ------------------------------------------
    def queue(self, channel: str, maxsize: int = 512) -> tuple[asyncio.Queue[Event], Subscription]:
        """Subscribe a bounded queue to ``channel``.

        Returns the queue and its subscription handle. When the queue is full
        the oldest event is dropped, so a slow consumer degrades rather than
        blocking publishers.
        """
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)

        def _enqueue(event: Event) -> None:
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover - race only
                    pass
                logger.warning("event queue full for %s; dropped oldest event", channel)
            q.put_nowait(event)

        return q, self.subscribe(channel, _enqueue)

    async def stream(self, channel: str, maxsize: int = 512) -> AsyncIterator[Event]:
        """Async-iterate events on ``channel`` until the consumer stops."""
        q, sub = self.queue(channel, maxsize=maxsize)
        try:
            while True:
                yield await q.get()
        finally:
            sub.cancel()


_bus: EventBus | None = None


def get_event_bus() -> EventBus:
    """Return the process-wide :class:`EventBus`, creating it on first use."""
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus


def reset_event_bus() -> EventBus:
    """Replace the process-wide bus with a fresh one. Intended for tests."""
    global _bus
    _bus = EventBus()
    return _bus
