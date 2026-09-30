"""Resolve an ingestion source's scope *label* to concrete symbols.

The Data Ingestion form's Scope dropdown offers universe names ("US Quality
50"), user collections and two special entries. A saved source therefore
carries either an explicit ``scope`` list (which the scheduler uses directly)
or only a ``scope_label``. This module builds the ``scope_resolver`` hook
:class:`~tradinglib.data.ingest.scheduler.IngestScheduler` accepts, so a source
scoped to a universe ingests that universe's real membership.

Two labels are deliberately *not* resolved:

* "Custom symbols…" / "Custom keywords" - the user is expected to type the
  items; an empty list is the honest answer until they do;
* "All available symbols" - no vendor here can enumerate its full symbol
  universe, so guessing one would be inventing scope. The source reports "no
  scope configured" instead.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable

from tradinglib.data.ingest.source import SourceConfig
from tradinglib.data.universe import UniverseRegistry

logger = logging.getLogger(__name__)

#: Labels that intentionally resolve to nothing (see the module docstring).
UNRESOLVABLE_LABELS: frozenset[str] = frozenset(
    {
        "custom symbols",
        "custom symbols…",
        "custom symbols...",
        "custom keywords",
        "all available symbols",
        "benchmark",
        "macro series",
        "manual",
        "none",
    }
)


def _norm(value: str) -> str:
    """Lower-case, collapse whitespace and drop the form's trailing ellipsis."""
    text = re.sub(r"\s+", " ", value.strip().lower())
    return text.rstrip("… .")


def _slug(value: str) -> str:
    """Return the registry-style slug of a display label."""
    return re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")


def resolve_scope_label(registry: UniverseRegistry, label: str | None) -> list[str]:
    """Resolve one scope label to symbols, or return ``[]`` when it names nothing.

    Resolution order: the label as an id, its slug, then a case-insensitive
    match against universe and watchlist *names*. Nothing is guessed: an
    unknown label logs a warning and yields an empty list, which the scheduler
    reports as "no scope configured".
    """
    if not label:
        return []
    normalised = _norm(label)
    if normalised in UNRESOLVABLE_LABELS:
        return []

    for candidate in (label.strip(), _slug(label)):
        if not candidate:
            continue
        try:
            symbols = registry.resolve(candidate)
        except KeyError:
            continue
        if symbols:
            return symbols

    try:
        for universe in registry.list_universes(include_members=False):
            if _norm(universe.name) == normalised:
                return registry.symbols(universe.id)
        for watchlist in registry.watchlists.list_watchlists():
            if _norm(watchlist.name) == normalised:
                return list(watchlist.symbols)
    except Exception:  # noqa: BLE001 - a storage fault must not stop ingestion
        logger.exception("scope: could not search the registries for %r", label)
        return []

    logger.warning(
        "scope label %r matches no universe or watchlist; the source will report "
        "'no scope configured' rather than ingesting a guessed symbol list",
        label,
    )
    return []


def build_scope_resolver(registry: UniverseRegistry) -> Callable[[SourceConfig], list[str]]:
    """Return the ``scope_resolver`` callable the ingest scheduler expects."""

    def resolver(config: SourceConfig) -> list[str]:
        """Resolve one source's scope label through the universe registry."""
        return resolve_scope_label(registry, config.scope_label)

    return resolver


__all__ = ["UNRESOLVABLE_LABELS", "build_scope_resolver", "resolve_scope_label"]
