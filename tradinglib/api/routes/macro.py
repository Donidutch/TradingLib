"""Macro-series endpoints backing the Overview's *FRED macro* intel panel.

The values come from :class:`tradinglib.data.macro.MacroService`: the Parquet store
first, the keyless FRED CSV endpoint when the stored copy is missing or older
than the series' publication cadence. Nothing here invents a level - when FRED
cannot be reached and the store is empty, the reading carries ``value: null``
and a ``reason`` the panel prints.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from tradinglib.api.deps import get_macro, translate_errors
from tradinglib.data.macro import MacroService

router = APIRouter(prefix="/api/macro", tags=["macro"])

Service = Annotated[MacroService, Depends(get_macro)]


@router.get("/series", summary="Macro panel readings")
async def macro_panel(
    service: Service,
    refresh: Annotated[bool, Query(description="force a vendor refresh")] = False,
) -> dict[str, Any]:
    """Return the four readings the Overview's *FRED macro* panel shows."""
    with translate_errors("macro series"):
        readings = await service.panel(refresh=refresh)
    return {
        "namespace": "fred",
        "series": [reading.as_dict() for reading in readings],
        "available": sum(1 for reading in readings if reading.value is not None),
        "total": len(readings),
    }


@router.get("/series/{series_id}", summary="One macro series")
async def macro_series(
    service: Service,
    series_id: str,
    start: Annotated[date | None, Query(description="earliest observation to return")] = None,
    refresh: Annotated[bool, Query(description="force a vendor refresh")] = False,
    history: Annotated[bool, Query(description="include the observation list")] = True,
) -> dict[str, Any]:
    """Return one macro series' current level and (optionally) its history."""
    spec = MacroService.spec_for(series_id)
    with translate_errors(f"macro series {series_id}"):
        reading = await service.reading(spec, start=start, refresh=refresh)
    return reading.as_dict(include_history=history)
