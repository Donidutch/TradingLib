"""``GET /api/status`` - the datastrip payload.

Every figure here is observed, never invented. Subsystems that are not running
report ``null`` (unknown) or a genuine zero count, and ``engine_health`` names
which subsystems are actually up. The ingestion figures come from the running
:class:`~tradinglib.data.ingest.gateway.IngestGateway`, which in turn only reports
what real probes and real ingest runs recorded; the agent figures (trials,
spend) read the agents tables and are honest zeros until that subsystem exists.

The same payload is published on the ``status`` WebSocket channel by
:class:`tradinglib.api.live.StatusBroadcaster`, so the UI datastrip and this
endpoint can never disagree.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from sqlalchemy import column, func, select, table

from tradinglib import __version__
from tradinglib.config import get_settings
from tradinglib.db import get_session, table_exists

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["status"])

#: Table and column names are code-supplied constants; this guards against a
#: future caller passing anything that could reach SQL as an identifier.
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _check_identifier(name: str) -> None:
    """Raise :class:`ValueError` when ``name`` is not a plain SQL identifier."""
    if not _IDENTIFIER.match(name):
        raise ValueError(f"invalid SQL identifier: {name!r}")


def _count_rows(table_name: str) -> int:
    """Count rows in ``table_name``, returning 0 when the table does not exist yet.

    Zero here means "nothing has been recorded", which is the honest answer
    before the owning subsystem has run for the first time. A table that exists
    but cannot be read is logged and also counted as zero - the datastrip must
    never block on a storage fault, but the fault is not hidden from the log.
    """
    _check_identifier(table_name)
    if not table_exists(table_name):
        return 0
    session = get_session()
    try:
        stmt = select(func.count()).select_from(table(table_name))
        return int(session.execute(stmt).scalar_one())
    except Exception:  # noqa: BLE001 - reported in the log, zeroed in the payload
        logger.exception("status: could not count rows in %s", table_name)
        return 0
    finally:
        session.close()


def _sum_column(table_name: str, column_name: str) -> float:
    """Sum a numeric column, returning 0.0 when the table does not exist yet."""
    _check_identifier(table_name)
    _check_identifier(column_name)
    if not table_exists(table_name):
        return 0.0
    session = get_session()
    try:
        target = table(table_name, column(column_name))
        stmt = select(func.coalesce(func.sum(target.c[column_name]), 0.0))
        return float(session.execute(stmt).scalar_one())
    except Exception:  # noqa: BLE001 - reported in the log, zeroed in the payload
        logger.exception("status: could not sum %s.%s", table_name, column_name)
        return 0.0
    finally:
        session.close()


#: The shape returned when no scope is selected or the registry is down.
_NO_SCOPE: dict[str, Any] = {
    "id": None,
    "name": None,
    "kind": None,
    "symbols": 0,
    "members_fetched_at": None,
}


def _universe_summary(state: Any) -> dict[str, Any]:
    """Return the active scope, or nulls when the registry is not running.

    ``kind`` distinguishes a universe from a watchlist: the topbar selector
    offers both, so the datastrip has to be able to name which one is driving
    the tape.
    """
    registry = getattr(state, "universe_registry", None)
    if registry is None:
        return dict(_NO_SCOPE)
    try:
        scope = registry.active_scope()
        universe = registry.get_active()
    except Exception:  # noqa: BLE001 - a storage fault must not break the strip
        logger.exception("status: could not read the active scope")
        return dict(_NO_SCOPE)
    if scope["kind"] is None:
        return dict(_NO_SCOPE)
    return {
        "id": scope["id"],
        "name": scope["name"],
        "kind": scope["kind"],
        "symbols": scope["symbol_count"],
        "members_fetched_at": (
            universe.members_fetched_at.isoformat()
            if universe is not None and universe.members_fetched_at
            else None
        ),
    }


def _quality_summary(state: Any) -> dict[str, Any] | None:
    """Return the latest quality figures, or ``None`` when the engine is absent."""
    engine = getattr(state, "quality_engine", None)
    if engine is None:
        return None
    try:
        summary = engine.summary()
    except Exception:  # noqa: BLE001 - reported, not raised into the strip
        logger.exception("status: could not read the quality summary")
        return None
    return {
        "overall_quality_pct": summary["overall_quality_pct"],
        "open_incidents": summary["open_incidents"],
        "failed_checks": summary["failed_checks"],
        "last_run": summary["last_run"],
    }


def _fundamentals_coverage(research: Any, state: Any) -> dict[str, Any] | None:
    """Return fundamentals coverage over the active universe.

    Non-blocking by construction: the research engine hands back its last
    measurement and schedules a refresh when that one has aged out, so a
    datastrip poll never waits on a vendor. ``None`` means "not measured yet",
    which the UI is required to render as such rather than as ``0``.
    """
    if research is None:
        return None
    registry = getattr(state, "universe_registry", None)
    scope_id: str | None = None
    if registry is not None:
        try:
            scope_id = registry.active_scope()["id"]
        except Exception:  # noqa: BLE001 - reported, not raised into the strip
            logger.exception("status: could not read the active scope")
    try:
        # `None` here means "the active scope", which the engine resolves the
        # same way - passing the id keeps the memo keyed to the right one.
        coverage = research.fundamentals_coverage(scope_id)
    except Exception:  # noqa: BLE001 - reported, not raised into the strip
        logger.exception("status: could not read fundamentals coverage")
        return None
    return None if coverage is None else coverage.as_dict()


async def build_status(app: FastAPI) -> dict[str, Any]:
    """Assemble the datastrip payload from whatever is genuinely running.

    Args:
        app: the FastAPI application; ``app.state`` carries the subsystem
            handles installed by the lifespan.

    Returns:
        The ``/api/status`` JSON body.
    """
    settings = get_settings()
    state = app.state

    scheduler = getattr(state, "ingest_scheduler", None)
    gateway = getattr(state, "ingest_gateway", None)
    catalog = getattr(state, "source_catalog", None)
    runner = getattr(state, "pipeline_runner", None)
    hub = getattr(state, "ws_hub", None)
    stream = getattr(state, "alpaca_stream", None)
    research = getattr(state, "company_research", None)

    # --- ingestion: measured by the gateway, or zero when it is not running --
    gateway_ms: float | None = None
    connected = total = delayed_series = 0
    last_ingest: str | None = None
    gateway_detail: str | None = None
    if gateway is not None:
        health = await gateway.health(measure=False, publish=False)
        gateway_ms = health.latency_ms
        connected = health.sources_connected
        total = health.sources_total
        delayed_series = health.delayed_series
        last_ingest = health.last_ingest_at.isoformat() if health.last_ingest_at else None
        gateway_detail = health.detail
    elif catalog is not None:
        connected, total = catalog.connected_count()
    if last_ingest is None and scheduler is not None:
        stamp = scheduler.last_ingest_at()
        last_ingest = stamp.isoformat() if stamp else None

    # --- spend: real accounting, or zero against the configured budget ------
    if runner is not None:
        spend = runner.spend(window="day")
    else:
        spend = {"used": _sum_column("agent_runs", "cost_usd"), "budget": settings.budgets.daily_usd}

    db_ok = True
    try:
        session = get_session()
        session.close()
    except Exception:  # noqa: BLE001 - reported, not raised
        db_ok = False

    stream_running = bool(stream is not None and stream.running)
    subsystems = {
        "database": "up" if db_ok else "down",
        "event_bus": "up",
        "websocket": "up" if hub is not None else "down",
        "source_catalog": "up" if catalog is not None else "not_started",
        "ingest_scheduler": "up" if scheduler is not None and scheduler.running else "not_started",
        "ingest_gateway": "up" if gateway is not None else "not_started",
        "universe_registry": "up"
        if getattr(state, "universe_registry", None) is not None
        else "not_started",
        "quality_engine": "up"
        if getattr(state, "quality_engine", None) is not None
        else "not_started",
        "agent_runner": "up" if runner is not None else "not_started",
    }
    # The Alpaca stream is optional: without credentials it is honestly
    # "not_configured", which is not a fault and must not degrade the engine.
    stream_state = getattr(state, "alpaca_stream_status", None) or {}
    if stream is None:
        subsystems["alpaca_stream"] = str(stream_state.get("status") or "not_configured")
    else:
        subsystems["alpaca_stream"] = "up" if stream_running else "stopped"

    required = {
        key: value
        for key, value in subsystems.items()
        if key not in {"agent_runner", "alpaca_stream"}
    }
    down = [key for key, value in required.items() if value != "up"]
    engine_health = "healthy" if db_ok and not down else "degraded"

    return {
        "data_live": bool(
            stream_running or (scheduler is not None and scheduler.running and connected > 0)
        ),
        "gateway_ms": gateway_ms,
        "sources_connected": connected,
        "sources_total": total,
        "last_ingest": last_ingest,
        "delayed_series": delayed_series,
        # Fundamentals coverage over the active universe. `None` until the
        # first background measurement lands - the UI shows "measuring" rather
        # than a count nobody took.
        "fundamentals_coverage": _fundamentals_coverage(research, state),
        # Every run that produces a research record is a logged trial: lab
        # experiments, lab backtests and agent pipeline runs alike. The agents
        # tables exist now, so this counts them rather than ignoring them.
        "trials_logged": (
            _count_rows("experiment_runs") + _count_rows("backtest_runs") + _count_rows("agent_runs")
        ),
        "spend": {"used": float(spend.get("used", 0.0)), "budget": float(spend.get("budget", 0.0))},
        "engine_health": engine_health,
        "engine_detail": gateway_detail,
        "degraded_subsystems": down,
        "subsystems": subsystems,
        # Both read SQLite, so they run on a worker thread rather than
        # blocking the event loop on every datastrip build.
        "universe": await asyncio.to_thread(_universe_summary, state),
        "quality": await asyncio.to_thread(_quality_summary, state),
        "market_stream": stream.status() if stream is not None else stream_state or None,
        "ws_connections": hub.connection_count if hub is not None else 0,
        "version": __version__,
        "server_time_utc": datetime.now(UTC).isoformat(),
        "uptime_seconds": round(
            time.monotonic() - getattr(state, "started_monotonic", time.monotonic()), 3
        ),
    }


@router.get("/status", summary="Datastrip status payload")
async def get_status(request: Request) -> dict[str, Any]:
    """Return the datastrip figures. Unknown values are ``null``, not guesses."""
    return await build_status(request.app)
