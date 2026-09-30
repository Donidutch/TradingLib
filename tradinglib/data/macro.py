"""Macro-series service: FRED levels the Overview's *intel* panel reads.

The FRED provider fetches raw observations and the ingest scheduler writes the
catalog's ``fred`` scope (``DGS10``, ``CPIAUCSL``, ``UNRATE``) into the Parquet
store. Neither of those exposes a *level* to the UI, which is what the macro
panel needs: "10Y treasury = 4.21% as of yesterday, +3bp on the month".

This module closes that gap. It is a domain service - it reads the store, falls
back to the provider when the store has nothing recent, writes what it fetched
back so the next call is local, and derives the transforms the panel shows
(a year-over-year rate for CPI, levels for everything else). It never imports
from ``tradinglib.api``; the REST layer wraps it.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Final

from tradinglib.data.base import (
    ProviderError,
    ProviderNotConfigured,
    ProviderUnavailable,
    Series,
    utcnow,
)
from tradinglib.data.store import TimeSeriesStore, get_store

logger = logging.getLogger(__name__)

#: Store namespace the FRED ingest writes into (see ``ingest/scheduler.py``).
FRED_NAMESPACE: Final[str] = "fred"


@dataclass(frozen=True, slots=True)
class MacroSeriesSpec:
    """One macro series as the Overview panel presents it.

    Attributes:
        series_id: the FRED series identifier.
        label: the panel's column heading.
        transform: ``level`` serves the observation as published;
            ``yoy`` divides it by the observation twelve months earlier.
        unit: how the value should be read (``percent`` or ``ratio``).
        description: what the series measures, shown as the cell's tooltip.
        refresh_after: how old the stored copy may get before the service goes
            back to FRED for a fresh one.
        stale_after: how old the newest *published* observation may be before
            the reading is flagged stale. It is deliberately larger than
            ``refresh_after``: a monthly series is normally two months behind
            the calendar and that is not a fault.
    """

    series_id: str
    label: str
    transform: str = "level"
    unit: str = "percent"
    description: str = ""
    refresh_after: timedelta = timedelta(days=1)
    stale_after: timedelta = timedelta(days=5)


#: The four series the reference's *FRED macro* panel shows, in its order.
MACRO_PANEL: Final[tuple[MacroSeriesSpec, ...]] = (
    MacroSeriesSpec(
        series_id="DGS10",
        label="10Y TREASURY",
        unit="percent",
        description="Market yield on 10-year US Treasury securities, constant maturity",
        refresh_after=timedelta(days=1),
        stale_after=timedelta(days=6),
    ),
    MacroSeriesSpec(
        series_id="FEDFUNDS",
        label="FED FUNDS",
        unit="percent",
        description="Effective federal funds rate, monthly average",
        refresh_after=timedelta(days=7),
        stale_after=timedelta(days=70),
    ),
    MacroSeriesSpec(
        series_id="CPILFESL",
        label="CORE CPI YOY",
        transform="yoy",
        unit="percent",
        description="CPI for all urban consumers less food and energy, year-over-year",
        refresh_after=timedelta(days=7),
        stale_after=timedelta(days=70),
    ),
    MacroSeriesSpec(
        series_id="UNRATE",
        label="UNEMPLOYMENT",
        unit="percent",
        description="Civilian unemployment rate, seasonally adjusted",
        refresh_after=timedelta(days=7),
        stale_after=timedelta(days=70),
    ),
)

#: Lookup by series id, so an ad-hoc request for a known series keeps its label.
_BY_ID: Final[dict[str, MacroSeriesSpec]] = {spec.series_id: spec for spec in MACRO_PANEL}

#: How much history the service keeps for a panel cell (enough for a YoY plus a
#: sparkline) when the caller does not ask for a specific window.
DEFAULT_HISTORY: Final[timedelta] = timedelta(days=1460)


@dataclass(slots=True)
class MacroObservation:
    """A single reading of a macro series after its transform is applied."""

    date: str
    value: float

    def as_dict(self) -> dict[str, Any]:
        """Return the observation as a JSON-serialisable mapping."""
        return {"date": self.date, "value": self.value}


@dataclass(slots=True)
class MacroReading:
    """The current level of one macro series plus how it got there."""

    series_id: str
    label: str
    transform: str
    unit: str
    description: str
    value: float | None = None
    as_of: str | None = None
    previous: float | None = None
    previous_as_of: str | None = None
    change: float | None = None
    year_ago: float | None = None
    change_1y: float | None = None
    observations: int = 0
    source: str = "unknown"
    origin: str = "store"
    stale: bool = False
    reason: str | None = None
    history: list[MacroObservation] = field(default_factory=list)

    def as_dict(self, *, include_history: bool = False) -> dict[str, Any]:
        """Return a JSON-serialisable mapping for the REST layer."""
        payload: dict[str, Any] = {
            "series_id": self.series_id,
            "label": self.label,
            "transform": self.transform,
            "unit": self.unit,
            "description": self.description,
            "value": self.value,
            "as_of": self.as_of,
            "previous": self.previous,
            "previous_as_of": self.previous_as_of,
            "change": self.change,
            "year_ago": self.year_ago,
            "change_1y": self.change_1y,
            "observations": self.observations,
            "source": self.source,
            "origin": self.origin,
            "stale": self.stale,
            "reason": self.reason,
        }
        if include_history:
            payload["history"] = [point.as_dict() for point in self.history]
        return payload


class MacroService:
    """Serves macro-series levels from the store, refreshing them from FRED.

    Args:
        store: the Parquet/DuckDB time-series store.
        provider_factory: builds a FRED provider on demand. Injected so tests
            can substitute a stub and so nothing is imported until a refresh is
            actually needed.
    """

    def __init__(
        self,
        store: TimeSeriesStore | None = None,
        *,
        provider_factory: Any | None = None,
    ) -> None:
        self._store = store or get_store()
        self._provider_factory = provider_factory
        self._lock = asyncio.Lock()

    @property
    def store(self) -> TimeSeriesStore:
        """The time-series store this service reads and writes."""
        return self._store

    def _build_provider(self) -> Any:
        """Instantiate the FRED provider (keyless CSV unless a key is set)."""
        if self._provider_factory is not None:
            return self._provider_factory()
        from tradinglib.data.providers.fred_provider import FredProvider

        return FredProvider()

    def specs(self) -> list[MacroSeriesSpec]:
        """Return the panel's series specifications, in display order."""
        return list(MACRO_PANEL)

    @staticmethod
    def spec_for(series_id: str) -> MacroSeriesSpec:
        """Return the spec for ``series_id``, synthesising one when unknown."""
        key = series_id.strip().upper()
        known = _BY_ID.get(key)
        if known is not None:
            return known
        return MacroSeriesSpec(
            series_id=key,
            label=key,
            unit="ratio",
            description="ad-hoc FRED series requested by identifier",
            refresh_after=timedelta(days=1),
            stale_after=timedelta(days=70),
        )

    # --- reading ----------------------------------------------------------
    async def reading(
        self,
        spec: MacroSeriesSpec,
        *,
        start: date | None = None,
        refresh: bool = False,
    ) -> MacroReading:
        """Return the current reading for one series.

        Args:
            spec: which series, and how to transform it.
            start: earliest observation to keep; defaults to four years back.
            refresh: force a vendor fetch even when the store looks current.
        """
        window_start = start or (utcnow().date() - DEFAULT_HISTORY)
        series = await asyncio.to_thread(
            self._store.read_series, FRED_NAMESPACE, spec.series_id, start=window_start
        )
        origin = "store"
        reason: str | None = None
        if refresh or self._needs_refresh(series, spec):
            fetched, failure = await self._refresh(spec, window_start)
            if fetched is not None and fetched.points:
                series = fetched
                origin = "fred"
            elif failure is not None:
                reason = failure
        return self._build_reading(spec, series, origin=origin, reason=reason)

    async def panel(self, *, refresh: bool = False) -> list[MacroReading]:
        """Return every reading the Overview's *FRED macro* panel shows."""
        readings = await asyncio.gather(
            *(self.reading(spec, refresh=refresh) for spec in MACRO_PANEL),
            return_exceptions=True,
        )
        out: list[MacroReading] = []
        for spec, item in zip(MACRO_PANEL, readings, strict=True):
            if isinstance(item, MacroReading):
                out.append(item)
                continue
            logger.warning("macro reading for %s failed: %s", spec.series_id, item)
            out.append(
                MacroReading(
                    series_id=spec.series_id,
                    label=spec.label,
                    transform=spec.transform,
                    unit=spec.unit,
                    description=spec.description,
                    reason=f"{type(item).__name__}: {item}",
                    stale=True,
                )
            )
        return out

    # --- internals --------------------------------------------------------
    def _needs_refresh(self, series: Series, spec: MacroSeriesSpec) -> bool:
        """Return whether the stored copy is empty or older than the spec allows."""
        last = series.last_ts
        if last is None:
            return True
        return (utcnow() - last) > spec.refresh_after

    async def _refresh(
        self, spec: MacroSeriesSpec, window_start: date
    ) -> tuple[Series | None, str | None]:
        """Fetch a series from FRED and write it into the store.

        Returns:
            ``(series, None)`` on success, ``(None, reason)`` when the vendor
            could not be reached - the caller then serves whatever the store has
            and says why it may be stale.
        """
        async with self._lock:
            provider = self._build_provider()
            try:
                fetched = await provider.get_series(spec.series_id, start=window_start)
            except (ProviderNotConfigured, ProviderUnavailable, ProviderError) as exc:
                message = getattr(exc, "message", None) or str(exc)
                logger.info("FRED refresh for %s failed: %s", spec.series_id, message)
                return None, f"FRED refresh failed: {message}"
            except Exception as exc:  # noqa: BLE001 - vendor errors must not 500
                logger.warning("FRED refresh for %s raised", spec.series_id, exc_info=True)
                return None, f"FRED refresh failed: {type(exc).__name__}: {exc}"
            finally:
                close = getattr(provider, "close", None)
                if close is not None:
                    try:
                        await close()
                    except Exception:  # noqa: BLE001 - closing is best effort
                        logger.debug("closing the FRED provider failed", exc_info=True)
            if not fetched.points:
                return None, f"FRED returned no observations for {spec.series_id}"
            try:
                await asyncio.to_thread(
                    self._store.write_series, FRED_NAMESPACE, fetched.sorted()
                )
            except Exception:  # noqa: BLE001 - a read-only store must not break the read
                logger.warning("could not persist %s into the store", spec.series_id, exc_info=True)
            return fetched.sorted(), None

    def _build_reading(
        self,
        spec: MacroSeriesSpec,
        series: Series,
        *,
        origin: str,
        reason: str | None,
    ) -> MacroReading:
        """Apply the spec's transform and package the level plus its deltas."""
        points = sorted(series.sorted().points)
        reading = MacroReading(
            series_id=spec.series_id,
            label=spec.label,
            transform=spec.transform,
            unit=spec.unit,
            description=spec.description,
            source=series.source if points else "unknown",
            origin=origin,
            reason=reason,
        )
        if not points:
            reading.stale = True
            reading.reason = reason or (
                f"no {spec.series_id} observations are stored and FRED returned none"
            )
            return reading
        transformed = _transform(points, spec.transform)
        if not transformed:
            reading.stale = True
            reading.reason = reason or (
                f"{spec.series_id}: not enough history for a {spec.transform} transform"
            )
            return reading
        reading.history = [
            MacroObservation(date=ts.date().isoformat(), value=value) for ts, value in transformed
        ]
        reading.observations = len(transformed)
        last_ts, last_value = transformed[-1]
        reading.value = last_value
        reading.as_of = last_ts.date().isoformat()
        if len(transformed) > 1:
            prev_ts, prev_value = transformed[-2]
            reading.previous = prev_value
            reading.previous_as_of = prev_ts.date().isoformat()
            reading.change = last_value - prev_value
        year_ago = _value_near(transformed, last_ts - timedelta(days=365))
        if year_ago is not None:
            reading.year_ago = year_ago
            reading.change_1y = last_value - year_ago
        reading.stale = (utcnow() - last_ts) > spec.stale_after
        if reading.stale and reading.reason is None:
            age = (utcnow() - last_ts).days
            reading.reason = f"newest {spec.series_id} observation is {age} day(s) old"
        return reading


def _transform(
    points: list[tuple[datetime, float]], transform: str
) -> list[tuple[datetime, float]]:
    """Apply ``level`` (identity) or ``yoy`` (percent change on 12 months)."""
    if transform != "yoy":
        return list(points)
    out: list[tuple[datetime, float]] = []
    for ts, value in points:
        base = _value_near(points, ts - timedelta(days=365), tolerance=timedelta(days=20))
        if base in (None, 0.0):
            continue
        out.append((ts, (value / float(base) - 1.0) * 100.0))
    return out


def _value_near(
    points: list[tuple[datetime, float]],
    target: datetime,
    *,
    tolerance: timedelta = timedelta(days=45),
) -> float | None:
    """Return the observation closest to ``target`` within ``tolerance``."""
    best: float | None = None
    best_gap: timedelta | None = None
    for ts, value in points:
        gap = abs(ts - target)
        if gap > tolerance:
            continue
        if best_gap is None or gap < best_gap:
            best, best_gap = value, gap
    return best


_service: MacroService | None = None


def get_macro_service() -> MacroService:
    """Return the process-wide macro service, creating it on first use."""
    global _service
    if _service is None:
        _service = MacroService()
    return _service


def reset_macro_service(service: MacroService | None = None) -> MacroService:
    """Replace the process-wide macro service (used by the app factory/tests)."""
    global _service
    _service = service or MacroService()
    return _service
