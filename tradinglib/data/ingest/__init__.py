"""Data ingestion: source definitions, connectors, scheduling and gateway health.

Every field of the Data Ingestion configure form maps 1:1 onto a field of
:class:`tradinglib.data.ingest.source.SourceConfig` (``docs/SPEC.md`` section 4).

Layering::

    SourceConfig  ->  build_connector()  ->  Connector.test()/fetch()/backfill()
          |                                        |
    SourceCatalog (SQLite persistence + observed statuses)
          |                                        |
    IngestScheduler (asyncio, per-schedule)  ->  tradinglib.data.store (Parquet/DuckDB)
          |
    IngestGateway (latency, connected count, delayed series)  ->  EventBus

Names are re-exported lazily (PEP 562) so importing this package does not pull
in SQLAlchemy, websockets or the vendor SDKs before they are needed.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:  # pragma: no cover - import-time typing only
    from tradinglib.data.ingest.catalog import SourceCatalog, SourceState, Subscription
    from tradinglib.data.ingest.connectors.base import (
        ConnectionTestResult,
        Connector,
        FetchResult,
        build_connector,
    )
    from tradinglib.data.ingest.gateway import GatewayHealth, IngestGateway
    from tradinglib.data.ingest.scheduler import IngestOutcome, IngestScheduler, ScheduledJob
    from tradinglib.data.ingest.source import (
        AdvancedConfig,
        AuthConfig,
        AuthType,
        BackfillConfig,
        DataDomain,
        PayloadFormat,
        Protocol,
        RetryPolicy,
        Schedule,
        SourceConfig,
        SourceStatus,
        parse_schedule,
    )

#: Exported name -> defining module.
_EXPORTS: Final[dict[str, str]] = {
    "AdvancedConfig": "tradinglib.data.ingest.source",
    "AuthConfig": "tradinglib.data.ingest.source",
    "AuthType": "tradinglib.data.ingest.source",
    "BackfillConfig": "tradinglib.data.ingest.source",
    "DataDomain": "tradinglib.data.ingest.source",
    "PayloadFormat": "tradinglib.data.ingest.source",
    "Protocol": "tradinglib.data.ingest.source",
    "RetryPolicy": "tradinglib.data.ingest.source",
    "Schedule": "tradinglib.data.ingest.source",
    "SourceConfig": "tradinglib.data.ingest.source",
    "SourceStatus": "tradinglib.data.ingest.source",
    "parse_schedule": "tradinglib.data.ingest.source",
    "ConnectionTestResult": "tradinglib.data.ingest.connectors.base",
    "Connector": "tradinglib.data.ingest.connectors.base",
    "FetchResult": "tradinglib.data.ingest.connectors.base",
    "build_connector": "tradinglib.data.ingest.connectors.base",
    "SourceCatalog": "tradinglib.data.ingest.catalog",
    "SourceState": "tradinglib.data.ingest.catalog",
    "Subscription": "tradinglib.data.ingest.catalog",
    "builtin_sources": "tradinglib.data.ingest.catalog",
    "IngestScheduler": "tradinglib.data.ingest.scheduler",
    "IngestOutcome": "tradinglib.data.ingest.scheduler",
    "ScheduledJob": "tradinglib.data.ingest.scheduler",
    "GatewayHealth": "tradinglib.data.ingest.gateway",
    "IngestGateway": "tradinglib.data.ingest.gateway",
}

__all__: list[str] = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    """Import an exported name on first attribute access."""
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(module_name), name)


def __dir__() -> list[str]:
    """Include the lazily exported names in ``dir()``."""
    return sorted(__all__)
