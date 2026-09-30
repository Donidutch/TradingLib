"""Membership alerting for pinned screens.

The Screener's "PIN + ALERT ON CHANGES" control makes two promises. Pinning is
a tag on the saved screen; *alerting on changes* is this module: a pinned screen
is re-run on a fixed cadence, its surviving symbols are diffed against the
previous stored run, and any entry or exit becomes a durable alert plus a live
event on the ``notifications`` channel.

Nothing is inferred. An alert names the symbols that entered and left and the
two runs it compared, so the Screener can show the diff rather than a claim
that "something changed".
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Final

from sqlalchemy import DateTime, Integer, JSON, String, select
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from tradinglib.db import Base, get_engine, get_session_factory
from tradinglib.events import EventBus

logger = logging.getLogger(__name__)

#: Channel the UI subscribes to for toast-worthy events.
NOTIFICATIONS_CHANNEL: Final[str] = "notifications"

#: Tag that turns membership alerting on for a saved screen. The Screener's
#: PIN button writes both this and ``pinned``.
ALERT_TAG: Final[str] = "alert"

#: Tag the PIN button writes to keep the screen at the top of the saved list.
PIN_TAG: Final[str] = "pinned"

#: How often the monitor re-runs every alerting screen, in seconds.
DEFAULT_INTERVAL_SECONDS: Final[float] = 900.0

#: How many alerts the API hands back by default.
DEFAULT_ALERT_LIMIT: Final[int] = 50


def _utcnow() -> datetime:
    """Return the current time as an aware UTC datetime."""
    return datetime.now(timezone.utc)


class ScreenAlertRow(Base):
    """SQLite row for one membership change detected on a pinned screen."""

    __tablename__ = "screen_alerts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    screen_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    screen_name: Mapped[str] = mapped_column(String(128), nullable=False)
    detected_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_run_id: Mapped[str | None] = mapped_column(String(64), default=None)
    entered: Mapped[list[str]] = mapped_column(JSON(), nullable=False, default=list)
    exited: Mapped[list[str]] = mapped_column(JSON(), nullable=False, default=list)
    pass_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    previous_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(), default=None)


def ensure_schema() -> None:
    """Create the screen-alert table if it does not exist yet."""
    Base.metadata.create_all(get_engine(), tables=[ScreenAlertRow.__table__])


@dataclass(slots=True)
class ScreenAlert:
    """One membership change on a pinned screen."""

    screen_id: str
    screen_name: str
    run_id: str
    entered: list[str] = field(default_factory=list)
    exited: list[str] = field(default_factory=list)
    previous_run_id: str | None = None
    pass_count: int = 0
    previous_count: int = 0
    detected_at: datetime = field(default_factory=_utcnow)
    id: str = field(default_factory=lambda: f"alr-{uuid.uuid4().hex[:10]}")
    acknowledged_at: datetime | None = None

    @property
    def headline(self) -> str:
        """A one-line summary suitable for a toast."""
        parts: list[str] = []
        if self.entered:
            parts.append("+" + ", ".join(self.entered[:4]))
        if self.exited:
            parts.append("−" + ", ".join(self.exited[:4]))
        return f"{self.screen_name}: " + (" · ".join(parts) or "membership changed")

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of the alert."""
        return {
            "id": self.id,
            "screen_id": self.screen_id,
            "screen_name": self.screen_name,
            "run_id": self.run_id,
            "previous_run_id": self.previous_run_id,
            "entered": list(self.entered),
            "exited": list(self.exited),
            "pass_count": self.pass_count,
            "previous_count": self.previous_count,
            "detected_at": self.detected_at.isoformat(),
            "acknowledged_at": (
                self.acknowledged_at.isoformat() if self.acknowledged_at else None
            ),
            "headline": self.headline,
        }


def _as_utc(moment: datetime | None) -> datetime | None:
    """Re-attach UTC to a timestamp SQLite handed back without a timezone.

    Everything is stored UTC (``docs/SPEC.md`` section 4), but SQLite's ``DateTime``
    column is naive, so a value read back would serialise without an offset and
    a browser would read it as local time - which is how a fresh alert ended up
    rendering as "2h ago".
    """
    if moment is None:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def _row_to_alert(row: ScreenAlertRow) -> ScreenAlert:
    """Map a stored row back to a :class:`ScreenAlert`."""
    return ScreenAlert(
        id=row.id,
        screen_id=row.screen_id,
        screen_name=row.screen_name,
        run_id=row.run_id,
        previous_run_id=row.previous_run_id,
        entered=list(row.entered or []),
        exited=list(row.exited or []),
        pass_count=row.pass_count,
        previous_count=row.previous_count,
        detected_at=_as_utc(row.detected_at) or _utcnow(),
        acknowledged_at=_as_utc(row.acknowledged_at),
    )


class ScreenAlertMonitor:
    """Re-runs pinned screens and reports membership changes.

    The monitor owns one background task. Each cycle it takes every saved
    screen carrying :data:`ALERT_TAG`, runs it through the screen engine
    (persisting the run, so the Screener's run history stays the single source
    of truth), diffs the survivors against the previous stored run and writes
    an alert when they differ.
    """

    def __init__(
        self,
        engine: Any,
        bus: EventBus | None = None,
        *,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        session_factory: sessionmaker[Session] | None = None,
    ) -> None:
        """Create the monitor.

        Args:
            engine: the :class:`~tradinglib.research.screen.funnel.ScreenEngine`.
            bus: event bus used to publish ``notifications``; optional so the
                monitor can run headless.
            interval_seconds: how often every alerting screen is re-run.
            session_factory: override, so a test can bind to its own database.
        """
        self.engine = engine
        self.bus = bus
        self.interval_seconds = max(30.0, float(interval_seconds))
        self._session_factory = session_factory or getattr(
            engine.registry, "session_factory", None
        )
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self.last_cycle_at: datetime | None = None
        self.last_error: str | None = None
        ensure_schema()

    # --- persistence ------------------------------------------------------
    @contextmanager
    def _session(self) -> Iterator[Session]:
        """Transactional session scope bound to this monitor."""
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

    def save(self, alert: ScreenAlert) -> ScreenAlert:
        """Persist an alert and return it."""
        with self._session() as session:
            session.add(
                ScreenAlertRow(
                    id=alert.id,
                    screen_id=alert.screen_id,
                    screen_name=alert.screen_name,
                    detected_at=alert.detected_at,
                    run_id=alert.run_id,
                    previous_run_id=alert.previous_run_id,
                    entered=list(alert.entered),
                    exited=list(alert.exited),
                    pass_count=alert.pass_count,
                    previous_count=alert.previous_count,
                    acknowledged_at=alert.acknowledged_at,
                )
            )
        return alert

    def list_alerts(
        self,
        *,
        screen_id: str | None = None,
        limit: int = DEFAULT_ALERT_LIMIT,
        unacknowledged_only: bool = False,
    ) -> list[ScreenAlert]:
        """Return recent alerts, newest first."""
        with self._session() as session:
            query = select(ScreenAlertRow)
            if screen_id is not None:
                query = query.where(ScreenAlertRow.screen_id == screen_id)
            if unacknowledged_only:
                query = query.where(ScreenAlertRow.acknowledged_at.is_(None))
            query = query.order_by(ScreenAlertRow.detected_at.desc()).limit(max(1, int(limit)))
            return [_row_to_alert(row) for row in session.scalars(query)]

    def acknowledge(self, alert_id: str) -> ScreenAlert:
        """Mark one alert as seen.

        Raises:
            KeyError: when no alert carries that id.
        """
        with self._session() as session:
            row = session.get(ScreenAlertRow, alert_id)
            if row is None:
                raise KeyError(f"unknown screen alert {alert_id!r}")
            row.acknowledged_at = _utcnow()
            session.flush()
            return _row_to_alert(row)

    # --- checking ---------------------------------------------------------
    def alerting_screens(self) -> list[Any]:
        """Return the saved screens that asked to be alerted on."""
        return [
            screen
            for screen in self.engine.registry.list_screens()
            if ALERT_TAG in {str(tag).lower() for tag in (screen.tags or [])}
        ]

    async def check(self, screen: Any) -> ScreenAlert | None:
        """Re-run one screen and return the membership change, if any.

        The run is persisted, so the comparison baseline for the next cycle is
        the run this one just wrote - exactly what the Screener shows.
        """
        previous = self.engine.registry.latest_run(screen.id)
        run = await self.engine.run(screen, persist=True)
        current = list(run.symbols)
        if previous is None:
            # The first observation establishes the baseline; there is no
            # change to report against nothing.
            logger.info("screen alert baseline recorded for %s (%s)", screen.id, run.id)
            return None
        before = list(previous.get("symbols") or [])
        entered = [symbol for symbol in current if symbol not in set(before)]
        exited = [symbol for symbol in before if symbol not in set(current)]
        if not entered and not exited:
            return None
        alert = self.save(
            ScreenAlert(
                screen_id=screen.id,
                screen_name=screen.name,
                run_id=run.id,
                previous_run_id=str(previous.get("id")) if previous.get("id") else None,
                entered=entered,
                exited=exited,
                pass_count=len(current),
                previous_count=len(before),
            )
        )
        await self._publish(alert)
        return alert

    async def check_all(self) -> list[ScreenAlert]:
        """Run one full cycle over every alerting screen."""
        alerts: list[ScreenAlert] = []
        for screen in self.alerting_screens():
            try:
                alert = await self.check(screen)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad screen must not stop the rest
                self.last_error = f"{screen.id}: {type(exc).__name__}: {exc}"
                logger.exception("screen alert check failed for %s", screen.id)
                continue
            if alert is not None:
                alerts.append(alert)
        self.last_cycle_at = _utcnow()
        return alerts

    async def _publish(self, alert: ScreenAlert) -> None:
        """Publish an alert on the ``notifications`` channel."""
        if self.bus is None:
            return
        await self.bus.publish(
            NOTIFICATIONS_CHANNEL,
            {
                "event": "screen_alert",
                "level": "warn",
                "message": alert.headline,
                "alert": alert.as_dict(),
            },
        )

    # --- lifecycle --------------------------------------------------------
    async def start(self) -> None:
        """Start the background cycle. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._loop(), name="screen-alert-monitor")
        logger.info("screen alert monitor started (every %.0fs)", self.interval_seconds)

    async def stop(self) -> None:
        """Stop the background cycle and await the task."""
        self._stopping.set()
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        logger.info("screen alert monitor stopped")

    @property
    def running(self) -> bool:
        """Whether the background cycle is alive."""
        return self._task is not None and not self._task.done()

    async def _loop(self) -> None:
        """Re-run the alerting screens forever, one cycle per interval."""
        while not self._stopping.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=self.interval_seconds)
            if self._stopping.is_set():
                return
            await self.check_all()

    def status(self) -> dict[str, Any]:
        """Report what the monitor is doing, for /api/screens/alerts."""
        return {
            "running": self.running,
            "interval_seconds": self.interval_seconds,
            "last_cycle_at": self.last_cycle_at.isoformat() if self.last_cycle_at else None,
            "last_error": self.last_error,
            "watched": [screen.id for screen in self.alerting_screens()],
        }


__all__ = [
    "ALERT_TAG",
    "DEFAULT_INTERVAL_SECONDS",
    "NOTIFICATIONS_CHANNEL",
    "PIN_TAG",
    "ScreenAlert",
    "ScreenAlertMonitor",
    "ScreenAlertRow",
]
