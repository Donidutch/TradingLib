"""The connector interface every protocol adapter implements."""

from __future__ import annotations

import abc
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from tradinglib.data.ingest.formats import (
    apply_schema_mapping,
    infer_schema,
    json_safe_records,
    parse_payload,
)
from tradinglib.data.ingest.source import Protocol, SourceConfig, SourceStatus

logger = logging.getLogger(__name__)

#: How many records a ``test()`` keeps as the response sample.
SAMPLE_SIZE: int = 3


def _utcnow() -> datetime:
    """Return the current time as an aware UTC datetime."""
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class ConnectionTestResult:
    """Result of a real "Test connection" attempt - never a canned success.

    ``reachable`` and ``authenticated`` are three-valued: ``None`` means the
    attempt never got far enough to find out (for example, credentials were
    missing so no request was sent), which is different from ``False``.
    """

    ok: bool
    reachable: bool | None = None
    authenticated: bool | None = None
    latency_ms: float | None = None
    message: str = ""
    error: str | None = None
    sample: list[dict[str, Any]] = field(default_factory=list)
    inferred_schema: dict[str, str] = field(default_factory=dict)
    status_code: int | None = None
    tested_at: datetime | None = None

    @property
    def sample_record(self) -> dict[str, Any] | None:
        """The first record the test actually retrieved, or ``None``."""
        return self.sample[0] if self.sample else None

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON payload for ``POST /api/ingest/sources/test``."""
        return {
            "ok": self.ok,
            "reachable": self.reachable,
            "authenticated": self.authenticated,
            "latency_ms": self.latency_ms,
            "message": self.message,
            "error": self.error,
            "sample": self.sample,
            "sample_record": self.sample_record,
            "inferred_schema": self.inferred_schema,
            "status_code": self.status_code,
            "tested_at": self.tested_at.isoformat() if self.tested_at else None,
        }

    @classmethod
    def failure(
        cls,
        message: str,
        *,
        error: str | None = None,
        reachable: bool | None = None,
        authenticated: bool | None = None,
        latency_ms: float | None = None,
        status_code: int | None = None,
    ) -> ConnectionTestResult:
        """Build a failed result, stamped with the current time."""
        return cls(
            ok=False,
            reachable=reachable,
            authenticated=authenticated,
            latency_ms=latency_ms,
            message=message,
            error=error or message,
            status_code=status_code,
            tested_at=_utcnow(),
        )

    @classmethod
    def success(
        cls,
        message: str,
        *,
        latency_ms: float | None = None,
        authenticated: bool | None = None,
        records: list[dict[str, Any]] | None = None,
        status_code: int | None = None,
    ) -> ConnectionTestResult:
        """Build a successful result carrying a real sample of the response."""
        sample = list(records or [])[:SAMPLE_SIZE]
        return cls(
            ok=True,
            reachable=True,
            authenticated=authenticated,
            latency_ms=latency_ms,
            message=message,
            sample=sample,
            inferred_schema=infer_schema(list(records or [])),
            status_code=status_code,
            tested_at=_utcnow(),
        )


#: The name used by the scaffold and by ``docs/SPEC.md``'s connector table.
TestResult = ConnectionTestResult
TestResult.__test__ = False  # type: ignore[attr-defined]  # not a pytest test class


@dataclass(frozen=True, slots=True)
class FetchResult:
    """Outcome of one fetch/backfill batch."""

    records: list[dict[str, Any]]
    fetched_at: datetime
    latency_ms: float | None = None
    complete: bool = True
    cursor: str | None = None
    dataset: str | None = None

    def __len__(self) -> int:
        return len(self.records)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable summary (records included)."""
        return {
            "records": self.records,
            "count": len(self.records),
            "fetched_at": self.fetched_at.isoformat(),
            "latency_ms": self.latency_ms,
            "complete": self.complete,
            "cursor": self.cursor,
            "dataset": self.dataset,
        }

    @classmethod
    def empty(cls, *, dataset: str | None = None) -> FetchResult:
        """An honest empty batch: zero records, stamped now."""
        return cls(records=[], fetched_at=_utcnow(), dataset=dataset)


class Connector(abc.ABC):
    """Protocol adapter for one ingestion source.

    Subclasses implement :meth:`connect`, :meth:`close`, :meth:`test`,
    :meth:`fetch` and :meth:`status`; :meth:`backfill` and :meth:`stream` are
    optional and raise :class:`NotImplementedError` when the protocol cannot
    serve them.
    """

    #: Protocol identifier this connector serves.
    protocol: str = "base"

    def __init__(self, config: SourceConfig) -> None:
        """Bind the connector to its source configuration."""
        self.config = config
        self.last_error: str | None = None
        self.last_success_at: datetime | None = None
        self.last_latency_ms: float | None = None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"{type(self).__name__}(source={self.config.id!r})"

    async def __aenter__(self) -> Connector:
        """Open the connector for use in an ``async with`` block."""
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Close the connector when the ``async with`` block ends."""
        await self.close()

    @abc.abstractmethod
    async def connect(self) -> None:
        """Open whatever the protocol needs (client, socket, pool, handle)."""
        raise NotImplementedError

    @abc.abstractmethod
    async def close(self) -> None:
        """Release all resources. Safe to call when never connected."""
        raise NotImplementedError

    @abc.abstractmethod
    async def test(self) -> ConnectionTestResult:
        """Perform a real connection test and return what actually happened."""
        raise NotImplementedError

    @abc.abstractmethod
    async def fetch(self, **params: Any) -> FetchResult:
        """Fetch one batch of records."""
        raise NotImplementedError

    async def backfill(
        self, *, range: str = "1y", batch_size: int = 500, end: datetime | None = None
    ) -> AsyncIterator[FetchResult]:
        """Yield historical batches covering ``range``.

        Args:
            range: how far back the window reaches - a duration token
                (``"5d"``, ``"6mo"``, ``"3y"``), ``"all"``, or an ISO date.
            batch_size: rows per request where the connector can control it.
            end: the upper bound of the window. ``None`` means "up to now",
                which is what the catalog's FROM/TO control sends when TO
                reads ``now``.

        The trailing ``yield`` is unreachable; it exists so this method is an
        async *generator*, which makes ``async for`` on a connector that cannot
        backfill raise :class:`NotImplementedError` instead of a type error.
        """
        raise NotImplementedError(f"{self.protocol} sources cannot backfill")
        yield FetchResult.empty()  # pragma: no cover - unreachable

    async def stream(self) -> AsyncIterator[dict[str, Any]]:
        """Yield records as they arrive. Only streaming protocols implement this."""
        raise NotImplementedError(f"{self.protocol} sources cannot stream")
        yield {}  # pragma: no cover - unreachable

    @abc.abstractmethod
    async def status(self) -> SourceStatus:
        """Return the observed status of the underlying connection."""
        raise NotImplementedError

    # --- shared helpers ---------------------------------------------------
    def parse_options(self) -> dict[str, Any]:
        """Format options taken from the source's advanced configuration."""
        advanced = self.config.advanced
        options: dict[str, Any] = {}
        for key in ("records_path", "delimiter", "header", "encoding"):
            value = advanced.get(key)
            if value is not None:
                options[key] = value
        return options

    def decode(self, body: bytes | str) -> list[dict[str, Any]]:
        """Parse a payload with this source's format, then apply schema mapping.

        Raises:
            ValueError: when the payload does not parse as the declared format.
        """
        options = self.parse_options()
        encoding = str(options.pop("encoding", "utf-8"))
        records = parse_payload(body, self.config.format, encoding=encoding, **options)
        return self.map_records(records)

    def map_records(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Apply the configured schema mapping and make records JSON-safe."""
        mapped = apply_schema_mapping(records, self.config.advanced.schema_mapping)
        return json_safe_records(mapped)

    def _mark_success(self, latency_ms: float | None = None) -> None:
        """Record a successful round-trip for :meth:`status` reporting."""
        self.last_error = None
        self.last_success_at = _utcnow()
        self.last_latency_ms = latency_ms

    def _mark_failure(self, error: str) -> None:
        """Record a failed round-trip for :meth:`status` reporting."""
        self.last_error = error

    def observed_status(self) -> SourceStatus:
        """Map the last recorded outcome onto a catalog status."""
        if self.last_error is not None:
            return SourceStatus.DEGRADED
        if self.last_success_at is not None:
            return SourceStatus.CONNECTED
        return SourceStatus.UNKNOWN


#: Protocol -> connector module/class, imported lazily to keep imports cheap.
_CONNECTOR_MODULES: dict[Protocol, tuple[str, str]] = {
    Protocol.WEBSOCKET: ("tradinglib.data.ingest.connectors.websocket", "WebSocketConnector"),
    Protocol.REST: ("tradinglib.data.ingest.connectors.rest", "RestConnector"),
    Protocol.POLLING: ("tradinglib.data.ingest.connectors.polling", "PollingConnector"),
    Protocol.DATABASE: ("tradinglib.data.ingest.connectors.database", "DatabaseConnector"),
    Protocol.FILE: ("tradinglib.data.ingest.connectors.file", "FileConnector"),
    Protocol.LIBRARY: ("tradinglib.data.ingest.connectors.library", "LibraryConnector"),
}


def get_connector_class(protocol: Protocol) -> type[Connector]:
    """Return the connector class registered for ``protocol``.

    Raises:
        ValueError: when the protocol has no registered connector.
    """
    import importlib

    entry = _CONNECTOR_MODULES.get(protocol)
    if entry is None:
        raise ValueError(f"no connector registered for protocol {protocol!r}")
    module_name, class_name = entry
    return getattr(importlib.import_module(module_name), class_name)


def build_connector(config: SourceConfig, **kwargs: Any) -> Connector:
    """Instantiate the connector class selected by ``config.protocol``.

    A source that names an in-process provider (``config.provider``) is served
    by the :class:`~tradinglib.data.ingest.connectors.library.LibraryConnector`
    whatever protocol it advertises: the protocol then describes what the vendor
    speaks (EDGAR really is REST, FRED really is polled), while the transport is
    the library adapter that already implements that vendor's rate limits and
    quirks. Everything else is dispatched on the protocol alone.

    Raises:
        ValueError: when the protocol has no registered connector.
    """
    if config.provider:
        from tradinglib.data.ingest.connectors.library import LibraryConnector

        return LibraryConnector(config, **kwargs)
    return get_connector_class(config.protocol)(config, **kwargs)


__all__ = [
    "SAMPLE_SIZE",
    "ConnectionTestResult",
    "Connector",
    "FetchResult",
    "TestResult",
    "build_connector",
    "get_connector_class",
]
