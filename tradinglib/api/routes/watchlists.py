"""Watchlist endpoints (``docs/SPEC.md`` section 5).

Watchlists are stored by :class:`tradinglib.data.universe.WatchlistRegistry` as a
name, an ordered symbol list and a default flag. The REFERENCE "New
collection" dialog also offers a description, a construction method and a
refresh cadence; those have nowhere to live on a watchlist, so instead of
dropping them silently the create response lists them under ``ignored_fields``
(a universe stores them - see ``POST /api/universes``).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from tradinglib.api.deps import get_watchlists, translate_errors
from tradinglib.data.universe import Watchlist, WatchlistRegistry

router = APIRouter(prefix="/api/watchlists", tags=["watchlists"])

Registry = Annotated[WatchlistRegistry, Depends(get_watchlists)]


class WatchlistCreate(BaseModel):
    """Body of ``POST /api/watchlists`` (canonical names or dialog control ids)."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    name: str = Field(validation_alias="collectionName", serialization_alias="name")
    symbols: list[str] | str = Field(default_factory=list, validation_alias="collectionSymbols")
    watchlist_id: str | None = None
    is_default: bool = Field(default=False, validation_alias="makeDefault")
    description: str | None = Field(default=None, validation_alias="collectionDescription")
    construction_method: str | None = Field(default=None, validation_alias="constructionMethod")
    refresh: str | None = Field(default=None, validation_alias="collectionRefresh")


class WatchlistUpdate(BaseModel):
    """Body of ``PUT /api/watchlists/{id}``; omitted fields are left alone."""

    model_config = ConfigDict(extra="ignore")

    name: str | None = None
    symbols: list[str] | str | None = None
    is_default: bool | None = None


class SymbolRequest(BaseModel):
    """Body of ``POST /api/watchlists/{id}/symbols``."""

    model_config = ConfigDict(extra="ignore")

    symbol: str


def _split(value: list[str] | str | None) -> list[str]:
    """Split a symbol payload (list, or comma/newline separated text) into items."""
    if value is None:
        return []
    if isinstance(value, str):
        parts = [part.strip() for part in value.replace("\n", ",").replace(";", ",").split(",")]
    else:
        parts = [str(part).strip() for part in value]
    return [part for part in parts if part]


def _payload(watchlist: Watchlist) -> dict[str, Any]:
    """Serialise one watchlist for the API."""
    return watchlist.as_dict()


@router.get("", summary="List watchlists")
async def list_watchlists(registry: Registry) -> dict[str, Any]:
    """Return every watchlist, the default one first."""
    with translate_errors("watchlist"):
        watchlists = registry.list_watchlists()
        default = registry.default_watchlist()
    return {
        "watchlists": [_payload(w) for w in watchlists],
        "count": len(watchlists),
        "default": default.id if default else None,
    }


@router.post("", status_code=201, summary="Create a watchlist")
async def create_watchlist(registry: Registry, body: WatchlistCreate) -> dict[str, Any]:
    """Create a watchlist. The first one created becomes the default."""
    with translate_errors("watchlist"):
        watchlist = registry.create(
            body.name,
            _split(body.symbols),
            watchlist_id=body.watchlist_id,
            is_default=body.is_default,
        )
    payload = _payload(watchlist)
    ignored = [
        field
        for field, value in (
            ("description", body.description),
            ("construction_method", body.construction_method),
            ("refresh", body.refresh),
        )
        if value
    ]
    if ignored:
        payload["ignored_fields"] = ignored
        payload["ignored_detail"] = (
            "a watchlist stores a name and symbols only; create a universe to keep "
            "a description, construction method or refresh cadence"
        )
    return payload


@router.get("/{watchlist_id}", summary="Return one watchlist")
async def get_watchlist(registry: Registry, watchlist_id: str) -> dict[str, Any]:
    """Return one watchlist by id."""
    with translate_errors("watchlist"):
        return _payload(registry.get(watchlist_id))


@router.put("/{watchlist_id}", summary="Update a watchlist")
async def update_watchlist(
    registry: Registry, watchlist_id: str, body: WatchlistUpdate
) -> dict[str, Any]:
    """Rename a watchlist, replace its symbols or make it the default."""
    changes: dict[str, Any] = {}
    if body.name is not None:
        changes["name"] = body.name
    if body.symbols is not None:
        changes["symbols"] = _split(body.symbols)
    if body.is_default is not None:
        changes["is_default"] = body.is_default
    with translate_errors("watchlist"):
        if not changes:
            return _payload(registry.get(watchlist_id))
        return _payload(registry.update(watchlist_id, **changes))


@router.delete("/{watchlist_id}", summary="Delete a watchlist")
async def delete_watchlist(registry: Registry, watchlist_id: str) -> dict[str, Any]:
    """Delete a watchlist; the default flag moves to another list if needed."""
    with translate_errors("watchlist"):
        registry.delete(watchlist_id)
        default = registry.default_watchlist()
    return {"deleted": watchlist_id, "default": default.id if default else None}


@router.post("/{watchlist_id}/symbols", summary="Add a symbol")
async def add_symbol(
    registry: Registry, watchlist_id: str, body: SymbolRequest
) -> dict[str, Any]:
    """Append one symbol (normalised to the store's spelling) to a watchlist."""
    with translate_errors("watchlist"):
        return _payload(registry.add_symbol(watchlist_id, body.symbol))


@router.delete("/{watchlist_id}/symbols/{symbol}", summary="Remove a symbol")
async def remove_symbol(registry: Registry, watchlist_id: str, symbol: str) -> dict[str, Any]:
    """Remove one symbol from a watchlist (a no-op when it is not there)."""
    with translate_errors("watchlist"):
        return _payload(registry.remove_symbol(watchlist_id, symbol))
