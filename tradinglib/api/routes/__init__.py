"""REST routers, one module per domain (``docs/SPEC.md`` section 5).

Convention for the modules here: each exposes a module-level
``router: fastapi.APIRouter`` with its own ``prefix`` and ``tags``, and
``tradinglib.api.app.create_app`` includes it. Routers depend on domain services
only - never the other way round - and reach them through
:mod:`tradinglib.api.deps`, which answers ``503`` when a subsystem is not running
rather than fabricating a reply.

Implemented: :mod:`~tradinglib.api.routes.status`,
:mod:`~tradinglib.api.routes.universes`, :mod:`~tradinglib.api.routes.watchlists`,
:mod:`~tradinglib.api.routes.ingest`, :mod:`~tradinglib.api.routes.quality`,
:mod:`~tradinglib.api.routes.research`, :mod:`~tradinglib.api.routes.screens`,
:mod:`~tradinglib.api.routes.lab`, :mod:`~tradinglib.api.routes.agents`.
"""

from __future__ import annotations

__all__: list[str] = []
