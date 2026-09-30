"""WebSocket hub: ``/ws`` endpoint, channel subscriptions and EventBus fan-out.

Protocol (``docs/SPEC.md`` section 5). Client frames are JSON objects::

    {"action": "subscribe",   "channel": "tape", "params": {"symbols": ["AAPL"]}}
    {"action": "unsubscribe", "channel": "tape"}
    {"action": "ping"}

Server frames::

    {"type": "welcome",     "channels": [...], "ts": ...}
    {"type": "subscribed",  "channel": "tape", "params": {...}}
    {"type": "unsubscribed","channel": "tape"}
    {"type": "pong",        "ts": ...}
    {"type": "error",       "error": "...", "detail": "..."}
    {"type": "event",       "channel": "...", "payload": ..., "ts": ..., "id": ...}
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

from tradinglib.events import Event, EventBus, Subscription, channel_matches

logger = logging.getLogger(__name__)

#: Channels a client may subscribe to (``docs/SPEC.md`` section 5), plus ``lab``.
#:
#: ``lab`` carries Strategy Lab progress - backtest started/complete/failed,
#: experiment run logs, validation verdicts, promotions - which
#: :mod:`tradinglib.research` publishes there. It is the ``runs`` channel's
#: counterpart for research runs (``runs`` carries agent pipeline runs), and
#: SPEC section 6 requires the lab's run log to stream live, so the browser must
#: be able to subscribe to it. Lab events are additionally mirrored onto
#: ``runs`` by :class:`tradinglib.api.live.LabRunBridge`, so a client that only
#: watches ``runs`` still sees research progress.
CHANNELS: frozenset[str] = frozenset(
    {"tape", "status", "ingest", "quality", "runs", "lab", "approvals", "notifications"}
)

#: Maximum events buffered per connection before the oldest are dropped.
QUEUE_MAXSIZE = 512


@dataclass(slots=True)
class Connection:
    """One connected browser and the channels it asked for."""

    id: str
    socket: WebSocket
    channels: dict[str, dict[str, Any]] = field(default_factory=dict)
    queue: asyncio.Queue[dict[str, Any]] = field(default_factory=lambda: asyncio.Queue(QUEUE_MAXSIZE))
    dropped: int = 0

    def wants(self, channel: str) -> bool:
        """Whether this connection subscribed to a pattern covering ``channel``."""
        return any(channel_matches(pattern, channel) for pattern in self.channels)

    def offer(self, frame: dict[str, Any]) -> None:
        """Enqueue a frame, dropping the oldest when the client is too slow."""
        if self.queue.full():
            try:
                self.queue.get_nowait()
                self.dropped += 1
            except asyncio.QueueEmpty:  # pragma: no cover - race only
                pass
        self.queue.put_nowait(frame)


class WsHub:
    """Fans :class:`~tradinglib.events.Event` objects out to subscribed browsers."""

    def __init__(self, bus: EventBus) -> None:
        """Create the hub over an event bus. Call :meth:`attach` to start fan-out."""
        self.bus = bus
        self._connections: dict[str, Connection] = {}
        self._subscription: Subscription | None = None

    # --- lifecycle --------------------------------------------------------
    def attach(self) -> None:
        """Subscribe the hub to every bus channel. Idempotent."""
        if self._subscription is None:
            self._subscription = self.bus.subscribe("*", self._on_event)
            logger.debug("ws hub attached to event bus")

    def detach(self) -> None:
        """Unsubscribe from the bus. Existing sockets keep working until closed."""
        if self._subscription is not None:
            self._subscription.cancel()
            self._subscription = None

    @property
    def connection_count(self) -> int:
        """Number of live WebSocket connections."""
        return len(self._connections)

    def stats(self) -> dict[str, Any]:
        """Return hub statistics for health reporting."""
        return {
            "connections": len(self._connections),
            "attached": self._subscription is not None,
            "channels": sorted({c for conn in self._connections.values() for c in conn.channels}),
        }

    # --- subscription introspection ---------------------------------------
    def subscriptions(self, root: str) -> list[tuple[str, dict[str, Any]]]:
        """Return ``(channel, params)`` for every subscription under ``root``.

        A subscription counts when it is the root itself (``tape``), a child of
        it (``tape.AAPL``) or a pattern that matches it (``tape.*``). Used by
        the live publishers to work only while a browser is actually watching.
        """
        out: list[tuple[str, dict[str, Any]]] = []
        for conn in self._connections.values():
            for pattern, params in conn.channels.items():
                if (
                    pattern == root
                    or pattern.startswith(root + ".")
                    or channel_matches(pattern, root)
                ):
                    out.append((pattern, params))
        return out

    def subscriber_count(self, root: str) -> int:
        """Number of live subscriptions covering ``root``."""
        return len(self.subscriptions(root))

    # --- fan-out ----------------------------------------------------------
    def _on_event(self, event: Event) -> None:
        """Bus callback: offer the event to every interested connection."""
        if not self._connections:
            return
        frame = event.as_dict()
        for conn in list(self._connections.values()):
            if conn.wants(event.channel):
                conn.offer(frame)

    async def broadcast(self, channel: str, payload: Any) -> None:
        """Publish on the bus, which reaches this hub and any other subscriber."""
        await self.bus.publish(channel, payload)

    # --- connection handling ---------------------------------------------
    async def handle(self, socket: WebSocket) -> None:
        """Accept ``socket`` and serve it until the client disconnects."""
        await socket.accept()
        conn = Connection(id=uuid.uuid4().hex, socket=socket)
        self._connections[conn.id] = conn
        self.attach()
        logger.info("ws connect %s (%d live)", conn.id, len(self._connections))

        writer = asyncio.create_task(self._writer(conn), name=f"ws-writer-{conn.id}")
        try:
            await self._send(conn, {"type": "welcome", "channels": sorted(CHANNELS), "id": conn.id})
            await self._reader(conn)
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001 - a broken socket must not take the server down
            logger.exception("ws connection %s failed", conn.id)
        finally:
            self._connections.pop(conn.id, None)
            writer.cancel()
            try:
                await writer
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            if socket.client_state is WebSocketState.CONNECTED:
                try:
                    await socket.close()
                except Exception:  # noqa: BLE001 - already gone
                    pass
            logger.info("ws disconnect %s (%d live)", conn.id, len(self._connections))

    async def _reader(self, conn: Connection) -> None:
        """Read and dispatch client frames until the socket closes."""
        while True:
            raw = await conn.socket.receive_text()
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                await self._send(conn, {"type": "error", "error": "invalid_json"})
                continue
            if not isinstance(message, dict):
                await self._send(conn, {"type": "error", "error": "invalid_message"})
                continue
            await self._dispatch(conn, message)

    async def _dispatch(self, conn: Connection, message: dict[str, Any]) -> None:
        """Handle one decoded client frame."""
        action = str(message.get("action") or "").lower()
        channel = message.get("channel")
        params = message.get("params") or {}
        if not isinstance(params, dict):
            params = {}

        if action == "ping":
            await self._send(conn, {"type": "pong"})
            return

        if action in {"subscribe", "unsubscribe"}:
            if not isinstance(channel, str) or not channel:
                await self._send(conn, {"type": "error", "error": "missing_channel"})
                return
            root = channel.split(".", 1)[0]
            if root not in CHANNELS:
                await self._send(
                    conn,
                    {
                        "type": "error",
                        "error": "unknown_channel",
                        "detail": channel,
                        "channels": sorted(CHANNELS),
                    },
                )
                return
            if action == "subscribe":
                conn.channels[channel] = params
                await self._send(conn, {"type": "subscribed", "channel": channel, "params": params})
            else:
                conn.channels.pop(channel, None)
                await self._send(conn, {"type": "unsubscribed", "channel": channel})
            return

        await self._send(conn, {"type": "error", "error": "unknown_action", "detail": action})

    async def _writer(self, conn: Connection) -> None:
        """Drain the connection queue onto the socket."""
        while True:
            frame = await conn.queue.get()
            await self._send(conn, frame)

    async def _send(self, conn: Connection, frame: dict[str, Any]) -> None:
        """Send one JSON frame, tolerating a socket that has already closed."""
        if conn.socket.client_state is not WebSocketState.CONNECTED:
            return
        try:
            await conn.socket.send_text(json.dumps(frame, default=str))
        except (WebSocketDisconnect, RuntimeError):
            logger.debug("ws send to %s failed; connection gone", conn.id)
