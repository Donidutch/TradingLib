"""``GET /api/search`` - the command palette's backing search (``docs/SPEC.md`` §5).

Four kinds of result, all of them real:

* **workspaces** - the seven pages and their tabs, matched on their names. This
  is the one static table in the module: they are facts about the UI, not data.
* **collections** - universes and watchlists from the registries, matched on
  name, id and the symbols they contain.
* **screens** - saved screens from the screen registry.
* **symbols** - resolved through :class:`tradinglib.research.company.CompanyResearch`,
  which searches the vendor's symbol index and falls back to the SEC ticker
  file. Only symbols a provider actually returned appear; a lookup that fails
  is reported in ``warnings`` and the rest of the payload still answers.

Results carry the ``hash``/``symbol``/``page`` fields ``ui/js/palette.js``
navigates with, so a hit is clickable rather than decorative.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Any, Final

from fastapi import APIRouter, Query, Request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["search"])

#: How long the symbol lookup may take before the rest of the payload is
#: returned without it. The palette types ahead; a slow vendor must not hold
#: the whole result list.
SYMBOL_TIMEOUT_SECONDS: Final[float] = 4.0

#: (title, subtitle, hash, keywords) for every navigable place in the UI.
WORKSPACES: Final[tuple[tuple[str, str, str, tuple[str, ...]], ...]] = (
    ("Overview", "Workspace", "#overview", ("overview", "home", "dashboard", "tape")),
    ("Stock Research", "Workspace", "#stock", ("stock", "research", "company", "ticker")),
    ("Strategy Lab", "Workspace", "#tl/lab", ("strategy", "lab", "research")),
    ("Hypotheses", "Strategy Lab", "#tl/lab/hypotheses", ("hypothesis", "hypotheses")),
    ("Experiments", "Strategy Lab", "#tl/lab/experiments", ("experiment", "experiments")),
    ("Backtests", "Strategy Lab", "#tl/lab/backtests", ("backtest", "backtests", "equity")),
    ("Validation", "Strategy Lab", "#tl/lab/validation", ("validation", "gates", "promote")),
    ("Evaluation", "Strategy Lab", "#tl/lab/evaluation", ("evaluation", "tracking", "decay")),
    ("Agent Center", "Workspace", "#agents", ("agent", "agents", "pipeline", "runs")),
    ("Market & Data", "Workspace", "#data", ("market", "data")),
    (
        "Universes & Watchlists",
        "Market & Data",
        "#data/collections",
        ("universe", "universes", "watchlist", "watchlists", "collection", "collections"),
    ),
    ("Screener", "Market & Data", "#data/screener", ("screen", "screener", "rules", "funnel")),
    (
        "Source Catalog",
        "Market & Data",
        "#data/source-catalog",
        ("source", "catalog", "adapter", "subscription", "subscriptions"),
    ),
    (
        "Data Ingestion",
        "Market & Data",
        "#data/ingestion",
        ("ingest", "ingestion", "connector", "protocol", "backfill"),
    ),
    (
        "Data Quality",
        "Market & Data",
        "#data/quality",
        ("quality", "incident", "incidents", "checks", "stale", "outlier"),
    ),
    ("Documentation", "Workspace", "#docs", ("docs", "documentation", "guide", "methodology")),
    ("Settings", "Workspace", "#settings", ("settings", "preferences", "theme", "keys", "api key")),
)


def _matches(query: str, *fields: Any) -> bool:
    """Whether ``query`` appears in any of ``fields`` (case-insensitive)."""
    for field in fields:
        if field is None:
            continue
        if isinstance(field, (list, tuple, set)):
            if any(query in str(item).lower() for item in field):
                return True
        elif query in str(field).lower():
            return True
    return False


def _workspace_hits(query: str, limit: int) -> list[dict[str, Any]]:
    """Match the static workspace table."""
    out: list[dict[str, Any]] = []
    for title, subtitle, hash_, keywords in WORKSPACES:
        if not _matches(query, title, subtitle, keywords):
            continue
        out.append({"type": "page", "title": title, "subtitle": subtitle, "hash": hash_})
        if len(out) >= limit:
            break
    return out


def _collection_hits(request: Request, query: str, limit: int) -> list[dict[str, Any]]:
    """Match universes and watchlists, including by the symbols they hold."""
    out: list[dict[str, Any]] = []
    registry = getattr(request.app.state, "universe_registry", None)
    if registry is None:
        return out
    upper = query.upper()
    try:
        for universe in registry.list_universes(include_members=True):
            symbols = universe.symbols()
            hit_symbol = upper in symbols
            if not (_matches(query, universe.id, universe.name, universe.description) or hit_symbol):
                continue
            detail = f"Universe · {len(symbols)} symbols"
            if hit_symbol:
                detail += f" · contains {upper}"
            out.append(
                {
                    "type": "universe",
                    "title": universe.name,
                    "subtitle": detail,
                    "hash": "#data/collections",
                    "id": universe.id,
                }
            )
            if len(out) >= limit:
                return out
    except Exception:  # noqa: BLE001 - one broken registry must not fail search
        logger.warning("search: universes could not be read", exc_info=True)

    try:
        for watchlist in registry.watchlists.list_watchlists():
            hit_symbol = upper in watchlist.symbols
            if not (_matches(query, watchlist.id, watchlist.name) or hit_symbol):
                continue
            detail = f"Watchlist · {len(watchlist.symbols)} symbols"
            if hit_symbol:
                detail += f" · contains {upper}"
            out.append(
                {
                    "type": "watchlist",
                    "title": watchlist.name,
                    "subtitle": detail,
                    "hash": "#data/collections",
                    "id": watchlist.id,
                }
            )
            if len(out) >= limit:
                break
    except Exception:  # noqa: BLE001 - reported, never fabricated
        logger.warning("search: watchlists could not be read", exc_info=True)
    return out


def _screen_hits(request: Request, query: str, limit: int) -> list[dict[str, Any]]:
    """Match saved screens by name, description and tags."""
    registry = getattr(request.app.state, "screen_registry", None)
    if registry is None:
        return []
    out: list[dict[str, Any]] = []
    try:
        for screen in registry.list_screens():
            if not _matches(query, screen.id, screen.name, screen.description, screen.tags):
                continue
            out.append(
                {
                    "type": "screen",
                    "title": screen.name,
                    "subtitle": f"Screen · {len(screen.active_rules)} active rules",
                    "hash": "#data/screener",
                    "id": screen.id,
                }
            )
            if len(out) >= limit:
                break
    except Exception:  # noqa: BLE001 - reported, never fabricated
        logger.warning("search: screens could not be read", exc_info=True)
    return out


async def _symbol_hits(request: Request, raw: str, limit: int) -> tuple[list[dict[str, Any]], str | None]:
    """Resolve ``raw`` to real symbols. Returns the hits and any warning."""
    engine = getattr(request.app.state, "company_research", None)
    if engine is None:
        return [], "symbol lookup is unavailable: the research engine is not running"
    try:
        matches = await asyncio.wait_for(
            engine.resolve(raw, limit=limit), timeout=SYMBOL_TIMEOUT_SECONDS
        )
    except TimeoutError:
        return [], f"symbol lookup timed out after {SYMBOL_TIMEOUT_SECONDS:.0f}s"
    except Exception as exc:  # noqa: BLE001 - a vendor may raise anything
        logger.info("search: symbol lookup for %r failed: %s", raw, exc)
        return [], f"symbol lookup failed: {exc}"

    needle = raw.lower()
    out: list[dict[str, Any]] = []
    for match in matches:
        symbol = str(match.get("symbol") or "").upper()
        if not symbol:
            continue
        name = match.get("name") or match.get("title") or ""
        # The vendor index is fuzzy: searching "screener" comes back with
        # unrelated ISINs. Keep only the hits that literally contain the query,
        # so the palette never offers a symbol the user did not ask about.
        if needle not in symbol.lower() and needle not in str(name).lower():
            continue
        exchange = match.get("exchange") or match.get("source") or ""
        subtitle = " · ".join(part for part in (str(name), str(exchange)) if part) or "Symbol"
        out.append(
            {
                "type": "symbol",
                "title": symbol,
                "subtitle": subtitle,
                "symbol": symbol,
                "hash": f"#stock/{symbol}",
            }
        )
    return out, None


@router.get("/search", summary="Palette search across pages, collections and symbols")
async def search(
    request: Request,
    q: Annotated[str, Query(min_length=1, description="free text")],
    limit: Annotated[int, Query(ge=1, le=50)] = 8,
) -> dict[str, Any]:
    """Search the console. Every hit names something that really exists."""
    raw = q.strip()
    query = raw.lower()
    warnings: list[str] = []

    pages = await asyncio.to_thread(_workspace_hits, query, limit)
    collections = await asyncio.to_thread(_collection_hits, request, query, limit)
    screens = await asyncio.to_thread(_screen_hits, request, query, limit)
    symbols, warning = await _symbol_hits(request, raw, limit)
    if warning:
        warnings.append(warning)

    # Local entities first (they resolve instantly and are unambiguous); the
    # vendor symbol hits follow, because the palette already offers a direct
    # "Research <TICKER>" jump above everything the server returns.
    results = [*collections, *screens, *pages, *symbols][: limit * 3]
    return {
        "query": raw,
        "count": len(results),
        "results": results,
        "groups": {
            "symbols": symbols,
            "collections": collections,
            "screens": screens,
            "pages": pages,
        },
        "warnings": warnings,
    }
