"""Data-quality checks: completeness, staleness and outlier detection.

Every check reads what was actually ingested into
:class:`~tradinglib.data.store.TimeSeriesStore` (Parquet + DuckDB) and compares it
against a schedule the module derives itself:

* **completeness** - the expected observation grid for the dataset (US market
  sessions for daily bars, regular-hours buckets for intraday bars, the
  inferred publication cadence for named series) against what is present;
* **staleness** - how many scheduled observations have been published upstream
  since the newest stored point, rather than a naive wall-clock age that would
  call every price series "stale" over a weekend;
* **outliers** - a robust (median/MAD) z-score over returns, so a decimal-shift
  or a bad print stands out without a handful of volatile days poisoning the
  threshold the way a plain standard deviation would.

Results and incidents are persisted through :mod:`tradinglib.db`, and incidents are
published on the ``quality`` event channel that ``api/ws.py`` fans out.

Calendar caveats, stated rather than hidden: the NYSE calendar below encodes
the recurring holiday rules and the standard early closes; it does not know
about ad-hoc closures (national days of mourning, weather). Such a day shows up
as one missing session, which is the safe direction for a data-quality check.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from enum import StrEnum
from functools import lru_cache
from typing import Any, Final, Literal
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from tradinglib.data.base import BarInterval, to_utc, utcnow
from tradinglib.data.store import TimeSeriesStore, get_store
from tradinglib.db import Base, ensure_columns, get_engine, get_session_factory
from tradinglib.events import EventBus, get_event_bus

logger = logging.getLogger(__name__)

#: Exchange timezone for the US equity session grid.
MARKET_TZ: Final[ZoneInfo] = ZoneInfo("America/New_York")

#: Regular trading hours for US equities.
SESSION_OPEN: Final[time] = time(9, 30)
SESSION_CLOSE: Final[time] = time(16, 0)
EARLY_CLOSE: Final[time] = time(13, 0)

#: The event channel quality incidents are published on (``docs/SPEC.md`` section 5).
QUALITY_CHANNEL: Final[str] = "quality"

#: Review verbs accepted by :meth:`QualityEngine.decide_outlier`, mapped to the
#: state each one puts the flagged point into.
OUTLIER_DECISIONS: Final[dict[str, str]] = {
    "quarantine": "quarantined",
    "accept": "accepted",
    "escalate": "escalated",
    "open": "open",
}

#: States that still count as "awaiting review". ``escalated`` is deliberately
#: one of them: escalating asks for a human, it does not resolve the point, and
#: unlike ``quarantined`` it leaves the value in the data.
OPEN_OUTLIER_STATES: Final[tuple[str, ...]] = ("open", "escalated")


class Severity(StrEnum):
    """Incident severity levels."""

    INFO = "info"
    WARN = "warn"
    ERROR = "error"


# --------------------------------------------------------------------------
# US trading calendars
# --------------------------------------------------------------------------
def easter_sunday(year: int) -> date:
    """Return Gregorian Easter Sunday for ``year`` (anonymous Gregorian algorithm)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    month, day = divmod(h + m - 7 * n + 114, 31)
    return date(year, month, day + 1)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """Return the ``n``-th ``weekday`` (Mon=0) of a month."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """Return the last ``weekday`` (Mon=0) of a month."""
    if month == 12:
        following = date(year + 1, 1, 1)
    else:
        following = date(year, month + 1, 1)
    last = following - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(day: date) -> date | None:
    """Apply the US weekend-observance rule to a fixed-date holiday.

    Saturday holidays move to the preceding Friday and Sunday holidays to the
    following Monday. A 1 January Saturday is the exception: US exchanges do
    not close on 31 December for it, so ``None`` is returned.
    """
    if day.weekday() == 5:
        if day.month == 1 and day.day == 1:
            return None
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


@lru_cache(maxsize=256)
def us_federal_holidays(year: int) -> frozenset[date]:
    """Return the observed US federal holidays for ``year``."""
    days: list[date | None] = [
        _observed(date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3),  # Martin Luther King Jr. Day
        _nth_weekday(year, 2, 0, 3),  # Washington's Birthday
        _last_weekday(year, 5, 0),  # Memorial Day
        _observed(date(year, 6, 19)) if year >= 2021 else None,  # Juneteenth
        _observed(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),  # Labor Day
        _nth_weekday(year, 10, 0, 2),  # Columbus Day
        _observed(date(year, 11, 11)),  # Veterans Day
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving
        _observed(date(year, 12, 25)),
    ]
    return frozenset(day for day in days if day is not None)


@lru_cache(maxsize=256)
def nyse_holidays(year: int) -> frozenset[date]:
    """Return NYSE holidays: the federal set without Columbus/Veterans, plus Good Friday."""
    federal = set(us_federal_holidays(year))
    federal.discard(_nth_weekday(year, 10, 0, 2))
    veterans = _observed(date(year, 11, 11))
    if veterans is not None:
        federal.discard(veterans)
    federal.add(easter_sunday(year) - timedelta(days=2))  # Good Friday
    return frozenset(federal)


@lru_cache(maxsize=256)
def bond_market_holidays(year: int) -> frozenset[date]:
    """Return the SIFMA-style bond-market holidays (federal set plus Good Friday).

    FRED's daily financial series (treasury yields, rates) publish on this
    calendar, which is why it is the expectation used for business-daily series
    rather than the NYSE one.
    """
    return frozenset(us_federal_holidays(year) | {easter_sunday(year) - timedelta(days=2)})


@lru_cache(maxsize=256)
def nyse_early_closes(year: int) -> frozenset[date]:
    """Return the recurring NYSE 13:00 early closes for ``year``.

    These are the day after Thanksgiving, 24 December when it is a weekday and
    Christmas is observed on the 25th, and 3 July when Independence Day falls on
    a weekday other than Monday.
    """
    days: set[date] = set()
    thanksgiving = _nth_weekday(year, 11, 3, 4)
    days.add(thanksgiving + timedelta(days=1))

    christmas_eve = date(year, 12, 24)
    if christmas_eve.weekday() < 5 and date(year, 12, 25).weekday() < 5:
        days.add(christmas_eve)

    july_3 = date(year, 7, 3)
    if date(year, 7, 4).weekday() < 5 and july_3.weekday() < 5:
        days.add(july_3)

    holidays = nyse_holidays(year)
    return frozenset(d for d in days if d not in holidays and d.weekday() < 5)


#: Calendar identifiers understood by :func:`business_days`.
CalendarName = Literal["nyse", "bond", "federal", "weekday"]


def is_business_day(day: date, calendar: CalendarName = "nyse") -> bool:
    """Whether ``day`` is a trading/publication day on ``calendar``."""
    if day.weekday() >= 5:
        return False
    if calendar == "weekday":
        return True
    if calendar == "nyse":
        return day not in nyse_holidays(day.year)
    if calendar == "bond":
        return day not in bond_market_holidays(day.year)
    return day not in us_federal_holidays(day.year)


def business_days(start: date, end: date, calendar: CalendarName = "nyse") -> list[date]:
    """Return every business day in ``[start, end]`` on ``calendar``."""
    if end < start:
        return []
    out: list[date] = []
    day = start
    while day <= end:
        if is_business_day(day, calendar):
            out.append(day)
        day += timedelta(days=1)
    return out


def session_close(day: date) -> datetime:
    """Return the UTC closing time of the US equity session on ``day``."""
    closing = EARLY_CLOSE if day in nyse_early_closes(day.year) else SESSION_CLOSE
    return datetime.combine(day, closing, tzinfo=MARKET_TZ).astimezone(timezone.utc)


def session_bar_starts(day: date, seconds: int) -> list[datetime]:
    """Return the UTC start timestamps of every intraday bucket on ``day``.

    Buckets start at 09:30 ET and repeat every ``seconds`` until the close; a
    final partial bucket (the 15:30-16:00 half hour of an hourly grid) counts,
    because vendors publish it.
    """
    if seconds <= 0:
        return []
    open_dt = datetime.combine(day, SESSION_OPEN, tzinfo=MARKET_TZ)
    close_dt = datetime.combine(
        day, EARLY_CLOSE if day in nyse_early_closes(day.year) else SESSION_CLOSE, tzinfo=MARKET_TZ
    )
    total = (close_dt - open_dt).total_seconds()
    count = int(math.ceil(total / seconds))
    return [
        (open_dt + timedelta(seconds=seconds * i)).astimezone(timezone.utc)
        for i in range(max(count, 0))
    ]


# --------------------------------------------------------------------------
# dataset references and cadences
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class DatasetRef:
    """Identifies one checkable dataset in the time-series store.

    ``kind="bars"`` uses ``symbol``/``interval``; ``kind="series"`` uses
    ``namespace``/``name``. The :attr:`key` is the stable string the incidents
    table, the API and the UI use.
    """

    kind: Literal["bars", "series"]
    symbol: str | None = None
    interval: BarInterval = BarInterval.DAY_1
    namespace: str | None = None
    name: str | None = None

    @classmethod
    def bars(cls, symbol: str, interval: BarInterval = BarInterval.DAY_1) -> DatasetRef:
        """Reference the stored bars of one symbol at one interval."""
        return cls(kind="bars", symbol=symbol.upper(), interval=interval)

    @classmethod
    def series(cls, namespace: str, name: str) -> DatasetRef:
        """Reference one stored named series."""
        return cls(kind="series", namespace=namespace, name=name)

    @classmethod
    def parse(cls, key: str) -> DatasetRef:
        """Rebuild a reference from its :attr:`key`.

        Raises:
            ValueError: when the key is not a dataset key.
        """
        parts = key.split(":")
        if len(parts) == 3 and parts[0] == "bars":
            return cls.bars(parts[1], BarInterval(parts[2]))
        if len(parts) == 3 and parts[0] == "series":
            return cls.series(parts[1], parts[2])
        raise ValueError(f"not a dataset key: {key!r}")

    @property
    def key(self) -> str:
        """Stable identifier, e.g. ``bars:AAPL:1d`` or ``series:fred:DGS10``."""
        if self.kind == "bars":
            return f"bars:{self.symbol}:{self.interval}"
        return f"series:{self.namespace}:{self.name}"

    @property
    def label(self) -> str:
        """Human-readable label for the UI."""
        if self.kind == "bars":
            return f"{self.symbol} {self.interval}"
        return f"{self.namespace}/{self.name}"

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of the reference."""
        return {
            "key": self.key,
            "kind": self.kind,
            "label": self.label,
            "symbol": self.symbol,
            "interval": str(self.interval) if self.kind == "bars" else None,
            "namespace": self.namespace,
            "name": self.name,
        }


@dataclass(frozen=True, slots=True)
class Cadence:
    """The publication schedule a dataset is expected to follow."""

    #: ``sessions`` (one point per trading day), ``business`` (bond calendar),
    #: ``weekly``, ``monthly``, ``quarterly``, ``annual`` or ``fixed``.
    kind: str
    seconds: float
    label: str
    calendar: CalendarName = "nyse"

    @property
    def daily_or_slower(self) -> bool:
        """Whether points are compared by calendar date rather than timestamp."""
        return self.seconds >= 86_400 - 1


#: Cadence of each bar interval.
_BAR_CADENCES: Final[dict[BarInterval, Cadence]] = {
    BarInterval.DAY_1: Cadence("sessions", 86_400, "one bar per trading session"),
    BarInterval.WEEK_1: Cadence("weekly", 604_800, "one bar per trading week"),
    BarInterval.MONTH_1: Cadence("monthly", 2_592_000, "one bar per calendar month"),
}


def bar_cadence(interval: BarInterval) -> Cadence:
    """Return the expected cadence of bars at ``interval``."""
    known = _BAR_CADENCES.get(interval)
    if known is not None:
        return known
    return Cadence(
        "fixed",
        float(interval.approx_seconds),
        f"one bar per {interval} of regular trading hours",
    )


def infer_cadence(timestamps: Sequence[datetime]) -> Cadence | None:
    """Infer the publication cadence of a series from its own timestamps.

    The median gap is used (robust to holidays and to a single late release),
    and classified into the nearest standard cadence. ``None`` is returned when
    there are fewer than three points - too little to claim a schedule.
    """
    if len(timestamps) < 3:
        return None
    ordered = sorted(to_utc(ts) for ts in timestamps)
    gaps = [
        (b - a).total_seconds() for a, b in zip(ordered, ordered[1:], strict=False) if b > a
    ]
    if not gaps:
        return None
    median = float(np.median(gaps))
    if median < 86_000:
        return Cadence("fixed", median, f"one point per {median / 3600:.2f}h")
    if median <= 1.8 * 86_400:
        weekend_gaps = sum(1 for gap in gaps if gap >= 2.5 * 86_400)
        if weekend_gaps >= max(1, len(gaps) // 20):
            return Cadence("business", 86_400, "one point per business day", calendar="bond")
        return Cadence("daily", 86_400, "one point per calendar day", calendar="weekday")
    if median <= 10 * 86_400:
        return Cadence("weekly", 604_800, "one point per week")
    if median <= 45 * 86_400:
        return Cadence("monthly", 2_592_000, "one point per month")
    if median <= 135 * 86_400:
        return Cadence("quarterly", 7_776_000, "one point per quarter")
    if median <= 400 * 86_400:
        return Cadence("annual", 31_536_000, "one point per year")
    return Cadence("fixed", median, f"one point per {median / 86_400:.0f} days")


def _month_steps(first: datetime, last: datetime, months: int) -> list[datetime]:
    """Return timestamps stepping ``months`` at a time from ``first`` to ``last``."""
    out: list[datetime] = []
    year, month, day = first.year, first.month, first.day
    current = first
    step = 0
    while current <= last:
        out.append(current)
        step += 1
        total = (month - 1) + months * step
        y = year + total // 12
        m = total % 12 + 1
        # Clamp to the length of the target month (31 Jan + 1 month -> 28/29 Feb).
        last_day = (date(y + (m == 12), (m % 12) + 1, 1) - timedelta(days=1)).day
        current = first.replace(year=y, month=m, day=min(day, last_day))
    return out


def expected_timestamps(cadence: Cadence, first: datetime, last: datetime) -> list[datetime]:
    """Return the expected observation grid between ``first`` and ``last``.

    Calendar cadences produce one timestamp per expected date (at ``first``'s
    time of day); fixed cadences step by the cadence length.
    """
    first, last = to_utc(first), to_utc(last)
    if last < first:
        return []
    if cadence.kind in {"sessions", "business", "daily"}:
        calendar: CalendarName = (
            "nyse" if cadence.kind == "sessions" else ("weekday" if cadence.kind == "daily" else "bond")
        )
        days = business_days(first.date(), last.date(), calendar)
        return [datetime.combine(day, first.timetz()) for day in days]
    if cadence.kind == "weekly":
        out: list[datetime] = []
        current = first
        while current <= last:
            out.append(current)
            current += timedelta(days=7)
        return out
    if cadence.kind in {"monthly", "quarterly", "annual"}:
        months = {"monthly": 1, "quarterly": 3, "annual": 12}[cadence.kind]
        return _month_steps(first, last, months)
    step = timedelta(seconds=max(cadence.seconds, 1.0))
    out = []
    current = first
    while current <= last:
        out.append(current)
        current += step
    return out


def expected_bar_timestamps(
    interval: BarInterval, first: datetime, last: datetime
) -> list[datetime]:
    """Return the expected bar grid for an interval between two timestamps.

    Intraday grids are built from regular-hours buckets on real sessions, so a
    weekend or a half-day never counts as missing data.
    """
    first, last = to_utc(first), to_utc(last)
    if last < first:
        return []
    if not interval.intraday:
        return expected_timestamps(bar_cadence(interval), first, last)
    out: list[datetime] = []
    for day in business_days(first.date() - timedelta(days=1), last.date() + timedelta(days=1)):
        for ts in session_bar_starts(day, int(interval.approx_seconds)):
            if first <= ts <= last:
                out.append(ts)
    return sorted(out)


# --------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class QualityCheckResult:
    """Outcome of one check against one dataset."""

    check: str
    dataset: str
    passed: bool
    score: float
    detail: str
    observed_at: datetime
    metrics: dict[str, Any] = field(default_factory=dict)
    severity: Severity = Severity.INFO

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of the result."""
        return {
            "check": self.check,
            "dataset": self.dataset,
            "passed": self.passed,
            "score": self.score,
            "detail": self.detail,
            "severity": str(self.severity),
            "observed_at": self.observed_at.isoformat(),
            "metrics": self.metrics,
        }


@dataclass(frozen=True, slots=True)
class QualityIncident:
    """A recorded quality problem, shown in the incidents table."""

    id: str
    dataset: str
    check: str
    severity: Severity
    message: str
    opened_at: datetime
    resolved_at: datetime | None = None
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def open(self) -> bool:
        """Whether the incident is still unresolved."""
        return self.resolved_at is None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of the incident."""
        return {
            "id": self.id,
            "dataset": self.dataset,
            "check": self.check,
            "severity": str(self.severity),
            "message": self.message,
            "opened_at": self.opened_at.isoformat(),
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "status": "open" if self.open else "resolved",
            "metrics": self.metrics,
        }


@dataclass(frozen=True, slots=True)
class DatasetQuality:
    """The full check suite plus composite score for one dataset."""

    ref: DatasetRef
    score: float
    results: list[QualityCheckResult]
    observed_at: datetime

    @property
    def failed(self) -> list[QualityCheckResult]:
        """The failing checks in this run."""
        return [r for r in self.results if not r.passed]

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping for the API layer."""
        return {
            **self.ref.as_dict(),
            "score": self.score,
            "observed_at": self.observed_at.isoformat(),
            "passed": not self.failed,
            "checks": [r.as_dict() for r in self.results],
        }


@dataclass(frozen=True, slots=True)
class QualityConfig:
    """Thresholds and weights for the check suite.

    The weights sum to 1 and turn the three check scores into the 0-100
    composite the Data Quality page shows.
    """

    z_threshold: float = 6.0
    min_points_for_outliers: int = 30
    completeness_warn: float = 0.995
    completeness_error: float = 0.97
    staleness_warn_periods: int = 1
    staleness_error_periods: int = 3
    outlier_warn_ratio: float = 0.0
    outlier_error_ratio: float = 0.005
    weight_completeness: float = 0.45
    weight_staleness: float = 0.30
    weight_outliers: float = 0.25
    max_missing_listed: int = 20

    def composite(self, completeness: float, staleness: float, outliers: float) -> float:
        """Return the 0-100 composite score for three check scores."""
        total = self.weight_completeness + self.weight_staleness + self.weight_outliers
        weighted = (
            self.weight_completeness * completeness
            + self.weight_staleness * staleness
            + self.weight_outliers * outliers
        )
        return round(100.0 * weighted / total, 2)


# --------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------
class QualityCheckRow(Base):
    """One persisted check result (the history behind the trend charts)."""

    __tablename__ = "quality_checks"

    id: Mapped[int] = mapped_column(Integer(), primary_key=True, autoincrement=True)
    dataset: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    check: Mapped[str] = mapped_column(String(32), nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean(), nullable=False)
    score: Mapped[float] = mapped_column(Float(), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default=str(Severity.INFO))
    detail: Mapped[str] = mapped_column(Text(), nullable=False, default="")
    observed_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, index=True)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON(), nullable=False, default=dict)


class QualityScoreRow(Base):
    """One persisted composite dataset score."""

    __tablename__ = "quality_scores"

    id: Mapped[int] = mapped_column(Integer(), primary_key=True, autoincrement=True)
    dataset: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="bars")
    label: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    score: Mapped[float] = mapped_column(Float(), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, index=True)


class QualitySchemaRow(Base):
    """The last observed shape of a dataset, used to detect schema drift."""

    __tablename__ = "quality_schemas"

    dataset: Mapped[str] = mapped_column(String(128), primary_key=True)
    signature: Mapped[str] = mapped_column(String(64), nullable=False)
    columns: Mapped[list[str]] = mapped_column(JSON(), nullable=False, default=list)
    dtypes: Mapped[dict[str, str]] = mapped_column(JSON(), nullable=False, default=dict)
    observed_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)


class QualityOutlierRow(Base):
    """One flagged observation awaiting (or carrying) a review decision.

    This is the Outlier review queue: ``check_outliers`` produces the flags,
    they are persisted here once, and a person quarantines or accepts each one.
    A quarantined row is excluded from every store read, so the decision has a
    real effect on features rather than only on a badge.
    """

    __tablename__ = "quality_outliers"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    dataset: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    ts: Mapped[datetime] = mapped_column(DateTime(), nullable=False, index=True)
    value: Mapped[float | None] = mapped_column(Float(), default=None)
    previous: Mapped[float | None] = mapped_column(Float(), default=None)
    change: Mapped[float | None] = mapped_column(Float(), default=None)
    z_score: Mapped[float] = mapped_column(Float(), nullable=False, default=0.0)
    basis: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    suggestion: Mapped[str] = mapped_column(String(16), nullable=False, default="human")
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="open", index=True)
    decided_by: Mapped[str | None] = mapped_column(String(64), default=None)
    note: Mapped[str | None] = mapped_column(Text(), default=None)
    detected_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(), default=None)


class QualityTicketRow(Base):
    """A workspace ticket raised from a failing check.

    TradingLib has no external tracker to hand off to, so "TICKET" opens a real,
    persisted, assignable ticket *here* and says so - the alternative would be a
    button that pretends to file into a system nobody configured.
    """

    __tablename__ = "quality_tickets"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    dataset: Mapped[str] = mapped_column(String(128), nullable=False, default="", index=True)
    check: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text(), nullable=False, default="")
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default=str(Severity.WARN))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open", index=True)
    opened_by: Mapped[str] = mapped_column(String(64), nullable=False, default="user")
    incident_id: Mapped[str | None] = mapped_column(String(32), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(), default=None)


class QualityIncidentRow(Base):
    """One persisted quality incident."""

    __tablename__ = "quality_incidents"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    dataset: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    check: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    message: Mapped[str] = mapped_column(Text(), nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(), default=None)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON(), nullable=False, default=dict)


_OWNED_TABLES = (
    QualityCheckRow.__table__,
    QualityScoreRow.__table__,
    QualityIncidentRow.__table__,
    QualitySchemaRow.__table__,
    QualityOutlierRow.__table__,
    QualityTicketRow.__table__,
)


def ensure_schema() -> None:
    """Create the quality tables if they do not exist yet, and migrate columns."""
    Base.metadata.create_all(get_engine(), tables=list(_OWNED_TABLES))
    for table in _OWNED_TABLES:
        ensure_columns(table)


def _incident_from_row(row: QualityIncidentRow) -> QualityIncident:
    """Map an incident row to the dataclass."""
    return QualityIncident(
        id=row.id,
        dataset=row.dataset,
        check=row.check,
        severity=Severity(row.severity),
        message=row.message,
        opened_at=to_utc(row.opened_at),
        resolved_at=to_utc(row.resolved_at) if row.resolved_at else None,
        metrics=dict(row.metrics or {}),
    )


# --------------------------------------------------------------------------
# engine
# --------------------------------------------------------------------------
class QualityEngine:
    """Runs the quality checks over the store and maintains the incident log."""

    def __init__(
        self,
        store: TimeSeriesStore | None = None,
        *,
        bus: EventBus | None = None,
        session_factory: sessionmaker[Session] | None = None,
        config: QualityConfig | None = None,
    ) -> None:
        """Create the engine.

        Args:
            store: time-series store to read; defaults to the process-wide one.
            bus: event bus incidents are published on; defaults to the
                process-wide bus.
            session_factory: SQLAlchemy session factory for persistence.
            config: thresholds and weights.
        """
        self._store = store
        self._bus = bus
        self._session_factory = session_factory
        self.config = config or QualityConfig()
        #: Cached quarantine list; invalidated whenever a decision is recorded.
        self._quarantine_cache: dict[str, set[datetime]] | None = None
        ensure_schema()
        self.install_quarantine_filter()

    @property
    def store(self) -> TimeSeriesStore:
        """The time-series store being checked."""
        if self._store is None:
            self._store = get_store()
        return self._store

    @property
    def bus(self) -> EventBus:
        """The event bus incidents are published on."""
        if self._bus is None:
            self._bus = get_event_bus()
        return self._bus

    @contextmanager
    def _session(self) -> Iterator[Session]:
        """Transactional session scope bound to this engine."""
        factory = self._session_factory or get_session_factory()
        session = factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # --- dataset discovery ------------------------------------------------
    def datasets(self) -> list[DatasetRef]:
        """Return every dataset currently present in the store."""
        refs: list[DatasetRef] = []
        frame = self.store.query(
            "SELECT DISTINCT symbol, interval FROM bars ORDER BY symbol, interval"
        )
        for row in frame.itertuples(index=False):
            try:
                interval = BarInterval(str(row.interval))
            except ValueError:
                logger.warning("stored bars use unknown interval %r", row.interval)
                continue
            refs.append(DatasetRef.bars(str(row.symbol), interval))
        for entry in self.store.series_names():
            refs.append(DatasetRef.series(entry["namespace"], entry["name"]))
        return refs

    # --- data access ------------------------------------------------------
    def _points(self, ref: DatasetRef) -> tuple[list[datetime], np.ndarray]:
        """Return the stored timestamps and values of a dataset."""
        if ref.kind == "bars":
            frame = self.store.read_bars(str(ref.symbol), interval=ref.interval)
            if frame.empty:
                return [], np.array([], dtype="float64")
            stamps = [to_utc(pd.Timestamp(ts).to_pydatetime()) for ts in frame.index]
            return stamps, frame["close"].to_numpy(dtype="float64")
        series = self.store.read_series(str(ref.namespace), str(ref.name))
        stamps = [to_utc(ts) for ts, _ in series.points]
        values = np.array([value for _, value in series.points], dtype="float64")
        return stamps, values

    def _cadence(self, ref: DatasetRef, stamps: Sequence[datetime]) -> Cadence | None:
        """Return the cadence expected of ``ref`` (inferred for named series)."""
        if ref.kind == "bars":
            return bar_cadence(ref.interval)
        return infer_cadence(stamps)

    # --- checks -----------------------------------------------------------
    def check_completeness(self, ref: DatasetRef) -> QualityCheckResult:
        """Compare the expected observation grid against what is stored.

        The score is ``observed / expected`` over the dataset's own span; a
        dataset that simply starts late is not penalised, only holes inside the
        span it claims to cover are.
        """
        now = utcnow()
        stamps, _ = self._points(ref)
        if len(stamps) < 2:
            return QualityCheckResult(
                check="completeness",
                dataset=ref.key,
                passed=True,
                score=1.0,
                detail=f"only {len(stamps)} stored point(s); nothing to compare a grid against",
                observed_at=now,
                metrics={"observed": len(stamps), "expected": len(stamps)},
            )

        first, last = stamps[0], stamps[-1]
        if ref.kind == "bars":
            cadence = bar_cadence(ref.interval)
            expected = expected_bar_timestamps(ref.interval, first, last)
        else:
            cadence = infer_cadence(stamps)
            if cadence is None:  # pragma: no cover - guarded by the len check above
                expected = list(stamps)
            else:
                expected = expected_timestamps(cadence, first, last)

        by_date = cadence is None or cadence.daily_or_slower
        observed_keys = {ts.date() if by_date else ts for ts in stamps}
        expected_keys = [ts.date() if by_date else ts for ts in expected]
        missing = [key for key in expected_keys if key not in observed_keys]
        expected_count = max(len(expected_keys), 1)
        present = expected_count - len(missing)
        ratio = max(0.0, min(1.0, present / expected_count))

        if ratio >= self.config.completeness_warn:
            severity, passed = Severity.INFO, True
        elif ratio >= self.config.completeness_error:
            severity, passed = Severity.WARN, False
        else:
            severity, passed = Severity.ERROR, False

        listed = [
            key.isoformat() for key in missing[: self.config.max_missing_listed]
        ]
        return QualityCheckResult(
            check="completeness",
            dataset=ref.key,
            passed=passed,
            score=round(ratio, 6),
            detail=(
                f"{present}/{expected_count} expected observations present "
                f"({len(missing)} missing, {cadence.label if cadence else 'unknown cadence'})"
            ),
            observed_at=now,
            metrics={
                "expected": expected_count,
                "observed": present,
                "missing": len(missing),
                "missing_ratio": round(len(missing) / expected_count, 6),
                "cadence": cadence.kind if cadence else None,
                "first": first.isoformat(),
                "last": last.isoformat(),
                "missing_sample": listed,
                "stored_points": len(stamps),
            },
            severity=severity,
        )

    def check_staleness(self, ref: DatasetRef, *, now: datetime | None = None) -> QualityCheckResult:
        """Count scheduled observations published since the newest stored point.

        Wall-clock age is the wrong measure for market data (Friday's close is
        not stale on Sunday), so this counts *missed scheduled slots*: for daily
        bars, closed trading sessions after the last stored bar; for series, the
        inferred cadence's slots, allowing one slot of publication lag.
        """
        moment = to_utc(now) if now is not None else utcnow()
        stamps, _ = self._points(ref)
        if not stamps:
            return QualityCheckResult(
                check="staleness",
                dataset=ref.key,
                passed=False,
                score=0.0,
                detail="no data stored",
                observed_at=moment,
                metrics={"missed_periods": None, "last": None},
                severity=Severity.ERROR,
            )

        last = stamps[-1]
        cadence = self._cadence(ref, stamps)
        missed = self._missed_periods(ref, cadence, last, moment)
        age_seconds = (moment - last).total_seconds()

        if missed <= 0:
            severity, passed, score = Severity.INFO, True, 1.0
        elif missed <= self.config.staleness_error_periods:
            severity, passed = Severity.WARN, False
            score = max(0.0, 1.0 - missed / (self.config.staleness_error_periods + 1))
        else:
            severity, passed = Severity.ERROR, False
            score = max(0.0, 1.0 - missed / 10.0)

        return QualityCheckResult(
            check="staleness",
            dataset=ref.key,
            passed=passed,
            score=round(score, 6),
            detail=(
                f"last observation {last.isoformat()}; {missed} scheduled "
                f"observation(s) missing since ({cadence.label if cadence else 'unknown cadence'})"
            ),
            observed_at=moment,
            metrics={
                "last": last.isoformat(),
                "age_seconds": round(age_seconds, 3),
                "missed_periods": missed,
                "cadence": cadence.kind if cadence else None,
                "checked_at": moment.isoformat(),
            },
            severity=severity,
        )

    def _missed_periods(
        self, ref: DatasetRef, cadence: Cadence | None, last: datetime, now: datetime
    ) -> int:
        """Count scheduled observations due since ``last`` but not stored."""
        if cadence is None:
            return 0
        if ref.kind == "bars" and ref.interval is BarInterval.DAY_1:
            # Sessions whose close has already passed count as due.
            return len(
                [
                    day
                    for day in business_days(last.date() + timedelta(days=1), now.date())
                    if session_close(day) <= now
                ]
            )
        if ref.kind == "bars" and not ref.interval.intraday:
            # A weekly/monthly bar is only due once its whole period has elapsed.
            grid = expected_timestamps(cadence, last, now)
            step = timedelta(seconds=cadence.seconds)
            return max(0, sum(1 for ts in grid if ts > last and ts + step <= now))
        if ref.kind == "bars":
            grid = expected_bar_timestamps(ref.interval, last, now)
            # The bucket covering "now" is still forming; only closed ones count.
            step = timedelta(seconds=int(ref.interval.approx_seconds))
            return max(0, sum(1 for ts in grid if ts > last and ts + step <= now))
        grid = expected_timestamps(cadence, last, now)
        due = max(0, len([ts for ts in grid if ts > last]))
        # Allow one cadence period of publication lag before calling it stale.
        return max(0, due - 1)

    def check_outliers(self, ref: DatasetRef, *, z_threshold: float | None = None) -> QualityCheckResult:
        """Flag observations whose robust z-score exceeds the threshold.

        Returns are scored with a median/MAD z-score (``0.6745*(x-med)/MAD``),
        which a handful of extreme prints cannot inflate the way a standard
        deviation can. When the MAD is exactly zero (a constant series with one
        jump) the classical z-score is used instead, and the metrics say which
        estimator was applied.
        """
        now = utcnow()
        threshold = self.config.z_threshold if z_threshold is None else float(z_threshold)
        stamps, values = self._points(ref)
        n = len(values)
        if n < self.config.min_points_for_outliers:
            return QualityCheckResult(
                check="outliers",
                dataset=ref.key,
                passed=True,
                score=1.0,
                detail=(
                    f"{n} points stored; at least {self.config.min_points_for_outliers} "
                    "are needed before outlier scoring is meaningful"
                ),
                observed_at=now,
                metrics={"points": n, "flagged": 0, "threshold": threshold},
            )

        finite = np.isfinite(values)
        if not finite.all():
            values = values[finite]
            stamps = [ts for ts, keep in zip(stamps, finite, strict=True) if keep]
        if len(values) < self.config.min_points_for_outliers:
            return QualityCheckResult(
                check="outliers",
                dataset=ref.key,
                passed=False,
                score=0.0,
                detail=f"{int((~finite).sum())} non-finite values stored",
                observed_at=now,
                metrics={"points": int(finite.sum()), "non_finite": int((~finite).sum())},
                severity=Severity.ERROR,
            )

        if np.all(values > 0):
            changes = np.diff(np.log(values))
            basis = "log-returns"
        else:
            changes = np.diff(values)
            basis = "first-differences"
        change_stamps = stamps[1:]

        median = float(np.median(changes))
        mad = float(np.median(np.abs(changes - median)))
        if mad > 0:
            z = 0.6745 * (changes - median) / mad
            estimator = "median/MAD"
        else:
            std = float(np.std(changes))
            if std == 0:
                z = np.zeros_like(changes)
                estimator = "constant-series"
            else:
                z = (changes - float(np.mean(changes))) / std
                estimator = "mean/std"

        flags = np.abs(z) > threshold
        flagged = int(flags.sum())
        total = int(len(changes))
        ratio = flagged / total if total else 0.0
        worst_order = np.argsort(-np.abs(z))[: self.config.max_missing_listed]
        worst = [
            {
                "ts": change_stamps[int(i)].isoformat(),
                "value": float(values[int(i) + 1]),
                "change": float(changes[int(i)]),
                "z": round(float(z[int(i)]), 3),
            }
            for i in worst_order
            if bool(flags[int(i)])
        ]

        if flagged == 0:
            severity, passed, score = Severity.INFO, True, 1.0
        else:
            passed = False
            severity = Severity.ERROR if ratio > self.config.outlier_error_ratio else Severity.WARN
            score = max(0.0, 1.0 - min(1.0, ratio * 50.0))

        return QualityCheckResult(
            check="outliers",
            dataset=ref.key,
            passed=passed,
            score=round(score, 6),
            detail=(
                f"{flagged}/{total} {basis} exceed |z|>{threshold:g} ({estimator})"
                if flagged
                else f"no {basis} exceed |z|>{threshold:g} ({estimator})"
            ),
            observed_at=now,
            metrics={
                "points": int(len(values)),
                "changes": total,
                "flagged": flagged,
                "flagged_ratio": round(ratio, 6),
                "threshold": threshold,
                "estimator": estimator,
                "basis": basis,
                "median": median,
                "mad": mad,
                "worst": worst,
            },
            severity=severity,
        )

    def check_duplicates(self, ref: DatasetRef) -> QualityCheckResult:
        """Count observations that share a timestamp ("dupe rows").

        The store upserts on the time key, so a duplicate here means two rows
        survived a merge - a real ingestion fault rather than a cosmetic one,
        because every downstream mean, sum and return would double-count it.
        """
        now = utcnow()
        stamps, _ = self._points(ref)
        total = len(stamps)
        if total == 0:
            return QualityCheckResult(
                check="duplicates",
                dataset=ref.key,
                passed=True,
                score=1.0,
                detail="no rows stored",
                observed_at=now,
                metrics={"rows": 0, "duplicates": 0},
            )
        seen: dict[datetime, int] = {}
        for stamp in stamps:
            seen[stamp] = seen.get(stamp, 0) + 1
        repeated = {ts: count for ts, count in seen.items() if count > 1}
        extra = sum(count - 1 for count in repeated.values())
        ratio = extra / total
        if extra == 0:
            severity, passed, score = Severity.INFO, True, 1.0
        else:
            severity, passed = Severity.ERROR, False
            score = max(0.0, 1.0 - min(1.0, ratio * 10.0))
        return QualityCheckResult(
            check="duplicates",
            dataset=ref.key,
            passed=passed,
            score=round(score, 6),
            detail=(
                f"{extra} duplicate row(s) across {len(repeated)} timestamp(s) in {total} rows"
                if extra
                else f"{total} rows, every timestamp unique"
            ),
            observed_at=now,
            metrics={
                "rows": total,
                "duplicates": extra,
                "timestamps": len(repeated),
                "sample": [ts.isoformat() for ts in sorted(repeated)[: self.config.max_missing_listed]],
            },
            severity=severity,
        )

    def check_schema_drift(self, ref: DatasetRef) -> QualityCheckResult:
        """Compare the dataset's current column signature against the last one.

        The first run records the baseline and passes (nothing to drift from);
        every later run compares columns and dtypes against it and reports what
        appeared, disappeared or changed type. The baseline is then updated, so
        a drift is reported exactly once - on the run that saw it happen.
        """
        now = utcnow()
        columns, dtypes = self._signature(ref)
        signature = hashlib.sha256(
            json.dumps({"columns": columns, "dtypes": dtypes}, sort_keys=True).encode("utf-8")
        ).hexdigest()[:32]
        with self._session() as session:
            row = session.get(QualitySchemaRow, ref.key)
            previous = (
                {
                    "signature": row.signature,
                    "columns": list(row.columns or []),
                    "dtypes": dict(row.dtypes or {}),
                }
                if row is not None
                else None
            )
            if row is None:
                session.add(
                    QualitySchemaRow(
                        dataset=ref.key,
                        signature=signature,
                        columns=columns,
                        dtypes=dtypes,
                        observed_at=now,
                    )
                )
            else:
                row.signature = signature
                row.columns = columns
                row.dtypes = dtypes
                row.observed_at = now

        if previous is None:
            return QualityCheckResult(
                check="schema_drift",
                dataset=ref.key,
                passed=True,
                score=1.0,
                detail=f"baseline recorded: {len(columns)} column(s)",
                observed_at=now,
                metrics={
                    "columns": columns,
                    "dtypes": dtypes,
                    "baseline": True,
                    "signature": signature,
                    "added": [],
                    "removed": [],
                    "retyped": [],
                },
            )
        added = [c for c in columns if c not in previous["columns"]]
        removed = [c for c in previous["columns"] if c not in columns]
        retyped = [
            {"column": c, "was": previous["dtypes"].get(c), "now": dtypes.get(c)}
            for c in columns
            if c in previous["dtypes"] and previous["dtypes"][c] != dtypes.get(c)
        ]
        drifted = bool(added or removed or retyped)
        pieces: list[str] = []
        if added:
            pieces.append(f"added {', '.join(added)}")
        if removed:
            pieces.append(f"removed {', '.join(removed)}")
        if retyped:
            pieces.append(
                "retyped " + ", ".join(f"{r['column']} {r['was']}->{r['now']}" for r in retyped)
            )
        return QualityCheckResult(
            check="schema_drift",
            dataset=ref.key,
            passed=not drifted,
            score=0.0 if drifted else 1.0,
            detail="; ".join(pieces) if drifted else f"stable at {len(columns)} column(s)",
            observed_at=now,
            metrics={
                "columns": columns,
                "dtypes": dtypes,
                "added": added,
                "removed": removed,
                "retyped": retyped,
                "previous_signature": previous["signature"],
                "signature": signature,
            },
            severity=Severity.ERROR if drifted else Severity.INFO,
        )

    def check_lookahead(
        self, ref: DatasetRef, *, now: datetime | None = None
    ) -> QualityCheckResult:
        """Flag observations stamped at a time they could not have been known.

        Two things count as look-ahead in stored data: a timestamp in the future,
        and (for bars) a bar stamped after the close of the session it claims to
        cover. Either one lets a backtest read a value before it existed, which
        is the single most expensive silent bug in this codebase's domain.
        """
        moment = to_utc(now) if now is not None else utcnow()
        stamps, _ = self._points(ref)
        if not stamps:
            return QualityCheckResult(
                check="lookahead",
                dataset=ref.key,
                passed=True,
                score=1.0,
                detail="no rows stored",
                observed_at=moment,
                metrics={"rows": 0, "future": 0},
            )
        future = [ts for ts in stamps if ts > moment]
        misaligned: list[str] = []
        if ref.kind == "bars" and ref.interval is BarInterval.DAY_1:
            for ts in stamps:
                if ts > session_close(ts.date()) + timedelta(hours=1):
                    misaligned.append(ts.isoformat())
                if len(misaligned) >= self.config.max_missing_listed:
                    break
        offenders = len(future) + len(misaligned)
        if offenders == 0:
            severity, passed, score = Severity.INFO, True, 1.0
        else:
            severity, passed, score = Severity.ERROR, False, 0.0
        return QualityCheckResult(
            check="lookahead",
            dataset=ref.key,
            passed=passed,
            score=score,
            detail=(
                f"{len(future)} future-dated and {len(misaligned)} post-close observation(s)"
                if offenders
                else f"{len(stamps)} rows, none dated ahead of their availability"
            ),
            observed_at=moment,
            metrics={
                "rows": len(stamps),
                "future": len(future),
                "post_close": len(misaligned),
                "checked_at": moment.isoformat(),
                "sample": [ts.isoformat() for ts in future[: self.config.max_missing_listed]]
                + misaligned,
            },
            severity=severity,
        )

    def _signature(self, ref: DatasetRef) -> tuple[list[str], dict[str, str]]:
        """Return the stored column names and dtypes of a dataset."""
        if ref.kind == "bars":
            frame = self.store.read_bars(str(ref.symbol), interval=ref.interval)
        else:
            frame = self.store.read_series_frame(str(ref.namespace), str(ref.name))
        columns = [str(c) for c in frame.columns]
        dtypes = {str(c): str(frame[c].dtype) for c in frame.columns}
        return columns, dtypes

    # --- suites -----------------------------------------------------------
    def evaluate(self, ref: DatasetRef, *, now: datetime | None = None) -> DatasetQuality:
        """Run every check against one dataset and compute its composite score.

        Six checks run; three of them (completeness, staleness, outliers) carry
        the weighted composite score, and the three integrity checks - duplicate
        rows, schema drift and look-ahead - are pass/fail gates that open an
        incident on their own rather than shading a number.
        """
        outliers = self.check_outliers(ref)
        results = [
            self.check_completeness(ref),
            self.check_staleness(ref, now=now),
            outliers,
            self.check_duplicates(ref),
            self.check_schema_drift(ref),
            self.check_lookahead(ref, now=now),
        ]
        self._record_outlier_flags(ref, outliers)
        by_check = {r.check: r.score for r in results}
        score = self.config.composite(
            by_check.get("completeness", 1.0),
            by_check.get("staleness", 1.0),
            by_check.get("outliers", 1.0),
        )
        return DatasetQuality(
            ref=ref,
            score=score,
            results=results,
            observed_at=to_utc(now) if now is not None else utcnow(),
        )

    async def run(
        self,
        refs: Sequence[DatasetRef | str] | None = None,
        *,
        persist: bool = True,
        publish: bool = True,
        now: datetime | None = None,
    ) -> list[DatasetQuality]:
        """Run the suite over ``refs`` (default: every dataset in the store).

        Checks are pandas/DuckDB work, so they run on a worker thread; results
        are persisted, incidents opened or resolved, and a ``run.completed``
        event published on the ``quality`` channel.
        """
        targets = [
            r if isinstance(r, DatasetRef) else DatasetRef.parse(r)
            for r in (refs if refs is not None else await asyncio.to_thread(self.datasets))
        ]
        outcomes: list[DatasetQuality] = []
        for ref in targets:
            outcome = await asyncio.to_thread(self.evaluate, ref, now=now)
            outcomes.append(outcome)
            if persist:
                await asyncio.to_thread(self._persist, outcome)
            await self._reconcile_incidents(outcome, publish=publish)
        if publish:
            await self.bus.publish(
                QUALITY_CHANNEL,
                {
                    "type": "run.completed",
                    "datasets": [o.ref.key for o in outcomes],
                    "failed": [o.ref.key for o in outcomes if o.failed],
                    "summary": await asyncio.to_thread(self.summary),
                },
            )
        return outcomes

    async def run_all(self, datasets: list[str] | None = None) -> list[QualityCheckResult]:
        """Run every check over ``datasets`` (default: all stored datasets).

        Convenience wrapper around :meth:`run` returning the flat list of check
        results, which is the shape the ``/api/quality/summary`` route needs.
        """
        outcomes = await self.run(datasets)
        return [result for outcome in outcomes for result in outcome.results]

    def _persist(self, outcome: DatasetQuality) -> None:
        """Store one dataset's check results and composite score."""
        with self._session() as session:
            for result in outcome.results:
                session.add(
                    QualityCheckRow(
                        dataset=result.dataset,
                        check=result.check,
                        passed=result.passed,
                        score=float(result.score),
                        severity=str(result.severity),
                        detail=result.detail,
                        observed_at=result.observed_at,
                        metrics=result.metrics,
                    )
                )
            session.add(
                QualityScoreRow(
                    dataset=outcome.ref.key,
                    kind=outcome.ref.kind,
                    label=outcome.ref.label,
                    score=float(outcome.score),
                    observed_at=outcome.observed_at,
                )
            )

    async def _reconcile_incidents(self, outcome: DatasetQuality, *, publish: bool) -> None:
        """Open incidents for failing checks and resolve those that recovered."""
        for result in outcome.results:
            if result.passed:
                await self.resolve_check_incident(
                    outcome.ref.key, result.check, publish=publish
                )
            else:
                await self.open_incident(
                    outcome.ref.key,
                    result.check,
                    result.severity,
                    result.detail,
                    publish=publish,
                    **result.metrics,
                )

    # --- outlier review queue --------------------------------------------
    def _record_outlier_flags(self, ref: DatasetRef, result: QualityCheckResult) -> None:
        """Persist newly flagged observations into the review queue.

        A flag already in the queue keeps its identity and its decision, so a
        re-run never resurrects a point somebody already accepted or duplicates
        one that is still open.
        """
        worst = list(result.metrics.get("worst") or [])
        if not worst:
            return
        basis = str(result.metrics.get("basis") or "")
        label = ref.label
        now = utcnow()
        with self._session() as session:
            existing = {
                to_utc(row.ts): row
                for row in session.scalars(
                    select(QualityOutlierRow).where(QualityOutlierRow.dataset == ref.key)
                )
            }
            for entry in worst:
                try:
                    stamp = to_utc(datetime.fromisoformat(str(entry["ts"])))
                except (KeyError, ValueError):
                    continue
                if stamp in existing:
                    continue
                value = entry.get("value")
                change = entry.get("change")
                previous = (
                    float(value) - float(change)
                    if value is not None and change is not None and basis != "log-returns"
                    else None
                )
                session.add(
                    QualityOutlierRow(
                        id=uuid.uuid4().hex,
                        dataset=ref.key,
                        label=label,
                        ts=stamp,
                        value=None if value is None else float(value),
                        previous=previous,
                        change=None if change is None else float(change),
                        z_score=float(entry.get("z") or 0.0),
                        basis=basis,
                        suggestion=self._suggest_decision(entry, basis),
                        state="open",
                        detected_at=now,
                    )
                )

    @staticmethod
    def _suggest_decision(entry: dict[str, Any], basis: str) -> str:
        """Suggest a disposition for one flagged point.

        The heuristic is deliberately narrow and stated in the UI: a change that
        is almost exactly a power of ten is a decimal shift and is suggested for
        quarantine; a very large but not decimal-shaped move is suggested for
        acceptance (it is probably a real event that the robust z-score simply
        cannot distinguish); anything in between needs a person.
        """
        change = entry.get("change")
        if change is None:
            return "human"
        magnitude = abs(float(change))
        if basis == "log-returns" and magnitude > 0.5:
            # log(10) = 2.302..., log(100) = 4.605...
            for decades in (1, 2, 3):
                if abs(magnitude - decades * math.log(10.0)) < 0.12:
                    return "quarantine"
            return "accept" if magnitude < 1.0 else "human"
        return "human"

    def outlier_queue(
        self, *, dataset: str | None = None, include_decided: bool = True, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Return the outlier review queue, newest detection first.

        ``include_decided=False`` returns the points still awaiting a verdict -
        which includes the *escalated* ones: escalation is a request for a second
        pair of eyes, not a decision, so an escalated point stays in the queue.
        """
        with self._session() as session:
            statement = select(QualityOutlierRow).order_by(
                QualityOutlierRow.detected_at.desc(), QualityOutlierRow.ts.desc()
            )
            if dataset:
                statement = statement.where(QualityOutlierRow.dataset == dataset)
            if not include_decided:
                statement = statement.where(QualityOutlierRow.state.in_(OPEN_OUTLIER_STATES))
            rows = list(session.scalars(statement.limit(max(1, limit))))
            return [self._outlier_dict(row) for row in rows]

    @staticmethod
    def _outlier_dict(row: QualityOutlierRow) -> dict[str, Any]:
        """Return one queue entry as a JSON-serialisable mapping."""
        return {
            "id": row.id,
            "dataset": row.dataset,
            "label": row.label,
            "ts": to_utc(row.ts).isoformat(),
            "value": row.value,
            "previous": row.previous,
            "change": row.change,
            "z": row.z_score,
            "basis": row.basis,
            "suggestion": row.suggestion,
            "state": row.state,
            # Escalated points are still awaiting a verdict, so the queue, the
            # "open" badge and the check suite's flag count all still count them.
            "needs_review": row.state in OPEN_OUTLIER_STATES,
            "escalated": row.state == "escalated",
            "decided_by": row.decided_by,
            "note": row.note,
            "detected_at": to_utc(row.detected_at).isoformat(),
            "decided_at": to_utc(row.decided_at).isoformat() if row.decided_at else None,
        }

    def outlier_counts(self) -> dict[str, int]:
        """Return how many queue entries sit in each state.

        ``awaiting_review`` is the figure the queue badge and the check suite's
        flag count use: open plus escalated, because neither has been resolved.
        """
        counts = {"open": 0, "quarantined": 0, "accepted": 0, "escalated": 0}
        with self._session() as session:
            for row in session.scalars(select(QualityOutlierRow)):
                counts[row.state] = counts.get(row.state, 0) + 1
        counts["awaiting_review"] = counts["open"] + counts["escalated"]
        return counts

    async def decide_outlier(
        self,
        outlier_id: str,
        decision: str,
        *,
        by: str = "user",
        note: str | None = None,
        publish: bool = True,
    ) -> dict[str, Any]:
        """Quarantine, accept or escalate one flagged observation.

        Quarantining takes effect immediately and everywhere: the store's read
        path drops the point, so features, screens and backtests stop seeing it
        until the decision is undone.

        Escalating resolves nothing on purpose. The point keeps its raw value,
        stays in the review queue and is marked as needing a human - the reviewer
        is saying "I cannot call this", and the data keeps flowing until somebody
        can. Who escalated it and when is recorded, so the request is auditable.

        Args:
            outlier_id: queue entry to decide.
            decision: ``quarantine``, ``accept``, ``escalate``, or ``open``
                (undo, returning the point to an undecided state).
            by: who decided.
            note: optional rationale kept with the row.
            publish: publish the change on the ``quality`` channel.

        Raises:
            KeyError: when the queue entry is unknown.
            ValueError: when ``decision`` is not one of the four verbs.
        """
        if decision not in OUTLIER_DECISIONS:
            raise ValueError(
                f"unknown decision {decision!r}; use "
                + ", ".join(sorted(OUTLIER_DECISIONS))
            )
        state = OUTLIER_DECISIONS[decision]
        with self._session() as session:
            row = session.get(QualityOutlierRow, outlier_id)
            if row is None:
                raise KeyError(f"unknown outlier {outlier_id!r}")
            row.state = state
            row.note = note
            if state == "open":
                row.decided_by = None
                row.decided_at = None
            else:
                row.decided_by = by
                row.decided_at = utcnow()
            session.flush()
            payload = self._outlier_dict(row)
        self._quarantine_cache = None
        if publish:
            await self.bus.publish(
                QUALITY_CHANNEL,
                {
                    "type": "outlier.decided",
                    "outlier": payload,
                    "counts": self.outlier_counts(),
                },
            )
        return payload

    def quarantined(self, dataset: str | None = None) -> dict[str, set[datetime]]:
        """Return the quarantined timestamps, keyed by dataset."""
        if self._quarantine_cache is None:
            cache: dict[str, set[datetime]] = {}
            with self._session() as session:
                for row in session.scalars(
                    select(QualityOutlierRow).where(QualityOutlierRow.state == "quarantined")
                ):
                    cache.setdefault(row.dataset, set()).add(to_utc(row.ts))
            self._quarantine_cache = cache
        if dataset is None:
            return self._quarantine_cache
        return {dataset: self._quarantine_cache.get(dataset, set())}

    def quarantine_filter(
        self, dataset: str, timestamps: Sequence[Any]
    ) -> set[Any]:
        """Store hook: return the subset of ``timestamps`` that is quarantined."""
        excluded = self.quarantined().get(dataset)
        if not excluded:
            return set()
        out: set[Any] = set()
        for stamp in timestamps:
            moment = stamp.to_pydatetime() if hasattr(stamp, "to_pydatetime") else stamp
            if to_utc(moment) in excluded:
                out.add(stamp)
        return out

    def install_quarantine_filter(self) -> None:
        """Wire this engine's quarantine list into the time-series read path."""
        from tradinglib.data.store import set_quarantine_filter

        set_quarantine_filter(self.quarantine_filter)

    # --- tickets ----------------------------------------------------------
    def open_ticket(
        self,
        *,
        title: str,
        dataset: str = "",
        check: str = "",
        body: str = "",
        severity: Severity = Severity.WARN,
        opened_by: str = "user",
        incident_id: str | None = None,
    ) -> dict[str, Any]:
        """Open a workspace ticket for a failing check.

        TradingLib has no external issue tracker configured, so this is a real local
        ticket rather than a pretend hand-off: it is persisted, listed, and can
        be closed. The UI says where it lives.

        Raises:
            ValueError: when the title is empty.
        """
        headline = str(title or "").strip()
        if not headline:
            raise ValueError("a ticket needs a title")
        row_id = uuid.uuid4().hex[:16]
        with self._session() as session:
            session.add(
                QualityTicketRow(
                    id=row_id,
                    dataset=dataset,
                    check=check,
                    title=headline,
                    body=body,
                    severity=str(severity),
                    status="open",
                    opened_by=opened_by,
                    incident_id=incident_id,
                    created_at=utcnow(),
                )
            )
        return self.ticket(row_id)

    def ticket(self, ticket_id: str) -> dict[str, Any]:
        """Return one ticket.

        Raises:
            KeyError: when the identifier is unknown.
        """
        with self._session() as session:
            row = session.get(QualityTicketRow, ticket_id)
            if row is None:
                raise KeyError(f"unknown ticket {ticket_id!r}")
            return self._ticket_dict(row)

    def tickets(self, *, include_closed: bool = False, limit: int = 50) -> list[dict[str, Any]]:
        """Return tickets, newest first."""
        with self._session() as session:
            statement = select(QualityTicketRow).order_by(QualityTicketRow.created_at.desc())
            if not include_closed:
                statement = statement.where(QualityTicketRow.status == "open")
            return [self._ticket_dict(row) for row in session.scalars(statement.limit(max(1, limit)))]

    def close_ticket(self, ticket_id: str) -> dict[str, Any]:
        """Close a ticket.

        Raises:
            KeyError: when the identifier is unknown.
        """
        with self._session() as session:
            row = session.get(QualityTicketRow, ticket_id)
            if row is None:
                raise KeyError(f"unknown ticket {ticket_id!r}")
            row.status = "closed"
            row.closed_at = utcnow()
            session.flush()
            return self._ticket_dict(row)

    @staticmethod
    def _ticket_dict(row: QualityTicketRow) -> dict[str, Any]:
        """Return one ticket as a JSON-serialisable mapping."""
        return {
            "id": row.id,
            "dataset": row.dataset,
            "check": row.check,
            "title": row.title,
            "body": row.body,
            "severity": row.severity,
            "status": row.status,
            "opened_by": row.opened_by,
            "incident_id": row.incident_id,
            "created_at": to_utc(row.created_at).isoformat(),
            "closed_at": to_utc(row.closed_at).isoformat() if row.closed_at else None,
        }

    # --- vendor SLA -------------------------------------------------------
    def sla_report(self, catalog: Any, *, now: datetime | None = None) -> list[dict[str, Any]]:
        """Return one SLA row per enabled ingestion source.

        The "contract" is the source's own configured schedule - there is no
        vendor paperwork to read, so the cadence the source promises is the only
        honest definition of the promise, and every row says which one it used.
        A source is *breached* once nothing has landed for longer than the
        tolerance :meth:`SourceCatalog.stale_sources` already applies, so the
        SLA row and the "stale sources" figure can never disagree.

        Args:
            catalog: the :class:`~tradinglib.data.ingest.catalog.SourceCatalog` to
                measure; passed in because the catalog is request-scoped.
            now: evaluation time (defaults to now).

        Returns:
            Breached rows first, then by name.
        """
        moment = to_utc(now) if now is not None else utcnow()
        try:
            sources = catalog.list_sources()
            states = catalog.states()
            overdue = {row["source_id"]: row for row in catalog.stale_sources()}
        except Exception:  # noqa: BLE001 - a catalog that is not up is not a breach
            logger.debug("source catalog unavailable for the SLA report", exc_info=True)
            return []
        rows: list[dict[str, Any]] = []
        for source in sources:
            if not source.enabled:
                continue
            schedule = source.parsed_schedule
            state = states.get(source.id)
            last = getattr(state, "last_ingest_at", None)
            last_dt = to_utc(last) if last else None
            age = (moment - last_dt).total_seconds() if last_dt else None
            breach = overdue.get(source.id)
            if breach is not None:
                state_name = "breached"
                hours = float(breach["age_seconds"]) / 3600.0
                detail = (
                    f"{hours:.1f}h since the last ingest, tolerance "
                    f"{float(breach['tolerance_seconds']) / 3600.0:.1f}h"
                    if not breach["never_ingested"]
                    else "no successful ingest since the source was created"
                )
            elif last_dt is None:
                state_name, detail = "pending", "no successful ingest recorded yet"
            else:
                state_name = "ok"
                detail = f"{(age or 0.0) / 3600.0:.1f}h since the last ingest"
            rows.append(
                {
                    "source_id": source.id,
                    "name": source.name,
                    "domain": str(source.data_domain),
                    "schedule": schedule.label,
                    "schedule_seconds": schedule.interval_seconds,
                    "last_ingest_at": last_dt.isoformat() if last_dt else None,
                    "age_seconds": None if age is None else round(age, 1),
                    "tolerance_seconds": (
                        breach["tolerance_seconds"] if breach is not None else None
                    ),
                    "state": state_name,
                    "detail": detail,
                }
            )
        rows.sort(key=lambda row: (row["state"] != "breached", row["name"].lower()))
        return rows

    # --- incidents --------------------------------------------------------
    async def open_incident(
        self,
        dataset: str,
        check: str,
        severity: Severity,
        message: str,
        *,
        publish: bool = True,
        **metrics: Any,
    ) -> QualityIncident:
        """Record a new incident and publish it on the ``quality`` channel.

        An already-open incident for the same ``(dataset, check)`` is updated
        instead of duplicated, so a check failing on every run yields one
        incident whose message and metrics track the latest observation.
        """
        now = utcnow()
        with self._session() as session:
            row = session.scalars(
                select(QualityIncidentRow)
                .where(
                    QualityIncidentRow.dataset == dataset,
                    QualityIncidentRow.check == check,
                    QualityIncidentRow.resolved_at.is_(None),
                )
                .order_by(QualityIncidentRow.opened_at.desc())
                .limit(1)
            ).first()
            created = row is None
            if row is None:
                row = QualityIncidentRow(
                    id=uuid.uuid4().hex,
                    dataset=dataset,
                    check=check,
                    severity=str(severity),
                    message=message,
                    opened_at=now,
                    metrics=metrics,
                )
                session.add(row)
            else:
                row.severity = str(severity)
                row.message = message
                row.metrics = metrics
            session.flush()
            incident = _incident_from_row(row)
        if publish:
            await self.bus.publish(
                QUALITY_CHANNEL,
                {
                    "type": "incident.opened" if created else "incident.updated",
                    "incident": incident.as_dict(),
                },
            )
        return incident

    async def resolve_incident(self, incident_id: str, *, publish: bool = True) -> QualityIncident:
        """Close an incident and publish the update.

        Raises:
            KeyError: when the incident id is unknown.
        """
        with self._session() as session:
            row = session.get(QualityIncidentRow, incident_id)
            if row is None:
                raise KeyError(f"unknown incident {incident_id!r}")
            if row.resolved_at is None:
                row.resolved_at = utcnow()
            session.flush()
            incident = _incident_from_row(row)
        if publish:
            await self.bus.publish(
                QUALITY_CHANNEL, {"type": "incident.resolved", "incident": incident.as_dict()}
            )
        return incident

    async def resolve_check_incident(
        self, dataset: str, check: str, *, publish: bool = True
    ) -> QualityIncident | None:
        """Resolve the open incident for ``(dataset, check)``, if there is one."""
        with self._session() as session:
            row = session.scalars(
                select(QualityIncidentRow)
                .where(
                    QualityIncidentRow.dataset == dataset,
                    QualityIncidentRow.check == check,
                    QualityIncidentRow.resolved_at.is_(None),
                )
                .order_by(QualityIncidentRow.opened_at.desc())
                .limit(1)
            ).first()
            if row is None:
                return None
            row.resolved_at = utcnow()
            session.flush()
            incident = _incident_from_row(row)
        if publish:
            await self.bus.publish(
                QUALITY_CHANNEL, {"type": "incident.resolved", "incident": incident.as_dict()}
            )
        return incident

    def incidents(
        self,
        *,
        include_resolved: bool = False,
        dataset: str | None = None,
        limit: int = 100,
    ) -> list[QualityIncident]:
        """Return recorded incidents, newest first."""
        with self._session() as session:
            stmt = select(QualityIncidentRow).order_by(QualityIncidentRow.opened_at.desc())
            if not include_resolved:
                stmt = stmt.where(QualityIncidentRow.resolved_at.is_(None))
            if dataset is not None:
                stmt = stmt.where(QualityIncidentRow.dataset == dataset)
            rows = session.scalars(stmt.limit(max(1, limit))).all()
            return [_incident_from_row(row) for row in rows]

    # --- reporting --------------------------------------------------------
    def _latest_checks(self, session: Session) -> dict[tuple[str, str], QualityCheckRow]:
        """Return the most recent row per ``(dataset, check)``."""
        rows = session.scalars(
            select(QualityCheckRow).order_by(QualityCheckRow.observed_at, QualityCheckRow.id)
        ).all()
        return {(row.dataset, row.check): row for row in rows}

    def _latest_scores(self, session: Session) -> dict[str, QualityScoreRow]:
        """Return the most recent composite score row per dataset."""
        rows = session.scalars(
            select(QualityScoreRow).order_by(QualityScoreRow.observed_at, QualityScoreRow.id)
        ).all()
        return {row.dataset: row for row in rows}

    def summary(self) -> dict[str, Any]:
        """Aggregate the latest run into the Data Quality page header figures."""
        with self._session() as session:
            checks = self._latest_checks(session)
            scores = self._latest_scores(session)
            open_incidents = session.scalars(
                select(QualityIncidentRow).where(QualityIncidentRow.resolved_at.is_(None))
            ).all()

            expected_total = 0
            observed_total = 0
            for (dataset, check), row in checks.items():
                if check != "completeness":
                    continue
                metrics = row.metrics or {}
                expected_total += int(metrics.get("expected") or 0)
                observed_total += int(metrics.get("observed") or 0)

            failed = [row for row in checks.values() if not row.passed]
            stale = [
                row for (_, check), row in checks.items() if check == "staleness" and not row.passed
            ]
            last_run = max(
                (to_utc(row.observed_at) for row in checks.values()), default=None
            )
            overall = (
                round(sum(row.score for row in scores.values()) / len(scores), 2) if scores else None
            )
            missing_pct = (
                round(100.0 * (expected_total - observed_total) / expected_total, 4)
                if expected_total
                else 0.0
            )
            return {
                "datasets": len(scores),
                "overall_quality_pct": overall,
                "stale_series": len(stale),
                "missing_values_pct": missing_pct,
                "expected_observations": expected_total,
                "observed_observations": observed_total,
                "failed_checks": len(failed),
                "open_incidents": len(open_incidents),
                "last_run": last_run.isoformat() if last_run else None,
                "worst": sorted(
                    (
                        {"dataset": row.dataset, "label": row.label, "score": row.score}
                        for row in scores.values()
                    ),
                    key=lambda item: item["score"],
                )[:5],
            }

    def latest(self) -> list[dict[str, Any]]:
        """Return the latest per-dataset scores and check outcomes."""
        with self._session() as session:
            checks = self._latest_checks(session)
            scores = self._latest_scores(session)
            out: list[dict[str, Any]] = []
            for dataset, score_row in sorted(scores.items()):
                dataset_checks = [
                    {
                        "check": check,
                        "passed": row.passed,
                        "score": row.score,
                        "severity": row.severity,
                        "detail": row.detail,
                        "observed_at": to_utc(row.observed_at).isoformat(),
                        "metrics": dict(row.metrics or {}),
                    }
                    for (ds, check), row in sorted(checks.items())
                    if ds == dataset
                ]
                out.append(
                    {
                        "dataset": dataset,
                        "kind": score_row.kind,
                        "label": score_row.label,
                        "score": score_row.score,
                        "observed_at": to_utc(score_row.observed_at).isoformat(),
                        "passed": all(c["passed"] for c in dataset_checks),
                        "checks": dataset_checks,
                    }
                )
            return out

    def series(self, dataset: str, *, limit: int = 200) -> dict[str, Any]:
        """Return per-dataset quality history for the trend charts."""
        with self._session() as session:
            score_rows = session.scalars(
                select(QualityScoreRow)
                .where(QualityScoreRow.dataset == dataset)
                .order_by(QualityScoreRow.observed_at.desc())
                .limit(max(1, limit))
            ).all()
            check_rows = session.scalars(
                select(QualityCheckRow)
                .where(QualityCheckRow.dataset == dataset)
                .order_by(QualityCheckRow.observed_at.desc())
                .limit(max(1, limit) * 3)
            ).all()
            history = [
                {"observed_at": to_utc(row.observed_at).isoformat(), "score": row.score}
                for row in reversed(score_rows)
            ]
            checks: dict[str, list[dict[str, Any]]] = {}
            for row in reversed(check_rows):
                checks.setdefault(row.check, []).append(
                    {
                        "observed_at": to_utc(row.observed_at).isoformat(),
                        "score": row.score,
                        "passed": row.passed,
                        "severity": row.severity,
                        "detail": row.detail,
                    }
                )
            return {
                "dataset": dataset,
                "points": len(history),
                "score": history[-1]["score"] if history else None,
                "history": history,
                "checks": checks,
            }

    def purge_history(self, *, keep_days: int = 90) -> int:
        """Delete check/score history older than ``keep_days``. Returns rows removed."""
        cutoff = utcnow() - timedelta(days=max(0, keep_days))
        removed = 0
        with self._session() as session:
            for model in (QualityCheckRow, QualityScoreRow):
                rows = session.scalars(
                    select(model).where(model.observed_at < cutoff)
                ).all()
                for row in rows:
                    session.delete(row)
                removed += len(rows)
        return removed


_engine: QualityEngine | None = None


def get_quality_engine() -> QualityEngine:
    """Return the process-wide quality engine, creating it on first use."""
    global _engine
    if _engine is None:
        _engine = QualityEngine()
    return _engine


def reset_quality_engine(engine: QualityEngine | None = None) -> QualityEngine | None:
    """Replace the process-wide engine. Intended for tests and reconfiguration."""
    global _engine
    _engine = engine
    return _engine


__all__ = [
    "OPEN_OUTLIER_STATES",
    "OUTLIER_DECISIONS",
    "Cadence",
    "DatasetQuality",
    "DatasetRef",
    "QUALITY_CHANNEL",
    "QualityCheckResult",
    "QualityCheckRow",
    "QualityConfig",
    "QualityEngine",
    "QualityIncident",
    "QualityIncidentRow",
    "QualityScoreRow",
    "Severity",
    "bar_cadence",
    "bond_market_holidays",
    "business_days",
    "easter_sunday",
    "ensure_schema",
    "expected_bar_timestamps",
    "expected_timestamps",
    "get_quality_engine",
    "infer_cadence",
    "is_business_day",
    "nyse_early_closes",
    "nyse_holidays",
    "reset_quality_engine",
    "session_bar_starts",
    "session_close",
    "us_federal_holidays",
]
