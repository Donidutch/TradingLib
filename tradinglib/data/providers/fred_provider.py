"""FRED macro data.

Keyless by default through the ``fredgraph`` CSV endpoints; when a FRED API key
is configured the JSON API is used instead for richer metadata (title, units,
frequency, last-updated) and for series search, which the CSV endpoint cannot
serve. If the optional ``fredapi`` package is installed it is used for the keyed
path, otherwise the same JSON API is called directly over HTTP - the results are
identical, so the extra dependency is genuinely optional.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
from datetime import date, datetime, time, timezone
from typing import Any, Final

import httpx

from tradinglib.data.base import (
    DataProvider,
    ProviderNotConfigured,
    ProviderRateLimited,
    ProviderStatus,
    ProviderUnavailable,
    Series,
    SymbolNotFound,
    utcnow,
)

logger = logging.getLogger(__name__)

CSV_URL: Final[str] = "https://fred.stlouisfed.org/graph/fredgraph.csv"
API_BASE: Final[str] = "https://api.stlouisfed.org/fred"

#: Series used to probe reachability in :meth:`FredProvider.status`.
PROBE_SERIES: Final[str] = "DGS10"

#: Value FRED writes for a missing observation in both CSV and JSON.
MISSING_VALUE: Final[str] = "."

#: User-Agent sent to FRED. It identifies TradingLib *and* keeps the underlying
#: client token, which is not cosmetic: fred.stlouisfed.org's edge silently
#: black-holes requests whose User-Agent carries no recognised client token
#: (verified against the live endpoint - a bare "TradingLib research" hangs until
#: the read timeout, while this value answers in ~200 ms).
USER_AGENT: Final[str] = f"TradingLib research (python-httpx/{httpx.__version__})"


def _as_utc_midnight(value: date) -> datetime:
    """Return a date as midnight UTC, the storage convention for macro series."""
    return datetime.combine(value, time.min, tzinfo=timezone.utc)


class FredProvider(DataProvider):
    """Federal Reserve Economic Data adapter (CSV keyless, JSON when keyed)."""

    name = "fred"

    def __init__(self, *, api_key: str | None = None, timeout: float = 30.0) -> None:
        """Create the provider; ``api_key`` is optional.

        When ``api_key`` is ``None`` the configured key is used if one exists,
        so a keyless install simply keeps using the CSV endpoints.
        """
        if api_key is None:
            from tradinglib.config import get_settings

            api_key = get_settings().fred_api_key
        self.api_key = api_key
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()
        self._last_success: datetime | None = None

    @property
    def uses_api(self) -> bool:
        """Whether the keyed JSON API will be used instead of CSV downloads."""
        return self.api_key is not None

    # --- http plumbing -----------------------------------------------------
    async def _get_client(self) -> httpx.AsyncClient:
        """Return the shared HTTP client, creating it on first use."""
        async with self._client_lock:
            if self._client is None or self._client.is_closed:
                self._client = httpx.AsyncClient(
                    headers={"User-Agent": USER_AGENT},
                    timeout=httpx.Timeout(self.timeout),
                    follow_redirects=True,
                )
            return self._client

    async def _request(self, url: str, params: dict[str, Any]) -> httpx.Response:
        """Issue a GET request, mapping HTTP failures onto provider errors.

        A read timeout is retried once: FRED occasionally stalls a connection,
        and a single retry costs less than failing a whole page render.
        """
        client = await self._get_client()
        try:
            try:
                response = await client.get(url, params=params)
            except httpx.TimeoutException:
                response = await client.get(url, params=params)
        except httpx.HTTPError as exc:
            detail = str(exc) or type(exc).__name__
            raise ProviderUnavailable(self.name, f"{url}: {detail}") from exc
        if response.status_code == 429:
            raise ProviderRateLimited(self.name, "FRED throttled the request")
        if response.status_code == 404:
            raise SymbolNotFound(self.name, f"FRED has no document at {url}")
        if response.status_code == 400:
            # FRED answers an unknown series id with 400 and an explanatory body.
            raise SymbolNotFound(self.name, response.text.strip()[:300] or "bad request")
        if response.status_code >= 400:
            raise ProviderUnavailable(self.name, f"{url} returned HTTP {response.status_code}")
        self._last_success = utcnow()
        return response

    async def close(self) -> None:
        """Close the HTTP client."""
        async with self._client_lock:
            if self._client is not None and not self._client.is_closed:
                await self._client.aclose()
            self._client = None

    # --- status ------------------------------------------------------------
    async def status(self) -> ProviderStatus:
        """Report reachability of the selected FRED endpoint."""
        started = utcnow()
        mode = "api" if self.uses_api else "csv"
        try:
            series = await self.get_series(PROBE_SERIES, start=date(2024, 1, 1))
        except ProviderRateLimited as exc:
            return ProviderStatus(
                name=self.name,
                configured=True,
                reachable=False,
                last_success=self._last_success,
                detail=f"rate limited ({mode}): {exc.message}",
            )
        except ProviderUnavailable as exc:
            return ProviderStatus(
                name=self.name,
                configured=True,
                reachable=False,
                last_success=self._last_success,
                detail=f"{mode}: {exc.message}",
            )
        latency_ms = (utcnow() - started).total_seconds() * 1000.0
        return ProviderStatus(
            name=self.name,
            configured=True,
            reachable=True,
            latency_ms=latency_ms,
            last_success=self._last_success,
            detail=f"{mode} endpoint; {PROBE_SERIES} has {len(series)} observations since 2024",
        )

    # --- series ------------------------------------------------------------
    @staticmethod
    def parse_csv(series_id: str, text: str) -> list[tuple[date, float]]:
        """Parse a ``fredgraph.csv`` body into dated observations.

        FRED has used both ``DATE`` and ``observation_date`` as the first column
        header over time, so the column is taken positionally; missing values
        (written as ``.`` or left blank) are dropped rather than zero-filled.
        """
        reader = csv.reader(io.StringIO(text))
        try:
            header = next(reader)
        except StopIteration:
            return []
        if len(header) < 2:
            return []
        value_index = 1
        for i, column in enumerate(header[1:], start=1):
            if column.strip().upper() == series_id.upper():
                value_index = i
                break
        points: list[tuple[date, float]] = []
        for row in reader:
            if len(row) <= value_index:
                continue
            raw_date = row[0].strip()
            raw_value = row[value_index].strip()
            if not raw_date or not raw_value or raw_value == MISSING_VALUE:
                continue
            try:
                observed = date.fromisoformat(raw_date[:10])
                value = float(raw_value)
            except ValueError:
                continue
            points.append((observed, value))
        return points

    async def _series_csv(
        self, series_id: str, start: date | None, end: date | None
    ) -> list[tuple[date, float]]:
        """Fetch observations through the keyless CSV endpoint."""
        params: dict[str, Any] = {"id": series_id}
        if start is not None:
            params["cosd"] = start.isoformat()
        if end is not None:
            params["coed"] = end.isoformat()
        try:
            response = await self._request(CSV_URL, params)
        except SymbolNotFound as exc:
            raise SymbolNotFound(self.name, f"{series_id}: {exc.message}") from exc
        text = response.text
        if text.lstrip().startswith("<"):
            raise SymbolNotFound(self.name, f"{series_id}: FRED returned an HTML error page")
        return self.parse_csv(series_id, text)

    async def _api_json(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """Call the keyed JSON API at ``path``."""
        if self.api_key is None:
            raise ProviderNotConfigured(self.name, "FRED_API_KEY is not configured")
        payload = dict(params)
        payload["api_key"] = self.api_key
        payload["file_type"] = "json"
        response = await self._request(f"{API_BASE}/{path}", payload)
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderUnavailable(self.name, f"{path} returned non-JSON body") from exc

    async def _series_api(
        self, series_id: str, start: date | None, end: date | None
    ) -> list[tuple[date, float]]:
        """Fetch observations through the keyed JSON API (or ``fredapi``)."""
        via_fredapi = await self._series_fredapi(series_id, start, end)
        if via_fredapi is not None:
            return via_fredapi
        params: dict[str, Any] = {"series_id": series_id}
        if start is not None:
            params["observation_start"] = start.isoformat()
        if end is not None:
            params["observation_end"] = end.isoformat()
        payload = await self._api_json("series/observations", params)
        points: list[tuple[date, float]] = []
        for observation in payload.get("observations") or []:
            raw_value = str(observation.get("value", "")).strip()
            if not raw_value or raw_value == MISSING_VALUE:
                continue
            try:
                observed = date.fromisoformat(str(observation.get("date"))[:10])
                value = float(raw_value)
            except (TypeError, ValueError):
                continue
            points.append((observed, value))
        return points

    async def _series_fredapi(
        self, series_id: str, start: date | None, end: date | None
    ) -> list[tuple[date, float]] | None:
        """Use the optional ``fredapi`` package when it is installed.

        Returns ``None`` when the package is absent so the caller falls back to
        the plain HTTP JSON path.
        """
        try:
            from fredapi import Fred  # type: ignore[import-not-found]
        except ImportError:
            return None

        def _fetch() -> list[tuple[date, float]]:
            """Blocking fredapi call; runs in a worker thread."""
            client = Fred(api_key=self.api_key)
            series = client.get_series(
                series_id,
                observation_start=start.isoformat() if start else None,
                observation_end=end.isoformat() if end else None,
            )
            points: list[tuple[date, float]] = []
            for index_value, value in series.items():
                if value is None or value != value:  # NaN check without numpy
                    continue
                points.append((index_value.date(), float(value)))
            return points

        try:
            return await asyncio.to_thread(_fetch)
        except Exception as exc:  # noqa: BLE001 - fall back to the HTTP path
            logger.info("fredapi path failed for %s (%s); using the JSON API", series_id, exc)
            return None

    async def get_series(
        self, series_id: str, *, start: date | None = None, end: date | None = None
    ) -> Series:
        """Return one FRED series (e.g. ``DGS10``, ``CPIAUCSL``).

        Observation dates are stored as midnight UTC, matching the store's
        UTC-everywhere convention.
        """
        key = series_id.strip().upper()
        if not key:
            raise SymbolNotFound(self.name, "empty series id")
        if self.uses_api:
            points = await self._series_api(key, start, end)
        else:
            points = await self._series_csv(key, start, end)
        meta: dict[str, Any] = {"series_id": key, "mode": "api" if self.uses_api else "csv"}
        unit: str | None = None
        if self.uses_api:
            try:
                info = await self.get_series_meta(key)
                unit = info.get("units")
                meta.update(info)
            except ProviderUnavailable as exc:  # metadata is a bonus, not a requirement
                logger.info("FRED metadata unavailable for %s: %s", key, exc.message)
        return Series(
            name=key,
            points=[(_as_utc_midnight(observed), value) for observed, value in points],
            unit=unit,
            source=self.name,
            meta=meta,
        )

    async def get_series_meta(self, series_id: str) -> dict[str, Any]:
        """Return title, units, frequency and last-updated metadata for a series.

        Requires an API key: the keyless CSV endpoint carries no metadata, and
        this provider will not invent any.
        """
        if not self.uses_api:
            raise ProviderNotConfigured(
                self.name,
                "series metadata needs FRED_API_KEY; the keyless CSV endpoint has none",
            )
        payload = await self._api_json("series", {"series_id": series_id.strip().upper()})
        rows = payload.get("seriess") or []
        if not rows:
            raise SymbolNotFound(self.name, f"{series_id}: not found in FRED")
        row = rows[0]
        return {
            "series_id": row.get("id"),
            "title": row.get("title"),
            "units": row.get("units"),
            "units_short": row.get("units_short"),
            "frequency": row.get("frequency"),
            "seasonal_adjustment": row.get("seasonal_adjustment"),
            "observation_start": row.get("observation_start"),
            "observation_end": row.get("observation_end"),
            "last_updated": row.get("last_updated"),
            "notes": row.get("notes"),
        }

    async def search(self, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
        """Search FRED series by text. Requires an API key."""
        if not self.uses_api:
            raise ProviderNotConfigured(
                self.name, "series search needs FRED_API_KEY; the CSV endpoint cannot search"
            )
        text = query.strip()
        if not text:
            return []
        payload = await self._api_json(
            "series/search", {"search_text": text, "limit": max(1, limit)}
        )
        return [
            {
                "series_id": row.get("id"),
                "title": row.get("title"),
                "units": row.get("units"),
                "frequency": row.get("frequency"),
                "last_updated": row.get("last_updated"),
                "source": self.name,
            }
            for row in (payload.get("seriess") or [])[:limit]
        ]
