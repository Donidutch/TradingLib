"""Data-quality endpoints (``docs/SPEC.md`` section 5).

Everything here is read back from real check runs performed by
:class:`tradinglib.data.quality.QualityEngine` over the Parquet store. Before the
first run the summary reports ``null`` for the overall score and zero counts -
the Data Quality page then shows "no checks have run yet" rather than a
plausible 97.4%.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from tradinglib.api.deps import get_catalog, get_quality, translate_errors
from tradinglib.data.ingest.catalog import SourceCatalog
from tradinglib.data.quality import QualityEngine, Severity

router = APIRouter(prefix="/api/quality", tags=["quality"])

Engine = Annotated[QualityEngine, Depends(get_quality)]
Catalog = Annotated[SourceCatalog, Depends(get_catalog)]


class RunRequest(BaseModel):
    """Body of ``POST /api/quality/run``."""

    model_config = ConfigDict(extra="ignore")

    datasets: list[str] | None = Field(
        default=None,
        description="dataset keys such as 'bars:AAPL:1d'; omit to check everything stored",
    )


@router.get("/summary", summary="Quality summary")
async def quality_summary(engine: Engine) -> dict[str, Any]:
    """Return the Data Quality header figures from the latest recorded run."""
    summary = await asyncio.to_thread(engine.summary)
    summary["datasets_in_store"] = len(await asyncio.to_thread(engine.datasets))
    return summary


@router.get("/series", summary="Per-dataset quality")
async def quality_series(
    engine: Engine,
    dataset: Annotated[str | None, Query(description="dataset key, e.g. bars:AAPL:1d")] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> dict[str, Any]:
    """Return per-dataset quality.

    Without ``dataset`` this is the latest score and check outcome for every
    dataset; with one it is that dataset's score history for the trend chart.
    """
    if dataset is None:
        latest = await asyncio.to_thread(engine.latest)
        return {"datasets": latest, "count": len(latest)}
    with translate_errors("dataset"):
        return await asyncio.to_thread(engine.series, dataset, limit=limit)


@router.get("/incidents", summary="Quality incidents")
async def quality_incidents(
    engine: Engine,
    include_resolved: Annotated[bool, Query()] = False,
    dataset: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    """Return recorded incidents, newest first."""
    incidents = await asyncio.to_thread(
        lambda: engine.incidents(
            include_resolved=include_resolved, dataset=dataset, limit=limit
        )
    )
    return {
        "incidents": [incident.as_dict() for incident in incidents],
        "count": len(incidents),
        "open": sum(1 for incident in incidents if incident.open),
    }


@router.post("/incidents/{incident_id}/resolve", summary="Resolve an incident")
async def resolve_incident(engine: Engine, incident_id: str) -> dict[str, Any]:
    """Mark one incident resolved and publish the change on ``quality``."""
    with translate_errors("incident"):
        incident = await engine.resolve_incident(incident_id)
    return incident.as_dict()


@router.get("/datasets", summary="Datasets present in the store")
async def quality_datasets(engine: Engine) -> dict[str, Any]:
    """List the datasets the store actually holds, as check targets."""
    refs = await asyncio.to_thread(engine.datasets)
    return {"datasets": [ref.as_dict() for ref in refs], "count": len(refs)}


@router.post("/run", summary="Run the quality suite")
async def run_quality(
    engine: Engine,
    body: Annotated[RunRequest, Body()] = RunRequest(),
) -> dict[str, Any]:
    """Run completeness, staleness and outlier checks and persist the results.

    Results are recorded, incidents opened or resolved, and ``run.completed``
    published on the ``quality`` channel.
    """
    with translate_errors("dataset"):
        outcomes = await engine.run(body.datasets)
    # ``dataset`` mirrors the engine's own ``ref.key`` so every quality payload
    # (run, series, incidents) names a dataset the same way.
    return {
        "results": [
            {"dataset": outcome.ref.key, **outcome.as_dict()} for outcome in outcomes
        ],
        "count": len(outcomes),
        "summary": await asyncio.to_thread(engine.summary),
    }


# ---------------------------------------------------------------------------
# outlier review queue
# ---------------------------------------------------------------------------
class OutlierDecision(BaseModel):
    """Body of ``POST /api/quality/outliers/{id}/decide``."""

    model_config = ConfigDict(extra="ignore")

    decision: Annotated[
        Literal["quarantine", "accept", "escalate", "open"],
        Field(
            description=(
                "quarantine (drop the point from every store read), accept (keep the "
                "value), escalate (leave the value in place and keep the point in the "
                "queue, flagged for a human), or open to undo a previous decision"
            )
        ),
    ]
    by: str = "user"
    note: str | None = None


class TicketRequest(BaseModel):
    """Body of ``POST /api/quality/tickets``."""

    model_config = ConfigDict(extra="ignore")

    title: str
    dataset: str = ""
    check: str = ""
    body: str = ""
    severity: str = "warn"
    opened_by: str = "user"
    incident_id: str | None = None


@router.get("/outliers", summary="The outlier review queue")
async def outlier_queue(
    engine: Engine,
    dataset: Annotated[str | None, Query()] = None,
    include_decided: Annotated[bool, Query()] = True,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    """Return flagged observations awaiting (or carrying) a review decision."""
    rows = await asyncio.to_thread(
        lambda: engine.outlier_queue(
            dataset=dataset, include_decided=include_decided, limit=limit
        )
    )
    counts = await asyncio.to_thread(engine.outlier_counts)
    return {"outliers": rows, "count": len(rows), "counts": counts}


@router.post(
    "/outliers/{outlier_id}/decide",
    summary="Quarantine, accept or escalate an outlier",
)
async def decide_outlier(
    engine: Engine, outlier_id: str, body: OutlierDecision
) -> dict[str, Any]:
    """Record a review decision.

    Quarantining takes effect immediately across every store read, so the point
    stops reaching features, screens and backtests until the decision is undone.
    Escalating changes no data: the point keeps its value, stays in the queue and
    is counted in ``counts.awaiting_review`` until a human resolves it.
    """
    with translate_errors("outlier"):
        outlier = await engine.decide_outlier(
            outlier_id, body.decision, by=body.by, note=body.note
        )
    counts = await asyncio.to_thread(engine.outlier_counts)
    return {"outlier": outlier, "counts": counts}


# ---------------------------------------------------------------------------
# vendor SLA and tickets
# ---------------------------------------------------------------------------
@router.get("/sla", summary="Ingestion SLA per source")
async def quality_sla(engine: Engine, catalog: Catalog) -> dict[str, Any]:
    """Return one SLA row per enabled source, breaches first.

    The "contract" is each source's own configured cadence - there is no vendor
    paperwork in this workspace, and every row says which schedule it measured.
    """
    rows = await asyncio.to_thread(lambda: engine.sla_report(catalog))
    return {
        "sla": rows,
        "count": len(rows),
        "breached": sum(1 for row in rows if row["state"] == "breached"),
    }


@router.get("/tickets", summary="Quality tickets")
async def list_tickets(
    engine: Engine,
    include_closed: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    """Return workspace tickets raised from failing checks."""
    rows = await asyncio.to_thread(
        lambda: engine.tickets(include_closed=include_closed, limit=limit)
    )
    return {"tickets": rows, "count": len(rows)}


@router.post("/tickets", status_code=201, summary="Open a quality ticket")
async def open_ticket(engine: Engine, body: TicketRequest) -> dict[str, Any]:
    """Open a ticket for a failing check.

    No external issue tracker is configured, so the ticket is recorded here and
    the response says where it lives rather than implying a hand-off happened.
    """
    with translate_errors("ticket"):
        ticket = await asyncio.to_thread(
            lambda: engine.open_ticket(
                title=body.title,
                dataset=body.dataset,
                check=body.check,
                body=body.body,
                severity=Severity(body.severity),
                opened_by=body.opened_by,
                incident_id=body.incident_id,
            )
        )
    return {"ticket": ticket, "tracker": "tradinglib workspace (no external tracker configured)"}


@router.post("/tickets/{ticket_id}/close", summary="Close a quality ticket")
async def close_ticket(engine: Engine, ticket_id: str) -> dict[str, Any]:
    """Close one ticket."""
    with translate_errors("ticket"):
        return await asyncio.to_thread(lambda: engine.close_ticket(ticket_id))
