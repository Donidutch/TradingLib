"""Database connector: ingests rows from an external SQL database."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from tradinglib.data.ingest.connectors.base import (
    ConnectionTestResult,
    Connector,
    FetchResult,
)
from tradinglib.data.ingest.formats import json_safe_records, parse_timestamp
from tradinglib.data.ingest.source import AuthType, SourceConfig, SourceStatus

logger = logging.getLogger(__name__)

#: Row cap applied to ``test()`` so a huge table cannot stall the UI.
TEST_ROW_LIMIT: int = 5


class DatabaseConnector(Connector):
    """Runs the configured SQL against an external database via SQLAlchemy.

    ``endpoint`` is the SQLAlchemy URL (``postgresql://host/db``,
    ``duckdb:///data/market.duckdb``, ``sqlite:////data/market.db``). When the
    source uses "Connection secret" authentication, a ``{secret}`` placeholder
    in the URL is replaced with the stored secret so the password never has to
    live in the visible endpoint.

    Advanced configuration keys::

        sql                the query to run (required)
        params             bound parameters for the query
        watermark_column   column used to page through history in backfill()
        order_by           override for the backfill ordering column
    """

    protocol = "database"

    def __init__(self, config: SourceConfig) -> None:
        """Bind the connector to its source configuration."""
        super().__init__(config)
        self._engine: Engine | None = None

    # --- lifecycle --------------------------------------------------------
    @property
    def url(self) -> str:
        """The SQLAlchemy URL, with the connection secret substituted in."""
        url = self.config.endpoint
        if self.config.auth.type is AuthType.SECRET and self.config.auth.key_value:
            url = url.replace("{secret}", self.config.auth.key_value)
        return url

    @property
    def sql(self) -> str:
        """The configured query, without a trailing semicolon.

        Raises:
            ValueError: when the source has no query configured.
        """
        raw = (self.config.advanced.sql or "").strip()
        if not raw:
            raise ValueError("database source has no 'sql' in its advanced configuration")
        return raw.rstrip(";").strip()

    def _connect_sync(self) -> Engine:
        """Create the engine and verify it can open a connection."""
        engine = create_engine(self.url, future=True)
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return engine

    async def connect(self) -> None:
        """Create the engine/connection pool for ``config.endpoint``.

        Raises:
            SQLAlchemyError: when the database cannot be reached.
        """
        if self._engine is not None:
            return
        self._engine = await asyncio.to_thread(self._connect_sync)

    async def close(self) -> None:
        """Dispose of the engine."""
        engine, self._engine = self._engine, None
        if engine is not None:
            await asyncio.to_thread(engine.dispose)

    # --- querying ---------------------------------------------------------
    def _run_sync(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Execute ``sql`` and return the rows as JSON-safe records."""
        assert self._engine is not None  # noqa: S101 - established by connect()
        with self._engine.connect() as connection:
            result = connection.execute(text(sql), params)
            rows = [dict(row) for row in result.mappings()]
        return json_safe_records(rows)

    async def _run(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Execute ``sql`` on a worker thread, applying the schema mapping."""
        await self.connect()
        rows = await asyncio.to_thread(self._run_sync, sql, dict(params or {}))
        return self.map_records(rows)

    async def fetch(self, **params: Any) -> FetchResult:
        """Execute the configured SQL and return the rows as records.

        Keyword arguments are passed as bound parameters, so a query may use
        ``:since`` and the scheduler can supply it.

        Raises:
            SQLAlchemyError: when the query or the connection fails.
            ValueError: when no query is configured.
        """
        bound = {**(self.config.advanced.get("params") or {}), **params}
        started = time.perf_counter()
        try:
            records = await self._run(self.sql, bound)
        except (SQLAlchemyError, ValueError) as exc:
            self._mark_failure(f"{type(exc).__name__}: {exc}")
            raise
        latency_ms = (time.perf_counter() - started) * 1000.0
        self._mark_success(latency_ms)
        return FetchResult(
            records=records,
            fetched_at=datetime.now(timezone.utc),
            latency_ms=latency_ms,
            complete=True,
        )

    async def backfill(
        self, *, range: str = "1y", batch_size: int = 500, end: datetime | None = None
    ) -> AsyncIterator[FetchResult]:
        """Page through history using a keyset cursor on the time column.

        Requires ``watermark_column`` in the advanced configuration; without it
        the query has no ordering this connector can page on, so the whole
        result is yielded as a single batch. ``end`` bounds the window above,
        which becomes a second predicate on the same column.
        """
        column = self.config.advanced.get("watermark_column") or self.config.advanced.get(
            "order_by"
        )
        if not column:
            yield await self.fetch()
            return

        from tradinglib.data.ingest.source import BackfillConfig

        cutoff = BackfillConfig(enabled=True, range=range).range_start(now=end)
        cursor: Any = cutoff.isoformat() if cutoff is not None else None
        base = self.sql
        while True:
            clause = f"SELECT * FROM ({base}) AS backfill_src"
            params: dict[str, Any] = {}
            predicates: list[str] = []
            if cursor is not None:
                predicates.append(f"{column} > :cursor")
                params["cursor"] = cursor
            if end is not None:
                predicates.append(f"{column} <= :window_end")
                params["window_end"] = end.isoformat()
            if predicates:
                clause += " WHERE " + " AND ".join(predicates)
            clause += f" ORDER BY {column} LIMIT {int(batch_size)}"
            started = time.perf_counter()
            records = await self._run(clause, params)
            latency_ms = (time.perf_counter() - started) * 1000.0
            self._mark_success(latency_ms)
            complete = len(records) < batch_size
            last = records[-1].get(str(column)) if records else None
            yield FetchResult(
                records=records,
                fetched_at=datetime.now(timezone.utc),
                latency_ms=latency_ms,
                complete=complete,
                cursor=str(last) if last is not None else None,
            )
            if complete or last is None:
                return
            cursor = last

    # --- test / status ----------------------------------------------------
    async def test(self) -> ConnectionTestResult:
        """Connect and run the configured SQL with a small row limit."""
        started = time.perf_counter()
        try:
            sql = self.sql
        except ValueError as exc:
            return ConnectionTestResult.failure(str(exc), error=str(exc))
        try:
            await self.connect()
        except SQLAlchemyError as exc:
            message = f"cannot connect to the database: {type(exc).__name__}: {exc}"
            self._mark_failure(message)
            authenticated = None if "auth" not in str(exc).lower() else False
            return ConnectionTestResult.failure(message, reachable=False, authenticated=authenticated)
        probe = f"SELECT * FROM ({sql}) AS test_src LIMIT {TEST_ROW_LIMIT}"
        try:
            records = await self._run(probe, self.config.advanced.get("params") or {})
        except SQLAlchemyError as exc:
            message = f"query failed: {type(exc).__name__}: {exc}"
            self._mark_failure(message)
            return ConnectionTestResult.failure(message, reachable=True, authenticated=True)
        latency_ms = (time.perf_counter() - started) * 1000.0
        self._mark_success(latency_ms)
        return ConnectionTestResult.success(
            f"connected · query returned {len(records)} sample row(s)",
            latency_ms=latency_ms,
            authenticated=True,
            records=records,
        )

    async def status(self) -> SourceStatus:
        """Report connectivity to the external database."""
        if self.config.auth.type is AuthType.SECRET and not self.config.auth.key_value:
            return SourceStatus.NOT_CONFIGURED
        if self._engine is None and self.last_success_at is None:
            return SourceStatus.UNKNOWN
        return self.observed_status()

    def latest_watermark(self, records: list[dict[str, Any]]) -> datetime | None:
        """Return the newest watermark timestamp among ``records``, if any."""
        column = self.config.advanced.get("watermark_column")
        if not column:
            return None
        stamps = [
            parse_timestamp(record.get(str(column)), tz=self.config.timezone)
            for record in records
        ]
        real = [s for s in stamps if s is not None]
        return max(real) if real else None


__all__ = ["TEST_ROW_LIMIT", "DatabaseConnector"]
