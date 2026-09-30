"""Vendor adapters. Each module exposes one :class:`~tradinglib.data.base.DataProvider`.

Names are re-exported lazily (PEP 562): importing this package must stay cheap,
because ``yfinance``, ``alpaca-py`` and ``pytrends`` each pull in a large
dependency tree that a caller wanting only one of them should not pay for.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any, Final

from tradinglib.data.base import DataProvider

if TYPE_CHECKING:  # pragma: no cover - import-time typing only
    from tradinglib.data.providers.alpaca_provider import AlpacaProvider, AlpacaStream
    from tradinglib.data.providers.edgar_provider import EdgarProvider
    from tradinglib.data.providers.fred_provider import FredProvider
    from tradinglib.data.providers.jobs_provider import JobPostingsProvider
    from tradinglib.data.providers.trends_provider import TrendsProvider
    from tradinglib.data.providers.yfinance_provider import YFinanceProvider

#: Exported symbol -> defining module.
_EXPORTS: Final[dict[str, str]] = {
    "YFinanceProvider": "tradinglib.data.providers.yfinance_provider",
    "AlpacaProvider": "tradinglib.data.providers.alpaca_provider",
    "AlpacaStream": "tradinglib.data.providers.alpaca_provider",
    "EdgarProvider": "tradinglib.data.providers.edgar_provider",
    "FredProvider": "tradinglib.data.providers.fred_provider",
    "JobPostingsProvider": "tradinglib.data.providers.jobs_provider",
    "TrendsProvider": "tradinglib.data.providers.trends_provider",
}

#: Provider id (as used by the source catalog and the UI) -> class name.
PROVIDER_NAMES: Final[dict[str, str]] = {
    "yfinance": "YFinanceProvider",
    "alpaca": "AlpacaProvider",
    "edgar": "EdgarProvider",
    "fred": "FredProvider",
    "jobs": "JobPostingsProvider",
    "trends": "TrendsProvider",
}

__all__ = [*_EXPORTS, "PROVIDER_NAMES", "get_provider_class", "build_provider"]


def __getattr__(name: str) -> Any:
    """Import an exported provider class on first attribute access."""
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(module_name), name)


def __dir__() -> list[str]:
    """Include the lazily exported names in ``dir()``."""
    return sorted(__all__)


def get_provider_class(provider_id: str) -> type[DataProvider]:
    """Return the provider class registered under ``provider_id``.

    Raises:
        KeyError: when the id is unknown; callers must not silently substitute
            a different vendor.
    """
    key = provider_id.strip().lower()
    if key not in PROVIDER_NAMES:
        raise KeyError(f"unknown data provider {provider_id!r}")
    return __getattr__(PROVIDER_NAMES[key])


def build_provider(provider_id: str, **kwargs: Any) -> DataProvider:
    """Instantiate the provider registered under ``provider_id``."""
    return get_provider_class(provider_id)(**kwargs)
