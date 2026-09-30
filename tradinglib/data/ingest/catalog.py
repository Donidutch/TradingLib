"""Source catalog: saved sources, built-in providers and live statuses.

Built-in providers (yfinance, Alpaca, EDGAR, FRED, Trends) are pre-registered
catalog entries and are peers of user-created sources. The "sources connected"
figure in the datastrip counts real observed statuses from this catalog: a
status only ever changes because a connector was actually probed.

Persistence uses the application-state database from :mod:`tradinglib.db` (SPEC.md
decision D4). Credentials are stored unmasked in that local SQLite file - the
same place the rest of the app state lives - and are masked on every path that
leaves the process.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, delete, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from tradinglib.data.ingest.connectors.base import (
    ConnectionTestResult,
    Connector,
    build_connector,
)
from tradinglib.data.ingest.source import (
    AdvancedConfig,
    AuthConfig,
    AuthType,
    BackfillConfig,
    DataDomain,
    PayloadFormat,
    Protocol,
    RetryMode,
    RetryPolicy,
    ScheduleKind,
    SourceConfig,
    SourceStatus,
)
from tradinglib.db import Base, ensure_columns, get_engine, get_session_factory
from tradinglib.events import EventBus

logger = logging.getLogger(__name__)

#: Event channel every catalog change is published on.
INGEST_CHANNEL: str = "ingest"

#: How many sources are probed concurrently by :meth:`SourceCatalog.refresh_all`.
PROBE_CONCURRENCY: int = 5

#: Bar granularities offered per data domain - the *dataset's* own resolution,
#: which is a different question from how often the connector runs (that is the
#: source's schedule). ``""`` always means "whatever the source is configured
#: for" and is the default.
GRANULARITY_OPTIONS: dict[DataDomain, tuple[str, ...]] = {
    DataDomain.MARKET: ("1m", "5m", "15m", "30m", "1h", "1d"),
    DataDomain.FUNDAMENTALS: ("quarterly", "annual"),
    DataDomain.MACRO: ("1d", "1wk", "1mo", "quarterly", "annual"),
    DataDomain.NEWS: ("realtime", "1h", "1d"),
    DataDomain.ALTERNATIVE: ("1d", "1wk", "1mo"),
    DataDomain.REFERENCE: ("1d", "1wk", "1mo", "quarterly", "annual"),
}

#: Every granularity token any domain offers, for validation.
GRANULARITIES: frozenset[str] = frozenset(
    token for tokens in GRANULARITY_OPTIONS.values() for token in tokens
)


def granularity_options(domain: DataDomain) -> tuple[str, ...]:
    """Return the granularities a source of this domain can be asked for."""
    return GRANULARITY_OPTIONS.get(domain, GRANULARITY_OPTIONS[DataDomain.REFERENCE])


def parse_granularity(value: Any) -> str:
    """Normalise a granularity token.

    ``""``/``None`` mean "the source's own default" and are accepted as such.

    Raises:
        ValueError: when the token is not one this catalog offers.
    """
    token = str(value or "").strip().lower()
    if not token:
        return ""
    if token not in GRANULARITIES:
        raise ValueError(
            f"unknown granularity {value!r}; expected one of {sorted(GRANULARITIES)}"
        )
    return token


def bar_interval_for(granularity: str) -> str | None:
    """Return the :class:`~tradinglib.data.base.BarInterval` token, if it is one.

    ``quarterly``/``annual``/``realtime`` are dataset cadences with no bar-size
    equivalent, so they return ``None`` - which is what makes them *advisory*
    rather than something the fetch path can act on.
    """
    from tradinglib.data.base import BarInterval

    token = str(granularity or "").strip().lower()
    try:
        return str(BarInterval(token))
    except ValueError:
        return None


def granularity_reaches_fetch(config: SourceConfig) -> bool:
    """Whether this source's fetch path takes a bar size at all.

    Only the library connector's ``bars`` dataset has an ``interval`` parameter
    (:meth:`LibraryConnector.fetch`), and the scheduler only passes one for a
    source that names a provider - which is also exactly the condition
    :func:`build_connector` uses to serve a source through the library
    connector, whatever protocol it advertises. Every other source records a
    subscription's granularity without being able to act on it - which is what
    makes that value advisory rather than applied.
    """
    from tradinglib.data.ingest.connectors.library import effective_dataset

    if not config.provider:
        return False
    try:
        return effective_dataset(config) == "bars"
    except ValueError:
        # An unrunnable dataset name fetches nothing at all, so it certainly
        # does not apply the subscription's bar size.
        return False


def agreed_bar_interval(
    subscriptions: Iterable[Subscription],
) -> tuple[str | None, list[str]]:
    """Return the bar interval a source's *active* subscriptions agree on.

    The second element is every distinct bar interval those subscriptions ask
    for, so a caller can explain a disagreement instead of merely refusing one.
    A resolution is only imposed on the source when exactly one is wanted:
    paused subscriptions do not vote, cadences with no bar equivalent
    (``quarterly``, ``realtime``) cannot vote, and two active subscriptions
    asking for different bars cancel each other out.

    This is the single rule behind both :meth:`IngestScheduler.requested_interval`
    (which acts on it) and the subscriptions API (which reports it).
    """
    wanted = sorted(
        {
            interval
            for sub in subscriptions
            if sub.active and (interval := bar_interval_for(sub.granularity)) is not None
        }
    )
    return (wanted[0] if len(wanted) == 1 else None, wanted)


def _utcnow() -> datetime:
    """Return the current time as an aware UTC datetime."""
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    """Attach UTC to a timestamp SQLite handed back without a timezone."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class IngestSourceRow(Base):
    """ORM row for one saved ingestion source."""

    __tablename__ = "ingest_sources"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    data_domain: Mapped[str] = mapped_column(String(32), nullable=False)
    protocol: Mapped[str] = mapped_column(String(32), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(1000), default="")
    payload_format: Mapped[str] = mapped_column(String(32), default="json")
    schedule: Mapped[str] = mapped_column(String(64), default="manual")
    source_tz: Mapped[str] = mapped_column(String(64), default="UTC")
    scope: Mapped[list[str]] = mapped_column(JSON, default=list)
    scope_label: Mapped[str | None] = mapped_column(String(200), nullable=True)
    auth: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    retry_policy: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    backfill: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    advanced: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class IngestSourceStateRow(Base):
    """ORM row holding the observed runtime state of one source."""

    __tablename__ = "ingest_source_state"

    source_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), default=str(SourceStatus.UNKNOWN))
    last_ingest_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    rows_ingested: Mapped[int] = mapped_column(Integer, default=0)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: When a historical backfill last completed for this source. This is what
    #: makes "backfill on first connection" a once-only event rather than
    #: something that restarts on every save.
    backfilled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class IngestSubscriptionRow(Base):
    """ORM row for a source subscription (a scope attached to a channel)."""

    __tablename__ = "ingest_subscriptions"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(96), nullable=False)
    channel: Mapped[str] = mapped_column(String(120), nullable=False)
    scope: Mapped[list[str]] = mapped_column(JSON, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    #: How this subscription wants its data delivered. ``live`` asks the source
    #: to stream, ``scheduled`` to poll on the source's schedule; a mode the
    #: source's protocol cannot honour is reported, never silently downgraded.
    feed_mode: Mapped[str] = mapped_column(String(16), default="scheduled")
    #: Keep the value that was true at each observation time rather than letting
    #: a later restatement overwrite it.
    preserve_pit: Mapped[bool] = mapped_column(Boolean, default=False)
    #: The dataset resolution this subscription wants (``1m``, ``1d``,
    #: ``quarterly``...). ``""`` leaves the source's own configuration alone.
    granularity: Mapped[str] = mapped_column(String(16), default="")
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


@dataclass(slots=True)
class SourceState:
    """Observed runtime state of one catalog source."""

    source_id: str
    status: SourceStatus = SourceStatus.UNKNOWN
    last_ingest_at: datetime | None = None
    last_error: str | None = None
    latency_ms: float | None = None
    rows_ingested: int = 0
    checked_at: datetime | None = None
    #: When a historical backfill last completed, or ``None`` if none ever has.
    backfilled_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping for the API."""
        return {
            "source_id": self.source_id,
            "status": str(self.status),
            "status_label": self.status.label,
            "last_ingest_at": self.last_ingest_at.isoformat() if self.last_ingest_at else None,
            "last_error": self.last_error,
            "latency_ms": self.latency_ms,
            "rows_ingested": self.rows_ingested,
            "checked_at": self.checked_at.isoformat() if self.checked_at else None,
            "backfilled_at": self.backfilled_at.isoformat() if self.backfilled_at else None,
        }


@dataclass(slots=True)
class Subscription:
    """A subscription of a source to a scope (symbols, series ids, feeds)."""

    id: str
    source_id: str
    channel: str
    scope: list[str] = field(default_factory=list)
    active: bool = True
    feed_mode: str = "scheduled"
    preserve_pit: bool = False
    #: The dataset's own resolution (bar size / reporting cadence). Empty means
    #: "leave the source's configuration alone".
    granularity: str = ""
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        """Reject a granularity the catalog does not offer, before it is stored."""
        self.granularity = parse_granularity(self.granularity)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping for the API."""
        return {
            "id": self.id,
            "source_id": self.source_id,
            "channel": self.channel,
            "scope": list(self.scope),
            "active": self.active,
            "feed_mode": self.feed_mode,
            "preserve_pit": self.preserve_pit,
            "granularity": self.granularity,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


def builtin_sources() -> list[SourceConfig]:
    """Return the five built-in provider sources, configured from settings.

    Nothing here is a status: these are configurations. Whether Alpaca is
    connected or merely configured is decided by probing it
    (:meth:`SourceCatalog.refresh_status`), never by this function.
    """
    from tradinglib.config import get_settings

    settings = get_settings()
    feed = settings.alpaca_data_feed or "iex"

    yfinance = SourceConfig(
        id="yfinance",
        name="Yahoo Finance (yfinance)",
        data_domain=DataDomain.MARKET,
        protocol=Protocol.LIBRARY,
        endpoint="yfinance",
        provider="yfinance",
        # "Database rows" is the form's name for an already-decoded record
        # stream, which is exactly what an in-process provider returns.
        format=PayloadFormat.ROWS,
        schedule="1d",
        scope=[settings.default_benchmark],
        scope_label="Benchmark",
        retry_policy=RetryPolicy.from_mode(RetryMode.EXPONENTIAL),
        backfill=BackfillConfig(enabled=True, range="5y", batch="auto"),
        advanced=AdvancedConfig(extra={"dataset": "bars", "interval": "1d"}),
        builtin=True,
    )
    alpaca = SourceConfig(
        id="alpaca",
        name="Alpaca Market Stream",
        data_domain=DataDomain.MARKET,
        protocol=Protocol.WEBSOCKET,
        endpoint=f"wss://stream.data.alpaca.markets/v2/{feed}",
        auth=AuthConfig(
            type=AuthType.API_KEY,
            key_name="APCA-API-KEY-ID",
            key_value=settings.alpaca_api_key,
            client_secret=settings.alpaca_secret_key,
            in_header=False,
        ),
        format=PayloadFormat.JSON,
        schedule="streaming",
        scope=[],
        scope_label="Custom symbols",
        retry_policy=RetryPolicy.from_mode(RetryMode.EXPONENTIAL),
        advanced=AdvancedConfig(
            extra={
                "auth_message": {"action": "auth", "key": "{key}", "secret": "{secret}"},
                "subscribe_message": {"action": "subscribe", "trades": "{channels}"},
                "feed": feed,
            }
        ),
        builtin=True,
    )
    edgar = SourceConfig(
        id="edgar",
        name="SEC EDGAR",
        data_domain=DataDomain.FUNDAMENTALS,
        protocol=Protocol.REST,
        endpoint="https://data.sec.gov",
        provider="edgar",
        format=PayloadFormat.JSON,
        schedule="1d",
        scope=[],
        scope_label="Custom symbols",
        retry_policy=RetryPolicy.from_mode(RetryMode.EXPONENTIAL),
        advanced=AdvancedConfig(extra={"dataset": "filings"}),
        builtin=True,
    )
    fred = SourceConfig(
        id="fred",
        name="FRED Macro",
        data_domain=DataDomain.MACRO,
        protocol=Protocol.POLLING,
        endpoint="https://fred.stlouisfed.org/graph/fredgraph.csv",
        provider="fred",
        format=PayloadFormat.CSV,
        schedule="1h",
        scope=["DGS10", "CPIAUCSL", "UNRATE"],
        scope_label="Macro series",
        retry_policy=RetryPolicy.from_mode(RetryMode.EXPONENTIAL),
        backfill=BackfillConfig(enabled=True, range="all", batch="auto"),
        advanced=AdvancedConfig(extra={"dataset": "series"}),
        builtin=True,
    )
    trends = SourceConfig(
        id="trends",
        name="Google Trends",
        data_domain=DataDomain.ALTERNATIVE,
        protocol=Protocol.POLLING,
        endpoint="https://trends.google.com/trends",
        provider="trends",
        format=PayloadFormat.ROWS,
        schedule="1d",
        scope=[],
        scope_label="Custom keywords",
        retry_policy=RetryPolicy.from_mode(RetryMode.EXPONENTIAL),
        advanced=AdvancedConfig(
            extra={"dataset": "trends", "timeframe": "today 12-m", "geo": "US"}
        ),
        builtin=True,
    )
    options = SourceConfig(
        id="options-iv",
        name="Options Implied Volatility",
        data_domain=DataDomain.MARKET,
        protocol=Protocol.LIBRARY,
        endpoint="yfinance",
        provider="yfinance",
        format=PayloadFormat.ROWS,
        schedule="1d",
        scope=[settings.default_benchmark],
        scope_label="Underlyings",
        retry_policy=RetryPolicy.from_mode(RetryMode.EXPONENTIAL),
        # An option chain is a snapshot: yesterday's chain is not retrievable,
        # so history accrues forward from the first ingest and backfill is off
        # rather than pretending a range could be recovered.
        backfill=BackfillConfig(enabled=False),
        advanced=AdvancedConfig(extra={"dataset": "options", "target_days": 30}),
        builtin=True,
    )
    jobs = SourceConfig(
        id="jobs",
        name="Job Boards (Greenhouse / Ashby / Lever)",
        data_domain=DataDomain.ALTERNATIVE,
        protocol=Protocol.LIBRARY,
        endpoint="jobs",
        provider="jobs",
        format=PayloadFormat.ROWS,
        schedule="1d",
        # Board slugs, not tickers: the boards are addressed by the company's
        # own slug ("datadog"), which is also what makes the ingested series
        # names stable when a ticker changes.
        scope=[],
        scope_label="Job board slugs",
        retry_policy=RetryPolicy.from_mode(RetryMode.EXPONENTIAL),
        # A board lists what is open right now; yesterday's board is not
        # retrievable, so history accrues forward from the first ingest.
        backfill=BackfillConfig(enabled=False),
        advanced=AdvancedConfig(extra={"dataset": "jobs"}),
        builtin=True,
    )
    return [yfinance, alpaca, edgar, fred, trends, options, jobs]


class SourceCatalog:
    """Registry of ingestion sources plus their observed state."""

    def __init__(
        self,
        bus: EventBus | None = None,
        *,
        connector_factory: Callable[[SourceConfig], Connector] | None = None,
    ) -> None:
        """Create the catalog.

        Args:
            bus: event bus used to publish ``ingest`` events; optional so the
                catalog can be used headless.
            connector_factory: override used by tests to inject fake or
                transport-bound connectors.
        """
        self.bus = bus
        self._connector_factory = connector_factory or build_connector
        self._ensure_tables()

    # --- persistence ------------------------------------------------------
    @staticmethod
    def _ensure_tables() -> None:
        """Create this module's tables if the database does not have them yet."""
        engine = get_engine()
        tables = [
            IngestSourceRow.__table__,
            IngestSourceStateRow.__table__,
            IngestSubscriptionRow.__table__,
        ]
        Base.metadata.create_all(engine, tables=tables)
        for table in tables:
            ensure_columns(table)

    @staticmethod
    def _session() -> Session:
        """Open a new database session; the caller closes it."""
        return get_session_factory()()

    @staticmethod
    def _to_config(row: IngestSourceRow) -> SourceConfig:
        """Rebuild a :class:`SourceConfig` from its database row."""
        return SourceConfig(
            id=row.id,
            name=row.name,
            data_domain=DataDomain(row.data_domain),
            protocol=Protocol(row.protocol),
            endpoint=row.endpoint or "",
            auth=AuthConfig.from_dict(row.auth),
            format=PayloadFormat(row.payload_format),
            schedule=row.schedule,
            scope=list(row.scope or []),
            scope_label=row.scope_label,
            source_tz=row.source_tz,
            retry_policy=RetryPolicy.from_dict(row.retry_policy),
            backfill=BackfillConfig.from_dict(row.backfill),
            advanced=AdvancedConfig.from_dict(row.advanced),
            provider=row.provider,
            builtin=bool(row.builtin),
            enabled=bool(row.enabled),
            created_at=_aware(row.created_at),
            updated_at=_aware(row.updated_at),
        )

    @staticmethod
    def _apply(row: IngestSourceRow, config: SourceConfig) -> IngestSourceRow:
        """Copy a config onto a database row."""
        row.id = config.id
        row.name = config.name
        row.data_domain = str(config.data_domain)
        row.protocol = str(config.protocol)
        row.endpoint = config.endpoint
        row.payload_format = str(config.format)
        row.schedule = config.schedule
        row.source_tz = config.source_tz
        row.scope = list(config.scope)
        row.scope_label = config.scope_label
        row.auth = config.auth.to_dict(mask_secrets=False)
        row.retry_policy = config.retry_policy.to_dict()
        row.backfill = config.backfill.to_dict()
        row.advanced = config.advanced.to_dict()
        row.provider = config.provider
        row.builtin = config.builtin
        row.enabled = config.enabled
        row.created_at = config.created_at
        row.updated_at = config.updated_at
        return row

    # --- sources ----------------------------------------------------------
    def register_builtins(self) -> list[SourceConfig]:
        """Insert the built-in provider sources if they are not present yet.

        Existing rows are refreshed in the fields the user cannot edit
        (endpoint, provider, credentials taken from settings) while keeping
        their scope, schedule and enabled flag, so a re-seed never discards a
        user's configuration.
        """
        seeded: list[SourceConfig] = []
        session = self._session()
        try:
            for config in builtin_sources():
                row = session.get(IngestSourceRow, config.id)
                if row is None:
                    config.created_at = _utcnow()
                    config.updated_at = config.created_at
                    session.add(self._apply(IngestSourceRow(), config))
                    seeded.append(config)
                else:
                    existing = self._to_config(row)
                    config.schedule = existing.schedule
                    config.scope = existing.scope
                    config.scope_label = existing.scope_label
                    config.enabled = existing.enabled
                    config.created_at = existing.created_at or _utcnow()
                    config.updated_at = _utcnow()
                    # user-set advanced options survive a re-seed
                    merged = dict(config.advanced.extra)
                    merged.update(
                        {k: v for k, v in existing.advanced.extra.items() if k not in ("dataset",)}
                    )
                    config.advanced.extra = merged
                    self._apply(row, config)
                    seeded.append(config)
                if session.get(IngestSourceStateRow, config.id) is None:
                    session.add(IngestSourceStateRow(source_id=config.id))
            session.commit()
        finally:
            session.close()
        logger.info("catalog: %d built-in sources registered", len(seeded))
        return seeded

    def list_sources(self) -> list[SourceConfig]:
        """Return every source, built-ins first."""
        session = self._session()
        try:
            rows = session.execute(select(IngestSourceRow)).scalars().all()
        finally:
            session.close()
        configs = [self._to_config(row) for row in rows]
        return sorted(configs, key=lambda c: (not c.builtin, c.name.lower()))

    def get(self, source_id: str) -> SourceConfig:
        """Return one source. Raises ``KeyError`` when unknown."""
        session = self._session()
        try:
            row = session.get(IngestSourceRow, source_id)
        finally:
            session.close()
        if row is None:
            raise KeyError(f"unknown ingestion source: {source_id!r}")
        return self._to_config(row)

    def exists(self, source_id: str) -> bool:
        """Whether a source with this id is saved."""
        try:
            self.get(source_id)
        except KeyError:
            return False
        return True

    def save(self, config: SourceConfig) -> SourceConfig:
        """Create or update a source and persist it.

        Raises:
            ValueError: when the configuration is not runnable as written.
        """
        config.validate()
        session = self._session()
        try:
            row = session.get(IngestSourceRow, config.id)
            now = _utcnow()
            if row is None:
                config.created_at = config.created_at or now
                config.updated_at = now
                session.add(self._apply(IngestSourceRow(), config))
                session.add(IngestSourceStateRow(source_id=config.id))
            else:
                existing = self._to_config(row)
                if existing.builtin:
                    # The endpoint and provider of a built-in are ours, not the
                    # user's: keep them whatever the payload said.
                    config.builtin = True
                    config.provider = existing.provider
                    config.endpoint = existing.endpoint
                # Secrets are masked out of API reads, so a GET->PUT round-trip
                # arrives with empty credential fields. An absent secret on an
                # update means "keep the stored one", never "clear it".
                if config.auth.type is existing.auth.type:
                    if config.auth.key_value is None:
                        config.auth.key_value = existing.auth.key_value
                    if config.auth.password is None:
                        config.auth.password = existing.auth.password
                    if config.auth.client_secret is None:
                        config.auth.client_secret = existing.auth.client_secret
                config.created_at = existing.created_at or now
                config.updated_at = now
                self._apply(row, config)
            session.commit()
        finally:
            session.close()
        self._publish_nowait({"event": "source_saved", "source": config.to_dict()})
        return config

    def delete(self, source_id: str) -> None:
        """Delete a user source. Built-ins cannot be deleted.

        Raises:
            KeyError: when the source does not exist.
            ValueError: when the source is a built-in.
        """
        session = self._session()
        try:
            row = session.get(IngestSourceRow, source_id)
            if row is None:
                raise KeyError(f"unknown ingestion source: {source_id!r}")
            if row.builtin:
                raise ValueError(f"{source_id!r} is a built-in source and cannot be deleted")
            session.delete(row)
            session.execute(
                delete(IngestSourceStateRow).where(IngestSourceStateRow.source_id == source_id)
            )
            session.execute(
                delete(IngestSubscriptionRow).where(IngestSubscriptionRow.source_id == source_id)
            )
            session.commit()
        finally:
            session.close()
        self._publish_nowait({"event": "source_deleted", "source_id": source_id})

    def set_enabled(self, source_id: str, enabled: bool) -> SourceConfig:
        """Enable or pause a source (the table's Pause action)."""
        config = self.get(source_id)
        config.enabled = enabled
        return self.save(config)

    # --- state ------------------------------------------------------------
    @staticmethod
    def _to_state(row: IngestSourceStateRow) -> SourceState:
        """Rebuild a :class:`SourceState` from its database row."""
        return SourceState(
            source_id=row.source_id,
            status=SourceStatus(row.status),
            last_ingest_at=_aware(row.last_ingest_at),
            last_error=row.last_error,
            latency_ms=row.latency_ms,
            rows_ingested=int(row.rows_ingested or 0),
            checked_at=_aware(row.checked_at),
            backfilled_at=_aware(row.backfilled_at),
        )

    def state(self, source_id: str) -> SourceState:
        """Return the observed state of one source.

        A source that has never been probed reports
        :attr:`SourceStatus.UNKNOWN` - not "connected".
        """
        session = self._session()
        try:
            row = session.get(IngestSourceStateRow, source_id)
        finally:
            session.close()
        return self._to_state(row) if row is not None else SourceState(source_id=source_id)

    def states(self) -> dict[str, SourceState]:
        """Return observed state for every source."""
        session = self._session()
        try:
            rows = session.execute(select(IngestSourceStateRow)).scalars().all()
            sources = session.execute(select(IngestSourceRow.id)).scalars().all()
        finally:
            session.close()
        states = {row.source_id: self._to_state(row) for row in rows}
        for source_id in sources:
            states.setdefault(source_id, SourceState(source_id=source_id))
        return states

    def _update_state(self, source_id: str, **changes: Any) -> SourceState:
        """Apply changes to a source's persisted state and return it."""
        session = self._session()
        try:
            row = session.get(IngestSourceStateRow, source_id)
            if row is None:
                row = IngestSourceStateRow(source_id=source_id)
                session.add(row)
            for key, value in changes.items():
                if key == "status":
                    row.status = str(value)
                elif key == "rows_delta":
                    row.rows_ingested = int(row.rows_ingested or 0) + int(value)
                else:
                    setattr(row, key, value)
            session.commit()
            state = self._to_state(row)
        finally:
            session.close()
        return state

    def record_test(self, source_id: str, result: ConnectionTestResult) -> SourceState:
        """Persist the outcome of a real connection test as the source's status."""
        status = status_from_test(result)
        return self._update_state(
            source_id,
            status=status,
            last_error=None if result.ok else (result.error or result.message)[:2000],
            latency_ms=result.latency_ms,
            checked_at=result.tested_at or _utcnow(),
        )

    def record_ingest(
        self,
        source_id: str,
        *,
        rows: int,
        latency_ms: float | None = None,
        error: str | None = None,
        at: datetime | None = None,
    ) -> SourceState:
        """Record the outcome of one ingestion run.

        A failed run updates the error and the status but leaves
        ``last_ingest_at`` alone: the last *successful* ingest is what the
        freshness figures are about.
        """
        moment = at or _utcnow()
        changes: dict[str, Any] = {
            "last_error": error[:2000] if error else None,
            "latency_ms": latency_ms,
            "checked_at": moment,
            "status": SourceStatus.DEGRADED if error else SourceStatus.CONNECTED,
        }
        if error is None:
            changes["last_ingest_at"] = moment
            changes["rows_delta"] = rows
        return self._update_state(source_id, **changes)

    def record_backfill(
        self,
        source_id: str,
        *,
        rows: int,
        error: str | None = None,
        at: datetime | None = None,
    ) -> SourceState:
        """Record that a historical backfill completed for this source.

        A failed backfill leaves ``backfilled_at`` unset, so the "on first
        connection" trigger will try again on the next save rather than
        recording a backfill that never landed.
        """
        moment = at or _utcnow()
        changes: dict[str, Any] = {"checked_at": moment}
        if error is None:
            changes["backfilled_at"] = moment
            changes["rows_delta"] = rows
            if rows:
                changes["last_ingest_at"] = moment
        else:
            changes["last_error"] = error[:2000]
        return self._update_state(source_id, **changes)

    # --- probing ----------------------------------------------------------
    def connector_for(self, source_id_or_config: str | SourceConfig) -> Connector:
        """Build the connector serving one source."""
        config = (
            source_id_or_config
            if isinstance(source_id_or_config, SourceConfig)
            else self.get(source_id_or_config)
        )
        return self._connector_factory(config)

    async def test(self, config: SourceConfig) -> ConnectionTestResult:
        """Run a real ``Connector.test()`` for a (possibly unsaved) config."""
        connector = self.connector_for(config)
        try:
            return await connector.test()
        except Exception as exc:  # noqa: BLE001 - reported honestly to the caller
            logger.info("catalog: test of %s raised %s", config.id, exc, exc_info=True)
            return ConnectionTestResult.failure(
                f"{type(exc).__name__}: {exc}", error=f"{type(exc).__name__}: {exc}"
            )
        finally:
            await _safe_close(connector)

    async def refresh_status(self, source_id: str) -> SourceState:
        """Probe a source, update its state and publish on ``ingest``."""
        config = self.get(source_id)
        result = await self.test(config)
        state = self.record_test(source_id, result)
        await self._publish(
            {
                "event": "source_status",
                "source_id": source_id,
                "status": str(state.status),
                "latency_ms": state.latency_ms,
                "message": result.message,
                "error": result.error,
                "checked_at": state.checked_at.isoformat() if state.checked_at else None,
            }
        )
        return state

    async def refresh_all(self, *, only_enabled: bool = True) -> dict[str, SourceState]:
        """Probe every enabled source concurrently."""
        sources = [s for s in self.list_sources() if s.enabled or not only_enabled]
        semaphore = asyncio.Semaphore(PROBE_CONCURRENCY)

        async def probe(config: SourceConfig) -> tuple[str, SourceState]:
            async with semaphore:
                return config.id, await self.refresh_status(config.id)

        results = await asyncio.gather(*(probe(s) for s in sources), return_exceptions=True)
        states: dict[str, SourceState] = {}
        for item in results:
            if isinstance(item, BaseException):
                logger.warning("catalog: status refresh failed: %s", item)
                continue
            source_id, state = item
            states[source_id] = state
        return states

    def connected_count(self) -> tuple[int, int]:
        """Return ``(connected, total)`` for the datastrip - observed, not assumed."""
        sources = self.list_sources()
        states = self.states()
        connected = sum(
            1
            for source in sources
            if states.get(source.id, SourceState(source.id)).status is SourceStatus.CONNECTED
        )
        return connected, len(sources)

    # --- subscriptions ----------------------------------------------------
    @staticmethod
    def _to_subscription(row: IngestSubscriptionRow) -> Subscription:
        """Rebuild a :class:`Subscription` from its database row."""
        return Subscription(
            id=row.id,
            source_id=row.source_id,
            channel=row.channel,
            scope=list(row.scope or []),
            active=bool(row.active),
            feed_mode=str(row.feed_mode or "scheduled"),
            preserve_pit=bool(row.preserve_pit),
            granularity=str(row.granularity or ""),
            created_at=_aware(row.created_at),
        )

    def list_subscriptions(self, *, source_id: str | None = None) -> list[Subscription]:
        """Return every configured subscription, newest last."""
        session = self._session()
        try:
            stmt = select(IngestSubscriptionRow)
            if source_id is not None:
                stmt = stmt.where(IngestSubscriptionRow.source_id == source_id)
            rows = session.execute(stmt).scalars().all()
        finally:
            session.close()
        return [self._to_subscription(row) for row in rows]

    def save_subscription(self, subscription: Subscription) -> Subscription:
        """Create or update a subscription.

        Raises:
            KeyError: when the subscription names a source that does not exist.
            ValueError: when the granularity is not one this source's data
                domain offers (a quarterly bar size for a price feed, say).
        """
        if not self.exists(subscription.source_id):
            raise KeyError(f"unknown ingestion source: {subscription.source_id!r}")
        config = self.get(subscription.source_id)
        allowed = granularity_options(config.data_domain)
        if subscription.granularity and subscription.granularity not in allowed:
            raise ValueError(
                f"{config.name} carries {config.data_domain} data, which is offered at "
                f"{', '.join(allowed)} - not {subscription.granularity!r}"
            )
        if not subscription.id:
            subscription.id = uuid.uuid4().hex
        session = self._session()
        try:
            row = session.get(IngestSubscriptionRow, subscription.id)
            if row is None:
                row = IngestSubscriptionRow(id=subscription.id)
                row.created_at = subscription.created_at or _utcnow()
                session.add(row)
            row.source_id = subscription.source_id
            row.channel = subscription.channel
            row.scope = list(subscription.scope)
            row.active = subscription.active
            row.feed_mode = subscription.feed_mode
            row.preserve_pit = subscription.preserve_pit
            row.granularity = subscription.granularity
            session.commit()
            saved = self._to_subscription(row)
        finally:
            session.close()
        self._publish_nowait({"event": "subscription_saved", "subscription": saved.as_dict()})
        return saved

    def delete_subscription(self, subscription_id: str) -> None:
        """Delete a subscription.

        Raises:
            KeyError: when the subscription does not exist.
        """
        session = self._session()
        try:
            row = session.get(IngestSubscriptionRow, subscription_id)
            if row is None:
                raise KeyError(f"unknown subscription: {subscription_id!r}")
            session.delete(row)
            session.commit()
        finally:
            session.close()
        self._publish_nowait({"event": "subscription_deleted", "subscription_id": subscription_id})

    # --- reporting --------------------------------------------------------
    def describe(self, source_id: str) -> dict[str, Any]:
        """Return one source plus its observed state, as the API serves it."""
        config = self.get(source_id)
        payload = config.to_dict()
        payload["state"] = self.state(source_id).as_dict()
        payload["subscriptions"] = [
            sub.as_dict() for sub in self.list_subscriptions(source_id=source_id)
        ]
        # The dataset resolutions this source can be subscribed at, so the
        # Source Catalog's frequency chips are the source's own, not a fixed list.
        payload["granularity_options"] = list(granularity_options(config.data_domain))
        payload["granularity_default"] = str(
            config.advanced.get("interval") or ""
        ).strip().lower()
        # Whether a bar size can reach this source's fetch at all. Published so
        # the UI states the effect from the backend's own rule instead of
        # re-deriving one from `provider`/`advanced.dataset`.
        payload["granularity_reaches_fetch"] = granularity_reaches_fetch(config)
        return payload

    def summary(self) -> dict[str, Any]:
        """Catalog-level counters for the Source Catalog page header."""
        sources = self.list_sources()
        states = self.states()
        by_status: dict[str, int] = {str(status): 0 for status in SourceStatus}
        by_protocol: dict[str, int] = {}
        by_domain: dict[str, int] = {}
        for source in sources:
            state = states.get(source.id, SourceState(source.id))
            by_status[str(state.status)] += 1
            by_protocol[str(source.protocol)] = by_protocol.get(str(source.protocol), 0) + 1
            by_domain[str(source.data_domain)] = by_domain.get(str(source.data_domain), 0) + 1
        connected, total = self.connected_count()
        last_ingests = [s.last_ingest_at for s in states.values() if s.last_ingest_at]
        return {
            "sources": total,
            "builtin": sum(1 for s in sources if s.builtin),
            "user": sum(1 for s in sources if not s.builtin),
            "enabled": sum(1 for s in sources if s.enabled),
            "connected": connected,
            "by_status": by_status,
            "by_protocol": by_protocol,
            "by_domain": by_domain,
            "subscriptions": len(self.list_subscriptions()),
            "last_ingest": max(last_ingests).isoformat() if last_ingests else None,
            "rows_ingested": sum(s.rows_ingested for s in states.values()),
        }

    def stale_sources(self, *, min_tolerance_seconds: float = 900.0) -> list[dict[str, Any]]:
        """Return the enabled sources whose last ingest is overdue.

        A periodic source is overdue when nothing has been ingested for more
        than twice its period (or ``min_tolerance_seconds``, whichever is
        larger); a streaming source is overdue after ``min_tolerance_seconds``.
        Manual and on-change sources are never overdue - nothing promised a
        cadence. A source that has never ingested is overdue only once its
        first due time plus the tolerance has passed, so a freshly seeded
        catalog does not report a wall of false alarms.
        """
        now = _utcnow()
        states = self.states()
        out: list[dict[str, Any]] = []
        for source in self.list_sources():
            if not source.enabled:
                continue
            schedule = source.parsed_schedule
            if schedule.kind in (ScheduleKind.MANUAL, ScheduleKind.ON_CHANGE):
                continue
            if schedule.kind is ScheduleKind.STREAMING:
                tolerance = min_tolerance_seconds
            elif schedule.interval_seconds:
                tolerance = max(min_tolerance_seconds, 2 * schedule.interval_seconds)
            else:  # cron
                nxt = schedule.next_run_after(None, now=now)
                following = schedule.next_run_after(nxt, now=now) if nxt else None
                period = (following - nxt).total_seconds() if (nxt and following) else 3600.0
                tolerance = max(min_tolerance_seconds, 2 * period)

            state = states.get(source.id, SourceState(source.id))
            reference = state.last_ingest_at or source.created_at
            if reference is None:
                continue
            age = (now - reference).total_seconds()
            if age <= tolerance:
                continue
            out.append(
                {
                    "source_id": source.id,
                    "name": source.name,
                    "domain": str(source.data_domain),
                    "schedule": schedule.label,
                    "scope": list(source.scope),
                    "items": len(source.scope) or 1,
                    "last_ingest": state.last_ingest_at.isoformat()
                    if state.last_ingest_at
                    else None,
                    "age_seconds": round(age, 1),
                    "tolerance_seconds": round(tolerance, 1),
                    "never_ingested": state.last_ingest_at is None,
                    "status": str(state.status),
                    "last_error": state.last_error,
                }
            )
        return out

    # --- events -----------------------------------------------------------
    async def _publish(self, payload: dict[str, Any]) -> None:
        """Publish one catalog event on the ``ingest`` channel."""
        if self.bus is not None:
            await self.bus.publish(INGEST_CHANNEL, payload)

    def _publish_nowait(self, payload: dict[str, Any]) -> None:
        """Publish from synchronous code when a loop is running; otherwise skip."""
        if self.bus is None:
            return
        try:
            self.bus.publish_nowait(INGEST_CHANNEL, payload)
        except RuntimeError:
            logger.debug("catalog: no running loop; %s not published", payload.get("event"))


def status_from_test(result: ConnectionTestResult) -> SourceStatus:
    """Map a connection-test outcome onto a catalog status.

    ``authenticated is False`` with nothing reachable means the source is not
    configured (no credentials), which is a different thing from a source whose
    endpoint is down.
    """
    if result.ok:
        return SourceStatus.CONNECTED
    if result.authenticated is False and result.reachable is None:
        return SourceStatus.NOT_CONFIGURED
    if result.reachable is False:
        return SourceStatus.DISCONNECTED
    return SourceStatus.DEGRADED


async def _safe_close(connector: Connector) -> None:
    """Close a connector without letting shutdown errors mask a result."""
    try:
        await connector.close()
    except Exception:  # noqa: BLE001 - closing must never raise into the caller
        logger.debug("catalog: closing %r failed", connector, exc_info=True)


def utc_age(moment: datetime | None, *, now: datetime | None = None) -> timedelta | None:
    """Return how long ago ``moment`` was, or ``None`` when it is unknown."""
    if moment is None:
        return None
    return (now or _utcnow()) - moment


def iter_enabled(sources: Iterable[SourceConfig]) -> list[SourceConfig]:
    """Return only the enabled sources of ``sources``."""
    return [source for source in sources if source.enabled]


__all__ = [
    "GRANULARITIES",
    "GRANULARITY_OPTIONS",
    "INGEST_CHANNEL",
    "IngestSourceRow",
    "IngestSourceStateRow",
    "IngestSubscriptionRow",
    "SourceCatalog",
    "SourceState",
    "Subscription",
    "bar_interval_for",
    "builtin_sources",
    "granularity_options",
    "parse_granularity",
    "status_from_test",
]
