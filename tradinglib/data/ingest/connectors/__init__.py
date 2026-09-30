"""Connector implementations, one per :class:`~tradinglib.data.ingest.source.Protocol`.

Names are re-exported lazily (PEP 562): importing this package must stay cheap,
because the database connector pulls in SQLAlchemy and the library connector
pulls in the vendor SDKs, and a caller wanting only the REST connector should
not pay for either.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any, Final

from tradinglib.data.ingest.connectors.base import (
    ConnectionTestResult,
    Connector,
    FetchResult,
    TestResult,
    build_connector,
    get_connector_class,
)

if TYPE_CHECKING:  # pragma: no cover - import-time typing only
    from tradinglib.data.ingest.connectors.database import DatabaseConnector
    from tradinglib.data.ingest.connectors.file import FileConnector
    from tradinglib.data.ingest.connectors.library import LibraryConnector
    from tradinglib.data.ingest.connectors.polling import PollingConnector
    from tradinglib.data.ingest.connectors.rest import RestConnector
    from tradinglib.data.ingest.connectors.websocket import WebSocketConnector

#: Exported connector class -> defining module.
_EXPORTS: Final[dict[str, str]] = {
    "WebSocketConnector": "tradinglib.data.ingest.connectors.websocket",
    "RestConnector": "tradinglib.data.ingest.connectors.rest",
    "PollingConnector": "tradinglib.data.ingest.connectors.polling",
    "DatabaseConnector": "tradinglib.data.ingest.connectors.database",
    "FileConnector": "tradinglib.data.ingest.connectors.file",
    "LibraryConnector": "tradinglib.data.ingest.connectors.library",
}

__all__ = [
    *_EXPORTS,
    "ConnectionTestResult",
    "Connector",
    "FetchResult",
    "TestResult",
    "build_connector",
    "get_connector_class",
]


def __getattr__(name: str) -> Any:
    """Import an exported connector class on first attribute access."""
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(module_name), name)


def __dir__() -> list[str]:
    """Include the lazily exported names in ``dir()``."""
    return sorted(__all__)
