"""Stock Research endpoints (``docs/SPEC.md`` section 5).

Every payload is composed by :class:`tradinglib.research.company.CompanyResearch`,
which reaches vendors only through ``tradinglib.data`` (providers -> cache ->
store). The routes add nothing to the numbers: they choose a subresource, pass
the query parameters through and translate the domain's exceptions into HTTP
status codes.

Two behaviours are worth naming because the REFERENCE UI got them wrong:

* **any ticker loads.** ``/api/research/company/{symbol}`` accepts any symbol
  the vendor knows; ``/api/research/resolve`` is the symbol search behind the
  form, which falls back to the SEC ticker index when Yahoo is unavailable.
* **a dead component degrades, it does not 404.** A vendor outage surfaces as
  ``warnings`` on an otherwise complete payload (or a 503 naming the provider),
  while a genuinely unknown ticker is the only thing that answers 404.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from tradinglib.api.deps import get_memory, get_research, translate_errors
from tradinglib.research.company import RANGES, CompanyResearch
from tradinglib.research.memory import ResearchMemoryComposer

router = APIRouter(prefix="/api/research", tags=["research"])

Research = Annotated[CompanyResearch, Depends(get_research)]
Memory = Annotated[ResearchMemoryComposer, Depends(get_memory)]

#: Default number of EDGAR filings requested. Kept equal to the engine's own
#: default so the freshness report (which is keyed on the request) matches the
#: fetch this route performs.
DEFAULT_FILING_LIMIT = 40


@router.get("/ranges", summary="Chart ranges the price endpoint accepts")
async def list_ranges() -> dict[str, Any]:
    """Return the valid ``range`` keys and the bar interval each one uses."""
    return {
        "ranges": [
            {"key": key, "interval": str(interval), "span_days": span.days if span else None}
            for key, (interval, span) in RANGES.items()
        ]
    }


@router.get("/resolve", summary="Resolve free text to symbols")
async def resolve_symbol(
    engine: Research,
    q: Annotated[str, Query(min_length=1, description="ticker or company name")],
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> dict[str, Any]:
    """Search the vendor's symbol index (EDGAR fallback) for ``q``."""
    with translate_errors("symbol"):
        matches = await engine.resolve(q, limit=limit)
    return {"query": q, "count": len(matches), "results": matches}


@router.get("/company/{symbol}", summary="Company overview payload")
async def get_company(engine: Research, symbol: str) -> dict[str, Any]:
    """Return the Overview tab: profile, quote, derived metrics, freshness."""
    with translate_errors("symbol"):
        return await engine.company(symbol)


@router.get("/company/{symbol}/price", summary="Price history for the chart")
async def get_price(
    engine: Research,
    symbol: str,
    range: Annotated[str, Query(description="chart range key, e.g. 1d/1m/1y/max")] = "1y",
) -> dict[str, Any]:
    """Return OHLCV bars for one chart range, served from the store when it can."""
    with translate_errors("symbol"):
        return await engine.price(symbol, range=range)


@router.get("/company/{symbol}/financials", summary="Financial statements")
async def get_financials(engine: Research, symbol: str) -> dict[str, Any]:
    """Return raw statements plus the normalised annual/quarterly summary."""
    with translate_errors("symbol"):
        return await engine.financials(symbol)


@router.get("/company/{symbol}/estimates", summary="Earnings and estimates")
async def get_estimates(engine: Research, symbol: str) -> dict[str, Any]:
    """Return earnings history with real price reactions, plus consensus."""
    with translate_errors("symbol"):
        return await engine.estimates(symbol)


@router.get("/company/{symbol}/valuation", summary="Valuation and peers")
async def get_valuation(
    engine: Research,
    symbol: str,
    peers: Annotated[
        list[str] | None,
        Query(description="explicit peer tickers; omitted means pick them from the universes"),
    ] = None,
) -> dict[str, Any]:
    """Return valuation multiples and the peer comparison table."""
    with translate_errors("symbol"):
        return await engine.valuation(symbol, peers=peers or None)


@router.get("/company/{symbol}/news", summary="News with sentiment")
async def get_news(
    engine: Research,
    symbol: str,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> dict[str, Any]:
    """Return recent news items, each scored, plus the aggregate sentiment."""
    with translate_errors("symbol"):
        return await engine.news(symbol, limit=limit)


@router.get("/company/{symbol}/thesis", summary="Measured catalysts and key risks")
async def get_thesis(engine: Research, symbol: str) -> dict[str, Any]:
    """Return the Research thesis card's CATALYSTS and KEY RISKS.

    Every entry is derived from a measurement on this company (consensus
    revisions, trailing margins, balance sheet, news sentiment, coverage
    warnings) and carries that measurement as its ``basis``. Nothing is
    generated prose.
    """
    with translate_errors("symbol"):
        return await engine.thesis(symbol)


@router.get("/company/{symbol}/altdata", summary="Alternative-data signals")
async def get_altdata(
    engine: Research,
    symbol: str,
    timeframe: Annotated[str, Query(description="Google Trends timeframe")] = "today 12-m",
) -> dict[str, Any]:
    """Return alternative signals; ones TradingLib has no source for are null with a reason."""
    with translate_errors("symbol"):
        return await engine.altdata(symbol, timeframe=timeframe)


@router.get("/company/{symbol}/filings", summary="SEC filings")
async def get_filings(
    engine: Research,
    symbol: str,
    limit: Annotated[int, Query(ge=1, le=200)] = DEFAULT_FILING_LIMIT,
) -> dict[str, Any]:
    """Return recent EDGAR filings and their form counts."""
    with translate_errors("symbol"):
        return await engine.filings(symbol, limit=limit)


@router.get("/company/{symbol}/memory", summary="Recorded research memory")
async def get_research_memory(memory: Memory, symbol: str) -> dict[str, Any]:
    """Return the *Research memory* narrative for a symbol.

    The current-thesis and open-question rows are composed by
    :class:`tradinglib.research.memory.ResearchMemoryComposer` from the hypotheses,
    agent findings and knowledge entries that actually name the symbol - never
    written by a model, and empty when nothing has been recorded.
    """
    with translate_errors("symbol"):
        composed = await asyncio.to_thread(memory.compose, symbol)
    return composed.as_dict()


@router.get("/company/{symbol}/freshness", summary="Data freshness report")
async def get_freshness(engine: Research, symbol: str) -> dict[str, Any]:
    """Return per-component data ages, measured from real cache fetch times."""
    with translate_errors("symbol"):
        return await engine.freshness(symbol)
