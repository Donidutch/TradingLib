"""Ingestion endpoints (``docs/SPEC.md`` sections 4 and 5).

The Data Ingestion form posts its controls verbatim; :meth:`SourceConfig.from_form`
maps them 1:1 onto the stored configuration. "Test connection" runs a real
:meth:`Connector.test` - there is no canned success anywhere in this module,
and a source that has never been probed keeps the status ``unknown``.

Long-running work (backfills) is dispatched as a background task whose
progress is published on the ``ingest`` WebSocket channel; pass ``wait=true``
to block until the summary is ready instead.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from tradinglib.api.deps import get_catalog, get_gateway, get_scheduler, translate_errors
from tradinglib.data.ingest.catalog import (
    SourceCatalog,
    Subscription,
    agreed_bar_interval,
    bar_interval_for,
    granularity_options,
    granularity_reaches_fetch,
)
from tradinglib.data.ingest.gateway import IngestGateway
from tradinglib.data.ingest.scheduler import IngestScheduler
from tradinglib.data.ingest.source import Protocol, ScheduleKind, SourceConfig

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ingest", tags=["ingest"])

Catalog = Annotated[SourceCatalog, Depends(get_catalog)]
Scheduler = Annotated[IngestScheduler, Depends(get_scheduler)]
Gateway = Annotated[IngestGateway, Depends(get_gateway)]


class SubscriptionRequest(BaseModel):
    """Body of ``POST /api/ingest/subscriptions``."""

    model_config = ConfigDict(extra="ignore")

    source_id: str
    channel: str
    scope: list[str] = Field(default_factory=list)
    active: bool = True
    #: ``live`` asks the source to stream, ``scheduled`` to poll on its own
    #: cadence. A mode the source's protocol cannot honour is reported back in
    #: ``feed_mode_supported`` rather than silently downgraded.
    feed_mode: Literal["live", "scheduled"] = "scheduled"
    #: Keep the value that was true at each observation time instead of letting
    #: a later restatement overwrite it.
    preserve_pit: bool = False
    #: The dataset's own resolution - a bar size (``1m``...``1d``) or a reporting
    #: cadence (``quarterly``, ``annual``, ``realtime``). Distinct from the poll
    #: interval, which is how often the connector runs. ``""`` keeps the
    #: source's configured resolution. Validated against the source's data
    #: domain; the response says whether it reaches the fetch path.
    granularity: str = ""
    id: str | None = None


class EnabledRequest(BaseModel):
    """Body of ``POST /api/ingest/sources/{id}/enabled``."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool


def _config_from_form(payload: dict[str, Any]) -> SourceConfig:
    """Build a :class:`SourceConfig` from the ingestion form payload."""
    with translate_errors("ingestion source"):
        return SourceConfig.from_form(payload)


async def _reschedule(scheduler: IngestScheduler, source_id: str) -> dict[str, Any] | None:
    """Re-register one source with the running scheduler, if it drives it.

    Returns the job description, or ``None`` when the source's schedule is not
    one the periodic scheduler runs (streaming, on-change, manual).
    """
    try:
        job = await scheduler.add_source(source_id)
    except ValueError:
        await scheduler.remove_source(source_id)
        return None
    except KeyError:
        return None
    return job.as_dict()


# --- sources --------------------------------------------------------------
@router.get("/sources", summary="List ingestion sources")
async def list_sources(catalog: Catalog) -> dict[str, Any]:
    """Return every catalog source with its observed state and subscriptions."""
    with translate_errors("ingestion source"):
        sources = [catalog.describe(source.id) for source in catalog.list_sources()]
        summary = catalog.summary()
    return {"sources": sources, "count": len(sources), "summary": summary}


@router.post("/sources", status_code=201, summary="Create or update a source")
async def save_source(
    request: Request,
    catalog: Catalog,
    payload: Annotated[dict[str, Any], Body()],
) -> dict[str, Any]:
    """Persist a source built from the ingestion form payload.

    The stored configuration is validated for its protocol (a WebSocket source
    needs a ``wss://`` endpoint, a database source needs SQL, ...) before it is
    written, so an unrunnable source can never reach the catalog.
    """
    config = _config_from_form(payload)
    with translate_errors("ingestion source"):
        saved = catalog.save(config)
    scheduler = getattr(request.app.state, "ingest_scheduler", None)
    job: dict[str, Any] | None = None
    if scheduler is not None and saved.enabled:
        job = await _reschedule(scheduler, saved.id)
    backfill = _start_first_connection_backfill(request, catalog, scheduler, saved)
    return {"source": catalog.describe(saved.id), "job": job, "backfill": backfill}


def _start_first_connection_backfill(
    request: Request,
    catalog: SourceCatalog,
    scheduler: IngestScheduler | None,
    config: SourceConfig,
) -> dict[str, Any] | None:
    """Honour the form's "backfill on first connection" flag.

    The control promises that saving an enabled source with the flag set starts
    a historical backfill the first time that source is connected, so this is
    what makes the promise true. It fires at most once per source: a source that
    already carries a ``backfilled_at`` stamp is left alone, which is what keeps
    an edit-and-save cycle from re-running a multi-year fetch.

    Returns a description of what was started, or ``None`` with a reason when
    nothing was - never a silent no-op.
    """
    if not config.backfill.enabled:
        return None
    if not config.enabled:
        return {"started": False, "reason": "the source is paused"}
    if not config.protocol.can_backfill:
        return {
            "started": False,
            "reason": f"the {config.protocol.value} connector has no historical backfill",
        }
    if scheduler is None:
        return {"started": False, "reason": "the ingest scheduler is not running"}
    state = catalog.state(config.id)
    if state.backfilled_at is not None:
        return {
            "started": False,
            "reason": "already backfilled",
            "backfilled_at": state.backfilled_at.isoformat(),
        }
    tasks: dict[str, asyncio.Task[dict[str, Any]]] = request.app.state.backfill_tasks
    existing = tasks.get(config.id)
    if existing is not None and not existing.done():
        return {"started": False, "reason": "a backfill for this source is already running"}
    task = asyncio.create_task(
        scheduler.backfill(config.id, range=config.backfill.range),
        name=f"backfill-{config.id}",
    )
    tasks[config.id] = task
    task.add_done_callback(lambda t: _log_backfill(config.id, t))
    logger.info(
        "first-connection backfill started for %s over %s", config.id, config.backfill.range
    )
    return {
        "started": True,
        "source_id": config.id,
        "range": config.backfill.range,
        "channel": "ingest",
    }


@router.post("/sources/test", summary="Test a connection (form payload)")
async def test_source_payload(
    catalog: Catalog,
    payload: Annotated[dict[str, Any], Body()],
) -> dict[str, Any]:
    """Run a real connection test for a (possibly unsaved) form payload.

    The result carries what the vendor actually answered: reachability,
    authentication, measured latency, a sample record and the schema inferred
    from it. Nothing is fabricated when the attempt fails.
    """
    config = _config_from_form(payload)
    result = await catalog.test(config)
    body = result.as_dict()
    body["source_id"] = config.id
    body["name"] = config.name
    if catalog.exists(config.id):
        state = catalog.record_test(config.id, result)
        body["state"] = state.as_dict()
    return body


@router.get("/sources/{source_id}", summary="Return one source")
async def get_source(catalog: Catalog, source_id: str) -> dict[str, Any]:
    """Return one source, its observed state and its subscriptions."""
    with translate_errors("ingestion source"):
        return catalog.describe(source_id)


@router.put("/sources/{source_id}", summary="Update one source")
async def update_source(
    request: Request,
    catalog: Catalog,
    source_id: str,
    payload: Annotated[dict[str, Any], Body()],
) -> dict[str, Any]:
    """Replace one source's configuration with the posted form payload."""
    with translate_errors("ingestion source"):
        catalog.get(source_id)
    merged = {**payload, "id": source_id}
    config = _config_from_form(merged)
    with translate_errors("ingestion source"):
        saved = catalog.save(config)
    scheduler = getattr(request.app.state, "ingest_scheduler", None)
    job: dict[str, Any] | None = None
    if scheduler is not None and saved.enabled:
        job = await _reschedule(scheduler, saved.id)
    return {"source": catalog.describe(saved.id), "job": job}


@router.delete("/sources/{source_id}", summary="Delete a user source")
async def delete_source(request: Request, catalog: Catalog, source_id: str) -> dict[str, Any]:
    """Delete a user-created source. Built-ins are protected and answer 400."""
    with translate_errors("ingestion source"):
        catalog.delete(source_id)
    scheduler = getattr(request.app.state, "ingest_scheduler", None)
    if scheduler is not None:
        await scheduler.remove_source(source_id)
    return {"deleted": source_id}


@router.post("/sources/{source_id}/test", summary="Test a saved source")
async def test_source(catalog: Catalog, source_id: str) -> dict[str, Any]:
    """Probe a saved source and record what the probe observed."""
    with translate_errors("ingestion source"):
        state = await catalog.refresh_status(source_id)
    return {"source_id": source_id, "state": state.as_dict()}


@router.post("/sources/{source_id}/run", summary="Run a source now")
async def run_source(scheduler: Scheduler, source_id: str) -> dict[str, Any]:
    """Ingest from one source immediately and report what was written.

    A failure is returned in the outcome (``ok: false`` with the vendor's own
    error text), not raised: one broken vendor must not take the API down.
    """
    with translate_errors("ingestion source"):
        outcome = await scheduler.run_now(source_id)
    return outcome.as_dict()


@router.post("/sources/{source_id}/backfill", summary="Backfill a source")
async def backfill_source(
    request: Request,
    scheduler: Scheduler,
    source_id: str,
    range: Annotated[str | None, Query(description="override the source's backfill range")] = None,
    end: Annotated[
        str | None,
        Query(description="upper bound of the window: an ISO date/datetime, or 'now'"),
    ] = None,
    wait: Annotated[bool, Query(description="await the summary instead of streaming it")] = False,
) -> dict[str, Any]:
    """Start (or run) a historical backfill.

    ``range`` is the FROM boundary (a duration token such as ``5y``, ``all``,
    or an ISO date) and ``end`` is the TO boundary — the Source Catalog's
    FROM/TO controls map onto them one for one, with ``now`` (or an omitted
    value) meaning "up to the present".

    By default the backfill runs in the background and progress arrives on the
    ``ingest`` WebSocket channel (``backfill_started`` / ``backfill_progress``
    / ``backfill_finished``). ``wait=true`` blocks until the summary is ready.
    """
    with translate_errors("ingestion source"):
        scheduler.catalog.get(source_id)
    window_end = _parse_window_end(end)
    if wait:
        with translate_errors("ingestion source"):
            return await scheduler.backfill(source_id, range=range, end=window_end)

    tasks: dict[str, asyncio.Task[dict[str, Any]]] = request.app.state.backfill_tasks
    existing = tasks.get(source_id)
    if existing is not None and not existing.done():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "backfill_running",
                "source_id": source_id,
                "detail": "a backfill for this source is already running",
            },
        )
    task = asyncio.create_task(
        scheduler.backfill(source_id, range=range, end=window_end),
        name=f"backfill-{source_id}",
    )
    tasks[source_id] = task
    task.add_done_callback(lambda t: _log_backfill(source_id, t))
    return {
        "started": True,
        "source_id": source_id,
        "range": range,
        "end": window_end.isoformat() if window_end is not None else None,
        "channel": "ingest",
    }


def _parse_window_end(value: str | None) -> datetime | None:
    """Turn the TO control's text into an upper bound, or ``None`` for "now".

    Raises:
        HTTPException: 422 when the text is neither empty, ``now``, nor an ISO
            date/datetime — an unparseable bound must not silently become "now".
    """
    token = (value or "").strip()
    if not token or token.lower() == "now":
        return None
    try:
        parsed = datetime.fromisoformat(token.upper().replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "invalid_window_end",
                "detail": f"{token!r} is not a date, a datetime, or 'now'",
            },
        ) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _log_backfill(source_id: str, task: asyncio.Task[dict[str, Any]]) -> None:
    """Log the outcome of a background backfill task."""
    if task.cancelled():
        logger.info("backfill of %s was cancelled", source_id)
        return
    exc = task.exception()
    if exc is not None:
        logger.error("backfill of %s failed: %s", source_id, exc, exc_info=exc)
        return
    logger.info("backfill of %s finished: %s", source_id, task.result())


@router.get("/backfills", summary="List running backfills")
async def list_backfills(request: Request) -> dict[str, Any]:
    """Report which backfills this process is currently running."""
    tasks: dict[str, asyncio.Task[dict[str, Any]]] = request.app.state.backfill_tasks
    running = [source_id for source_id, task in tasks.items() if not task.done()]
    finished = {
        source_id: (task.result() if task.exception() is None else str(task.exception()))
        for source_id, task in tasks.items()
        if task.done() and not task.cancelled()
    }
    return {"running": running, "finished": finished}


@router.post("/sources/{source_id}/enabled", summary="Enable or pause a source")
async def set_enabled(
    request: Request, catalog: Catalog, source_id: str, body: EnabledRequest
) -> dict[str, Any]:
    """Enable or pause one source; the scheduler follows immediately."""
    with translate_errors("ingestion source"):
        config = catalog.set_enabled(source_id, body.enabled)
    scheduler = getattr(request.app.state, "ingest_scheduler", None)
    job: dict[str, Any] | None = None
    if scheduler is not None:
        if config.enabled:
            job = await _reschedule(scheduler, source_id)
        else:
            await scheduler.remove_source(source_id)
    return {"source": catalog.describe(source_id), "job": job}


@router.post("/refresh", summary="Probe every source")
async def refresh_all(catalog: Catalog) -> dict[str, Any]:
    """Probe every enabled source and return the observed states."""
    states = await catalog.refresh_all()
    connected, total = catalog.connected_count()
    return {
        "states": {source_id: state.as_dict() for source_id, state in states.items()},
        "connected": connected,
        "total": total,
    }


# --- subscriptions --------------------------------------------------------
@router.get("/subscriptions", summary="List subscriptions")
async def list_subscriptions(
    catalog: Catalog,
    source_id: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """Return the configured subscriptions, optionally filtered by source."""
    with translate_errors("subscription"):
        subs = catalog.list_subscriptions(source_id=source_id)
        sources = {source.id: source for source in catalog.list_sources()}
    # Whether one subscription's resolution reaches the fetch depends on its
    # siblings (two active ones asking for different bars cancel out), and the
    # listing already carries every subscription of each source it names.
    by_source: dict[str, list[Subscription]] = {}
    for sub in subs:
        by_source.setdefault(sub.source_id, []).append(sub)
    payload = []
    for sub in subs:
        entry = sub.as_dict()
        source = sources.get(sub.source_id)
        if source is not None:
            entry.update(_feed_capability(source, sub.feed_mode))
            entry.update(_granularity_effect(source, sub, by_source[sub.source_id]))
        payload.append(entry)
    return {"subscriptions": payload, "count": len(payload)}


@router.post("/subscriptions", status_code=201, summary="Create a subscription")
async def save_subscription(catalog: Catalog, body: SubscriptionRequest) -> dict[str, Any]:
    """Create or update a subscription of one source to a scope."""
    with translate_errors("subscription"):
        # Constructed inside the guard: an unknown granularity is rejected by the
        # model itself, and must surface as a 400 rather than a 500.
        subscription = Subscription(
            id=body.id or "",
            source_id=body.source_id,
            channel=body.channel,
            scope=list(body.scope),
            active=body.active,
            feed_mode=body.feed_mode,
            preserve_pit=body.preserve_pit,
            granularity=body.granularity,
        )
        source = catalog.get(body.source_id)
        saved = catalog.save_subscription(subscription)
        siblings = catalog.list_subscriptions(source_id=body.source_id)
    payload = saved.as_dict()
    payload.update(_feed_capability(source, saved.feed_mode))
    payload.update(_granularity_effect(source, saved, siblings))
    return payload


def _granularity_effect(
    source: Any, subscription: Subscription, siblings: Sequence[Subscription]
) -> dict[str, Any]:
    """Say whether this subscription's granularity really changes what is fetched.

    The answer is the scheduler's own: ``IngestScheduler.requested_interval()``
    hands an interval to the connector's fetch only when the source's fetch path
    takes a bar size (:func:`granularity_reaches_fetch`) *and* the source's
    active subscriptions agree on exactly one bar interval
    (:func:`agreed_bar_interval`). So there are three distinct ways a recorded
    resolution fails to reach the vendor call, and each is named rather than
    papered over:

    * the source's fetch path has no bar-size parameter (EDGAR filings, a
      reporting cadence like ``quarterly``);
    * the subscription is paused - a paused subscription is not read at all;
    * two active subscriptions of the source ask for different bars, so neither
      is imposed and the source's own configuration stands.

    ``granularity_applied`` is never ``true`` unless the scheduler would really
    pass this interval to the fetch right now.
    """
    options = list(granularity_options(source.data_domain))
    configured = str(source.advanced.get("interval") or "").strip().lower() or "default"

    def answer(applied: bool, detail: str) -> dict[str, Any]:
        """Bundle one verdict with the options the chips are drawn from."""
        return {
            "granularity_applied": applied,
            "granularity_options": options,
            "granularity_detail": detail,
        }

    granularity = subscription.granularity
    if not granularity:
        return answer(
            False,
            f"no resolution requested; {source.name} ingests at its configured "
            f"{configured} resolution",
        )
    interval = bar_interval_for(granularity)
    if interval is None or not granularity_reaches_fetch(source):
        return answer(
            False,
            f"advisory: {granularity} is recorded on the subscription, but "
            f"{source.name}'s fetch path has no bar-size parameter to apply it to",
        )
    if not subscription.active:
        return answer(
            False,
            f"advisory while paused: the scheduler only reads active subscriptions, "
            f"so {source.name} keeps fetching at its configured {configured} "
            f"resolution until this one is resumed",
        )
    # The saved subscription is authoritative over whatever the sibling list
    # holds for the same id, so it votes exactly once and with its new value.
    pool = [sub for sub in siblings if sub.id != subscription.id] + [subscription]
    agreed, wanted = agreed_bar_interval(pool)
    if agreed != interval:
        return answer(
            False,
            f"not applied: {source.name}'s active subscriptions ask for different bars "
            f"({', '.join(wanted)}), so none of them is imposed and the source keeps "
            f"its configured {configured} resolution",
        )
    return answer(
        True,
        f"the scheduler fetches {interval} bars for {source.name} while this "
        "subscription is active",
    )


def _feed_capability(source: Any, requested: str) -> dict[str, Any]:
    """Report whether the source can actually deliver the requested feed mode.

    A REST or polling source cannot stream, and a streaming source has no poll
    cadence to fall back on. The subscription keeps the mode the user asked for
    either way - the honest answer is to say what will really happen, not to
    rewrite the request.
    """
    streaming_protocols = {Protocol.WEBSOCKET}
    schedule = source.parsed_schedule
    can_stream = source.protocol in streaming_protocols or schedule.kind is ScheduleKind.STREAMING
    if requested == "live":
        return {
            "feed_mode_supported": bool(can_stream),
            "feed_mode_detail": (
                f"{source.name} streams over {source.protocol}"
                if can_stream
                else (
                    f"{source.name} is a {source.protocol} source with no stream; "
                    f"it will be polled on its {schedule.label.lower()} schedule"
                )
            ),
        }
    return {
        "feed_mode_supported": schedule.kind is not ScheduleKind.STREAMING,
        "feed_mode_detail": (
            f"polled on the source's {schedule.label.lower()} schedule"
            if schedule.kind is not ScheduleKind.STREAMING
            else f"{source.name} only streams; there is no polling cadence to use"
        ),
    }


@router.delete("/subscriptions/{subscription_id}", summary="Delete a subscription")
async def delete_subscription(catalog: Catalog, subscription_id: str) -> dict[str, Any]:
    """Delete one subscription."""
    with translate_errors("subscription"):
        catalog.delete_subscription(subscription_id)
    return {"deleted": subscription_id}


# --- gateway and scheduler ------------------------------------------------
@router.get("/gateway", summary="Ingestion gateway health")
async def gateway_report(
    gateway: Gateway,
    measure: Annotated[bool, Query(description="issue a real latency probe")] = False,
) -> dict[str, Any]:
    """Return gateway health plus the per-source table the UI renders.

    ``latency_ms`` is a measured round trip or ``null``; ``latency_measured``
    says whether this process measured it itself or is reporting the median of
    the latencies the last probes recorded.
    """
    return await gateway.report(measure=measure)


@router.get("/scheduler", summary="Scheduler state")
async def scheduler_state(scheduler: Scheduler) -> dict[str, Any]:
    """Return the scheduler's job table and last successful ingest."""
    return scheduler.status()


@router.get("/summary", summary="Catalog counters")
async def catalog_summary(catalog: Catalog) -> dict[str, Any]:
    """Return the Source Catalog page's header counters."""
    return catalog.summary()
