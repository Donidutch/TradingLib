"""Universe endpoints (``docs/SPEC.md`` section 5).

Every payload comes from :class:`tradinglib.data.universe.UniverseRegistry`, so a
built-in whose constituents have never been fetched reports ``member_count``
0 and ``members_fetched_at`` ``null`` instead of a plausible-looking list.
Point-in-time membership names the basis it used (``snapshot`` versus
``membership-dates``) so the UI can say which one the user is looking at.
"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from tradinglib.api.deps import get_universes, translate_errors
from tradinglib.data.universe import (
    Universe,
    UniverseRegistry,
    detect_corporate_actions,
    moving_average_breadth,
)

router = APIRouter(prefix="/api/universes", tags=["universes"])

Registry = Annotated[UniverseRegistry, Depends(get_universes)]


class UniverseCreate(BaseModel):
    """Body of ``POST /api/universes``.

    Accepts both canonical field names and the REFERENCE "New collection"
    dialog's control ids (``collectionName``, ``collectionSymbols``...), so the
    UI can post the form as-is.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    name: str = Field(validation_alias="collectionName", serialization_alias="name")
    symbols: list[str] | str = Field(default_factory=list, validation_alias="collectionSymbols")
    description: str | None = Field(default=None, validation_alias="collectionDescription")
    rule: str | None = None
    universe_id: str | None = None
    make_active: bool = Field(default=False, validation_alias="makeDefault")
    construction_method: str | None = Field(default=None, validation_alias="constructionMethod")
    refresh: str | None = Field(default=None, validation_alias="collectionRefresh")


class UniverseUpdate(BaseModel):
    """Body of ``PATCH``/``PUT /api/universes/{id}``; omitted fields are left alone."""

    model_config = ConfigDict(extra="ignore")

    name: str | None = None
    description: str | None = None
    rule: str | None = None
    symbols: list[str] | str | None = None


class SnapshotRequest(BaseModel):
    """Body of ``POST /api/universes/{id}/snapshot``."""

    model_config = ConfigDict(extra="ignore")

    as_of: date | None = None
    source: str = "manual"
    note: str | None = None


class ActiveRequest(BaseModel):
    """Body of ``POST /api/universes/active``.

    ``universe_id`` accepts any selectable scope reference, because the topbar
    selector offers both populations: a universe id (``"sp500"``), or an
    explicit kind (``"watchlist:primary-research"``).
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    universe_id: str = Field(validation_alias="id", serialization_alias="universe_id")


def _split(value: list[str] | str | None) -> list[str]:
    """Split a symbol payload (list, or comma/newline separated text) into items."""
    if value is None:
        return []
    if isinstance(value, str):
        parts = [part.strip() for part in value.replace("\n", ",").replace(";", ",").split(",")]
    else:
        parts = [str(part).strip() for part in value]
    return [part for part in parts if part]


def _universe_payload(universe: Universe, *, include_members: bool = True) -> dict[str, Any]:
    """Serialise one universe for the API."""
    return universe.as_dict(include_members=include_members)


@router.get("", summary="List universes")
async def list_universes(
    registry: Registry,
    include_members: Annotated[bool, Query()] = False,
) -> dict[str, Any]:
    """Return every universe, built-ins first, with the active one flagged."""
    with translate_errors("universe"):
        universes = registry.list_universes(include_members=include_members)
        active = registry.active_universe_id()
    return {
        "universes": [_universe_payload(u, include_members=include_members) for u in universes],
        "count": len(universes),
        "active": active,
    }


@router.post("", status_code=201, summary="Create a universe")
async def create_universe(registry: Registry, body: UniverseCreate) -> dict[str, Any]:
    """Create a user universe from an explicit symbol list.

    The dialog's *construction method* and *refresh* choices are recorded in
    the universe's ``rule`` text (there is no rule engine behind them yet), so
    nothing the user typed is silently dropped.
    """
    symbols = _split(body.symbols)
    rule = body.rule
    if rule is None and (body.construction_method or body.refresh):
        parts = [p for p in (body.construction_method, body.refresh) if p]
        rule = " / ".join(parts)
    with translate_errors("universe"):
        universe = registry.create(
            body.name,
            symbols,
            description=body.description,
            universe_id=body.universe_id,
            rule=rule,
        )
        if body.make_active:
            universe = registry.set_active(universe.id)
    return _universe_payload(universe)


@router.get("/active", summary="Return the active scope")
async def get_active(registry: Registry) -> dict[str, Any]:
    """Return the collection the topbar selector currently points at.

    ``universe`` stays populated only when the selection *is* a universe;
    ``scope`` describes the selection whichever kind it is, so a client that
    supports watchlists reads ``scope`` and an older one still works.
    """
    with translate_errors("universe"):
        universe = registry.get_active()
        scope = registry.active_scope()
    return {
        "active": scope["id"],
        "scope": scope,
        "universe": _universe_payload(universe, include_members=False) if universe else None,
        "symbols": scope["symbols"],
    }


@router.post("/active", summary="Select the active scope")
async def set_active(registry: Registry, body: ActiveRequest) -> dict[str, Any]:
    """Select the active collection; it drives the tape, screens and scanner scope.

    Accepts a universe id or a ``watchlist:<id>`` reference - the topbar offers
    both, and selecting either means the same thing downstream.
    """
    with translate_errors("universe"):
        scope = registry.set_active_scope(body.universe_id)
        universe = registry.get_active()
    return {
        "active": scope["id"],
        "scope": scope,
        "universe": _universe_payload(universe, include_members=False) if universe else None,
        "symbols": scope["symbols"],
    }


@router.get("/{universe_id}", summary="Return one universe")
async def get_universe(
    registry: Registry,
    universe_id: str,
    include_members: Annotated[bool, Query()] = True,
) -> dict[str, Any]:
    """Return one universe by id."""
    with translate_errors("universe"):
        universe = registry.get(universe_id)
    return _universe_payload(universe, include_members=include_members)


@router.put("/{universe_id}", summary="Update a user universe")
async def update_universe(
    registry: Registry, universe_id: str, body: UniverseUpdate
) -> dict[str, Any]:
    """Update a user universe's name, description, rule or membership."""
    changes: dict[str, Any] = {}
    if body.name is not None:
        changes["name"] = body.name
    if body.description is not None:
        changes["description"] = body.description
    if body.rule is not None:
        changes["rule"] = body.rule
    if body.symbols is not None:
        changes["symbols"] = _split(body.symbols)
    if not changes:
        with translate_errors("universe"):
            return _universe_payload(registry.get(universe_id))
    with translate_errors("universe"):
        universe = registry.update(universe_id, **changes)
    return _universe_payload(universe)


@router.delete("/{universe_id}", summary="Delete a user universe")
async def delete_universe(registry: Registry, universe_id: str) -> dict[str, Any]:
    """Delete a user universe. Built-ins are protected and answer 400."""
    with translate_errors("universe"):
        registry.delete(universe_id)
    return {"deleted": universe_id}


@router.get("/{universe_id}/members", summary="Return universe membership")
async def get_members(
    registry: Registry,
    universe_id: str,
    as_of: Annotated[date | None, Query(description="point-in-time date")] = None,
) -> dict[str, Any]:
    """Return membership, point-in-time when ``as_of`` is given.

    The response's ``basis`` says whether the answer came from a stored
    snapshot or was reconstructed from each member's added/removed dates.
    """
    with translate_errors("universe"):
        payload = registry.membership(universe_id, as_of=as_of)
        universe = registry.get(universe_id)
    payload["count"] = len(payload["members"])
    payload["name"] = universe.name
    payload["members_fetched_at"] = (
        universe.members_fetched_at.isoformat() if universe.members_fetched_at else None
    )
    return payload


@router.post("/{universe_id}/snapshot", status_code=201, summary="Freeze membership")
async def create_snapshot(
    registry: Registry,
    universe_id: str,
    body: Annotated[SnapshotRequest, Body()] = SnapshotRequest(),
) -> dict[str, Any]:
    """Freeze current membership as a dated snapshot (idempotent per date)."""
    with translate_errors("universe"):
        return registry.snapshot(
            universe_id, as_of=body.as_of, source=body.source, note=body.note
        )


@router.get("/{universe_id}/snapshots", summary="List snapshots")
async def list_snapshots(registry: Registry, universe_id: str) -> dict[str, Any]:
    """List the dated snapshots recorded for a universe, newest first."""
    with translate_errors("universe"):
        snapshots = registry.snapshots(universe_id)
    return {"universe": universe_id, "snapshots": snapshots, "count": len(snapshots)}


@router.get("/{universe_id}/changelog", summary="Membership change log")
async def universe_changelog(
    registry: Registry,
    universe_id: str,
    limit: Annotated[int, Query(ge=1, le=50, description="how many transitions")] = 6,
    corporate_actions: Annotated[
        bool, Query(description="reconcile add/drop pairs against SEC EDGAR")
    ] = True,
) -> dict[str, Any]:
    """Return the add/drop diff between consecutive snapshots.

    Each transition names the symbols that entered and left, the construction
    rule that was in force when the later snapshot was frozen, and — when
    ``corporate_actions`` is on and EDGAR is reachable — the add/drop pairs that
    are really one registrant changing ticker or consolidating share classes.
    Pairs that EDGAR cannot confirm are simply absent; nothing is guessed.
    """
    with translate_errors("universe"):
        entries = await asyncio.to_thread(registry.change_log, universe_id, limit=limit)
        revisions = await asyncio.to_thread(registry.rule_revisions, universe_id)
    if corporate_actions:
        paired = await asyncio.gather(
            *(
                detect_corporate_actions(entry["added"], entry["dropped"])
                for entry in entries
            )
        )
        for entry, actions in zip(entries, paired, strict=True):
            entry["corporate_actions"] = actions
            # A symbol explained by a corporate action is not a research
            # decision, so the UI can show the two populations apart.
            renamed_in = {action["to"] for action in actions}
            renamed_out = {action["from"] for action in actions}
            entry["added_by_rule"] = [s for s in entry["added"] if s not in renamed_in]
            entry["dropped_by_rule"] = [s for s in entry["dropped"] if s not in renamed_out]
    else:
        for entry in entries:
            entry["corporate_actions"] = []
            entry["added_by_rule"] = list(entry["added"])
            entry["dropped_by_rule"] = list(entry["dropped"])
    return {
        "universe": universe_id,
        "entries": entries,
        "count": len(entries),
        "rule_revisions": revisions,
        "rule_version": len(revisions),
    }


@router.delete("/{universe_id}/snapshots/{as_of}", summary="Delete one snapshot")
async def delete_snapshot(registry: Registry, universe_id: str, as_of: date) -> dict[str, Any]:
    """Delete one dated snapshot; reports whether a row was actually removed."""
    with translate_errors("universe"):
        removed = registry.delete_snapshot(universe_id, as_of)
    return {"universe": universe_id, "as_of": as_of.isoformat(), "deleted": removed}


@router.post("/{universe_id}/refresh", summary="Refresh a built-in universe")
async def refresh_universe(
    registry: Registry,
    universe_id: str,
    request: Request,
    force: Annotated[bool, Query(description="ignore the constituent cache")] = False,
) -> dict[str, Any]:
    """Re-materialise a built-in universe from its real upstream source.

    ``us-quality-50`` is rebuilt from its documented static definition; the
    index universes are fetched from their published constituent tables. A
    fetch failure with a usable cache falls back to the cache and keeps
    ``members_fetched_at`` truthful about its age.
    """
    with translate_errors("universe"):
        universe = await registry.refresh_builtin(universe_id, force=force)
    bus = getattr(request.app.state, "bus", None)
    if bus is not None:
        await bus.publish(
            "notifications",
            {
                "event": "universe_refreshed",
                "universe": universe.id,
                "members": len(universe.members),
            },
        )
    return _universe_payload(universe, include_members=False)


@router.get("/{universe_id}/breadth", summary="Share of constituents above their moving average")
async def universe_breadth(
    registry: Registry,
    universe_id: str,
    request: Request,
    window: Annotated[int, Query(ge=20, le=400, description="moving-average length in bars")] = 200,
    refresh: Annotated[
        bool, Query(description="fetch constituents first when the universe has none")
    ] = True,
) -> dict[str, Any]:
    """Return index breadth: the share of members closing above their own SMA.

    This is the *constituent* statistic the Overview's market-context panel
    shows ("S&P above 200D"), not the index measured against its own average.
    Members without ``window`` stored closes are excluded and listed under
    ``uncovered``, and a sample too thin to mean anything returns
    ``percent: null`` plus the reason rather than a flattering number.
    """
    with translate_errors("universe"):
        symbols = registry.symbols(universe_id)
        if not symbols and refresh:
            universe = await registry.refresh_builtin(universe_id)
            symbols = [member.symbol for member in universe.members]
        universe_name = registry.get(universe_id).name
    store = getattr(request.app.state, "store", None)
    if store is None:  # pragma: no cover - the lifespan always installs a store
        raise HTTPException(status_code=503, detail="the time-series store is not available")
    payload = await asyncio.to_thread(
        moving_average_breadth, store, symbols, window=window
    )
    payload["universe_id"] = universe_id
    payload["universe"] = universe_name
    return payload


@router.get("/{universe_id}/resolve", summary="Resolve a universe to symbols")
async def resolve_universe(
    registry: Registry,
    universe_id: str,
    as_of: Annotated[date | None, Query()] = None,
) -> dict[str, Any]:
    """Resolve a universe/watchlist reference to the concrete symbol list."""
    with translate_errors("universe"):
        symbols = registry.resolve(universe_id, as_of=as_of)
    return {"ref": universe_id, "symbols": symbols, "count": len(symbols)}
