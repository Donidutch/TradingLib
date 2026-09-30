"""Screener endpoints (``docs/SPEC.md`` section 5).

CRUD over saved screens plus the two actions the Market & Data workspace
performs: running a screen (which returns the real pass funnel, the survivors
and the result-profile statistics) and queueing a backtest of the survivors.

The queue-backtest route creates a *real* Strategy Lab backtest record: the
screener hands a :class:`tradinglib.research.BacktestRequest` to whatever queue the
lab registered, and the lab's own record id comes back in the response. If no
lab runner is registered in this process the request is still persisted, with
status ``pending_runner`` and a detail saying so - the UI shows that text
rather than a fabricated "backtest started".
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import date
from typing import Annotated, Any, TypeVar

from fastapi import APIRouter, Body, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from tradinglib.api.deps import (
    get_funnel,
    get_screen_alerts,
    get_screen_registry,
    translate_errors,
)
from tradinglib.research.screen.alerts import ScreenAlertMonitor
from tradinglib.research.screen.funnel import RESULT_COLUMNS, FunnelEvaluator, ScreenRegistry
from tradinglib.research.screen.rules import METRICS, Operator, Scope, Screen

router = APIRouter(prefix="/api/screens", tags=["screens"])

Registry = Annotated[ScreenRegistry, Depends(get_screen_registry)]
Funnel = Annotated[FunnelEvaluator, Depends(get_funnel)]
Alerts = Annotated[ScreenAlertMonitor, Depends(get_screen_alerts)]

T = TypeVar("T")

#: Keys a caller may not smuggle into ``params``: they are already explicit
#: arguments of the queue-backtest call and a duplicate would be a TypeError.
_RESERVED_QUEUE_PARAMS = frozenset(
    {"run_id", "name", "start", "end", "benchmark", "rebalance", "initial_capital"}
)


async def _thread(fn: Callable[[], T]) -> T:
    """Run a blocking registry call on a worker thread."""
    return await asyncio.to_thread(fn)


class ScreenCreate(BaseModel):
    """Body of ``POST /api/screens``."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    name: str = Field(validation_alias="screenName", serialization_alias="name")
    universe_id: str = Field(validation_alias="universeId", serialization_alias="universe_id")
    rules: list[dict[str, Any]] = Field(default_factory=list)
    expression: str | None = None
    description: str | None = None
    rank_by: str = "composite"
    owner: str | None = None
    tags: list[str] = Field(default_factory=list)
    screen_id: str | None = Field(default=None, validation_alias="id")


class ScreenUpdate(BaseModel):
    """Body of ``PUT /api/screens/{id}``; omitted fields are left alone."""

    model_config = ConfigDict(extra="ignore")

    name: str | None = None
    universe_id: str | None = None
    rules: list[dict[str, Any]] | None = None
    expression: str | None = None
    description: str | None = None
    rank_by: str | None = None
    owner: str | None = None
    tags: list[str] | None = None


class RunRequest(BaseModel):
    """Body of ``POST /api/screens/{id}/run``."""

    model_config = ConfigDict(extra="ignore")

    as_of: date | None = None
    refresh: bool = False
    limit: int | None = Field(default=None, ge=1)
    persist: bool = True


class PreviewRequest(BaseModel):
    """Body of ``POST /api/screens/preview`` - an unsaved rule set."""

    model_config = ConfigDict(extra="ignore")

    universe_id: str | None = None
    rules: list[dict[str, Any]] = Field(default_factory=list)
    expression: str | None = None
    rank_by: str = "composite"
    as_of: date | None = None
    name: str = "preview"


class QueueBacktestRequest(BaseModel):
    """Body of ``POST /api/screens/{id}/queue-backtest``."""

    model_config = ConfigDict(extra="ignore")

    run_id: str | None = None
    name: str | None = None
    start: date | None = None
    end: date | None = None
    benchmark: str = "SPY"
    rebalance: str = "monthly"
    initial_capital: float = 1_000_000.0
    params: dict[str, Any] = Field(default_factory=dict)


def _screen_payload(screen: Screen) -> dict[str, Any]:
    """Serialise a screen, including the "View as query" text it renders to."""
    payload = screen.as_dict()
    payload["query"] = screen.to_query()
    payload["active_rule_count"] = len(screen.active_rules)
    return payload


@router.get("", summary="List saved screens")
async def list_screens(registry: Registry) -> dict[str, Any]:
    """Return every saved screen, most recently updated first."""
    with translate_errors("screen"):
        screens = await _thread(registry.list_screens)
    return {"screens": [_screen_payload(s) for s in screens], "count": len(screens)}


@router.post("", status_code=201, summary="Create a screen")
async def create_screen(registry: Registry, body: ScreenCreate) -> dict[str, Any]:
    """Create a screen; its rules are validated before anything is stored."""
    with translate_errors("screen"):
        screen = await _thread(
            lambda: registry.create(
                body.name,
                body.universe_id,
                rules=body.rules,
                expression=body.expression,
                description=body.description,
                rank_by=body.rank_by,
                owner=body.owner,
                tags=body.tags,
                screen_id=body.screen_id,
            )
        )
    return _screen_payload(screen)


@router.get("/metrics", summary="Screenable metric catalogue")
async def list_metrics() -> dict[str, Any]:
    """Return every screenable metric with its unit, source and description.

    Also returns the operators and scopes the rule builder may offer, and the
    columns a run's survivor rows carry, so the UI never hardcodes them.
    """
    return {
        "metrics": [spec.as_dict() for spec in METRICS.values()],
        "count": len(METRICS),
        "operators": [str(op) for op in Operator],
        "scopes": [str(scope) for scope in Scope],
        "result_columns": list(RESULT_COLUMNS),
    }


@router.post("/preview", summary="Count survivors of an unsaved rule set")
async def preview_screen(funnel: Funnel, body: PreviewRequest) -> dict[str, Any]:
    """Return the live survivor count while the user edits rules."""
    with translate_errors("screen"):
        universe_id = await _active_universe(funnel, body.universe_id)
        screen = Screen.from_dict(
            {
                "id": "preview",
                "name": body.name,
                "universe_id": universe_id,
                "rules": body.rules,
                "expression": body.expression,
                "rank_by": body.rank_by,
            }
        )
        panel = await funnel.metric_frame(universe_id, as_of=body.as_of)
        count = await funnel.preview_count(screen, as_of=body.as_of)
    return {
        "count": count,
        "base_count": len(panel),
        "universe_id": universe_id,
        "as_of": body.as_of.isoformat() if body.as_of else None,
        "query": screen.to_query(as_of=body.as_of),
        "coverage": panel.as_dict(),
    }


@router.get("/runs", summary="Recent screen runs")
async def list_runs(
    registry: Registry,
    screen_id: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
) -> dict[str, Any]:
    """Return recent runs across every screen (or one screen), newest first."""
    with translate_errors("screen run"):
        runs = await _thread(lambda: registry.list_runs(screen_id, limit=limit))
    return {"runs": runs, "count": len(runs)}


@router.get("/runs/{run_id}", summary="One screen run")
async def get_run(registry: Registry, run_id: str) -> dict[str, Any]:
    """Return one stored run including its survivors and funnel stages."""
    with translate_errors("screen run"):
        return await _thread(lambda: registry.get_run(run_id))


@router.get("/alerts", summary="Membership alerts on pinned screens")
async def list_screen_alerts(
    monitor: Alerts,
    screen_id: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    unacknowledged_only: Annotated[bool, Query()] = False,
) -> dict[str, Any]:
    """Return recent membership changes on screens tagged for alerting.

    The monitor's own state comes back alongside them, so the UI can say
    whether alerting is actually running and when it last cycled.
    """
    with translate_errors("screen alert"):
        alerts = await _thread(
            lambda: monitor.list_alerts(
                screen_id=screen_id, limit=limit, unacknowledged_only=unacknowledged_only
            )
        )
        status_payload = await _thread(monitor.status)
    return {
        "alerts": [alert.as_dict() for alert in alerts],
        "count": len(alerts),
        "monitor": status_payload,
    }


@router.post("/alerts/check", summary="Run the alert cycle now")
async def check_screen_alerts(monitor: Alerts) -> dict[str, Any]:
    """Re-run every alerting screen immediately and return what changed."""
    with translate_errors("screen alert"):
        alerts = await monitor.check_all()
    return {"alerts": [alert.as_dict() for alert in alerts], "count": len(alerts)}


@router.post("/alerts/{alert_id}/acknowledge", summary="Acknowledge one alert")
async def acknowledge_screen_alert(monitor: Alerts, alert_id: str) -> dict[str, Any]:
    """Mark one membership alert as seen."""
    with translate_errors("screen alert"):
        alert = await _thread(lambda: monitor.acknowledge(alert_id))
    return alert.as_dict()


@router.get("/backtests", summary="Backtest requests raised by screens")
async def list_backtest_requests(
    registry: Registry,
    screen_id: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
) -> dict[str, Any]:
    """Return the backtest requests the screener has raised, newest first."""
    with translate_errors("screen backtest request"):
        rows = await _thread(lambda: registry.list_backtest_requests(screen_id, limit=limit))
    return {"requests": rows, "count": len(rows)}


@router.get("/{screen_id}", summary="Return one screen")
async def get_screen(registry: Registry, screen_id: str) -> dict[str, Any]:
    """Return one saved screen."""
    with translate_errors("screen"):
        screen = await _thread(lambda: registry.get(screen_id))
    return _screen_payload(screen)


@router.put("/{screen_id}", summary="Update a screen")
async def update_screen(registry: Registry, screen_id: str, body: ScreenUpdate) -> dict[str, Any]:
    """Update a screen's editable fields; rules are re-validated."""
    changes = body.model_dump(exclude_none=True)
    with translate_errors("screen"):
        if not changes:
            screen = await _thread(lambda: registry.get(screen_id))
        else:
            screen = await _thread(lambda: registry.update(screen_id, **changes))
    return _screen_payload(screen)


@router.delete("/{screen_id}", summary="Delete a screen")
async def delete_screen(registry: Registry, screen_id: str) -> dict[str, Any]:
    """Delete a screen. Its runs are kept as research history."""
    with translate_errors("screen"):
        await _thread(lambda: registry.delete(screen_id))
    return {"deleted": screen_id}


@router.get("/{screen_id}/runs", summary="Runs of one screen")
async def list_screen_runs(
    registry: Registry,
    screen_id: str,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
) -> dict[str, Any]:
    """Return the runs of one screen, newest first."""
    with translate_errors("screen"):
        await _thread(lambda: registry.get(screen_id))
        runs = await _thread(lambda: registry.list_runs(screen_id, limit=limit))
    return {"screen_id": screen_id, "runs": runs, "count": len(runs)}


@router.post("/{screen_id}/run", summary="Run a screen")
async def run_screen(
    funnel: Funnel,
    registry: Registry,
    screen_id: str,
    body: Annotated[RunRequest, Body()] = RunRequest(),
) -> dict[str, Any]:
    """Evaluate a saved screen and return the funnel, survivors and statistics."""
    with translate_errors("screen"):
        screen = await _thread(lambda: registry.get(screen_id))
        run = await funnel.run(
            screen,
            as_of=body.as_of,
            persist=body.persist,
            refresh=body.refresh,
            limit=body.limit,
        )
    payload = run.as_dict()
    payload["screen"] = _screen_payload(screen)
    return payload


@router.post("/{screen_id}/queue-backtest", status_code=201, summary="Backtest this screen")
async def queue_backtest(
    funnel: Funnel,
    screen_id: str,
    body: Annotated[QueueBacktestRequest, Body()] = QueueBacktestRequest(),
) -> dict[str, Any]:
    """Queue a real backtest of a screen's survivors in the Strategy Lab.

    Raises:
        HTTPException: 404 when the screen or run is unknown, 400 when the
            screen has never been run (there would be no symbol list).
    """
    extras = {k: v for k, v in body.params.items() if k not in _RESERVED_QUEUE_PARAMS}
    with translate_errors("screen"):
        return await funnel.queue_backtest(
            screen_id,
            run_id=body.run_id,
            name=body.name,
            start=body.start,
            end=body.end,
            benchmark=body.benchmark,
            rebalance=body.rebalance,
            initial_capital=body.initial_capital,
            **extras,
        )


async def _active_universe(funnel: FunnelEvaluator, universe_id: str | None) -> str:
    """Return ``universe_id``, or the active universe when none was given.

    Raises:
        KeyError: when neither is available - translated to 404 rather than
            silently screening some default population.
    """
    if universe_id:
        return universe_id
    active = await asyncio.to_thread(funnel.universes.get_active)
    if active is None:
        raise KeyError("no universe was given and no active universe is set")
    return active.id
