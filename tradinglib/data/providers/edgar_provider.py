"""SEC EDGAR provider (direct REST, no SDK).

EDGAR requires a descriptive ``User-Agent`` with contact details and rate-limits
to roughly ten requests per second; both are this provider's responsibility.

Endpoints used (all keyless, all documented public JSON APIs):

* ``https://www.sec.gov/files/company_tickers.json`` - ticker to CIK map
* ``https://data.sec.gov/submissions/CIK##########.json`` - filing history
* ``https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json`` - all facts
* ``https://data.sec.gov/api/xbrl/companyconcept/CIK.../<taxonomy>/<tag>.json``
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime
from typing import Any, Final

import httpx

from tradinglib.data.base import (
    DataProvider,
    Filing,
    ProviderRateLimited,
    ProviderStatus,
    ProviderUnavailable,
    Series,
    SymbolNotFound,
    to_utc,
    utcnow,
)

logger = logging.getLogger(__name__)

#: Contact identity SEC requires. Overridable through settings/env, but never
#: blank - EDGAR blocks anonymous clients.
SPEC_USER_AGENT: Final[str] = "TradingLib research mantelrs@gmail.com"

TICKERS_URL: Final[str] = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL: Final[str] = "https://data.sec.gov/submissions/CIK{cik}.json"
COMPANY_FACTS_URL: Final[str] = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
COMPANY_CONCEPT_URL: Final[str] = (
    "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/{taxonomy}/{tag}.json"
)
ARCHIVES_URL: Final[str] = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession}"

#: How long the ticker->CIK map is reused before it is re-fetched (seconds).
TICKER_MAP_TTL: Final[float] = 24 * 3600.0


class _RateLimiter:
    """Simple async rate limiter: at most ``rate`` acquisitions per second."""

    def __init__(self, rate: float) -> None:
        self._min_interval = 1.0 / rate if rate > 0 else 0.0
        self._lock = asyncio.Lock()
        self._next_at = 0.0

    async def acquire(self) -> None:
        """Block until the caller may issue its request."""
        if self._min_interval <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            wait = self._next_at - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._next_at = now + self._min_interval


def normalise_cik(cik: str | int) -> str:
    """Return a CIK as the zero-padded ten-digit string EDGAR expects."""
    digits = str(cik).strip().upper().removeprefix("CIK").lstrip("0") or "0"
    if not digits.isdigit():
        raise ValueError(f"not a CIK: {cik!r}")
    return digits.zfill(10)


def _parse_date(value: Any) -> date | None:
    """Parse an EDGAR ``YYYY-MM-DD`` string, returning ``None`` when absent."""
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


class EdgarProvider(DataProvider):
    """Keyless SEC EDGAR adapter for filings, company facts and concepts."""

    name = "edgar"

    def __init__(self, *, user_agent: str | None = None, rate_limit_per_sec: float = 8.0) -> None:
        """Create the provider.

        Args:
            user_agent: value sent as ``User-Agent``; defaults to the setting,
                falling back to :data:`SPEC_USER_AGENT` when the setting still
                holds the library's generic placeholder.
            rate_limit_per_sec: client-side request ceiling honouring SEC policy.
        """
        self.user_agent = user_agent or self._default_user_agent()
        self.rate_limit_per_sec = rate_limit_per_sec
        self._limiter = _RateLimiter(rate_limit_per_sec)
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()
        self._ticker_map: dict[str, dict[str, Any]] | None = None
        self._ticker_map_at = 0.0
        self._last_success: datetime | None = None

    @staticmethod
    def _default_user_agent() -> str:
        """Resolve the User-Agent from settings, preferring a real contact."""
        from tradinglib.config import DEFAULT_EDGAR_USER_AGENT, get_settings

        configured = get_settings().edgar_user_agent
        if not configured or configured == DEFAULT_EDGAR_USER_AGENT:
            return SPEC_USER_AGENT
        return configured

    # --- http plumbing -----------------------------------------------------
    async def _get_client(self) -> httpx.AsyncClient:
        """Return the shared HTTP client, creating it on first use."""
        async with self._client_lock:
            if self._client is None or self._client.is_closed:
                self._client = httpx.AsyncClient(
                    headers={
                        "User-Agent": self.user_agent,
                        "Accept": "application/json",
                        "Accept-Encoding": "gzip, deflate",
                    },
                    timeout=httpx.Timeout(30.0),
                    follow_redirects=True,
                )
            return self._client

    async def _get_json(self, url: str) -> Any:
        """GET ``url`` and decode JSON, mapping HTTP failures to provider errors."""
        client = await self._get_client()
        await self._limiter.acquire()
        try:
            response = await client.get(url)
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(self.name, f"{url}: {exc}") from exc
        if response.status_code == 429:
            retry_after = response.headers.get("retry-after")
            raise ProviderRateLimited(
                self.name,
                f"SEC throttled the request for {url}",
                retry_after=float(retry_after) if retry_after and retry_after.isdigit() else None,
            )
        if response.status_code == 404:
            raise SymbolNotFound(self.name, f"EDGAR has no document at {url}")
        if response.status_code >= 400:
            raise ProviderUnavailable(
                self.name, f"{url} returned HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderUnavailable(self.name, f"{url} returned non-JSON body") from exc
        self._last_success = utcnow()
        return payload

    async def close(self) -> None:
        """Close the HTTP client."""
        async with self._client_lock:
            if self._client is not None and not self._client.is_closed:
                await self._client.aclose()
            self._client = None

    # --- status ------------------------------------------------------------
    async def status(self) -> ProviderStatus:
        """Report reachability of ``data.sec.gov``."""
        started = utcnow()
        try:
            await self._ticker_index()
        except ProviderRateLimited as exc:
            return ProviderStatus(
                name=self.name,
                configured=True,
                reachable=False,
                last_success=self._last_success,
                detail=f"rate limited: {exc.message}",
            )
        except ProviderUnavailable as exc:
            return ProviderStatus(
                name=self.name,
                configured=True,
                reachable=False,
                last_success=self._last_success,
                detail=exc.message,
            )
        latency_ms = (utcnow() - started).total_seconds() * 1000.0
        count = len(self._ticker_map or {})
        return ProviderStatus(
            name=self.name,
            configured=True,
            reachable=True,
            latency_ms=latency_ms,
            last_success=self._last_success,
            detail=f"{count} tickers in EDGAR index; UA={self.user_agent!r}",
        )

    # --- ticker resolution -------------------------------------------------
    async def _ticker_index(self) -> dict[str, dict[str, Any]]:
        """Return (and cache) the ticker -> {cik, title} index."""
        fresh = self._ticker_map is not None and (
            time.monotonic() - self._ticker_map_at < TICKER_MAP_TTL
        )
        if self._ticker_map is not None and fresh:
            return self._ticker_map
        payload = await self._get_json(TICKERS_URL)
        index: dict[str, dict[str, Any]] = {}
        rows = payload.values() if isinstance(payload, dict) else payload
        for row in rows:
            if not isinstance(row, dict):
                continue
            ticker = str(row.get("ticker", "")).strip().upper()
            if not ticker:
                continue
            index[ticker] = {
                "cik": normalise_cik(row.get("cik_str", 0)),
                "title": str(row.get("title", "")).strip(),
            }
        self._ticker_map = index
        self._ticker_map_at = time.monotonic()
        return index

    async def resolve_cik(self, symbol: str) -> str:
        """Map a ticker to its zero-padded ten-digit CIK."""
        key = symbol.strip().upper()
        index = await self._ticker_index()
        entry = index.get(key) or index.get(key.replace(".", "-"))
        if entry is None:
            raise SymbolNotFound(self.name, f"{symbol}: no CIK in the EDGAR ticker index")
        return str(entry["cik"])

    async def company_name(self, symbol: str) -> str | None:
        """Return the registrant name EDGAR files ``symbol`` under."""
        index = await self._ticker_index()
        entry = index.get(symbol.strip().upper())
        return str(entry["title"]) if entry else None

    async def search(self, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
        """Search the EDGAR ticker index by ticker or company name."""
        text = query.strip().upper()
        if not text:
            return []
        index = await self._ticker_index()
        scored: list[tuple[int, int, str, dict[str, Any]]] = []
        for ticker, entry in index.items():
            title = entry["title"].upper()
            words = title.replace(",", " ").replace(".", " ").split()
            if ticker == text:
                rank = 0
            elif ticker.startswith(text):
                rank = 1
            elif title.startswith(text):
                rank = 2
            elif any(word.startswith(text) for word in words):
                rank = 3
            elif text in title or text in ticker:
                rank = 4
            else:
                continue
            scored.append(
                (
                    rank,
                    len(ticker),
                    ticker,
                    {
                        "symbol": ticker,
                        "name": entry["title"],
                        "cik": entry["cik"],
                        "source": self.name,
                    },
                )
            )
        scored.sort(key=lambda item: (item[0], item[1], item[2]))
        return [item[3] for item in scored[:limit]]

    # --- submissions -------------------------------------------------------
    async def get_submissions(self, symbol: str) -> dict[str, Any]:
        """Return the raw EDGAR submissions document for ``symbol``."""
        cik = await self.resolve_cik(symbol)
        return await self._get_json(SUBMISSIONS_URL.format(cik=cik))

    async def get_filings(
        self, symbol: str, *, forms: list[str] | None = None, limit: int = 40
    ) -> list[Filing]:
        """Return recent filings for ``symbol``, optionally filtered by form type.

        Only the "recent" block of the submissions document is read; EDGAR
        pages older filings into separate files, and this provider does not
        pretend those are present.
        """
        submissions = await self.get_submissions(symbol)
        cik = normalise_cik(submissions.get("cik", await self.resolve_cik(symbol)))
        recent = ((submissions.get("filings") or {}).get("recent")) or {}
        accessions: list[str] = list(recent.get("accessionNumber") or [])
        if not accessions:
            return []
        wanted = {f.strip().upper() for f in forms} if forms else None

        forms_col = list(recent.get("form") or [])
        filed_col = list(recent.get("filingDate") or [])
        period_col = list(recent.get("reportDate") or [])
        doc_col = list(recent.get("primaryDocument") or [])
        desc_col = list(recent.get("primaryDocDescription") or [])

        out: list[Filing] = []
        for i, accession in enumerate(accessions):
            form = str(forms_col[i]) if i < len(forms_col) else ""
            if wanted is not None and form.upper() not in wanted:
                continue
            filed = _parse_date(filed_col[i] if i < len(filed_col) else None)
            if filed is None:
                continue
            plain = accession.replace("-", "")
            document = str(doc_col[i]) if i < len(doc_col) and doc_col[i] else ""
            base = ARCHIVES_URL.format(cik_int=int(cik), accession=plain)
            url = f"{base}/{document}" if document else f"{base}/{accession}-index.htm"
            out.append(
                Filing(
                    symbol=symbol.upper(),
                    cik=cik,
                    form=form,
                    filed_at=filed,
                    accession=accession,
                    url=url,
                    period_of_report=_parse_date(period_col[i] if i < len(period_col) else None),
                    title=str(desc_col[i]) if i < len(desc_col) and desc_col[i] else None,
                )
            )
            if len(out) >= limit:
                break
        return out

    # --- XBRL --------------------------------------------------------------
    async def get_company_facts(self, symbol: str) -> dict[str, Any]:
        """Return the full XBRL ``companyfacts`` document for ``symbol``."""
        cik = await self.resolve_cik(symbol)
        return await self._get_json(COMPANY_FACTS_URL.format(cik=cik))

    async def get_concept(
        self, symbol: str, taxonomy: str, tag: str, *, unit: str = "USD"
    ) -> list[tuple[date, float]]:
        """Return one XBRL concept as a dated value series.

        Points are keyed by the period end date and deduplicated keeping the
        most recently filed value, so a restated figure supersedes the original.
        """
        cik = await self.resolve_cik(symbol)
        payload = await self._get_json(
            COMPANY_CONCEPT_URL.format(cik=cik, taxonomy=taxonomy, tag=tag)
        )
        entries = ((payload.get("units") or {}).get(unit)) or []
        by_end: dict[date, tuple[date, float]] = {}
        for entry in entries:
            end = _parse_date(entry.get("end"))
            value = entry.get("val")
            if end is None or value is None:
                continue
            filed = _parse_date(entry.get("filed")) or end
            previous = by_end.get(end)
            if previous is None or filed >= previous[0]:
                by_end[end] = (filed, float(value))
        return [(end, value) for end, (_filed, value) in sorted(by_end.items())]

    async def get_concept_series(
        self, symbol: str, taxonomy: str, tag: str, *, unit: str = "USD"
    ) -> Series:
        """Return :meth:`get_concept` as a :class:`~tradinglib.data.base.Series`."""
        points = await self.get_concept(symbol, taxonomy, tag, unit=unit)
        return Series(
            name=f"{symbol.upper()}:{taxonomy}:{tag}",
            points=[
                (to_utc(datetime.combine(end, datetime.min.time())), value) for end, value in points
            ],
            unit=unit,
            source=self.name,
            meta={"symbol": symbol.upper(), "taxonomy": taxonomy, "tag": tag},
        )
