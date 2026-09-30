"""Asyncio ingestion scheduler.

Runs each enabled source on its configured schedule, records outcomes on the
catalog and publishes progress on the ``ingest`` event channel. Started and
stopped by the FastAPI lifespan in ``tradinglib.api.app``.

The scheduler owns one background task. Every tick it asks which jobs are due,
runs those concurrently, persists what each one ingested and reschedules it.
Records land in the Parquet/DuckDB store through :mod:`tradinglib.data.store`:
bar-shaped records become bars, ``namespace``/``name``/``value`` records become
named series, and anything else is written as a dataset frame named after the
source. Nothing is written when a source returns no records.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from tradinglib.data.base import Bar, BarInterval, ProviderError, Series, to_utc
from tradinglib.data.ingest.catalog import INGEST_CHANNEL, SourceCatalog, agreed_bar_interval
from tradinglib.data.ingest.connectors.base import Connector, FetchResult
from tradinglib.data.ingest.formats import parse_timestamp
from tradinglib.data.ingest.source import Schedule, ScheduleKind, SourceConfig, parse_schedule
from tradinglib.data.store import TimeSeriesStore, get_store
from tradinglib.events import EventBus

logger = logging.getLogger(__name__)

#: How often the scheduler wakes up to look for due jobs.
DEFAULT_TICK_SECONDS: float = 1.0

#: Maximum number of sources ingested concurrently in one tick.
MAX_CONCURRENT_JOBS: int = 4

#: Record keys that identify a bar row.
_BAR_KEYS: frozenset[str] = frozenset({"symbol", "ts", "close"})

#: Record keys that identify a named-series row.
_SERIES_KEYS: frozenset[str] = frozenset({"name", "ts", "value"})


def _utcnow() -> datetime:
    """Return the current time as an aware UTC datetime."""
    return datetime.now(timezone.utc)


def _same_cadence(previous: Schedule | None, current: Schedule) -> bool:
    """Whether two schedules fire at the same times.

    Compares what drives the schedule rather than how it was written, so
    ``1d`` and ``24h`` count as the same cadence and do not re-time a pending
    fire. ``None`` (no previous schedule) counts as unchanged - there is
    nothing to re-time.
    """
    if previous is None:
        return True
    return (previous.kind, previous.interval_seconds, previous.cron) == (
        current.kind,
        current.interval_seconds,
        current.cron,
    )


@dataclass(slots=True)
class ScheduledJob:
    """One scheduled ingestion job."""

    source_id: str
    schedule: str
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    last_status: str = "pending"
    last_error: str | None = None
    runs: int = 0
    rows: int = 0
    last_rows: int = 0
    last_latency_ms: float | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping for the Data Ingestion page."""
        return {
            "source_id": self.source_id,
            "schedule": self.schedule,
            "next_run_at": self.next_run_at.isoformat() if self.next_run_at else None,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "last_status": self.last_status,
            "last_error": self.last_error,
            "runs": self.runs,
            "rows": self.rows,
            "last_rows": self.last_rows,
            "last_latency_ms": self.last_latency_ms,
        }


@dataclass(slots=True)
class IngestOutcome:
    """What one ingestion run actually did."""

    source_id: str
    ok: bool
    rows_fetched: int = 0
    rows_written: int = 0
    dataset: str | None = None
    latency_ms: float | None = None
    error: str | None = None
    skipped: str | None = None
    started_at: datetime = field(default_factory=_utcnow)
    finished_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping (the ``ingest`` event payload)."""
        return {
            "source_id": self.source_id,
            "ok": self.ok,
            "rows_fetched": self.rows_fetched,
            "rows_written": self.rows_written,
            "dataset": self.dataset,
            "latency_ms": self.latency_ms,
            "error": self.error,
            "skipped": self.skipped,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


def classify_records(records: list[dict[str, Any]]) -> str:
    """Return ``"bars"``, ``"series"`` or ``"frame"`` for a record batch."""
    if not records:
        return "frame"
    keys = set(records[0])
    if _BAR_KEYS <= keys:
        return "bars"
    if _SERIES_KEYS <= keys:
        return "series"
    return "frame"


def records_to_bars(records: list[dict[str, Any]], *, default_source: str) -> list[Bar]:
    """Convert bar-shaped records into :class:`~tradinglib.data.base.Bar` objects.

    Records missing a usable timestamp or close price are dropped (and counted
    by the caller as not written) rather than being written with a made-up
    value.
    """
    bars: list[Bar] = []
    for record in records:
        ts = parse_timestamp(record.get("ts"))
        close = _as_float(record.get("close"))
        symbol = str(record.get("symbol") or "").strip().upper()
        if ts is None or close is None or not symbol:
            continue
        raw_interval = str(record.get("interval") or BarInterval.DAY_1)
        try:
            interval = BarInterval(raw_interval)
        except ValueError:
            interval = BarInterval.DAY_1
        bars.append(
            Bar(
                symbol=symbol,
                ts=ts,
                open=_as_float(record.get("open")) or close,
                high=_as_float(record.get("high")) or close,
                low=_as_float(record.get("low")) or close,
                close=close,
                volume=_as_float(record.get("volume")) or 0.0,
                interval=interval,
                source=str(record.get("source") or default_source),
            )
        )
    return bars


def records_to_series(
    records: list[dict[str, Any]], *, default_namespace: str
) -> list[tuple[str, Series]]:
    """Group series-shaped records into ``(namespace, Series)`` pairs."""
    grouped: dict[tuple[str, str], Series] = {}
    for record in records:
        ts = parse_timestamp(record.get("ts"))
        value = _as_float(record.get("value"))
        name = str(record.get("name") or "").strip()
        if ts is None or value is None or not name:
            continue
        namespace = str(record.get("namespace") or default_namespace)
        key = (namespace, name)
        series = grouped.get(key)
        if series is None:
            series = Series(
                name=name,
                points=[],
                unit=record.get("unit"),
                source=str(record.get("source") or default_namespace),
                meta={"namespace": namespace},
            )
            grouped[key] = series
        series.points.append((ts, value))
    return [(namespace, series.sorted()) for (namespace, _), series in grouped.items()]


def _as_float(value: Any) -> float | None:
    """Coerce a record value to ``float``, or ``None`` when it is not numeric."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # NaN is not a value


class IngestScheduler:
    """Drives periodic ingestion for every enabled catalog source."""

    def __init__(
        self,
        catalog: SourceCatalog,
        bus: EventBus,
        *,
        store: TimeSeriesStore | None = None,
        tick_seconds: float = DEFAULT_TICK_SECONDS,
        scope_resolver: Callable[[SourceConfig], list[str]] | None = None,
    ) -> None:
        """Create the scheduler bound to a catalog and an event bus.

        Args:
            catalog: the source catalog to run.
            bus: event bus for ``ingest`` progress events.
            store: time-series store; defaults to the process-wide one.
            tick_seconds: how often the loop looks for due jobs.
            scope_resolver: optional hook that turns a source's ``scope_label``
                (a universe name) into concrete symbols. Without it, only the
                explicit ``scope`` list is ingested - a universe name is never
                guessed at.
        """
        self.catalog = catalog
        self.bus = bus
        self.store = store if store is not None else get_store()
        self.tick_seconds = tick_seconds
        self.scope_resolver = scope_resolver
        self._jobs: dict[str, ScheduledJob] = {}
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._lock = asyncio.Lock()

    # --- lifecycle --------------------------------------------------------
    async def start(self) -> None:
        """Schedule every enabled source and begin running jobs."""
        if self._running:
            return
        await self.reload()
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="ingest-scheduler")
        await self.bus.publish(
            INGEST_CHANNEL, {"event": "scheduler_started", "jobs": len(self._jobs)}
        )
        logger.info("ingest scheduler started with %d job(s)", len(self._jobs))

    async def stop(self) -> None:
        """Cancel running jobs and shut the scheduler down cleanly."""
        self._running = False
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self.bus.publish(INGEST_CHANNEL, {"event": "scheduler_stopped"})
        logger.info("ingest scheduler stopped")

    @property
    def running(self) -> bool:
        """Whether the scheduler is currently active."""
        return self._running and self._task is not None and not self._task.done()

    async def _loop(self) -> None:
        """Run :meth:`tick` on the configured cadence until cancelled."""
        while self._running:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad tick must not stop the loop
                logger.exception("ingest scheduler tick failed")
            await asyncio.sleep(self.tick_seconds)

    # --- job table --------------------------------------------------------
    def jobs(self) -> list[ScheduledJob]:
        """Return the current job table for the Data Ingestion page."""
        return sorted(self._jobs.values(), key=lambda job: job.source_id)

    def job(self, source_id: str) -> ScheduledJob | None:
        """Return one job, or ``None`` when the source is not scheduled."""
        return self._jobs.get(source_id)

    async def reload(self) -> list[ScheduledJob]:
        """Rebuild the job table from the catalog, keeping existing timings."""
        wanted: set[str] = set()
        for source in self.catalog.list_sources():
            if not source.enabled or not source.parsed_schedule.periodic:
                continue
            wanted.add(source.id)
            await self.add_source(source.id)
        for source_id in list(self._jobs):
            if source_id not in wanted:
                await self.remove_source(source_id)
        return self.jobs()

    async def add_source(self, source_id: str) -> ScheduledJob:
        """Schedule (or reschedule) one source from its saved config.

        A job that is already registered keeps its counters and its last run
        time. Its *next* run is re-derived from the new cadence whenever the
        cadence actually changed - ``last_run_at + new period`` (and "now" when
        the source has never run), which is the same arithmetic
        :meth:`_run_job` uses after every fire. That is what makes a re-timed
        source honest: switching ``1d`` to ``45s`` makes the pending fire due
        45 seconds after the last one (usually immediately) instead of leaving
        it a day out, and switching ``45s`` to ``1d`` pushes it a day past the
        last run instead of firing within the minute. A cadence written
        differently but meaning the same thing (``1d`` / ``24h``) is not a
        change and does not disturb the pending fire.

        Raises:
            KeyError: when the source does not exist.
            ValueError: when the source's schedule is not periodic.
        """
        config = self.catalog.get(source_id)
        schedule = config.parsed_schedule
        if not schedule.periodic:
            raise ValueError(
                f"{source_id!r} has schedule {schedule.label!r}, which the periodic "
                "scheduler does not drive"
            )
        state = self.catalog.state(source_id)
        existing = self._jobs.get(source_id)
        job = existing or ScheduledJob(source_id=source_id, schedule=config.schedule)
        previous = parse_schedule(job.schedule) if existing is not None else None
        job.schedule = config.schedule
        if existing is None:
            job.last_run_at = state.last_ingest_at
            job.next_run_at = schedule.next_run_after(state.last_ingest_at)
        elif job.next_run_at is None or not _same_cadence(previous, schedule):
            job.next_run_at = schedule.next_run_after(job.last_run_at)
        self._jobs[source_id] = job
        return job

    async def remove_source(self, source_id: str) -> None:
        """Unschedule one source."""
        self._jobs.pop(source_id, None)

    # --- running ----------------------------------------------------------
    def due_jobs(self, *, now: datetime | None = None) -> list[ScheduledJob]:
        """Return the jobs whose next run time has arrived."""
        moment = now or _utcnow()
        return [
            job
            for job in self.jobs()
            if job.next_run_at is not None and job.next_run_at <= moment
        ]

    async def tick(self, *, now: datetime | None = None) -> list[IngestOutcome]:
        """Run every job that is due and reschedule it. Returns the outcomes."""
        moment = now or _utcnow()
        due = self.due_jobs(now=moment)
        if not due:
            return []
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)

        async def run(job: ScheduledJob) -> IngestOutcome:
            async with semaphore:
                return await self._run_job(job, now=moment)

        results = await asyncio.gather(*(run(job) for job in due), return_exceptions=True)
        outcomes: list[IngestOutcome] = []
        for job, item in zip(due, results, strict=True):
            if isinstance(item, BaseException):
                logger.exception("ingest job %s crashed", job.source_id, exc_info=item)
                outcomes.append(
                    IngestOutcome(
                        source_id=job.source_id,
                        ok=False,
                        error=f"{type(item).__name__}: {item}",
                        finished_at=_utcnow(),
                    )
                )
                continue
            outcomes.append(item)
        return outcomes

    async def _run_job(self, job: ScheduledJob, *, now: datetime) -> IngestOutcome:
        """Run one job, persist its outcome and reschedule it."""
        outcome = await self.run_now(job.source_id)
        job.runs += 1
        job.last_run_at = now
        job.last_rows = outcome.rows_written
        job.rows += outcome.rows_written
        job.last_latency_ms = outcome.latency_ms
        job.last_error = outcome.error
        job.last_status = "ok" if outcome.ok else ("skipped" if outcome.skipped else "error")
        schedule = self.catalog.get(job.source_id).parsed_schedule
        job.next_run_at = schedule.next_run_after(now, now=now)
        return outcome

    def resolve_scope(self, config: SourceConfig) -> list[str]:
        """Return the concrete items a source should fetch."""
        if config.scope:
            return list(config.scope)
        if self.scope_resolver is not None:
            return list(self.scope_resolver(config))
        return []

    async def run_now(self, source_id: str) -> IngestOutcome:
        """Run one source immediately and return the ingestion result.

        Failures are recorded and returned, not raised: one broken vendor must
        not stop the scheduler. The error text is the vendor's own.
        """
        started = _utcnow()
        config = self.catalog.get(source_id)
        outcome = IngestOutcome(source_id=source_id, ok=False, started_at=started)

        if config.parsed_schedule.kind is ScheduleKind.STREAMING:
            outcome.skipped = "streaming sources are driven by their connector, not the scheduler"
            outcome.finished_at = _utcnow()
            return outcome
        if config.provider and not self.resolve_scope(config):
            outcome.skipped = "no scope configured"
            outcome.finished_at = _utcnow()
            await self._publish(outcome)
            return outcome

        connector = self.catalog.connector_for(config)
        # Only the library connector takes a scope; for HTTP-shaped connectors
        # every keyword becomes a query parameter, so nothing extra is passed.
        params: dict[str, Any] = (
            {"scope": self.resolve_scope(config)} if config.provider else {}
        )
        interval = self.requested_interval(config) if config.provider else None
        if interval is not None:
            params["interval"] = interval
        try:
            batch = await connector.fetch(**params)
        except (ProviderError, ValueError, RuntimeError, OSError) as exc:
            outcome.error = f"{type(exc).__name__}: {exc}"
            outcome.finished_at = _utcnow()
            self.catalog.record_ingest(source_id, rows=0, error=outcome.error)
            await self._publish(outcome)
            logger.info("ingest %s failed: %s", source_id, outcome.error)
            return outcome
        except Exception as exc:  # noqa: BLE001 - a vendor SDK may raise anything
            outcome.error = f"{type(exc).__name__}: {exc}"
            outcome.finished_at = _utcnow()
            self.catalog.record_ingest(source_id, rows=0, error=outcome.error)
            await self._publish(outcome)
            logger.exception("ingest %s raised", source_id)
            return outcome
        finally:
            await self._close(connector)

        try:
            written, dataset = await self.persist(config, batch)
        except Exception as exc:  # noqa: BLE001 - arrow/parquet raise several types
            outcome.error = f"could not store the batch: {type(exc).__name__}: {exc}"
            outcome.rows_fetched = len(batch.records)
            outcome.finished_at = _utcnow()
            self.catalog.record_ingest(source_id, rows=0, error=outcome.error)
            await self._publish(outcome)
            logger.exception("ingest %s: persisting the batch failed", source_id)
            return outcome

        outcome.ok = True
        outcome.rows_fetched = len(batch.records)
        outcome.rows_written = written
        outcome.dataset = dataset
        outcome.latency_ms = batch.latency_ms
        outcome.finished_at = _utcnow()
        self.catalog.record_ingest(
            source_id, rows=written, latency_ms=batch.latency_ms, at=outcome.finished_at
        )
        await self._publish(outcome)
        return outcome

    async def backfill(
        self, source_id: str, *, range: str | None = None, end: datetime | None = None
    ) -> dict[str, Any]:
        """Run a historical backfill for one source, reporting progress on ``ingest``.

        Args:
            source_id: the catalog source to backfill.
            range: the FROM boundary - a duration token, ``"all"``, or an ISO
                date. Defaults to the source's own configured range.
            end: the TO boundary. ``None`` runs the window up to the present.

        Returns a summary of what was fetched and written. A connector that
        cannot backfill reports that instead of pretending to have run.
        """
        config = self.catalog.get(source_id)
        window = range or config.backfill.range
        connector = self.catalog.connector_for(config)
        batches = 0
        fetched = 0
        written = 0
        error: str | None = None
        started = _utcnow()
        await self._publish_event(
            {
                "event": "backfill_started",
                "source_id": source_id,
                "range": window,
                "end": end.isoformat() if end is not None else None,
            }
        )
        try:
            async for batch in connector.backfill(
                range=window, batch_size=config.backfill.batch_size, end=end
            ):
                batches += 1
                fetched += len(batch.records)
                rows, dataset = await self.persist(config, batch)
                written += rows
                await self._publish_event(
                    {
                        "event": "backfill_progress",
                        "source_id": source_id,
                        "batch": batches,
                        "rows_fetched": len(batch.records),
                        "rows_written": rows,
                        "dataset": dataset,
                        "cursor": batch.cursor,
                        "complete": batch.complete,
                    }
                )
        except NotImplementedError as exc:
            error = str(exc)
        except (ProviderError, ValueError, RuntimeError, OSError) as exc:
            error = f"{type(exc).__name__}: {exc}"
        except Exception as exc:  # noqa: BLE001 - vendor SDKs raise anything
            error = f"{type(exc).__name__}: {exc}"
            logger.exception("backfill %s raised", source_id)
        finally:
            await self._close(connector)

        if written:
            self.catalog.record_ingest(source_id, rows=written)
        elif error:
            self.catalog.record_ingest(source_id, rows=0, error=error)
        # Stamp the backfill separately from an ordinary ingest: a completed
        # backfill that wrote nothing (the window held no data) still counts as
        # done, and must not be re-triggered on the next save.
        self.catalog.record_backfill(source_id, rows=0, error=error)
        summary = {
            "source_id": source_id,
            "range": window,
            "end": end.isoformat() if end is not None else None,
            "batches": batches,
            "rows_fetched": fetched,
            "rows_written": written,
            "ok": error is None,
            "error": error,
            "started_at": started.isoformat(),
            "finished_at": _utcnow().isoformat(),
        }
        await self._publish_event({"event": "backfill_finished", **summary})
        return summary

    # --- persistence ------------------------------------------------------
    async def persist(self, config: SourceConfig, batch: FetchResult) -> tuple[int, str]:
        """Write a batch into the time-series store. Returns ``(rows, dataset)``.

        The record shape decides the destination: bar rows go to the ``bars``
        dataset, ``name``/``value`` rows to ``series`` under the source's
        namespace, and everything else to a per-source frame dataset. An empty
        batch writes nothing and reports zero.

        Bars and series are upserted (existing rows for the same key are
        replaced); a frame dataset is *replaced* per partition, because an
        arbitrary record has no key the store could merge on - a source whose
        records need merging should map them onto the bar or series shape.
        """
        records = batch.records
        if not records:
            return 0, batch.dataset or "none"
        kind = classify_records(records)
        pit = self._preserves_pit(config)
        if kind == "bars":
            bars = records_to_bars(records, default_source=config.provider or config.id)
            if pit:
                bars = await asyncio.to_thread(self._drop_restated_bars, bars)
            if not bars:
                return 0, "bars"
            written = await self.store.awrite_bars(bars)
            return written, "bars"
        if kind == "series":
            namespace = config.provider or config.id
            pairs = records_to_series(records, default_namespace=namespace)
            total = 0
            for series_namespace, series in pairs:
                if pit:
                    series = await asyncio.to_thread(
                        self._drop_restated_series, series_namespace, series
                    )
                    if not series.points:
                        continue
                total += await self.store.awrite_series(series_namespace, series)
            return total, "series"

        dataset = f"ingest_{config.id}"
        frame = pd.DataFrame(records)
        partition = ["symbol"] if "symbol" in frame.columns else None
        written = await asyncio.to_thread(
            self.store.write_frame, dataset, frame, partition_cols=partition
        )
        return written, dataset

    def requested_interval(self, config: SourceConfig) -> str | None:
        """Return the bar interval this source's subscriptions ask for.

        A subscription's ``granularity`` is the *dataset's* resolution. When it
        names a real bar size, and every active subscription of the source agrees
        on it, the interval is handed to the connector's fetch so the request
        genuinely changes what is downloaded. Disagreeing subscriptions, or a
        cadence with no bar equivalent (``quarterly``, ``realtime``), leave the
        source's own configuration in charge - the API says so rather than
        pretending the value was applied.

        The rule itself lives in :func:`agreed_bar_interval` so the API reports
        exactly what this method acts on.
        """
        try:
            subscriptions = self.catalog.list_subscriptions(source_id=config.id)
        except Exception:  # noqa: BLE001 - a catalog read must not break an ingest
            logger.debug("could not read subscriptions for %s", config.id, exc_info=True)
            return None
        interval, wanted = agreed_bar_interval(subscriptions)
        if interval is None and len(wanted) > 1:
            logger.info(
                "%s has subscriptions asking for %s; leaving its configured interval alone",
                config.id,
                ", ".join(wanted),
            )
        return interval

    def _preserves_pit(self, config: SourceConfig) -> bool:
        """Whether any subscription of this source asks for point-in-time values."""
        try:
            subscriptions = self.catalog.list_subscriptions(source_id=config.id)
        except Exception:  # noqa: BLE001 - a catalog read must not break an ingest
            logger.debug("could not read subscriptions for %s", config.id, exc_info=True)
            return False
        return any(sub.active and sub.preserve_pit for sub in subscriptions)

    def _drop_restated_bars(self, bars: list[Bar]) -> list[Bar]:
        """Drop incoming bars whose ``(symbol, ts)`` is already stored.

        This is what "preserve point-in-time values" means for a price series:
        the value that was true at the time stays, and a vendor restatement of an
        old bar is refused rather than silently rewriting history. New timestamps
        are written normally.
        """
        kept: list[Bar] = []
        cache: dict[tuple[str, str], set[datetime]] = {}
        for bar in bars:
            key = (bar.symbol.upper(), str(bar.interval))
            known = cache.get(key)
            if known is None:
                frame = self.store.read_bars(bar.symbol, interval=bar.interval)
                known = {to_utc(pd.Timestamp(ts).to_pydatetime()) for ts in frame.index}
                cache[key] = known
            if to_utc(bar.ts) not in known:
                kept.append(bar)
        dropped = len(bars) - len(kept)
        if dropped:
            logger.info("point-in-time: kept %d stored bar(s) against a restatement", dropped)
        return kept

    def _drop_restated_series(self, namespace: str, series: Series) -> Series:
        """Drop incoming series points whose timestamp is already stored."""
        stored = self.store.read_series(namespace, series.name)
        known = {to_utc(ts) for ts, _ in stored.points}
        points = [(ts, value) for ts, value in series.points if to_utc(ts) not in known]
        dropped = len(series.points) - len(points)
        if dropped:
            logger.info(
                "point-in-time: kept %d stored point(s) of %s/%s against a restatement",
                dropped,
                namespace,
                series.name,
            )
        return Series(name=series.name, points=points, unit=series.unit, source=series.source,
                      meta=dict(series.meta))

    # --- reporting --------------------------------------------------------
    def last_ingest_at(self) -> datetime | None:
        """Most recent successful ingestion across all sources, or ``None``."""
        stamps = [
            state.last_ingest_at
            for state in self.catalog.states().values()
            if state.last_ingest_at is not None
        ]
        return max(stamps) if stamps else None

    def status(self) -> dict[str, Any]:
        """Return the scheduler's own state, for ``/api/status`` and the UI."""
        last = self.last_ingest_at()
        return {
            "running": self.running,
            "tick_seconds": self.tick_seconds,
            "jobs": [job.as_dict() for job in self.jobs()],
            "job_count": len(self._jobs),
            "last_ingest": last.isoformat() if last else None,
        }

    # --- helpers ----------------------------------------------------------
    async def _publish(self, outcome: IngestOutcome) -> None:
        """Publish one run outcome on the ``ingest`` channel."""
        await self._publish_event({"event": "ingest_run", **outcome.as_dict()})

    async def _publish_event(self, payload: dict[str, Any]) -> None:
        """Publish an arbitrary ingestion event."""
        await self.bus.publish(INGEST_CHANNEL, payload)

    @staticmethod
    async def _close(connector: Connector) -> None:
        """Close a connector, logging (never raising) a shutdown failure."""
        try:
            await connector.close()
        except Exception:  # noqa: BLE001 - shutdown must not mask the result
            logger.debug("scheduler: closing %r failed", connector, exc_info=True)


__all__ = [
    "DEFAULT_TICK_SECONDS",
    "MAX_CONCURRENT_JOBS",
    "IngestOutcome",
    "IngestScheduler",
    "ScheduledJob",
    "classify_records",
    "records_to_bars",
    "records_to_series",
]
