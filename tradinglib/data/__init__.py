"""Market, fundamental and alternative data access.

Layering (``docs/SPEC.md`` section 4): vendors are reached only through
``tradinglib.data.providers``; results pass through :mod:`tradinglib.data.cache` and are
persisted by :mod:`tradinglib.data.store`. ``tradinglib.research`` and ``tradinglib.agents``
consume this package and never call vendor SDKs directly.
"""

from __future__ import annotations

__all__: list[str] = []
