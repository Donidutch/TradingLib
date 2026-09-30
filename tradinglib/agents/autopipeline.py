"""AutoPipeline: scheduled autonomous pipeline runs.

A schedule points at a pipeline, carries the cadence it fires on and the brief
each run is given, and is fenced by guardrails: how many runs a day it may
start and how many dollars those runs may cost. APScheduler owns the timing
(``AsyncIOScheduler``); this module owns what happens when a timer fires -
re-read the schedule, check the guardrails against the **real** run history,
and start a real run through :class:`tradinglib.agents.runner.PipelineRunner`.

Two cadence forms are accepted:

``* * * * *``
    a five-field crontab, evaluated in the schedule's own timezone.
``@every 30s`` / ``@every 15m`` / ``@hourly`` / ``@daily`` / ``@weekly``
    interval and alias shorthands, for cadences a crontab cannot express (or
    that a test needs to observe inside a second).

Nothing here bypasses the run's own limits: the runner still enforces the
per-run, daily and monthly LLM budgets, and a schedule whose pipeline ends in a
human-approval stage still stops there. The guardrails are an extra, earlier
fence - a schedule that has spent its allowance never starts the run at all.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from tradinglib.agents.runner import (
    ACTIVE_RUN_STATES,
    RUNS_CHANNEL,
    AgentRunRow,
    PipelineRunner,
    RunStatus,
)
from tradinglib.db import Base, ensure_columns, get_engine, get_session_factory
from tradinglib.events import EventBus

logger = logging.getLogger(__name__)

#: Channel schedule activity is announced on (toasts in the UI).
NOTIFICATIONS_CHANNEL: Final[str] = "notifications"

#: Prefix of the ``trigger`` recorded on every run a schedule starts. It is what
#: makes "how many runs did this schedule start today" a real query.
TRIGGER_PREFIX: Final[str] = "schedule:"

#: Crontab expansions of the ``@alias`` cadences.
CRON_ALIASES: Final[dict[str, str]] = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}

_EVERY_RE = re.compile(r"^@every\s+(\d+(?:\.\d+)?)\s*(s|sec|secs|seconds?|m|min|mins|minutes?|h|hours?|d|days?)$")

_UNIT_SECONDS: Final[dict[str, float]] = {
    "s": 1.0,
    "sec": 1.0,
    "secs": 1.0,
    "second": 1.0,
    "seconds": 1.0,
    "m": 60.0,
    "min": 60.0,
    "mins": 60.0,
    "minute": 60.0,
    "minutes": 60.0,
    "h": 3600.0,
    "hour": 3600.0,
    "hours": 3600.0,
    "d": 86400.0,
    "day": 86400.0,
    "days": 86400.0,
}


def utcnow() -> datetime:
    """Return the current UTC time as an aware datetime."""
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    """Attach UTC to a naive datetime read back from SQLite."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _iso(value: datetime | None) -> str | None:
    """Render a datetime as ISO-8601, or ``None``."""
    return value.isoformat() if value else None


def new_schedule_id() -> str:
    """Return a fresh schedule identifier (``SCH-XXXXXX``)."""
    return f"SCH-{uuid.uuid4().hex[:6].upper()}"


def load_timezone(name: str) -> ZoneInfo:
    """Return the :class:`ZoneInfo` for ``name``.

    Raises:
        ValueError: when the zone is unknown, naming what was asked for.
    """
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown timezone {name!r}") from exc


def build_trigger(cron: str, *, timezone_name: str = "UTC") -> BaseTrigger:
    """Build the APScheduler trigger for a cadence expression.

    Raises:
        ValueError: when the expression is not a crontab, an ``@alias`` or an
            ``@every <n><unit>`` interval.
    """
    expression = str(cron or "").strip()
    if not expression:
        raise ValueError("a schedule needs a cadence")
    zone = load_timezone(timezone_name)
    lowered = expression.lower()
    if lowered in CRON_ALIASES:
        return CronTrigger.from_crontab(CRON_ALIASES[lowered], timezone=zone)
    match = _EVERY_RE.match(lowered)
    if match:
        seconds = float(match.group(1)) * _UNIT_SECONDS[match.group(2)]
        if seconds <= 0:
            raise ValueError("an interval cadence must be greater than zero")
        return IntervalTrigger(seconds=seconds, timezone=zone)
    if lowered.startswith("@"):
        raise ValueError(
            f"unknown cadence alias {expression!r}; use one of "
            + ", ".join(sorted(CRON_ALIASES))
            + " or '@every <n><unit>'"
        )
    try:
        return CronTrigger.from_crontab(expression, timezone=zone)
    except ValueError as exc:
        raise ValueError(f"invalid cadence {expression!r}: {exc}") from exc


def next_fire_time(trigger: BaseTrigger, *, after: datetime | None = None) -> datetime | None:
    """Return when ``trigger`` fires next, in UTC."""
    now = after or utcnow()
    fire = trigger.get_next_fire_time(None, now)
    return None if fire is None else fire.astimezone(timezone.utc)


@dataclass(slots=True)
class Schedule:
    """A recurring trigger for one pipeline, with its guardrails."""

    id: str
    pipeline_id: str
    cron: str
    enabled: bool = True
    timezone: str = "UTC"
    name: str = ""
    objective: str | None = None
    pipeline_version: int | None = None
    inputs: dict[str, Any] = field(default_factory=dict)
    max_runs_per_day: int | None = None
    max_spend_usd_per_day: float | None = None
    max_concurrent_runs: int = 1
    #: Per-run stage overrides: stage id -> model id. These change which model
    #: answers for this schedule's runs without editing the saved pipeline.
    model_overrides: dict[str, str] = field(default_factory=dict)
    #: Per-run stage on/off switches: stage id -> included. A stage missing from
    #: the mapping keeps the pipeline's own setting.
    stage_toggles: dict[str, bool] = field(default_factory=dict)
    #: Advance the experiment a passing run produced to VALIDATED automatically.
    #: Promotion past that point always needs human approvals - this only moves
    #: research that cleared its gates out of the queue.
    auto_advance: bool = False
    last_advanced_run_id: str | None = None
    last_advance_note: str | None = None
    last_run_at: datetime | None = None
    last_run_id: str | None = None
    last_status: str | None = None
    last_error: str | None = None
    run_count: int = 0
    next_run_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def trigger_name(self) -> str:
        """The ``trigger`` value the runs this schedule starts carry."""
        return f"{TRIGGER_PREFIX}{self.id}"

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping for the API layer."""
        return {
            "id": self.id,
            "name": self.name,
            "pipeline_id": self.pipeline_id,
            "pipeline_version": self.pipeline_version,
            "cron": self.cron,
            "timezone": self.timezone,
            "enabled": self.enabled,
            "objective": self.objective,
            "inputs": dict(self.inputs),
            "model_overrides": dict(self.model_overrides),
            "stage_toggles": dict(self.stage_toggles),
            "auto_advance": self.auto_advance,
            "last_advanced_run_id": self.last_advanced_run_id,
            "last_advance_note": self.last_advance_note,
            "guardrails": {
                "max_runs_per_day": self.max_runs_per_day,
                "max_spend_usd_per_day": self.max_spend_usd_per_day,
                "max_concurrent_runs": self.max_concurrent_runs,
            },
            "last_run_at": _iso(self.last_run_at),
            "last_run_id": self.last_run_id,
            "last_status": self.last_status,
            "last_error": self.last_error,
            "run_count": self.run_count,
            "next_run_at": _iso(self.next_run_at),
            "trigger": self.trigger_name,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }


class AgentScheduleRow(Base):
    """SQLite row for one AutoPipeline schedule."""

    __tablename__ = "agent_schedules"

    id: Mapped[str] = mapped_column(String(24), primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    pipeline_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    pipeline_version: Mapped[int | None] = mapped_column(Integer(), default=None)
    cron: Mapped[str] = mapped_column(String(120), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    enabled: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=True)
    objective: Mapped[str | None] = mapped_column(Text(), default=None)
    inputs: Mapped[dict[str, Any]] = mapped_column(JSON(), nullable=False, default=dict)
    max_runs_per_day: Mapped[int | None] = mapped_column(Integer(), default=None)
    max_spend_usd_per_day: Mapped[float | None] = mapped_column(Float(), default=None)
    max_concurrent_runs: Mapped[int] = mapped_column(Integer(), nullable=False, default=1)
    model_overrides: Mapped[dict[str, Any]] = mapped_column(JSON(), nullable=False, default=dict)
    stage_toggles: Mapped[dict[str, Any]] = mapped_column(JSON(), nullable=False, default=dict)
    auto_advance: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    last_advanced_run_id: Mapped[str | None] = mapped_column(String(32), default=None)
    last_advance_note: Mapped[str | None] = mapped_column(Text(), default=None)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(), default=None)
    last_run_id: Mapped[str | None] = mapped_column(String(32), default=None)
    last_status: Mapped[str | None] = mapped_column(String(32), default=None)
    last_error: Mapped[str | None] = mapped_column(Text(), default=None)
    run_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)


_OWNED_TABLES = (AgentScheduleRow.__table__,)


def ensure_schema() -> None:
    """Create the schedule table if it does not exist yet, then migrate it."""
    Base.metadata.create_all(get_engine(), tables=list(_OWNED_TABLES))
    for table in _OWNED_TABLES:
        ensure_columns(table)


def _research_ids(outputs: Mapping[str, Any]) -> tuple[set[str], set[str]]:
    """Collect the experiment and backtest ids a run's stage outputs mention.

    Stage outputs are free-form JSON produced by the agents, so the ids are
    gathered by walking the structure for the two well-known keys rather than
    assuming one particular stage shape.
    """
    experiments: set[str] = set()
    backtests: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                name = str(key)
                if name in {"experiment_id", "experiment"} and isinstance(value, str) and value:
                    experiments.add(value)
                elif name in {"backtest_id", "backtest"} and isinstance(value, str) and value:
                    backtests.add(value)
                else:
                    walk(value)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)

    walk(outputs)
    return experiments, backtests


def _clean_model_overrides(value: Any) -> dict[str, str]:
    """Normalise a ``{stage_id: model}`` mapping, dropping empty selections.

    An empty string means "use the stage's own model", which is expressed by
    the key being absent rather than by storing a blank.
    """
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, str] = {}
    for key, model in value.items():
        stage_id = str(key).strip()
        chosen = "" if model is None else str(model).strip()
        if stage_id and chosen:
            out[stage_id] = chosen
    return out


def _clean_stage_toggles(value: Any) -> dict[str, bool]:
    """Normalise a ``{stage_id: included}`` mapping."""
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key).strip(): bool(flag) for key, flag in value.items() if str(key).strip()
    }


class GuardrailBlocked(RuntimeError):
    """Raised when a schedule is refused because a guardrail is spent."""

    def __init__(self, schedule_id: str, reason: str) -> None:
        """Create the error naming the schedule and the guardrail."""
        super().__init__(f"schedule {schedule_id} was blocked: {reason}")
        self.schedule_id = schedule_id
        self.reason = reason


class AutoPipeline:
    """Owns the schedules and drives them through the runner."""

    #: Fields :meth:`create` and :meth:`update` accept.
    FIELDS: Final[frozenset[str]] = frozenset(
        {
            "name",
            "pipeline_id",
            "cron",
            "timezone",
            "enabled",
            "objective",
            "pipeline_version",
            "inputs",
            "max_runs_per_day",
            "max_spend_usd_per_day",
            "max_concurrent_runs",
            "model_overrides",
            "stage_toggles",
            "auto_advance",
        }
    )

    def __init__(
        self,
        runner: PipelineRunner,
        bus: EventBus,
        *,
        session_factory: sessionmaker[Session] | None = None,
        scheduler: AsyncIOScheduler | None = None,
        audit: Any | None = None,
    ) -> None:
        """Create the scheduler bound to a runner and an event bus.

        Args:
            runner: the runner every fired schedule starts a run through.
            bus: event bus schedule activity is announced on.
            session_factory: SQLAlchemy session factory; defaults to the
                process-wide one from :mod:`tradinglib.db`.
            scheduler: an APScheduler instance to drive. One is created on
                :meth:`start` when this is not given.
            audit: audit sink; defaults to the process-wide audit log.
        """
        self.runner = runner
        self.bus = bus
        self._session_factory = session_factory
        self._scheduler = scheduler
        self._owns_scheduler = scheduler is None
        self._audit = audit
        self._audit_resolved = audit is not None
        self._running = False
        self._advance_sub: Any | None = None
        ensure_schema()

    # --- plumbing ---------------------------------------------------------
    @contextmanager
    def _session(self) -> Iterator[Session]:
        """Transactional session scope bound to this scheduler."""
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

    @property
    def running(self) -> bool:
        """Whether schedules are currently being fired."""
        return self._running

    @property
    def scheduler(self) -> AsyncIOScheduler | None:
        """The APScheduler instance, once :meth:`start` has built one."""
        return self._scheduler

    @property
    def audit(self) -> Any | None:
        """The audit log schedule activity is recorded in."""
        if not self._audit_resolved:
            self._audit_resolved = True
            try:
                from tradinglib.agents.audit import get_audit_log

                self._audit = get_audit_log()
            except Exception:  # noqa: BLE001 - auditing must not stop a schedule
                logger.warning("audit log unavailable to AutoPipeline", exc_info=True)
                self._audit = None
        return self._audit

    def _record_audit(self, actor: str, action: str, **fields: Any) -> None:
        """Append an audit record, tolerating an audit store that is not up."""
        log = self.audit
        if log is None:
            return
        try:
            log.record(actor, action, **fields)
        except Exception:  # noqa: BLE001 - auditing must never break a run
            logger.warning("audit record failed for %s/%s", actor, action, exc_info=True)

    async def _announce(self, message: str, **detail: Any) -> None:
        """Publish schedule activity on the notifications channel."""
        try:
            await self.bus.publish(
                NOTIFICATIONS_CHANNEL,
                {"kind": "autopipeline", "message": message, **detail},
            )
        except Exception:  # noqa: BLE001 - a toast is not the schedule
            logger.warning("publishing AutoPipeline activity failed", exc_info=True)

    @staticmethod
    def _to_dataclass(row: AgentScheduleRow) -> Schedule:
        """Map an ORM row onto the dataclass (without the computed next run)."""
        return Schedule(
            id=row.id,
            pipeline_id=row.pipeline_id,
            cron=row.cron,
            enabled=bool(row.enabled),
            timezone=row.timezone,
            name=row.name,
            objective=row.objective,
            pipeline_version=row.pipeline_version,
            inputs=dict(row.inputs or {}),
            max_runs_per_day=row.max_runs_per_day,
            max_spend_usd_per_day=row.max_spend_usd_per_day,
            max_concurrent_runs=int(row.max_concurrent_runs),
            model_overrides={
                str(key): str(value)
                for key, value in (row.model_overrides or {}).items()
                if value
            },
            stage_toggles={
                str(key): bool(value) for key, value in (row.stage_toggles or {}).items()
            },
            auto_advance=bool(row.auto_advance),
            last_advanced_run_id=row.last_advanced_run_id,
            last_advance_note=row.last_advance_note,
            last_run_at=_aware(row.last_run_at),
            last_run_id=row.last_run_id,
            last_status=row.last_status,
            last_error=row.last_error,
            run_count=int(row.run_count),
            created_at=_aware(row.created_at),
            updated_at=_aware(row.updated_at),
        )

    def _with_next_run(self, schedule: Schedule) -> Schedule:
        """Attach the next fire time, from the live job when there is one."""
        job = self._job(schedule.id)
        if job is not None and getattr(job, "next_run_time", None):
            schedule.next_run_at = job.next_run_time.astimezone(timezone.utc)
            return schedule
        if not schedule.enabled:
            schedule.next_run_at = None
            return schedule
        try:
            schedule.next_run_at = next_fire_time(
                build_trigger(schedule.cron, timezone_name=schedule.timezone)
            )
        except ValueError:
            logger.warning("schedule %s has an invalid cadence %r", schedule.id, schedule.cron)
            schedule.next_run_at = None
        return schedule

    # --- lifecycle --------------------------------------------------------
    async def start(self) -> None:
        """Load enabled schedules and begin firing them."""
        if self._running:
            return
        if self._scheduler is None:
            self._scheduler = AsyncIOScheduler(timezone=timezone.utc)
            self._owns_scheduler = True
        if not self._scheduler.running:
            self._scheduler.start()
        self._running = True
        loaded = 0
        for schedule in self._all():
            if schedule.enabled and self._install(schedule):
                loaded += 1
        if self._advance_sub is None and self.bus is not None:
            self._advance_sub = self.bus.subscribe(RUNS_CHANNEL, self._on_run_event)
        logger.info("AutoPipeline started with %d enabled schedule(s)", loaded)
        self._record_audit("autopipeline", "started the schedule loop", result=f"{loaded} enabled")
        await self._announce(f"AutoPipeline started with {loaded} enabled schedule(s)", count=loaded)

    async def stop(self) -> None:
        """Stop firing schedules and cancel pending timers."""
        if not self._running:
            return
        self._running = False
        if self._advance_sub is not None:
            self._advance_sub.cancel()
            self._advance_sub = None
        scheduler = self._scheduler
        if scheduler is not None:
            for schedule in self._all():
                self._remove_job(schedule.id)
            if self._owns_scheduler and scheduler.running:
                scheduler.shutdown(wait=False)
                self._scheduler = None
        logger.info("AutoPipeline stopped")
        self._record_audit("autopipeline", "stopped the schedule loop")

    async def _on_run_event(self, event: Any) -> None:
        """Apply *auto-advance on pass* when a scheduled run finishes.

        Subscribed to the ``runs`` channel while the loop is running. Anything
        that is not a completed run started by one of this workspace's schedules
        is ignored, and a failure here is logged rather than allowed to break
        the event fan-out.
        """
        payload = getattr(event, "payload", event)
        if not isinstance(payload, Mapping) or payload.get("phase") != "complete":
            return
        run_id = str(payload.get("run_id") or "")
        if not run_id:
            return
        try:
            run = self.runner.get(run_id, include_logs=False)
        except KeyError:  # pragma: no cover - the run was deleted meanwhile
            return
        trigger = str(run.trigger or "")
        if not trigger.startswith(TRIGGER_PREFIX):
            return
        schedule_id = trigger[len(TRIGGER_PREFIX) :]
        try:
            await self.advance(schedule_id, run_id)
        except KeyError:  # pragma: no cover - the schedule was deleted meanwhile
            logger.debug("auto-advance skipped: schedule %s is gone", schedule_id)
        except Exception:  # noqa: BLE001 - the policy must not break the bus
            logger.warning("auto-advance for %s failed", run_id, exc_info=True)

    # --- job management ---------------------------------------------------
    def _job_id(self, schedule_id: str) -> str:
        """APScheduler job id for a schedule."""
        return f"tradinglib-autopipeline-{schedule_id}"

    def _job(self, schedule_id: str) -> Any | None:
        """Return the live APScheduler job for a schedule, if there is one."""
        if self._scheduler is None or not self._scheduler.running:
            return None
        return self._scheduler.get_job(self._job_id(schedule_id))

    def _install(self, schedule: Schedule) -> bool:
        """Register (or replace) the APScheduler job for a schedule."""
        if self._scheduler is None or not self._running:
            return False
        try:
            trigger = build_trigger(schedule.cron, timezone_name=schedule.timezone)
        except ValueError:
            logger.warning(
                "schedule %s not installed: invalid cadence %r", schedule.id, schedule.cron
            )
            return False
        self._scheduler.add_job(
            self._fire_job,
            trigger=trigger,
            args=[schedule.id],
            id=self._job_id(schedule.id),
            name=schedule.name or schedule.pipeline_id,
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=None,
        )
        return True

    def _remove_job(self, schedule_id: str) -> None:
        """Remove a schedule's job, if it is installed."""
        if self._scheduler is None or not self._scheduler.running:
            return
        job = self._scheduler.get_job(self._job_id(schedule_id))
        if job is not None:
            job.remove()

    def _sync(self, schedule: Schedule) -> None:
        """Install or remove a schedule's job to match its enabled flag."""
        if not self._running:
            return
        if schedule.enabled:
            self._install(schedule)
        else:
            self._remove_job(schedule.id)

    async def _fire_job(self, schedule_id: str) -> None:
        """APScheduler entry point: fire a schedule, swallowing its failures."""
        try:
            await self.fire(schedule_id)
        except GuardrailBlocked as exc:
            logger.info("%s", exc)
        except Exception:  # noqa: BLE001 - a bad schedule must not kill the loop
            logger.exception("schedule %s failed to fire", schedule_id)

    # --- CRUD -------------------------------------------------------------
    def _all(self) -> list[Schedule]:
        """Return every schedule, oldest first, without the computed next run."""
        statement = select(AgentScheduleRow).order_by(AgentScheduleRow.created_at.asc())
        with self._session() as session:
            return [self._to_dataclass(row) for row in session.scalars(statement)]

    def list(self, *, enabled: bool | None = None) -> list[Schedule]:
        """Return every schedule with its computed next run time."""
        items = [self._with_next_run(schedule) for schedule in self._all()]
        if enabled is not None:
            items = [item for item in items if item.enabled is enabled]
        return items

    def get(self, schedule_id: str) -> Schedule:
        """Return one schedule.

        Raises:
            KeyError: when the identifier is unknown.
        """
        with self._session() as session:
            row = session.get(AgentScheduleRow, schedule_id)
            if row is None:
                raise KeyError(f"unknown schedule {schedule_id!r}")
            schedule = self._to_dataclass(row)
        return self._with_next_run(schedule)

    def create(self, pipeline_id: str, cron: str, **fields: Any) -> Schedule:
        """Create a schedule.

        Raises:
            ValueError: when the pipeline id is empty, the cadence does not
                parse, a guardrail is negative, or a field is unknown.
            KeyError: when the pipeline does not exist.
        """
        identifier = str(pipeline_id or "").strip()
        if not identifier:
            raise ValueError("a schedule needs a pipeline id")
        unknown = set(fields) - self.FIELDS - {"schedule_id"}
        if unknown:
            raise ValueError(f"unknown field(s): {', '.join(sorted(unknown))}")
        # A schedule for a pipeline that does not exist can never fire; refusing
        # here beats a timer that fails silently every night.
        self.runner.pipelines.get(identifier)
        zone = str(fields.get("timezone") or "UTC")
        build_trigger(cron, timezone_name=zone)
        now = utcnow()
        row = AgentScheduleRow(
            id=str(fields.get("schedule_id") or new_schedule_id()),
            name=str(fields.get("name") or "").strip(),
            pipeline_id=identifier,
            pipeline_version=fields.get("pipeline_version"),
            cron=str(cron).strip(),
            timezone=zone,
            enabled=bool(fields.get("enabled", True)),
            objective=fields.get("objective"),
            inputs=dict(fields.get("inputs") or {}),
            max_runs_per_day=self._positive_int(fields.get("max_runs_per_day"), "max_runs_per_day"),
            max_spend_usd_per_day=self._positive_float(
                fields.get("max_spend_usd_per_day"), "max_spend_usd_per_day"
            ),
            max_concurrent_runs=int(fields.get("max_concurrent_runs") or 1),
            model_overrides=_clean_model_overrides(fields.get("model_overrides")),
            stage_toggles=_clean_stage_toggles(fields.get("stage_toggles")),
            auto_advance=bool(fields.get("auto_advance", False)),
            created_at=now,
            updated_at=now,
        )
        if row.max_concurrent_runs < 1:
            raise ValueError("max_concurrent_runs must be at least 1")
        with self._session() as session:
            if session.get(AgentScheduleRow, row.id) is not None:
                raise ValueError(f"schedule {row.id!r} already exists")
            session.add(row)
            session.flush()
            schedule = self._to_dataclass(row)
        self._sync(schedule)
        self._record_audit(
            "autopipeline",
            "created a schedule",
            target=schedule.id,
            pipeline_id=schedule.pipeline_id,
            cron=schedule.cron,
        )
        return self._with_next_run(schedule)

    def update(self, schedule_id: str, **changes: Any) -> Schedule:
        """Update a schedule.

        Raises:
            KeyError: when the identifier is unknown.
            ValueError: when a field is unknown or a value does not validate.
        """
        unknown = set(changes) - self.FIELDS
        if unknown:
            raise ValueError(f"unknown field(s): {', '.join(sorted(unknown))}")
        with self._session() as session:
            row = session.get(AgentScheduleRow, schedule_id)
            if row is None:
                raise KeyError(f"unknown schedule {schedule_id!r}")
            if "pipeline_id" in changes:
                pipeline_id = str(changes["pipeline_id"] or "").strip()
                if not pipeline_id:
                    raise ValueError("a schedule needs a pipeline id")
                self.runner.pipelines.get(pipeline_id)
                row.pipeline_id = pipeline_id
            cron = str(changes.get("cron", row.cron)).strip()
            zone = str(changes.get("timezone", row.timezone))
            if "cron" in changes or "timezone" in changes:
                build_trigger(cron, timezone_name=zone)
                row.cron = cron
                row.timezone = zone
            if "name" in changes:
                row.name = str(changes["name"] or "").strip()
            if "objective" in changes:
                row.objective = changes["objective"]
            if "pipeline_version" in changes:
                value = changes["pipeline_version"]
                row.pipeline_version = None if value is None else int(value)
            if "inputs" in changes:
                row.inputs = dict(changes["inputs"] or {})
            if "enabled" in changes:
                row.enabled = bool(changes["enabled"])
            if "max_runs_per_day" in changes:
                row.max_runs_per_day = self._positive_int(
                    changes["max_runs_per_day"], "max_runs_per_day"
                )
            if "max_spend_usd_per_day" in changes:
                row.max_spend_usd_per_day = self._positive_float(
                    changes["max_spend_usd_per_day"], "max_spend_usd_per_day"
                )
            if "max_concurrent_runs" in changes:
                value = int(changes["max_concurrent_runs"] or 1)
                if value < 1:
                    raise ValueError("max_concurrent_runs must be at least 1")
                row.max_concurrent_runs = value
            if "model_overrides" in changes:
                row.model_overrides = _clean_model_overrides(changes["model_overrides"])
            if "stage_toggles" in changes:
                row.stage_toggles = _clean_stage_toggles(changes["stage_toggles"])
            if "auto_advance" in changes:
                row.auto_advance = bool(changes["auto_advance"])
            row.updated_at = utcnow()
            session.flush()
            schedule = self._to_dataclass(row)
        self._sync(schedule)
        self._record_audit(
            "autopipeline",
            "updated a schedule",
            target=schedule.id,
            changed=sorted(changes),
        )
        return self._with_next_run(schedule)

    def delete(self, schedule_id: str) -> None:
        """Delete a schedule and cancel its timer.

        Raises:
            KeyError: when the identifier is unknown.
        """
        with self._session() as session:
            row = session.get(AgentScheduleRow, schedule_id)
            if row is None:
                raise KeyError(f"unknown schedule {schedule_id!r}")
            session.delete(row)
        self._remove_job(schedule_id)
        self._record_audit("autopipeline", "deleted a schedule", target=schedule_id)

    @staticmethod
    def _positive_int(value: Any, name: str) -> int | None:
        """Validate an optional positive integer guardrail."""
        if value is None or value == "":
            return None
        number = int(value)
        if number < 0:
            raise ValueError(f"{name} cannot be negative")
        return number

    @staticmethod
    def _positive_float(value: Any, name: str) -> float | None:
        """Validate an optional positive float guardrail."""
        if value is None or value == "":
            return None
        number = float(value)
        if number < 0:
            raise ValueError(f"{name} cannot be negative")
        return number

    # --- guardrails -------------------------------------------------------
    def _day_start(self, schedule: Schedule, *, now: datetime | None = None) -> datetime:
        """Return the UTC instant the schedule's local day began."""
        moment = (now or utcnow()).astimezone(load_timezone(schedule.timezone))
        local_midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
        return local_midnight.astimezone(timezone.utc)

    def usage(self, schedule_id: str, *, now: datetime | None = None) -> dict[str, Any]:
        """Return what this schedule has really used today.

        Counted from the run table by the ``schedule:<id>`` trigger the runs
        carry - not from a counter this module keeps, which a restart would
        lose and a manual run would never touch.
        """
        schedule = self.get(schedule_id)
        since = self._day_start(schedule, now=now)
        statement = select(AgentRunRow).where(
            AgentRunRow.trigger == schedule.trigger_name, AgentRunRow.created_at >= since
        )
        with self._session() as session:
            rows = list(session.scalars(statement))
        active = [row for row in rows if row.status in {str(state) for state in ACTIVE_RUN_STATES}]
        return {
            "schedule_id": schedule.id,
            "window_start": _iso(since),
            "runs": len(rows),
            "active_runs": len(active),
            "spend_usd": round(sum(float(row.cost_usd) for row in rows), 6),
            "max_runs_per_day": schedule.max_runs_per_day,
            "max_spend_usd_per_day": schedule.max_spend_usd_per_day,
            "max_concurrent_runs": schedule.max_concurrent_runs,
        }

    def guardrail_block(self, schedule_id: str, *, now: datetime | None = None) -> str | None:
        """Return why the schedule may not run right now, or ``None``."""
        usage = self.usage(schedule_id, now=now)
        limit_runs = usage["max_runs_per_day"]
        if limit_runs is not None and usage["runs"] >= limit_runs:
            return (
                f"the daily run guardrail is spent ({usage['runs']}/{limit_runs} runs "
                "started today)"
            )
        limit_spend = usage["max_spend_usd_per_day"]
        if limit_spend is not None and usage["spend_usd"] >= limit_spend:
            return (
                f"the daily spend guardrail is spent (${usage['spend_usd']:.4f} of "
                f"${limit_spend:.2f})"
            )
        if usage["active_runs"] >= usage["max_concurrent_runs"]:
            return (
                f"{usage['active_runs']} run(s) from this schedule are still active "
                f"(limit {usage['max_concurrent_runs']})"
            )
        return None

    # --- firing -----------------------------------------------------------
    async def fire(self, schedule_id: str, *, force: bool = False) -> str:
        """Start one run for a schedule and return its run id.

        Args:
            schedule_id: the schedule to fire.
            force: skip the guardrail check (an operator overriding it by hand).

        Raises:
            KeyError: when the schedule is unknown.
            GuardrailBlocked: when a guardrail refuses the run, or the schedule
                is disabled and this is not a forced run.
        """
        schedule = self.get(schedule_id)
        if not schedule.enabled and not force:
            raise GuardrailBlocked(schedule_id, "the schedule is disabled")
        if not force:
            reason = self.guardrail_block(schedule_id)
            if reason:
                self._note_block(schedule, reason)
                self._record_audit(
                    "autopipeline",
                    "blocked a scheduled run",
                    target=schedule.id,
                    result="blocked",
                    reason=reason,
                    pipeline_id=schedule.pipeline_id,
                )
                await self._announce(
                    f"AutoPipeline held {schedule.name or schedule.pipeline_id}: {reason}",
                    schedule_id=schedule.id,
                    blocked=True,
                    reason=reason,
                )
                raise GuardrailBlocked(schedule_id, reason)
        try:
            run = await self.runner.start(
                schedule.pipeline_id,
                trigger=schedule.trigger_name,
                inputs=dict(schedule.inputs),
                version=schedule.pipeline_version,
                objective=schedule.objective,
                name=schedule.name or None,
                stage_overrides=self.stage_overrides(schedule),
            )
        except Exception as exc:  # noqa: BLE001 - recorded on the schedule and re-raised
            self._note_failure(schedule, str(exc))
            self._record_audit(
                "autopipeline",
                "failed to start a scheduled run",
                target=schedule.id,
                result="error",
                error=str(exc),
            )
            await self._announce(
                f"AutoPipeline could not start {schedule.pipeline_id}: {exc}",
                schedule_id=schedule.id,
                error=str(exc),
            )
            raise
        self._note_started(schedule, run.id)
        self._record_audit(
            "autopipeline",
            "started a scheduled run",
            target=run.id,
            run_id=run.id,
            result=str(run.status),
            schedule_id=schedule.id,
            pipeline_id=schedule.pipeline_id,
        )
        await self._announce(
            f"AutoPipeline started {run.id} for {schedule.name or schedule.pipeline_id}",
            schedule_id=schedule.id,
            run_id=run.id,
            pipeline_id=schedule.pipeline_id,
        )
        return run.id

    async def trigger_now(self, schedule_id: str, *, force: bool = False) -> str:
        """Fire a schedule immediately; returns the created run id."""
        return await self.fire(schedule_id, force=force)

    @staticmethod
    def stage_overrides(schedule: Schedule) -> dict[str, dict[str, Any]]:
        """Return the per-run stage overrides a schedule's runs are started with.

        Model picks and stage toggles are *per-run* overrides of the saved
        pipeline (that is exactly what the reference's model selects say they
        are), so they are applied to the run's frozen document and recorded on
        the run rather than written back to the pipeline.
        """
        overrides: dict[str, dict[str, Any]] = {}
        for stage_id, model in schedule.model_overrides.items():
            overrides.setdefault(stage_id, {})["model"] = model
        for stage_id, included in schedule.stage_toggles.items():
            overrides.setdefault(stage_id, {})["include_in_run"] = bool(included)
        return overrides

    async def advance(self, schedule_id: str, run_id: str) -> dict[str, Any]:
        """Apply the *auto-advance on pass* policy to a finished run.

        The policy moves research that cleared its gates out of the queue and
        does nothing else: an experiment produced by the run is set to
        ``validated`` when a validation report for its backtest passed every
        gate. Anything beyond that - paper, live - still requires the human
        approvals the pipeline's approval stage enforces.

        Args:
            schedule_id: the schedule whose policy applies.
            run_id: the finished run to consider.

        Returns:
            ``{"advanced": bool, "reason": str, ...}`` - always with a reason,
            so a run that did *not* advance says why.
        """
        schedule = self.get(schedule_id)
        if not schedule.auto_advance:
            return {"advanced": False, "reason": "auto-advance is off for this schedule"}
        run = self.runner.get(run_id, include_logs=False)
        if str(run.status) != "succeeded":
            outcome = {
                "advanced": False,
                "reason": f"the run finished {run.status}, so there is nothing to advance",
            }
            self._note_advance(schedule.id, run_id, outcome["reason"])
            return outcome
        experiment_ids, backtest_ids = _research_ids(run.outputs)
        if not experiment_ids:
            outcome = {
                "advanced": False,
                "reason": "the run produced no experiment to advance",
                "backtests": sorted(backtest_ids),
            }
            self._note_advance(schedule.id, run_id, outcome["reason"])
            return outcome
        passed, detail = self._gates_passed(backtest_ids)
        if not passed:
            outcome = {"advanced": False, "reason": detail, "experiments": sorted(experiment_ids)}
            self._note_advance(schedule.id, run_id, outcome["reason"])
            return outcome
        advanced: list[str] = []
        for experiment_id in sorted(experiment_ids):
            try:
                self._experiments().update(experiment_id, stage="validated")
            except Exception as exc:  # noqa: BLE001 - one bad id must not break the policy
                logger.warning("auto-advance could not update %s: %s", experiment_id, exc)
                continue
            advanced.append(experiment_id)
        reason = (
            f"{detail}; advanced {', '.join(advanced)} to validated"
            if advanced
            else f"{detail}, but no experiment could be updated"
        )
        self._note_advance(schedule.id, run_id, reason, advanced=bool(advanced))
        self._record_audit(
            "autopipeline",
            "auto-advanced research",
            target=schedule.id,
            run_id=run_id,
            result="advanced" if advanced else "no-op",
            experiments=advanced,
        )
        if advanced:
            await self._announce(
                f"AutoPipeline advanced {', '.join(advanced)} to validated after {run_id}",
                schedule_id=schedule.id,
                run_id=run_id,
                experiments=advanced,
            )
        return {"advanced": bool(advanced), "reason": reason, "experiments": advanced}

    def _experiments(self) -> Any:
        """Return the process-wide experiment engine (imported lazily)."""
        from tradinglib.research.experiments import get_experiment_engine

        return get_experiment_engine()

    def _gates_passed(self, backtest_ids: set[str]) -> tuple[bool, str]:
        """Return whether every backtest the run produced cleared its gates."""
        if not backtest_ids:
            return False, "the run recorded no backtest, so no gate has judged it"
        from tradinglib.research.validation import get_validation_engine

        engine = get_validation_engine()
        verdicts: list[str] = []
        for backtest_id in sorted(backtest_ids):
            reports = engine.reports(backtest_id=backtest_id, limit=1)
            if not reports:
                return False, f"{backtest_id} has no validation report yet"
            if not reports[0].passed:
                return False, f"{backtest_id} did not pass every gate"
            verdicts.append(backtest_id)
        return True, f"{', '.join(verdicts)} passed every gate"

    def _note_advance(
        self, schedule_id: str, run_id: str, note: str, *, advanced: bool = False
    ) -> None:
        """Record the auto-advance outcome on the schedule."""
        with self._session() as session:
            row = session.get(AgentScheduleRow, schedule_id)
            if row is None:  # pragma: no cover - the schedule was just read
                return
            row.last_advance_note = note
            if advanced:
                row.last_advanced_run_id = run_id
            row.updated_at = utcnow()

    def _note_started(self, schedule: Schedule, run_id: str) -> None:
        """Record on the schedule that a run started."""
        with self._session() as session:
            row = session.get(AgentScheduleRow, schedule.id)
            if row is None:
                return
            row.last_run_at = utcnow()
            row.last_run_id = run_id
            row.last_status = str(RunStatus.RUNNING)
            row.last_error = None
            row.run_count = int(row.run_count) + 1
            row.updated_at = utcnow()

    def _note_block(self, schedule: Schedule, reason: str) -> None:
        """Record on the schedule that a firing was held back."""
        with self._session() as session:
            row = session.get(AgentScheduleRow, schedule.id)
            if row is None:
                return
            row.last_status = "blocked"
            row.last_error = reason
            row.updated_at = utcnow()

    def _note_failure(self, schedule: Schedule, error: str) -> None:
        """Record on the schedule that a firing failed to start."""
        with self._session() as session:
            row = session.get(AgentScheduleRow, schedule.id)
            if row is None:
                return
            row.last_status = "error"
            row.last_error = error
            row.updated_at = utcnow()

    # --- reporting --------------------------------------------------------
    def status(self) -> dict[str, Any]:
        """Return the AutoPipeline tab header: schedules, next fire, activity."""
        schedules = self.list()
        enabled = [item for item in schedules if item.enabled]
        upcoming = [item.next_run_at for item in enabled if item.next_run_at]
        return {
            "running": self._running,
            "total": len(schedules),
            "enabled": len(enabled),
            "disabled": len(schedules) - len(enabled),
            "next_run_at": _iso(min(upcoming)) if upcoming else None,
            "schedules": [item.as_dict() for item in schedules],
        }

    def runs(self, schedule_id: str, *, limit: int = 25) -> list[dict[str, Any]]:
        """Return the runs this schedule really started, newest first."""
        schedule = self.get(schedule_id)
        statement = (
            select(AgentRunRow)
            .where(AgentRunRow.trigger == schedule.trigger_name)
            .order_by(AgentRunRow.created_at.desc())
            .limit(max(1, int(limit)))
        )
        with self._session() as session:
            rows = list(session.scalars(statement))
        return [
            {
                "run_id": row.id,
                "status": row.status,
                "pipeline_id": row.pipeline_id,
                "pipeline_version": int(row.pipeline_version),
                "cost_usd": round(float(row.cost_usd), 6),
                "created_at": _iso(_aware(row.created_at)),
                "finished_at": _iso(_aware(row.finished_at)),
            }
            for row in rows
        ]


_autopipeline: AutoPipeline | None = None


def get_autopipeline() -> AutoPipeline:
    """Return the process-wide AutoPipeline, creating it on first use."""
    global _autopipeline
    if _autopipeline is None:
        from tradinglib.agents.runner import get_pipeline_runner
        from tradinglib.events import get_event_bus

        _autopipeline = AutoPipeline(get_pipeline_runner(), get_event_bus())
    return _autopipeline


def reset_autopipeline(auto: AutoPipeline | None = None) -> AutoPipeline | None:
    """Install (or forget) the process-wide AutoPipeline. Returns it."""
    global _autopipeline
    _autopipeline = auto
    return _autopipeline


__all__ = [
    "CRON_ALIASES",
    "TRIGGER_PREFIX",
    "AgentScheduleRow",
    "AutoPipeline",
    "GuardrailBlocked",
    "Schedule",
    "build_trigger",
    "ensure_schema",
    "get_autopipeline",
    "load_timezone",
    "new_schedule_id",
    "next_fire_time",
    "reset_autopipeline",
]
