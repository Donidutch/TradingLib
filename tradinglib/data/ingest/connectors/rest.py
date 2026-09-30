"""REST connector: request/response HTTP endpoints with optional pagination."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from tradinglib.data.ingest.auth import AuthError, AuthStrategy, build_auth
from tradinglib.data.ingest.connectors.base import (
    ConnectionTestResult,
    Connector,
    FetchResult,
)
from tradinglib.data.ingest.formats import MISSING, dig
from tradinglib.data.ingest.source import SourceConfig, SourceStatus

logger = logging.getLogger(__name__)

#: Pagination styles the connector knows how to walk.
PAGINATION_STYLES: frozenset[str] = frozenset({"none", "page", "offset", "cursor", "link"})

#: Hard ceiling on pages per fetch, so a broken "next" link cannot loop forever.
MAX_PAGES: int = 50


class RestConnector(Connector):
    """Calls an HTTP endpoint, applies auth, parses and maps the payload.

    Advanced configuration keys this connector reads::

        method            "GET" (default) / "POST" / ...
        path              appended to the endpoint, e.g. "/v1/bars"
        body              JSON body for non-GET methods
        records_path      dotted path to the record list inside a JSON payload
        delimiter/header  CSV options
        pagination        {"style": "page"|"offset"|"cursor"|"link",
                           "param": "page", "start": 1, "size_param": "limit",
                           "size": 100, "max_pages": 10,
                           "cursor_path": "meta.next", "cursor_param": "cursor"}
        window_params     {"start": "from", "end": "to", "format": "date"|"iso"|"epoch"}
                          - used by backfill() to walk history window by window
    """

    protocol = "rest"

    def __init__(
        self,
        config: SourceConfig,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """Bind the connector; ``transport`` is injected by tests."""
        super().__init__(config)
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._auth: AuthStrategy | None = None
        self._auth_error: str | None = None
        self.last_status_code: int | None = None

    # --- lifecycle --------------------------------------------------------
    async def connect(self) -> None:
        """Create the HTTP client and resolve the auth strategy."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.config.retry_policy.timeout_seconds),
                follow_redirects=True,
                transport=self._transport,
                headers={"User-Agent": self._user_agent()},
            )
        if self._auth is None and self._auth_error is None:
            try:
                self._auth = build_auth(self.config.auth, transport=self._transport)
            except AuthError as exc:
                self._auth_error = str(exc)

    def _user_agent(self) -> str:
        """Return the User-Agent header this source sends."""
        configured = self.config.advanced.headers.get("User-Agent")
        return configured or f"TradingLib ingestion ({self.config.id})"

    async def close(self) -> None:
        """Close the HTTP client."""
        client, self._client = self._client, None
        if client is not None and not client.is_closed:
            await client.aclose()
        auth, self._auth = self._auth, None
        if auth is not None:
            await auth.close()

    # --- request plumbing -------------------------------------------------
    def _url(self) -> str:
        """Return the full request URL (endpoint plus optional advanced path)."""
        base = self.config.endpoint.rstrip("/")
        path = str(self.config.advanced.get("path") or "").strip()
        if not path:
            return self.config.endpoint
        return f"{base}/{path.lstrip('/')}"

    async def _prepare(self, params: dict[str, Any]) -> dict[str, Any]:
        """Build the request dict with headers, params and credentials applied.

        Raises:
            AuthError: when the source needs credentials it does not have.
        """
        await self.connect()
        if self._auth_error is not None:
            raise AuthError(self._auth_error)
        request: dict[str, Any] = {
            "url": self._url(),
            "headers": dict(self.config.advanced.headers),
            "params": {**self.config.advanced.query_params, **params},
        }
        assert self._auth is not None  # noqa: S101 - established by connect()
        return await self._auth.apply(request)

    async def _send(self, request: dict[str, Any]) -> tuple[httpx.Response, float]:
        """Send one request with the configured retry policy.

        Returns:
            The response and the measured round-trip latency in milliseconds.

        Raises:
            httpx.HTTPError: when every attempt failed at the transport level.
        """
        await self.connect()
        assert self._client is not None  # noqa: S101 - established by connect()
        policy = self.config.retry_policy
        method = str(self.config.advanced.get("method") or "GET").upper()
        body = self.config.advanced.get("body")
        last_exc: Exception | None = None
        for attempt in range(1, max(1, policy.max_attempts) + 1):
            started = time.perf_counter()
            try:
                response = await self._client.request(
                    method,
                    request["url"],
                    params=request.get("params") or None,
                    headers=request.get("headers") or None,
                    json=body if body is not None and method != "GET" else None,
                )
            except httpx.HTTPError as exc:
                last_exc = exc
                delay = policy.delay_for(attempt)
                if attempt >= policy.max_attempts:
                    break
                logger.info(
                    "ingest %s: attempt %d failed (%s); retrying in %.1fs",
                    self.config.id,
                    attempt,
                    exc,
                    delay,
                )
                if delay:
                    await asyncio.sleep(delay)
                continue
            latency_ms = (time.perf_counter() - started) * 1000.0
            self.last_status_code = response.status_code
            if response.status_code >= 500 and attempt < policy.max_attempts:
                delay = policy.delay_for(attempt)
                logger.info(
                    "ingest %s: HTTP %d; retrying in %.1fs",
                    self.config.id,
                    response.status_code,
                    delay,
                )
                if delay:
                    await asyncio.sleep(delay)
                continue
            return response, latency_ms
        assert last_exc is not None  # noqa: S101 - loop only breaks with an error
        raise last_exc

    def _records(self, response: httpx.Response) -> list[dict[str, Any]]:
        """Decode a response body into mapped records."""
        return self.decode(response.content)

    # --- pagination -------------------------------------------------------
    def _pagination(self) -> dict[str, Any]:
        """Return the normalised pagination configuration."""
        raw = dict(self.config.advanced.pagination or {})
        style = str(raw.get("style") or raw.get("type") or "none").lower()
        if style not in PAGINATION_STYLES:
            raise ValueError(
                f"unknown pagination style {style!r}; expected one of {sorted(PAGINATION_STYLES)}"
            )
        raw["style"] = style
        return raw

    def _next_page_params(
        self,
        page_config: dict[str, Any],
        response: httpx.Response,
        records: list[dict[str, Any]],
        page_index: int,
        current: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Return the query parameters for the next page, or ``None`` when done."""
        style = page_config["style"]
        size = int(page_config.get("size", 0) or 0)
        if not records or (size and len(records) < size):
            return None
        if style == "page":
            param = str(page_config.get("param", "page"))
            start = int(page_config.get("start", 1))
            return {**current, param: start + page_index + 1}
        if style == "offset":
            param = str(page_config.get("param", "offset"))
            step = size or len(records)
            return {**current, param: int(current.get(param, 0)) + step}
        if style == "cursor":
            path = str(page_config.get("cursor_path", "next"))
            try:
                payload = response.json()
            except ValueError:
                return None
            cursor = dig(payload, path)
            if cursor is MISSING or not cursor:
                return None
            return {**current, str(page_config.get("cursor_param", "cursor")): cursor}
        if style == "link":
            link = response.links.get("next", {}).get("url")
            return {"__url__": link} if link else None
        return None

    # --- fetch ------------------------------------------------------------
    async def fetch(self, **params: Any) -> FetchResult:
        """Fetch one batch of records, following configured pagination.

        Keyword arguments are merged into the query string, so callers (the
        scheduler, backfill windows) can narrow the request.

        Raises:
            AuthError: when required credentials are missing.
            httpx.HTTPError: when the endpoint is unreachable.
            RuntimeError: when the endpoint answers with an HTTP error status.
            ValueError: when the payload does not parse as the declared format.
        """
        page_config = self._pagination()
        query: dict[str, Any] = {k: v for k, v in params.items() if v is not None}
        if page_config["style"] in ("page", "offset") and page_config.get("size"):
            query.setdefault(
                str(page_config.get("size_param", "limit")), int(page_config["size"])
            )
        if page_config["style"] == "page":
            query.setdefault(str(page_config.get("param", "page")), int(page_config.get("start", 1)))

        max_pages = min(int(page_config.get("max_pages", MAX_PAGES) or MAX_PAGES), MAX_PAGES)
        all_records: list[dict[str, Any]] = []
        total_latency = 0.0
        cursor: str | None = None
        override_url: str | None = None
        complete = True

        for page_index in range(max_pages):
            request = await self._prepare(query)
            if override_url:
                request["url"] = override_url
                request["params"] = {}
            try:
                response, latency_ms = await self._send(request)
            except httpx.HTTPError as exc:
                self._mark_failure(f"{type(exc).__name__}: {exc}")
                raise
            total_latency += latency_ms
            if response.status_code >= 400:
                message = f"HTTP {response.status_code} from {request['url']}"
                self._mark_failure(message)
                raise RuntimeError(f"{message}: {response.text.strip()[:200]}")
            try:
                records = self._records(response)
            except ValueError as exc:
                self._mark_failure(str(exc))
                raise
            all_records.extend(records)

            if page_config["style"] == "none":
                break
            nxt = self._next_page_params(page_config, response, records, page_index, query)
            if nxt is None:
                break
            if "__url__" in nxt:
                override_url = str(nxt["__url__"])
                cursor = override_url
            else:
                query = nxt
                cursor = str(next(iter(nxt.values()), ""))
            if page_index == max_pages - 1:
                complete = False

        self._mark_success(total_latency)
        return FetchResult(
            records=all_records,
            fetched_at=datetime.now(timezone.utc),
            latency_ms=total_latency,
            complete=complete,
            cursor=cursor,
        )

    # --- test -------------------------------------------------------------
    async def test(self) -> ConnectionTestResult:
        """Issue one real request and report status, latency and a sample."""
        try:
            request = await self._prepare(dict(self.config.advanced.get("test_params") or {}))
        except AuthError as exc:
            return ConnectionTestResult.failure(
                f"not configured: {exc}", error=str(exc), authenticated=False
            )
        try:
            response, latency_ms = await self._send(request)
        except httpx.HTTPError as exc:
            message = f"{request['url']} is unreachable: {type(exc).__name__}: {exc}"
            self._mark_failure(message)
            return ConnectionTestResult.failure(message, reachable=False)

        if response.status_code in (401, 403):
            message = f"authentication rejected (HTTP {response.status_code})"
            self._mark_failure(message)
            return ConnectionTestResult.failure(
                message,
                reachable=True,
                authenticated=False,
                latency_ms=latency_ms,
                status_code=response.status_code,
            )
        if response.status_code >= 400:
            message = f"HTTP {response.status_code}: {response.text.strip()[:200]}"
            self._mark_failure(message)
            return ConnectionTestResult.failure(
                message,
                reachable=True,
                authenticated=True,
                latency_ms=latency_ms,
                status_code=response.status_code,
            )
        try:
            records = self._records(response)
        except ValueError as exc:
            self._mark_failure(str(exc))
            return ConnectionTestResult.failure(
                f"response did not parse as {self.config.format.label}: {exc}",
                reachable=True,
                authenticated=True,
                latency_ms=latency_ms,
                status_code=response.status_code,
            )
        self._mark_success(latency_ms)
        return ConnectionTestResult.success(
            f"HTTP {response.status_code} · {len(records)} records · "
            f"{latency_ms:.0f} ms",
            latency_ms=latency_ms,
            authenticated=self.config.auth.type.value != "none",
            records=records,
            status_code=response.status_code,
        )

    # --- backfill ---------------------------------------------------------
    async def backfill(
        self, *, range: str = "1y", batch_size: int = 500, end: datetime | None = None
    ) -> AsyncIterator[FetchResult]:
        """Walk the endpoint's history window by window.

        The window parameters are named in the advanced configuration
        (``window_params``). Without them the endpoint has no history dimension
        this connector can drive, so exactly one batch is yielded - the current
        response - rather than pretending to have walked history.
        """
        from tradinglib.data.ingest.source import BackfillConfig

        config = BackfillConfig(
            enabled=True,
            range=range,
            batch=self.config.backfill.batch,
            batch_size=batch_size,
            start_date=self.config.backfill.start_date,
        )
        window_params = dict(self.config.advanced.get("window_params") or {})
        start_param = window_params.get("start")
        end_param = window_params.get("end")
        # `end` is the FROM/TO control's upper bound; without one the window
        # runs to the present, which is what the reference's TO="now" means.
        now = end or datetime.now(timezone.utc)
        start = config.range_start(now=now)

        if not start_param or start is None:
            yield await self.fetch()
            return

        step = config.window or timedelta(days=365)
        fmt = str(window_params.get("format", "date"))
        cursor = start
        while cursor < now:
            upper = min(cursor + step, now)
            params: dict[str, Any] = {start_param: _format_moment(cursor, fmt)}
            if end_param:
                params[end_param] = _format_moment(upper, fmt)
            yield await self.fetch(**params)
            cursor = upper

    # --- status -----------------------------------------------------------
    async def status(self) -> SourceStatus:
        """Report status from the most recent request outcome."""
        if self._auth_error is not None or not self.config.auth.configured:
            return SourceStatus.NOT_CONFIGURED
        return self.observed_status()


def _format_moment(moment: datetime, fmt: str) -> Any:
    """Render a window boundary in the format the endpoint expects."""
    if fmt == "epoch":
        return int(moment.timestamp())
    if fmt == "iso":
        return moment.isoformat()
    return moment.date().isoformat()


__all__ = ["MAX_PAGES", "PAGINATION_STYLES", "RestConnector"]
