"""``SourceConfig`` - the ingestion source definition.

One field per control on the Data Ingestion form (``docs/SPEC.md`` section 4). The
``protocol`` field selects the connector class used to talk to the source.

The form posts the human labels shown in the REFERENCE UI (``"Prices &
quotes"``, ``"Every 5 minutes"``, ``"Last 10 years"``...). Every enum in this
module therefore carries a label table and :func:`SourceConfig.from_form`
accepts either the label or the canonical value, so the API can round-trip a
form payload without the UI having to know the internal vocabulary.

Field <-> control map (REFERENCE ``#ingestionForm``)::

    sourceName        -> name
    sourceDomain      -> data_domain
    sourceProtocol    -> protocol
    sourceEndpoint    -> endpoint
    sourceAuth        -> auth.type
    sourceFormat      -> format
    sourceFrequency   -> schedule
    sourceScope       -> scope_label (+ scope, the resolved items)
    sourceTimezone    -> source_tz
    sourceRetries     -> retry_policy.mode
    backfillEnabled   -> backfill.enabled
    backfillRange     -> backfill.range
    backfillBatch     -> backfill.batch
    advancedConfig    -> advanced.{headers,query_params,subscriptions,sql,glob,
                                   pagination,schema_mapping,...}
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, ClassVar, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)


def _norm(value: Any) -> str:
    """Normalise a label for lookup: lowercase, collapsed whitespace."""
    text = str(value).strip().lower()
    text = text.replace("…", "")  # the form's trailing ellipsis
    return re.sub(r"\s+", " ", text).strip(" .")


class Protocol(StrEnum):
    """Transport protocol; selects the connector implementation."""

    WEBSOCKET = "websocket"
    REST = "rest"
    POLLING = "polling"
    DATABASE = "database"
    FILE = "file"
    LIBRARY = "library"

    @property
    def label(self) -> str:
        """The label the Data Ingestion form shows for this protocol."""
        return _PROTOCOL_LABELS[self]

    @property
    def can_backfill(self) -> bool:
        """Whether this protocol's connector implements a historical backfill.

        WebSocket and polling connectors only ever see what the vendor is
        publishing now, so they inherit the base ``NotImplementedError``. The
        Source Catalog reads this to decide whether to offer FROM/TO at all.
        """
        return self in _BACKFILL_PROTOCOLS

    @classmethod
    def parse(cls, value: Any) -> Protocol:
        """Parse a canonical value or a form label.

        Raises:
            ValueError: when the value names no known protocol.
        """
        return _parse_enum(cls, _PROTOCOL_ALIASES, value, "protocol")


#: Protocols whose connector overrides ``Connector.backfill``. Kept beside the
#: enum so ``Protocol.can_backfill`` cannot drift from the connector package.
#: ``POLLING`` is in the set because ``PollingConnector`` extends
#: ``RestConnector`` and therefore walks the same history windows; only
#: ``WEBSOCKET`` derives straight from ``Connector`` and keeps the base's
#: ``NotImplementedError``.
_BACKFILL_PROTOCOLS: frozenset[Protocol] = frozenset(
    {Protocol.REST, Protocol.POLLING, Protocol.DATABASE, Protocol.FILE, Protocol.LIBRARY}
)

_PROTOCOL_LABELS: dict[Protocol, str] = {
    Protocol.WEBSOCKET: "WebSocket stream",
    Protocol.REST: "REST API",
    Protocol.POLLING: "Polling API",
    Protocol.DATABASE: "Database",
    Protocol.FILE: "Database file / flat file",
    Protocol.LIBRARY: "Python library",
}

_PROTOCOL_ALIASES: dict[str, Protocol] = {
    "websocket": Protocol.WEBSOCKET,
    "websocket stream": Protocol.WEBSOCKET,
    "ws": Protocol.WEBSOCKET,
    "stream": Protocol.WEBSOCKET,
    "rest": Protocol.REST,
    "rest api": Protocol.REST,
    "http": Protocol.REST,
    "polling": Protocol.POLLING,
    "polling api": Protocol.POLLING,
    "poll": Protocol.POLLING,
    "database": Protocol.DATABASE,
    "sql": Protocol.DATABASE,
    "file": Protocol.FILE,
    "database file / flat file": Protocol.FILE,
    "flat file": Protocol.FILE,
    "library": Protocol.LIBRARY,
    "python library": Protocol.LIBRARY,
}


class DataDomain(StrEnum):
    """What kind of data a source carries."""

    MARKET = "market"
    FUNDAMENTALS = "fundamentals"
    MACRO = "macro"
    NEWS = "news"
    ALTERNATIVE = "alternative"
    REFERENCE = "reference"

    @property
    def label(self) -> str:
        """The label the Data Ingestion form shows for this domain."""
        return _DOMAIN_LABELS[self]

    @classmethod
    def parse(cls, value: Any) -> DataDomain:
        """Parse a canonical value or a form label."""
        return _parse_enum(cls, _DOMAIN_ALIASES, value, "data domain")


_DOMAIN_LABELS: dict[DataDomain, str] = {
    DataDomain.MARKET: "Prices & quotes",
    DataDomain.FUNDAMENTALS: "Fundamentals",
    DataDomain.MACRO: "Macro / economic",
    DataDomain.NEWS: "News",
    DataDomain.ALTERNATIVE: "Alternative data",
    DataDomain.REFERENCE: "Custom dataset",
}

_DOMAIN_ALIASES: dict[str, DataDomain] = {
    "market": DataDomain.MARKET,
    "prices & quotes": DataDomain.MARKET,
    "prices and quotes": DataDomain.MARKET,
    "prices": DataDomain.MARKET,
    "quotes": DataDomain.MARKET,
    "fundamentals": DataDomain.FUNDAMENTALS,
    "macro": DataDomain.MACRO,
    "macro / economic": DataDomain.MACRO,
    "macro/economic": DataDomain.MACRO,
    "economic": DataDomain.MACRO,
    "news": DataDomain.NEWS,
    "alternative": DataDomain.ALTERNATIVE,
    "alternative data": DataDomain.ALTERNATIVE,
    "alt data": DataDomain.ALTERNATIVE,
    "reference": DataDomain.REFERENCE,
    "custom dataset": DataDomain.REFERENCE,
    "custom": DataDomain.REFERENCE,
}


class AuthType(StrEnum):
    """Authentication strategy names (see ``tradinglib.data.ingest.auth``)."""

    NONE = "none"
    API_KEY = "api_key"
    OAUTH = "oauth"
    BASIC = "basic"
    SECRET = "secret"

    @property
    def label(self) -> str:
        """The label the Data Ingestion form shows for this strategy."""
        return _AUTH_LABELS[self]

    @classmethod
    def parse(cls, value: Any) -> AuthType:
        """Parse a canonical value or a form label."""
        return _parse_enum(cls, _AUTH_ALIASES, value, "authentication")


_AUTH_LABELS: dict[AuthType, str] = {
    AuthType.NONE: "None",
    AuthType.API_KEY: "API key",
    AuthType.OAUTH: "OAuth 2.0",
    AuthType.BASIC: "Basic auth",
    AuthType.SECRET: "Connection secret",
}

_AUTH_ALIASES: dict[str, AuthType] = {
    "none": AuthType.NONE,
    "": AuthType.NONE,
    "api key": AuthType.API_KEY,
    "api_key": AuthType.API_KEY,
    "apikey": AuthType.API_KEY,
    "oauth": AuthType.OAUTH,
    "oauth 2.0": AuthType.OAUTH,
    "oauth2": AuthType.OAUTH,
    "basic": AuthType.BASIC,
    "basic auth": AuthType.BASIC,
    "secret": AuthType.SECRET,
    "connection secret": AuthType.SECRET,
}


class PayloadFormat(StrEnum):
    """Wire format of the source payload."""

    JSON = "json"
    NDJSON = "ndjson"
    CSV = "csv"
    PARQUET = "parquet"
    ARROW = "arrow"
    XML = "xml"
    TEXT = "text"
    ROWS = "rows"

    @property
    def label(self) -> str:
        """The label the Data Ingestion form shows for this format."""
        return _FORMAT_LABELS[self]

    @classmethod
    def parse(cls, value: Any) -> PayloadFormat:
        """Parse a canonical value or a form label."""
        return _parse_enum(cls, _FORMAT_ALIASES, value, "format")


_FORMAT_LABELS: dict[PayloadFormat, str] = {
    PayloadFormat.JSON: "JSON",
    PayloadFormat.NDJSON: "NDJSON",
    PayloadFormat.CSV: "CSV",
    PayloadFormat.PARQUET: "Parquet",
    PayloadFormat.ARROW: "Arrow",
    PayloadFormat.XML: "XML",
    PayloadFormat.TEXT: "Text",
    PayloadFormat.ROWS: "Database rows",
}

_FORMAT_ALIASES: dict[str, PayloadFormat] = {
    "json": PayloadFormat.JSON,
    "ndjson": PayloadFormat.NDJSON,
    "jsonl": PayloadFormat.NDJSON,
    "json lines": PayloadFormat.NDJSON,
    "csv": PayloadFormat.CSV,
    "tsv": PayloadFormat.CSV,
    "parquet": PayloadFormat.PARQUET,
    "arrow": PayloadFormat.ARROW,
    "feather": PayloadFormat.ARROW,
    "xml": PayloadFormat.XML,
    "text": PayloadFormat.TEXT,
    "plain text": PayloadFormat.TEXT,
    "rows": PayloadFormat.ROWS,
    "database rows": PayloadFormat.ROWS,
}


class SourceStatus(StrEnum):
    """Live status of a catalog source. Never guessed - always observed."""

    CONNECTED = "connected"
    DEGRADED = "degraded"
    DISCONNECTED = "disconnected"
    NOT_CONFIGURED = "not_configured"
    UNKNOWN = "unknown"

    @property
    def label(self) -> str:
        """Short label for the Configured ingestion sources table."""
        return {
            SourceStatus.CONNECTED: "Live",
            SourceStatus.DEGRADED: "Degraded",
            SourceStatus.DISCONNECTED: "Disconnected",
            SourceStatus.NOT_CONFIGURED: "Not configured",
            SourceStatus.UNKNOWN: "Unknown",
        }[self]


class ScheduleKind(StrEnum):
    """How a source is driven by :class:`~tradinglib.data.ingest.scheduler.IngestScheduler`."""

    STREAMING = "streaming"
    INTERVAL = "interval"
    CRON = "cron"
    ON_CHANGE = "on_change"
    MANUAL = "manual"


class RetryMode(StrEnum):
    """Retry/reconnect behaviour selected in the form."""

    EXPONENTIAL = "exponential"
    FIXED_30S = "fixed_30s"
    IMMEDIATE_5 = "immediate_5"
    MANUAL = "manual"

    @property
    def label(self) -> str:
        """The label the Data Ingestion form shows for this policy."""
        return _RETRY_LABELS[self]

    @classmethod
    def parse(cls, value: Any) -> RetryMode:
        """Parse a canonical value or a form label."""
        return _parse_enum(cls, _RETRY_ALIASES, value, "retry policy")


_RETRY_LABELS: dict[RetryMode, str] = {
    RetryMode.EXPONENTIAL: "Exponential backoff",
    RetryMode.FIXED_30S: "Fixed 30 seconds",
    RetryMode.IMMEDIATE_5: "Immediate, max 5",
    RetryMode.MANUAL: "Manual only",
}

_RETRY_ALIASES: dict[str, RetryMode] = {
    "exponential": RetryMode.EXPONENTIAL,
    "exponential backoff": RetryMode.EXPONENTIAL,
    "fixed_30s": RetryMode.FIXED_30S,
    "fixed 30 seconds": RetryMode.FIXED_30S,
    "immediate_5": RetryMode.IMMEDIATE_5,
    "immediate, max 5": RetryMode.IMMEDIATE_5,
    "immediate max 5": RetryMode.IMMEDIATE_5,
    "manual": RetryMode.MANUAL,
    "manual only": RetryMode.MANUAL,
}


def _parse_enum(enum_cls: Any, aliases: dict[str, Any], value: Any, what: str) -> Any:
    """Resolve ``value`` to a member of ``enum_cls`` via its alias table."""
    if isinstance(value, enum_cls):
        return value
    key = _norm(value)
    if key in aliases:
        return aliases[key]
    for member in enum_cls:
        if _norm(member.value) == key:
            return member
    raise ValueError(f"unknown {what}: {value!r}")


# --- schedules -----------------------------------------------------------
#: Canonical interval tokens accepted in ``SourceConfig.schedule``.
_INTERVAL_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(s|sec|secs|seconds?|m|min|mins?|minutes?|h|hours?|d|days?|w|wk|weeks?)$")

_UNIT_SECONDS: dict[str, float] = {
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
    "d": 86_400.0,
    "w": 604_800.0,
}

_SCHEDULE_ALIASES: dict[str, str] = {
    "streaming": "streaming",
    "stream": "streaming",
    "tick": "streaming",
    "real-time": "streaming",
    "every second": "1s",
    "every minute": "1m",
    "every 5 minutes": "5m",
    "every five minutes": "5m",
    "hourly": "1h",
    "every hour": "1h",
    "daily": "1d",
    "every day": "1d",
    "weekly": "7d",
    "on file change": "on_change",
    "on change": "on_change",
    "on_change": "on_change",
    "manual": "manual",
    "on demand": "manual",
    "none": "manual",
    "": "manual",
}


@dataclass(frozen=True, slots=True)
class Schedule:
    """A parsed schedule: what drives the source and how often."""

    kind: ScheduleKind
    raw: str
    interval_seconds: float | None = None
    cron: str | None = None

    @property
    def periodic(self) -> bool:
        """Whether the scheduler fires this source repeatedly on its own."""
        return self.kind in (ScheduleKind.INTERVAL, ScheduleKind.CRON)

    @property
    def label(self) -> str:
        """A human label matching the form's Frequency dropdown."""
        if self.kind is ScheduleKind.STREAMING:
            return "Streaming"
        if self.kind is ScheduleKind.ON_CHANGE:
            return "On file change"
        if self.kind is ScheduleKind.MANUAL:
            return "Manual"
        if self.kind is ScheduleKind.CRON:
            return f"Cron {self.cron}"
        seconds = self.interval_seconds or 0.0
        for label, value in (
            ("Every second", 1.0),
            ("Every minute", 60.0),
            ("Every 5 minutes", 300.0),
            ("Hourly", 3600.0),
            ("Daily", 86_400.0),
        ):
            if abs(seconds - value) < 1e-9:
                return label
        # A custom cadence ("45s", "4h", "3d") is a first-class schedule, so it
        # gets a label in its own units rather than a raw second count.
        for unit, size in (("d", 86_400.0), ("h", 3600.0), ("m", 60.0)):
            if seconds >= size and abs(seconds % size) < 1e-9:
                return f"Every {int(seconds / size)}{unit}"
        return f"Every {seconds:g}s"

    def next_run_after(
        self, previous: datetime | None, *, now: datetime | None = None
    ) -> datetime | None:
        """Return when this schedule next fires, or ``None`` when it never does.

        ``previous`` is the last fire time (``None`` before the first run, which
        makes an interval schedule due immediately). Streaming, on-change and
        manual schedules are not fired by the periodic scheduler and return
        ``None``.
        """
        moment = now or datetime.now(timezone.utc)
        if self.kind is ScheduleKind.INTERVAL and self.interval_seconds:
            if previous is None:
                return moment
            return previous + timedelta(seconds=self.interval_seconds)
        if self.kind is ScheduleKind.CRON and self.cron:
            from apscheduler.triggers.cron import CronTrigger

            trigger = CronTrigger.from_crontab(self.cron, timezone="UTC")
            nxt = trigger.get_next_fire_time(previous, moment)
            return nxt.astimezone(timezone.utc) if nxt is not None else None
        return None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable description of the schedule."""
        return {
            "kind": str(self.kind),
            "raw": self.raw,
            "label": self.label,
            "interval_seconds": self.interval_seconds,
            "cron": self.cron,
            "periodic": self.periodic,
        }


def parse_schedule(value: Any) -> Schedule:
    """Parse a schedule string (canonical token or form label).

    Accepted: ``streaming``, ``on_change``, ``manual``, interval tokens such as
    ``15s``/``5m``/``1h``/``1d``, ``cron:<expression>``, and the form's labels
    (``"Every 5 minutes"``, ``"Hourly"``...).

    Raises:
        ValueError: when the value is not a schedule this scheduler can run.
    """
    raw = str(value or "").strip()
    key = _norm(raw)
    canonical = _SCHEDULE_ALIASES.get(key, raw.strip() or "manual")
    lowered = canonical.strip().lower()

    if lowered in ("streaming", "stream"):
        return Schedule(kind=ScheduleKind.STREAMING, raw=raw or "streaming")
    if lowered in ("on_change", "on change"):
        return Schedule(kind=ScheduleKind.ON_CHANGE, raw=raw or "on_change")
    if lowered in ("manual", "none", ""):
        return Schedule(kind=ScheduleKind.MANUAL, raw=raw or "manual")
    if lowered.startswith("cron:"):
        expression = canonical.split(":", 1)[1].strip()
        if not expression:
            raise ValueError("cron schedule needs an expression, e.g. 'cron:*/5 * * * *'")
        from apscheduler.triggers.cron import CronTrigger

        try:
            CronTrigger.from_crontab(expression, timezone="UTC")
        except ValueError as exc:
            raise ValueError(f"invalid cron expression {expression!r}: {exc}") from exc
        return Schedule(kind=ScheduleKind.CRON, raw=raw, cron=expression)

    match = _INTERVAL_RE.match(lowered)
    if match:
        amount = float(match.group(1))
        unit = match.group(2)[0]
        seconds = amount * _UNIT_SECONDS[unit]
        if seconds <= 0:
            raise ValueError(f"schedule interval must be positive: {value!r}")
        return Schedule(kind=ScheduleKind.INTERVAL, raw=raw, interval_seconds=seconds)
    raise ValueError(f"unknown schedule: {value!r}")


def resolve_timezone(name: str) -> ZoneInfo | None:
    """Return the IANA zone named ``name``, or ``None`` when it is not one.

    The form offers ``"Exchange local"``, which is not a zone: callers that get
    ``None`` must treat naive vendor timestamps as UTC (and say so) rather than
    inventing an offset.
    """
    text = (name or "").strip()
    if not text or _norm(text) in ("exchange local", "local", "source local"):
        return None
    try:
        return ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        logger.warning("unknown source timezone %r; timestamps will be read as UTC", name)
        return None


@dataclass(slots=True)
class RetryPolicy:
    """Retry/backoff behaviour applied to a source's fetches."""

    mode: RetryMode = RetryMode.EXPONENTIAL
    max_attempts: int = 3
    backoff_seconds: float = 2.0
    backoff_multiplier: float = 2.0
    max_backoff_seconds: float = 60.0
    timeout_seconds: float = 30.0

    @classmethod
    def from_mode(cls, mode: RetryMode, *, timeout_seconds: float = 30.0) -> RetryPolicy:
        """Build the concrete policy the form's dropdown stands for."""
        if mode is RetryMode.FIXED_30S:
            return cls(
                mode=mode,
                max_attempts=3,
                backoff_seconds=30.0,
                backoff_multiplier=1.0,
                max_backoff_seconds=30.0,
                timeout_seconds=timeout_seconds,
            )
        if mode is RetryMode.IMMEDIATE_5:
            return cls(
                mode=mode,
                max_attempts=5,
                backoff_seconds=0.0,
                backoff_multiplier=1.0,
                max_backoff_seconds=0.0,
                timeout_seconds=timeout_seconds,
            )
        if mode is RetryMode.MANUAL:
            return cls(
                mode=mode,
                max_attempts=1,
                backoff_seconds=0.0,
                backoff_multiplier=1.0,
                max_backoff_seconds=0.0,
                timeout_seconds=timeout_seconds,
            )
        return cls(mode=mode, timeout_seconds=timeout_seconds)

    def delay_for(self, attempt: int) -> float:
        """Seconds to wait before retry ``attempt`` (1 = after the first failure)."""
        if attempt < 1 or self.backoff_seconds <= 0:
            return 0.0
        delay = self.backoff_seconds * (self.backoff_multiplier ** (attempt - 1))
        return min(delay, self.max_backoff_seconds) if self.max_backoff_seconds else delay

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping (used by the API and persistence)."""
        return {
            "mode": str(self.mode),
            "label": self.mode.label,
            "max_attempts": self.max_attempts,
            "backoff_seconds": self.backoff_seconds,
            "backoff_multiplier": self.backoff_multiplier,
            "max_backoff_seconds": self.max_backoff_seconds,
            "timeout_seconds": self.timeout_seconds,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> RetryPolicy:
        """Rebuild a policy from :meth:`to_dict` output."""
        if not data:
            return cls()
        mode = RetryMode.parse(data.get("mode", RetryMode.EXPONENTIAL))
        base = cls.from_mode(mode)
        return cls(
            mode=mode,
            max_attempts=int(data.get("max_attempts", base.max_attempts)),
            backoff_seconds=float(data.get("backoff_seconds", base.backoff_seconds)),
            backoff_multiplier=float(data.get("backoff_multiplier", base.backoff_multiplier)),
            max_backoff_seconds=float(data.get("max_backoff_seconds", base.max_backoff_seconds)),
            timeout_seconds=float(data.get("timeout_seconds", base.timeout_seconds)),
        )


#: A calendar quarter used as a history boundary, e.g. "2009-Q2".
_QUARTER_RE: Final[re.Pattern[str]] = re.compile(r"^(\d{4})-q([1-4])$")


def _parse_boundary(token: str) -> datetime | None:
    """Parse an absolute history boundary, or return ``None``.

    Accepts an ISO date or datetime (``"2016-01-04"``, ``"2016-01-04T00:00``
    ``:00+00:00"``) and a calendar quarter (``"2009-q2"``). Anything else -
    duration tokens such as ``"5y"``, or plain nonsense - is not a boundary and
    is left to the caller to interpret.
    """
    quarter = _QUARTER_RE.match(token)
    if quarter is not None:
        year = int(quarter.group(1))
        month = (int(quarter.group(2)) - 1) * 3 + 1
        return datetime(year, month, 1, tzinfo=timezone.utc)
    if not re.match(r"^\d{4}-\d{2}", token):
        return None
    try:
        parsed = datetime.fromisoformat(token.upper().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


#: Backfill range labels -> canonical tokens.
_RANGE_ALIASES: dict[str, str] = {
    "all available history": "all",
    "all": "all",
    "complete": "all",
    "last 10 years": "10y",
    "last 5 years": "5y",
    "last 1 year": "1y",
    "last year": "1y",
    "custom start date": "custom",
}

#: Backfill batch labels -> canonical tokens.
_BATCH_ALIASES: dict[str, str] = {
    "automatic": "auto",
    "auto": "auto",
    "1 day": "1d",
    "1 month": "1mo",
    "1 year": "1y",
    "10,000 rows": "10000rows",
    "10000 rows": "10000rows",
}

#: Canonical batch token -> (window, rows). ``None`` window = single request.
_BATCH_WINDOWS: dict[str, timedelta | None] = {
    "auto": None,
    "1d": timedelta(days=1),
    "1mo": timedelta(days=30),
    "1y": timedelta(days=365),
    "10000rows": None,
}


@dataclass(slots=True)
class BackfillConfig:
    """Historical backfill settings from the form's backfill section."""

    enabled: bool = False
    range: str = "1y"
    batch: str = "auto"
    batch_size: int = 500
    start_date: str | None = None

    @classmethod
    def from_form(
        cls, *, enabled: Any, range_value: Any, batch_value: Any, start_date: Any = None
    ) -> BackfillConfig:
        """Build the config from the three backfill controls."""
        range_token = _RANGE_ALIASES.get(_norm(range_value), str(range_value or "1y").strip())
        batch_token = _BATCH_ALIASES.get(_norm(batch_value), str(batch_value or "auto").strip())
        start = str(start_date).strip() if start_date else None
        if range_token == "custom" and not start:
            raise ValueError("a custom backfill range needs 'backfill_start' (an ISO date)")
        size = 10_000 if batch_token == "10000rows" else 500
        return cls(
            enabled=bool(enabled),
            range=range_token,
            batch=batch_token,
            batch_size=size,
            start_date=start,
        )

    @property
    def window(self) -> timedelta | None:
        """The time window each backfill batch covers, or ``None`` for one shot."""
        return _BATCH_WINDOWS.get(self.batch)

    def range_start(self, *, now: datetime | None = None) -> datetime | None:
        """Return the first timestamp the backfill should cover.

        ``None`` means "all available history" - the connector then asks the
        vendor for everything it has instead of guessing a start date.
        """
        moment = now or datetime.now(timezone.utc)
        token = self.range.strip().lower()
        if token in ("all", ""):
            return None
        # A literal calendar boundary: an ISO date/datetime ("2016-01-04") or a
        # quarter ("2009-q2"). The Source Catalog's FROM control sends these.
        absolute = _parse_boundary(token)
        if absolute is not None:
            return absolute
        if token == "custom" and self.start_date:
            try:
                parsed = datetime.fromisoformat(self.start_date)
            except ValueError as exc:
                raise ValueError(f"invalid backfill start date {self.start_date!r}") from exc
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        match = _INTERVAL_RE.match(token)
        if match:
            return moment - timedelta(
                seconds=float(match.group(1)) * _UNIT_SECONDS[match.group(2)[0]]
            )
        years = re.match(r"^(\d+)y$", token)
        if years:
            return moment - timedelta(days=365 * int(years.group(1)))
        months = re.match(r"^(\d+)mo$", token)
        if months:
            return moment - timedelta(days=30 * int(months.group(1)))
        raise ValueError(f"unknown backfill range: {self.range!r}")

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping."""
        return {
            "enabled": self.enabled,
            "range": self.range,
            "batch": self.batch,
            "batch_size": self.batch_size,
            "start_date": self.start_date,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> BackfillConfig:
        """Rebuild a config from :meth:`to_dict` output."""
        if not data:
            return cls()
        return cls(
            enabled=bool(data.get("enabled", False)),
            range=str(data.get("range", "1y")),
            batch=str(data.get("batch", "auto")),
            batch_size=int(data.get("batch_size", 500)),
            start_date=data.get("start_date"),
        )


@dataclass(slots=True)
class AdvancedConfig:
    """The form's advanced section: protocol-dependent extras.

    The textarea takes a JSON object; every key below is optional. Unknown keys
    are preserved in :attr:`extra` so a connector can read settings this class
    does not name (and the UI can round-trip them).
    """

    headers: dict[str, str] = field(default_factory=dict)
    query_params: dict[str, str] = field(default_factory=dict)
    subscriptions: list[str] = field(default_factory=list)
    sql: str | None = None
    glob: str | None = None
    pagination: dict[str, Any] = field(default_factory=dict)
    schema_mapping: dict[str, str] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    #: Keys consumed by named fields; everything else lands in ``extra``.
    NAMED: ClassVar[tuple[str, ...]] = (
        "headers",
        "query_params",
        "params",
        "subscriptions",
        "sql",
        "query",
        "glob",
        "pattern",
        "pagination",
        "schema_mapping",
        "mapping",
    )

    @classmethod
    def from_payload(cls, payload: Any) -> AdvancedConfig:
        """Build from the advanced textarea (a JSON object, or already-parsed).

        Raises:
            ValueError: when the text is not a JSON object.
        """
        if payload is None or payload == "":
            return cls()
        if isinstance(payload, AdvancedConfig):
            return payload
        data: Any = payload
        if isinstance(payload, (str, bytes)):
            text = payload.decode() if isinstance(payload, bytes) else payload
            if not text.strip():
                return cls()
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"advanced configuration must be a JSON object: {exc.msg} (line {exc.lineno})"
                ) from exc
        if not isinstance(data, dict):
            raise ValueError("advanced configuration must be a JSON object")

        headers = {str(k): str(v) for k, v in (data.get("headers") or {}).items()}
        raw_params = data.get("query_params") if "query_params" in data else data.get("params")
        params = {str(k): str(v) for k, v in (raw_params or {}).items()}
        subs_raw = data.get("subscriptions") or []
        if isinstance(subs_raw, str):
            subs = [s.strip() for s in subs_raw.split(",") if s.strip()]
        else:
            subs = [str(s).strip() for s in subs_raw if str(s).strip()]
        sql = data.get("sql") if data.get("sql") is not None else data.get("query")
        glob = data.get("glob") if data.get("glob") is not None else data.get("pattern")
        mapping_raw = (
            data.get("schema_mapping") if "schema_mapping" in data else data.get("mapping")
        )
        mapping = {str(k): str(v) for k, v in (mapping_raw or {}).items()}
        extra = {k: v for k, v in data.items() if k not in cls.NAMED}
        return cls(
            headers=headers,
            query_params=params,
            subscriptions=subs,
            sql=str(sql) if sql else None,
            glob=str(glob) if glob else None,
            pagination=dict(data.get("pagination") or {}),
            schema_mapping=mapping,
            extra=extra,
        )

    def get(self, key: str, default: Any = None) -> Any:
        """Read an unnamed advanced option from :attr:`extra`."""
        return self.extra.get(key, default)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of every advanced option."""
        payload: dict[str, Any] = {
            "headers": dict(self.headers),
            "query_params": dict(self.query_params),
            "subscriptions": list(self.subscriptions),
            "sql": self.sql,
            "glob": self.glob,
            "pagination": dict(self.pagination),
            "schema_mapping": dict(self.schema_mapping),
        }
        payload.update(self.extra)
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> AdvancedConfig:
        """Rebuild from :meth:`to_dict` output."""
        return cls.from_payload(data or {})


@dataclass(slots=True)
class AuthConfig:
    """Credential configuration; secrets are stored write-only and masked.

    Secret values live in the local SQLite application database next to the
    rest of the app state. They are never returned by the API: every
    serialisation path goes through :meth:`masked`.
    """

    type: AuthType = AuthType.NONE
    key_name: str | None = None
    key_value: str | None = None
    in_header: bool = True
    username: str | None = None
    password: str | None = None
    token_url: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    scopes: list[str] = field(default_factory=list)

    @property
    def configured(self) -> bool:
        """Whether the credentials this strategy needs are actually present."""
        if self.type is AuthType.NONE:
            return True
        if self.type is AuthType.API_KEY:
            return bool(self.key_value)
        if self.type is AuthType.BASIC:
            return bool(self.username and self.password)
        if self.type is AuthType.OAUTH:
            return bool(self.token_url and self.client_id and self.client_secret)
        if self.type is AuthType.SECRET:
            return bool(self.key_value)
        return False

    def masked(self) -> dict[str, Any]:
        """Return the config with every secret masked, for API responses."""
        from tradinglib.config import mask_secret

        return {
            "type": str(self.type),
            "label": self.type.label,
            "configured": self.configured,
            "key_name": self.key_name,
            "key_value": mask_secret(self.key_value),
            "key_set": bool(self.key_value),
            "in_header": self.in_header,
            "username": self.username,
            "password": mask_secret(self.password),
            "token_url": self.token_url,
            "client_id": self.client_id,
            "client_secret": mask_secret(self.client_secret),
            "scopes": list(self.scopes),
        }

    def to_dict(self, *, mask_secrets: bool = True) -> dict[str, Any]:
        """Serialise, masked by default; the unmasked form is for persistence."""
        if mask_secrets:
            return self.masked()
        return {
            "type": str(self.type),
            "key_name": self.key_name,
            "key_value": self.key_value,
            "in_header": self.in_header,
            "username": self.username,
            "password": self.password,
            "token_url": self.token_url,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "scopes": list(self.scopes),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> AuthConfig:
        """Rebuild from :meth:`to_dict(mask_secrets=False)` output."""
        if not data:
            return cls()
        return cls(
            type=AuthType.parse(data.get("type", AuthType.NONE)),
            key_name=data.get("key_name"),
            key_value=data.get("key_value"),
            in_header=bool(data.get("in_header", True)),
            username=data.get("username"),
            password=data.get("password"),
            token_url=data.get("token_url"),
            client_id=data.get("client_id"),
            client_secret=data.get("client_secret"),
            scopes=[str(s) for s in (data.get("scopes") or [])],
        )


def _split_scope(value: Any) -> list[str]:
    """Split a scope value (list, or comma/whitespace separated text) into items."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        items = [str(v).strip() for v in value]
    else:
        items = [part.strip() for part in re.split(r"[,\n;]+", str(value))]
    return [item for item in items if item]


@dataclass(slots=True)
class SourceConfig:
    """A saved ingestion source. One field per UI form control."""

    id: str
    name: str
    data_domain: DataDomain
    protocol: Protocol
    endpoint: str
    auth: AuthConfig = field(default_factory=AuthConfig)
    format: PayloadFormat = PayloadFormat.JSON
    schedule: str = "manual"
    scope: list[str] = field(default_factory=list)
    scope_label: str | None = None
    source_tz: str = "UTC"
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    backfill: BackfillConfig = field(default_factory=BackfillConfig)
    advanced: AdvancedConfig = field(default_factory=AdvancedConfig)
    provider: str | None = None
    builtin: bool = False
    enabled: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        """Validate the schedule eagerly so a bad value cannot be persisted."""
        parse_schedule(self.schedule)

    @property
    def parsed_schedule(self) -> Schedule:
        """The parsed :class:`Schedule` this source runs on."""
        return parse_schedule(self.schedule)

    @property
    def timezone(self) -> ZoneInfo | None:
        """The source's IANA timezone, or ``None`` when it is not a real zone."""
        return resolve_timezone(self.source_tz)

    def to_dict(self, *, mask_secrets: bool = True) -> dict[str, Any]:
        """Serialise for the API, masking credentials by default."""
        return {
            "id": self.id,
            "name": self.name,
            "data_domain": str(self.data_domain),
            "data_domain_label": self.data_domain.label,
            "protocol": str(self.protocol),
            "protocol_label": self.protocol.label,
            "can_backfill": self.protocol.can_backfill,
            "endpoint": self.endpoint,
            "auth": self.auth.to_dict(mask_secrets=mask_secrets),
            "format": str(self.format),
            "format_label": self.format.label,
            "schedule": self.schedule,
            "schedule_detail": self.parsed_schedule.as_dict(),
            "scope": list(self.scope),
            "scope_label": self.scope_label,
            "source_tz": self.source_tz,
            "retry_policy": self.retry_policy.to_dict(),
            "backfill": self.backfill.to_dict(),
            "advanced": self.advanced.to_dict(),
            "provider": self.provider,
            "builtin": self.builtin,
            "enabled": self.enabled,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SourceConfig:
        """Rebuild a config from :meth:`to_dict(mask_secrets=False)` output."""
        return cls(
            id=str(data["id"]),
            name=str(data["name"]),
            data_domain=DataDomain.parse(data["data_domain"]),
            protocol=Protocol.parse(data["protocol"]),
            endpoint=str(data.get("endpoint") or ""),
            auth=AuthConfig.from_dict(data.get("auth")),
            format=PayloadFormat.parse(data.get("format", PayloadFormat.JSON)),
            schedule=str(data.get("schedule", "manual")),
            scope=[str(s) for s in (data.get("scope") or [])],
            scope_label=data.get("scope_label"),
            source_tz=str(data.get("source_tz", "UTC")),
            retry_policy=RetryPolicy.from_dict(data.get("retry_policy")),
            backfill=BackfillConfig.from_dict(data.get("backfill")),
            advanced=AdvancedConfig.from_dict(data.get("advanced")),
            provider=data.get("provider"),
            builtin=bool(data.get("builtin", False)),
            enabled=bool(data.get("enabled", True)),
            created_at=_parse_dt(data.get("created_at")),
            updated_at=_parse_dt(data.get("updated_at")),
        )

    @classmethod
    def from_form(cls, payload: dict[str, Any]) -> SourceConfig:
        """Build a config from the Data Ingestion form payload.

        Accepts both the form's control ids (``sourceName``, ``sourceDomain``,
        ...) and the canonical field names, so the API can take either shape.

        Raises:
            ValueError: when a required field is missing or a protocol-specific
                field is inconsistent with the chosen protocol.
        """

        def pick(*names: str, default: Any = None) -> Any:
            for name in names:
                if name in payload and payload[name] not in (None, ""):
                    return payload[name]
            for name in names:
                if name in payload:
                    return payload[name]
            return default

        name = str(pick("name", "sourceName", default="") or "").strip()
        if not name:
            raise ValueError("source name is required")

        protocol = Protocol.parse(pick("protocol", "sourceProtocol", default="rest"))
        endpoint = str(pick("endpoint", "sourceEndpoint", default="") or "").strip()

        advanced = AdvancedConfig.from_payload(
            pick("advanced", "advancedConfig", "advanced_config", default=None)
        )

        auth = AuthConfig(
            type=AuthType.parse(pick("auth_type", "sourceAuth", default="none")),
            key_name=pick("auth_key_name", "key_name", default=None),
            key_value=pick("auth_key_value", "key_value", "api_key", default=None),
            in_header=bool(pick("auth_in_header", default=True)),
            username=pick("auth_username", "username", default=None),
            password=pick("auth_password", "password", default=None),
            token_url=pick("auth_token_url", "token_url", default=None),
            client_id=pick("auth_client_id", "client_id", default=None),
            client_secret=pick("auth_client_secret", "client_secret", default=None),
            scopes=_split_scope(pick("auth_scopes", "scopes", default=None)),
        )
        if isinstance(payload.get("auth"), dict):
            auth = AuthConfig.from_dict(payload["auth"])

        schedule_raw = pick("schedule", "sourceFrequency", default="manual")
        if _norm(schedule_raw) in ("custom cron", "cron"):
            cron = advanced.get("cron") or pick("cron", default=None)
            if not cron:
                raise ValueError(
                    "a custom cron schedule needs a 'cron' expression in the advanced configuration"
                )
            schedule = f"cron:{cron}"
        else:
            # Normalise the form label into its canonical token, then validate.
            schedule = _SCHEDULE_ALIASES.get(_norm(schedule_raw), str(schedule_raw).strip())
            parse_schedule(schedule)

        scope_label_raw = pick("scope_label", "sourceScope", default=None)
        scope_label = str(scope_label_raw).strip() if scope_label_raw else None
        scope_items = _split_scope(pick("scope", "symbols", "sourceSymbols", default=None))
        if not scope_items and scope_label and _norm(scope_label) not in (
            "custom symbols",
            "all available symbols",
        ):
            # A universe name is a label, not a symbol list; the scheduler
            # resolves it through the universe registry when one is wired in.
            scope_items = []

        backfill = BackfillConfig.from_form(
            enabled=pick("backfill_enabled", "backfillEnabled", default=False),
            range_value=pick("backfill_range", "backfillRange", default="1y"),
            batch_value=pick("backfill_batch", "backfillBatch", default="auto"),
            start_date=pick("backfill_start", "backfillStart", default=None)
            or advanced.get("backfill_start"),
        )
        if isinstance(payload.get("backfill"), dict):
            backfill = BackfillConfig.from_dict(payload["backfill"])

        if isinstance(payload.get("retry_policy"), dict):
            retry = RetryPolicy.from_dict(payload["retry_policy"])
        else:
            retry_mode = RetryMode.parse(pick("retry_policy", "sourceRetries", default="exponential"))
            retry = RetryPolicy.from_mode(retry_mode)

        config = cls(
            id=str(pick("id", default="") or "").strip() or _slug(name),
            name=name,
            data_domain=DataDomain.parse(pick("data_domain", "sourceDomain", default="market")),
            protocol=protocol,
            endpoint=endpoint,
            auth=auth,
            format=PayloadFormat.parse(pick("format", "sourceFormat", default="json")),
            schedule=schedule,
            scope=scope_items,
            scope_label=scope_label,
            source_tz=str(pick("source_tz", "sourceTimezone", default="UTC")),
            retry_policy=retry,
            backfill=backfill,
            advanced=advanced,
            provider=pick("provider", default=None),
            builtin=bool(pick("builtin", default=False)),
            enabled=bool(pick("enabled", default=True)),
        )
        config.validate()
        return config

    def validate(self) -> None:
        """Check the protocol-specific requirements.

        Raises:
            ValueError: when the configuration cannot be run as written.
        """
        if not self.name.strip():
            raise ValueError("source name is required")
        if self.provider is None and not self.endpoint.strip():
            raise ValueError("endpoint is required")
        parse_schedule(self.schedule)

        if self.protocol is Protocol.WEBSOCKET and self.provider is None:
            if not self.endpoint.startswith(("ws://", "wss://")):
                raise ValueError("a WebSocket source needs a ws:// or wss:// endpoint")
        if self.protocol in (Protocol.REST, Protocol.POLLING) and self.provider is None:
            if not self.endpoint.startswith(("http://", "https://")):
                raise ValueError(
                    f"a {self.protocol.label} source needs an http:// or https:// endpoint"
                )
        if self.protocol is Protocol.DATABASE and self.provider is None:
            if not self.advanced.sql:
                raise ValueError(
                    "a database source needs a 'sql' query in the advanced configuration"
                )
        if self.protocol is Protocol.FILE and self.provider is None:
            if not (self.advanced.glob or self.endpoint):
                raise ValueError("a file source needs a path or a 'glob' discovery pattern")
        if self.protocol is Protocol.LIBRARY and not (self.provider or self.endpoint):
            raise ValueError("a library source must name the provider it wraps")


def _slug(text: str) -> str:
    """Return a stable, filesystem/url-safe identifier for a source name."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")
    return slug or "source"


def _parse_dt(value: Any) -> datetime | None:
    """Parse an ISO timestamp into an aware UTC datetime, or ``None``."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


__all__ = [
    "AdvancedConfig",
    "AuthConfig",
    "AuthType",
    "BackfillConfig",
    "DataDomain",
    "PayloadFormat",
    "Protocol",
    "RetryMode",
    "RetryPolicy",
    "Schedule",
    "ScheduleKind",
    "SourceConfig",
    "SourceStatus",
    "parse_schedule",
    "resolve_timezone",
]
