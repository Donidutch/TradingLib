"""WebSocket connector: persistent subscription-based feeds."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import InvalidStatus, InvalidURI, WebSocketException

from tradinglib.data.ingest.auth import AuthError, AuthStrategy, build_auth
from tradinglib.data.ingest.connectors.base import (
    ConnectionTestResult,
    Connector,
    FetchResult,
)
from tradinglib.data.ingest.source import AuthType, SourceConfig, SourceStatus

logger = logging.getLogger(__name__)

#: Default number of frames buffered between ``fetch()`` calls.
DEFAULT_BUFFER: int = 2_000

#: Default seconds ``test()`` waits for the first data frame.
DEFAULT_TEST_WAIT: float = 5.0

#: Default seconds to wait for an authentication acknowledgement.
DEFAULT_AUTH_TIMEOUT: float = 5.0


def _looks_like_error(record: dict[str, Any]) -> bool:
    """Whether a decoded frame is the feed telling us something went wrong."""
    if record.get("T") == "error" or record.get("type") == "error":
        return True
    if str(record.get("status", "")).lower() in ("error", "failed", "failure"):
        return True
    return bool(record.get("error"))


def _looks_like_auth_ack(record: dict[str, Any]) -> bool:
    """Whether a decoded frame acknowledges a successful authentication."""
    blob = " ".join(str(v) for v in record.values()).lower()
    return "auth" in blob and not _looks_like_error(record)


def _render(template: Any, values: dict[str, Any]) -> Any:
    """Substitute ``{placeholder}`` tokens in a JSON template.

    A string that is exactly ``"{name}"`` is replaced by the *value* (so a list
    stays a list); a string containing the token is interpolated as text.
    """
    if isinstance(template, dict):
        return {key: _render(value, values) for key, value in template.items()}
    if isinstance(template, list):
        return [_render(item, values) for item in template]
    if isinstance(template, str):
        stripped = template.strip()
        for name, value in values.items():
            token = "{" + name + "}"
            if stripped == token:
                return value
            if token in template:
                template = template.replace(token, str(value))
        return template
    return template


class WebSocketConnector(Connector):
    """Maintains a WebSocket session with reconnect/backoff and subscriptions.

    Advanced configuration keys::

        auth_message      JSON template sent after connecting, e.g.
                          {"action": "auth", "key": "{key}", "secret": "{secret}"}
        subscribe_message JSON template sent next, e.g.
                          {"action": "subscribe", "trades": "{channels}"}
        subscriptions     the channels substituted for "{channels}"
        buffer            frames retained between fetch() calls
        test_wait         seconds test() waits for the first data frame
        auth_timeout      seconds to wait for the authentication acknowledgement

    Placeholders available to the templates: ``{key}``, ``{secret}``,
    ``{username}``, ``{password}``, ``{channels}``.
    """

    protocol = "websocket"

    def __init__(self, config: SourceConfig) -> None:
        """Bind the connector to its source configuration."""
        super().__init__(config)
        self._ws: ClientConnection | None = None
        self._reader: asyncio.Task[None] | None = None
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self.buffer_size)
        self._connected_at: datetime | None = None
        self.frames_received = 0
        self.authenticated: bool | None = None

    # --- configuration ----------------------------------------------------
    @property
    def buffer_size(self) -> int:
        """How many frames are buffered before the oldest is dropped."""
        return int(self.config.advanced.get("buffer", DEFAULT_BUFFER))

    @property
    def channels(self) -> list[str]:
        """The subscription channels configured for this source."""
        return list(self.config.advanced.subscriptions or self.config.scope)

    @property
    def credentials_required(self) -> bool:
        """Whether the feed needs credentials this source may not have."""
        return self.config.auth.type is not AuthType.NONE

    @property
    def auth_ready(self) -> bool:
        """Whether every credential the handshake needs is actually configured.

        A feed whose auth frame carries both a key and a secret (Alpaca's, for
        instance) is not ready with only one of the two, even though the key
        alone satisfies the generic API-key check.
        """
        if not self.credentials_required:
            return True
        if not self.config.auth.configured:
            return False
        template = self.config.advanced.get("auth_message")
        if template is None:
            return True
        blob = json.dumps(template)
        values = self._placeholders()
        return all(
            values.get(name)
            for name in ("key", "secret", "username", "password")
            if "{" + name + "}" in blob
        )

    def _placeholders(self) -> dict[str, Any]:
        """The substitution values available to the message templates."""
        auth = self.config.auth
        return {
            "key": auth.key_value or "",
            "secret": auth.client_secret or auth.password or "",
            "username": auth.username or "",
            "password": auth.password or "",
            "channels": self.channels,
        }

    async def _handshake_headers(self) -> dict[str, str]:
        """Return the HTTP headers sent with the WebSocket handshake.

        Raises:
            AuthError: when the source needs credentials it does not have.
        """
        request: dict[str, Any] = {
            "url": self.config.endpoint,
            "headers": dict(self.config.advanced.headers),
            "params": {},
        }
        strategy: AuthStrategy = build_auth(self.config.auth)
        # Only header-shaped credentials belong in the handshake; key/secret
        # frames are sent afterwards by the auth_message template.
        if self.config.advanced.get("auth_message") is None:
            request = await strategy.apply(request)
        headers = request["headers"]
        return {str(k): str(v) for k, v in headers.items()}

    # --- lifecycle --------------------------------------------------------
    async def connect(self) -> None:
        """Open the socket, authenticate and send configured subscriptions.

        Raises:
            AuthError: when credentials are required but not configured.
            WebSocketException / OSError: when the socket cannot be opened.
        """
        if self._ws is not None:
            return
        headers = await self._handshake_headers()
        timeout = self.config.retry_policy.timeout_seconds
        self._ws = await ws_connect(
            self.config.endpoint,
            additional_headers=headers or None,
            open_timeout=timeout,
            user_agent_header=f"TradingLib ingestion ({self.config.id})",
        )
        self._connected_at = datetime.now(UTC)

        auth_message = self.config.advanced.get("auth_message")
        if auth_message is not None:
            await self._ws.send(json.dumps(_render(auth_message, self._placeholders())))
            self.authenticated = await self._await_auth_ack()
            if self.authenticated is False:
                raise AuthError(self.last_error or "the feed rejected the credentials")
        subscribe = self.config.advanced.get("subscribe_message")
        if subscribe is None and self.channels:
            subscribe = {"action": "subscribe", "channels": "{channels}"}
        if subscribe is not None and self.channels:
            await self._ws.send(json.dumps(_render(subscribe, self._placeholders())))

        self._reader = asyncio.create_task(self._read_loop(), name=f"ws-{self.config.id}")
        self._mark_success()

    async def _await_auth_ack(self) -> bool | None:
        """Read frames until the feed acknowledges (or rejects) the credentials.

        Returns ``True`` on an acknowledgement, ``False`` on an error frame and
        ``None`` when the feed said nothing within the timeout - some feeds
        stay silent, and silence is not proof of success.
        """
        assert self._ws is not None
        budget = float(self.config.advanced.get("auth_timeout", DEFAULT_AUTH_TIMEOUT))
        deadline = time.monotonic() + budget
        while time.monotonic() < deadline:
            remaining = max(deadline - time.monotonic(), 0.01)
            try:
                frame = await asyncio.wait_for(self._ws.recv(), timeout=remaining)
            except TimeoutError:
                return None
            except WebSocketException as exc:
                self._mark_failure(f"socket closed during authentication: {exc}")
                return False
            for record in self._decode_frame(frame):
                if _looks_like_error(record):
                    self._mark_failure(json.dumps(record)[:200])
                    return False
                if _looks_like_auth_ack(record):
                    return True
                self._offer(record)
        return None

    def _decode_frame(self, frame: str | bytes) -> list[dict[str, Any]]:
        """Decode one frame into zero or more records."""
        try:
            records = self.decode(frame)
        except ValueError:
            text = frame.decode("utf-8", "replace") if isinstance(frame, bytes) else frame
            return [{"message": text}]
        return records

    def _offer(self, record: dict[str, Any]) -> None:
        """Put a record on the buffer, dropping the oldest when it is full."""
        if self._queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
            logger.debug("ingest %s: frame buffer full; dropped the oldest frame", self.config.id)
        self._queue.put_nowait(record)
        self.frames_received += 1

    async def _read_loop(self) -> None:
        """Consume frames from the socket into the buffer until it closes."""
        ws = self._ws
        if ws is None:  # pragma: no cover - guarded by connect()
            return
        try:
            async for frame in ws:
                for record in self._decode_frame(frame):
                    self._offer(record)
        except asyncio.CancelledError:
            raise
        except WebSocketException as exc:
            self._mark_failure(f"stream closed: {exc}")
            logger.info("ingest %s: websocket closed (%s)", self.config.id, exc)

    async def close(self) -> None:
        """Close the socket and stop the reader task."""
        reader, self._reader = self._reader, None
        ws, self._ws = self._ws, None
        if reader is not None and not reader.done():
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reader
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()
        self._connected_at = None

    # --- data -------------------------------------------------------------
    async def fetch(self, **params: Any) -> FetchResult:
        """Drain records buffered since the previous call.

        With ``wait=<seconds>`` the call blocks until at least one record
        arrives or the wait elapses; without it, it returns whatever is
        buffered right now (possibly nothing, which is the honest answer for a
        quiet feed).
        """
        await self.connect()
        wait = float(params.get("wait", 0.0) or 0.0)
        if wait and self._queue.empty():
            with contextlib.suppress(TimeoutError):
                record = await asyncio.wait_for(self._queue.get(), timeout=wait)
                self._queue.put_nowait(record)

        records: list[dict[str, Any]] = []
        limit = int(params.get("limit", 0) or 0)
        while not self._queue.empty():
            records.append(self._queue.get_nowait())
            if limit and len(records) >= limit:
                break
        return FetchResult(
            records=records,
            fetched_at=datetime.now(UTC),
            latency_ms=None,
            complete=self._queue.empty(),
        )

    async def stream(self) -> AsyncIterator[dict[str, Any]]:
        """Yield decoded frames as they arrive."""
        await self.connect()
        while True:
            yield await self._queue.get()

    # --- test / status ----------------------------------------------------
    async def test(self) -> ConnectionTestResult:
        """Connect, authenticate, wait briefly for one frame, then disconnect."""
        if not self.auth_ready:
            message = (
                f"not configured: {self.config.auth.type.label} credentials are missing "
                f"for {self.config.name}"
            )
            return ConnectionTestResult.failure(message, authenticated=False, reachable=None)

        started = time.perf_counter()
        try:
            await self.connect()
        except AuthError as exc:
            latency_ms = (time.perf_counter() - started) * 1000.0
            await self.close()
            return ConnectionTestResult.failure(
                f"authentication rejected: {exc}",
                reachable=True,
                authenticated=False,
                latency_ms=latency_ms,
            )
        except InvalidStatus as exc:
            await self.close()
            code = exc.response.status_code
            return ConnectionTestResult.failure(
                f"handshake rejected with HTTP {code}",
                reachable=True,
                authenticated=False if code in (401, 403) else None,
                status_code=code,
            )
        except (InvalidURI, ValueError) as exc:
            return ConnectionTestResult.failure(f"invalid endpoint: {exc}", reachable=None)
        except (WebSocketException, OSError, TimeoutError) as exc:
            await self.close()
            return ConnectionTestResult.failure(
                f"{self.config.endpoint} is unreachable: {type(exc).__name__}: {exc}",
                reachable=False,
            )

        latency_ms = (time.perf_counter() - started) * 1000.0
        wait = float(self.config.advanced.get("test_wait", DEFAULT_TEST_WAIT))
        batch = await self.fetch(wait=wait)
        await self.close()
        self._mark_success(latency_ms)
        detail = (
            f"connected in {latency_ms:.0f} ms · {len(batch.records)} frame(s) in {wait:.0f}s"
            if batch.records
            else f"connected in {latency_ms:.0f} ms · no frames within {wait:.0f}s "
            f"(the feed may be quiet outside market hours)"
        )
        return ConnectionTestResult.success(
            detail,
            latency_ms=latency_ms,
            authenticated=self.authenticated,
            records=batch.records,
        )

    async def status(self) -> SourceStatus:
        """Report whether the socket is currently connected."""
        if not self.auth_ready:
            return SourceStatus.NOT_CONFIGURED
        if self._ws is not None and self._reader is not None and not self._reader.done():
            return SourceStatus.CONNECTED
        if self.last_error is not None:
            return SourceStatus.DISCONNECTED
        return SourceStatus.UNKNOWN

    def state(self) -> dict[str, Any]:
        """Return the live session state, for the UI and debugging."""
        return {
            "connected": self._ws is not None,
            "connected_at": self._connected_at.isoformat() if self._connected_at else None,
            "authenticated": self.authenticated,
            "channels": self.channels,
            "frames_received": self.frames_received,
            "buffered": self._queue.qsize(),
            "library": f"websockets {websockets.__version__}",
        }


__all__ = [
    "DEFAULT_AUTH_TIMEOUT",
    "DEFAULT_BUFFER",
    "DEFAULT_TEST_WAIT",
    "WebSocketConnector",
]
