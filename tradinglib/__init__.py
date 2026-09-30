"""TradingLib - trading research library and service.

The package is deliberately import-light: importing :mod:`tradinglib` pulls in the
cross-cutting core only (configuration, database handles, event bus). Heavy
subsystems (data providers, research engines, agents, the API) are imported
from their own modules so the library stays usable headless.
"""

from __future__ import annotations

__version__ = "0.1.0"

from tradinglib.config import Settings, get_settings, reload_settings
from tradinglib.events import Event, EventBus, Subscription, get_event_bus

__all__ = [
    "__version__",
    "Settings",
    "get_settings",
    "reload_settings",
    "Event",
    "EventBus",
    "Subscription",
    "get_event_bus",
]
