"""Ingestion gateway health: latency, connected sources, delayed series.

Everything reported here is measured. ``latency_ms`` is a real round-trip to a
real source; ``sources_connected`` counts sources whose last probe actually
succeeded; ``delayed_series`` counts series whose feed is overdue against its
own schedule. A figure that has not been measured yet is ``None``, never zero.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from tradinglib.data.ingest.catalog import SourceCatalog
from tradinglib.data.ingest.source import ScheduleKind, SourceStatus
from tradinglib.events import EventBus

logger = logging.getLogger(__name__)

#: Event channels the gateway publishes on.
INGEST_CHANNEL: str = "ingest"
STATUS_CHANNEL: str = "status"

#: How long a measured latency is considered current, in seconds.
LATENCY_TTL: float = 60.0

#: Default staleness tolerance for feeds without a periodic schedule.
DEFAULT_MAX_AGE: float = 900.0

#: Sources probed for latency, in preference order (the primary market feed
#: first: it is the one the datastrip's "research gateway" figure is about).
LATENCY_PROBE_ORDER: tuple[str, ...] = ("yfinance", "alpaca", "fred", "edgar", "trends")


def _utcnow() -> datetime:
    """Return the current time as an aware UTC datetime."""
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class GatewayHealth:
    """Observed gateway health. Unknown values stay ``None``, never zero."""

    latency_ms: float | None
    sources_connected: int
    sources_total: int
    delayed_series: int
    last_ingest_at: datetime | None
    healthy: bool
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON payload used by ``/api/ingest/gateway`` and the datastrip."""
        return {
            "latency_ms": self.latency_ms,
            "sources_connected": self.sources_connected,
            "sources_total": self.sources_total,
            "delayed_series": self.delayed_series,
            "last_ingest": self.last_ingest_at.isoformat() if self.last_ingest_at else None,
            "healthy": self.healthy,
            "detail": self.detail,
        }


class IngestGateway:
    """Aggregates per-source observations into one honest health report."""

    def __init__(
        self,
        catalog: SourceCatalog,
        bus: EventBus | None = None,
        *,
        max_age_seconds: float = DEFAULT_MAX_AGE,
        latency_ttl: float = LATENCY_TTL,
    ) -> None:
        """Create the gateway over a source catalog.

        Args:
            catalog: the catalog whose sources are aggregated.
            bus: event bus for ``ingest``/``status`` publications.
            max_age_seconds: staleness floor used by :meth:`delayed_series`.
            latency_ttl: how long a measured latency stays current.
        """
        self.catalog = catalog
        self.bus = bus
        self.max_age_seconds = max_age_seconds
        self.latency_ttl = latency_ttl
        self._latency_ms: float | None = None
        self._latency_at: float | None = None
        self._latency_source: str | None = None

    # --- latency ----------------------------------------------------------
    @property
    def latency_fresh(self) -> bool:
        """Whether the cached latency measurement is still current."""
        return (
            self._latency_at is not None
            and (time.monotonic() - self._latency_at) < self.latency_ttl
        )

    def observed_latency(self) -> float | None:
        """Return the best latency already observed, without issuing a request.

        Prefers this gateway's own recent measurement, then the latency the
        catalog recorded the last time each source was probed (median of the
        connected ones, so a single slow vendor does not define the figure).
        """
        if self.latency_fresh:
            return self._latency_ms
        states = self.catalog.states()
        latencies = sorted(
            state.latency_ms
            for state in states.values()
            if state.latency_ms is not None and state.status is SourceStatus.CONNECTED
        )
        if not latencies:
            return None
        middle = len(latencies) // 2
        if len(latencies) % 2:
            return latencies[middle]
        return (latencies[middle - 1] + latencies[middle]) / 2.0

    async def measure_latency(self) -> float | None:
        """Measure round-trip latency to the primary market-data source.

        Probes the first connected built-in in :data:`LATENCY_PROBE_ORDER`
        (falling back to any enabled source) with a real
        ``Connector.test()``. Returns ``None`` when nothing could be probed -
        the caller reports "unknown", not "0 ms".
        """
        sources = {source.id: source for source in self.catalog.list_sources() if source.enabled}
        ordered = [sources[sid] for sid in LATENCY_PROBE_ORDER if sid in sources]
        ordered.extend(s for s in sources.values() if s.id not in LATENCY_PROBE_ORDER)
        for source in ordered:
            started = time.perf_counter()
            result = await self.catalog.test(source)
            self.catalog.record_test(source.id, result)
            if not result.ok:
                logger.debug(
                    "gateway: latency probe of %s failed: %s", source.id, result.error
                )
                continue
            latency = result.latency_ms
            if latency is None:
                latency = (time.perf_counter() - started) * 1000.0
            self._latency_ms = latency
            self._latency_at = time.monotonic()
            self._latency_source = source.id
            await self._publish(
                INGEST_CHANNEL,
                {
                    "event": "gateway_latency",
                    "source_id": source.id,
                    "latency_ms": latency,
                    "measured_at": _utcnow().isoformat(),
                },
            )
            return latency
        self._latency_ms = None
        self._latency_at = time.monotonic()
        self._latency_source = None
        return None

    # --- delayed series ---------------------------------------------------
    def delayed_series(self, *, max_age_seconds: float | None = None) -> list[dict[str, Any]]:
        """List series whose newest observation is older than ``max_age_seconds``.

        A *series* here is one item in a source's scope (a FRED series id, a
        symbol, a keyword). Its freshness is the freshness of the feed that
        writes it, compared against that feed's own cadence: a daily source is
        not late after fifteen minutes. ``max_age_seconds`` is the floor
        applied to every source.
        """
        floor = self.max_age_seconds if max_age_seconds is None else max_age_seconds
        entries: list[dict[str, Any]] = []
        for stale in self.catalog.stale_sources(min_tolerance_seconds=floor):
            scope = stale["scope"] or [stale["source_id"]]
            for item in scope:
                entries.append(
                    {
                        "series": item,
                        "source_id": stale["source_id"],
                        "source": stale["name"],
                        "domain": stale["domain"],
                        "schedule": stale["schedule"],
                        "last_ingest": stale["last_ingest"],
                        "age_seconds": stale["age_seconds"],
                        "tolerance_seconds": stale["tolerance_seconds"],
                        "never_ingested": stale["never_ingested"],
                        "last_error": stale["last_error"],
                    }
                )
        return entries

    # --- health -----------------------------------------------------------
    async def health(self, *, measure: bool = False, publish: bool = True) -> GatewayHealth:
        """Compute current gateway health from observed source states.

        Args:
            measure: issue a real latency probe when no fresh measurement
                exists. ``False`` (the default) keeps ``/api/status`` cheap by
                reusing what the last probes already measured.
            publish: publish the result on the ``status`` channel.
        """
        connected, total = self.catalog.connected_count()
        delayed = self.delayed_series()
        states = self.catalog.states()
        stamps = [s.last_ingest_at for s in states.values() if s.last_ingest_at is not None]
        last_ingest = max(stamps) if stamps else None

        latency = self.observed_latency()
        if latency is None and measure:
            latency = await self.measure_latency()

        streaming_only = all(
            source.parsed_schedule.kind is ScheduleKind.STREAMING
            for source in self.catalog.list_sources()
            if source.enabled
        )
        healthy = connected > 0 and not delayed
        if total == 0:
            detail = "no sources are configured"
        elif connected == 0:
            detail = "no source has answered a probe yet"
        elif delayed:
            names = sorted({entry["source"] for entry in delayed})
            detail = f"{len(delayed)} delayed series on {', '.join(names)}"
        elif streaming_only:
            detail = f"{connected}/{total} sources connected (streaming only)"
        else:
            detail = f"{connected}/{total} sources connected"

        health = GatewayHealth(
            latency_ms=latency,
            sources_connected=connected,
            sources_total=total,
            delayed_series=len(delayed),
            last_ingest_at=last_ingest,
            healthy=healthy,
            detail=detail,
        )
        if publish:
            await self._publish(
                STATUS_CHANNEL, {"event": "gateway_health", **health.as_dict()}
            )
        return health

    async def report(self, *, measure: bool = False) -> dict[str, Any]:
        """Return the full ``GET /api/ingest/gateway`` payload.

        Includes the per-source table the Data Ingestion page renders, so the
        UI needs one request rather than one per source.
        """
        health = await self.health(measure=measure, publish=False)
        states = self.catalog.states()
        sources: list[dict[str, Any]] = []
        for source in self.catalog.list_sources():
            state = states.get(source.id)
            sources.append(
                {
                    "id": source.id,
                    "name": source.name,
                    "domain": str(source.data_domain),
                    "domain_label": source.data_domain.label,
                    "protocol": str(source.protocol),
                    "protocol_label": source.protocol.label,
                    "schedule": source.parsed_schedule.label,
                    "backfill": source.backfill.to_dict(),
                    "scope": list(source.scope),
                    "builtin": source.builtin,
                    "enabled": source.enabled,
                    "state": state.as_dict() if state else None,
                }
            )
        payload = health.as_dict()
        payload.update(
            {
                "latency_source": self._latency_source,
                "latency_measured": self.latency_fresh,
                "delayed": self.delayed_series(),
                "sources": sources,
                "summary": self.catalog.summary(),
                "generated_at": _utcnow().isoformat(),
            }
        )
        return payload

    async def _publish(self, channel: str, payload: dict[str, Any]) -> None:
        """Publish one gateway event, when a bus is attached."""
        if self.bus is not None:
            await self.bus.publish(channel, payload)


__all__ = [
    "DEFAULT_MAX_AGE",
    "LATENCY_PROBE_ORDER",
    "LATENCY_TTL",
    "GatewayHealth",
    "IngestGateway",
]
