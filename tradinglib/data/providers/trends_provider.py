"""Google Trends provider via ``pytrends``.

Google throttles aggressively; the implementation backs off and surfaces
throttling honestly (:class:`~tradinglib.data.base.ProviderRateLimited`) instead of
returning empty series as if they were data.

``pytrends`` is a blocking, ``requests``-based client, so every call is
dispatched to a worker thread. One :class:`~pytrends.request.TrendReq` session
is reused across calls because Google's cookie/token handshake is the expensive
part; a session is rebuilt only when a call fails on it.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime
from typing import Any, Final

import pandas as pd
from pytrends import exceptions as pytrends_exceptions
from pytrends.request import TrendReq

from tradinglib.data.base import (
    DataProvider,
    ProviderRateLimited,
    ProviderStatus,
    ProviderUnavailable,
    Series,
    TrendPoint,
    to_utc,
    utcnow,
)

logger = logging.getLogger(__name__)

#: Google's own cap: at most five keywords per interest-over-time payload.
MAX_KEYWORDS: Final[int] = 5

#: Keyword used to probe reachability in :meth:`TrendsProvider.status`.
PROBE_KEYWORD: Final[str] = "stock market"

#: Retry schedule (seconds) used when Google throttles a request.
BACKOFF_SECONDS: Final[tuple[float, ...]] = (2.0, 6.0, 15.0)


class TrendsProvider(DataProvider):
    """Keyless Google Trends adapter for alternative-data signals."""

    name = "trends"

    def __init__(self, *, hl: str = "en-US", tz: int = 360, timeout: float = 20.0) -> None:
        """Create the provider with pytrends locale/timezone settings.

        Args:
            hl: Google host language.
            tz: Google timezone offset in minutes west of UTC (360 = US CST).
            timeout: connect/read timeout handed to the underlying session.
        """
        self.hl = hl
        self.tz = tz
        self.timeout = timeout
        self._client: TrendReq | None = None
        self._lock = asyncio.Lock()
        self._last_success: datetime | None = None

    # --- plumbing ---------------------------------------------------------
    def _build_client(self) -> TrendReq:
        """Create a pytrends session (blocking; runs in a worker thread)."""
        return TrendReq(
            hl=self.hl,
            tz=self.tz,
            timeout=(self.timeout, self.timeout),
            retries=0,
            backoff_factor=0,
        )

    async def _get_client(self) -> TrendReq:
        """Return the shared pytrends session, creating it on first use."""
        if self._client is None:
            self._client = await asyncio.to_thread(self._build_client)
        return self._client

    async def _run(self, label: str, call: Any) -> Any:
        """Run a blocking pytrends call with throttling-aware retries.

        ``call`` receives the live :class:`TrendReq` and runs in a worker
        thread. Google's 429s are retried on the schedule in
        :data:`BACKOFF_SECONDS`; when they persist the caller gets a
        :class:`~tradinglib.data.base.ProviderRateLimited`, never an empty result
        dressed up as data.
        """
        last_error: Exception | None = None
        async with self._lock:
            for attempt in range(len(BACKOFF_SECONDS) + 1):
                client = await self._get_client()
                try:
                    result = await asyncio.to_thread(call, client)
                except pytrends_exceptions.TooManyRequestsError as exc:
                    last_error = exc
                    self._client = None  # a throttled session rarely recovers
                    if attempt < len(BACKOFF_SECONDS):
                        delay = BACKOFF_SECONDS[attempt] * (1.0 + random.random() * 0.25)
                        logger.info(
                            "Google Trends throttled %s; retrying in %.1fs", label, delay
                        )
                        await asyncio.sleep(delay)
                        continue
                    raise ProviderRateLimited(
                        self.name,
                        f"Google Trends throttled {label} after "
                        f"{len(BACKOFF_SECONDS) + 1} attempts",
                        retry_after=BACKOFF_SECONDS[-1],
                    ) from exc
                except pytrends_exceptions.ResponseError as exc:
                    self._client = None
                    raise ProviderUnavailable(self.name, f"{label}: {exc}") from exc
                except Exception as exc:  # noqa: BLE001 - requests/urllib3 error surface
                    self._client = None
                    raise ProviderUnavailable(
                        self.name, f"{label}: {type(exc).__name__}: {exc}"
                    ) from exc
                self._last_success = utcnow()
                return result
        raise ProviderUnavailable(self.name, f"{label}: {last_error}")

    @staticmethod
    def _normalise_keywords(keywords: list[str]) -> list[str]:
        """Trim, deduplicate and cap a keyword list at Google's limit."""
        seen: list[str] = []
        for keyword in keywords:
            text = keyword.strip()
            if text and text not in seen:
                seen.append(text)
        if len(seen) > MAX_KEYWORDS:
            logger.info(
                "Google Trends accepts %d keywords per request; dropping %d",
                MAX_KEYWORDS,
                len(seen) - MAX_KEYWORDS,
            )
        return seen[:MAX_KEYWORDS]

    # --- status -----------------------------------------------------------
    async def status(self) -> ProviderStatus:
        """Report reachability, distinguishing throttling from failure."""
        started = utcnow()
        try:
            frame = await self._interest_frame([PROBE_KEYWORD], "today 3-m", "US")
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
        rows = 0 if frame is None or frame.empty else len(frame)
        return ProviderStatus(
            name=self.name,
            configured=True,
            reachable=True,
            latency_ms=latency_ms,
            last_success=self._last_success,
            detail=f"probe {PROBE_KEYWORD!r} returned {rows} weekly points",
        )

    # --- interest over time ------------------------------------------------
    async def _interest_frame(
        self, keywords: list[str], timeframe: str, geo: str
    ) -> pd.DataFrame | None:
        """Fetch the raw interest-over-time frame for ``keywords``."""

        def call(client: TrendReq) -> pd.DataFrame | None:
            """Blocking pytrends payload build + fetch."""
            client.build_payload(keywords, cat=0, timeframe=timeframe, geo=geo, gprop="")
            return client.interest_over_time()

        return await self._run(f"interest_over_time({keywords!r})", call)

    async def interest_points(
        self, keywords: list[str], *, timeframe: str = "today 12-m", geo: str = "US"
    ) -> list[TrendPoint]:
        """Return raw :class:`~tradinglib.data.base.TrendPoint` readings.

        Google's ``isPartial`` flag is preserved on each point instead of being
        silently dropped, so the UI can mark the incomplete trailing bucket.
        """
        wanted = self._normalise_keywords(keywords)
        if not wanted:
            return []
        frame = await self._interest_frame(wanted, timeframe, geo)
        if frame is None or frame.empty:
            return []
        partial_col = (
            frame["isPartial"] if "isPartial" in frame.columns else pd.Series(False, frame.index)
        )
        points: list[TrendPoint] = []
        for keyword in wanted:
            if keyword not in frame.columns:
                continue
            for index_value, value in frame[keyword].items():
                if pd.isna(value):
                    continue
                ts = pd.Timestamp(index_value).to_pydatetime()
                points.append(
                    TrendPoint(
                        keyword=keyword,
                        ts=to_utc(ts),
                        value=float(value),
                        partial=bool(partial_col.get(index_value, False)),
                        geo=geo,
                        timeframe=timeframe,
                        source=self.name,
                    )
                )
        return points

    async def interest_over_time(
        self, keywords: list[str], *, timeframe: str = "today 12-m", geo: str = "US"
    ) -> dict[str, Series]:
        """Return one interest-over-time series per keyword.

        Partial (still-accumulating) buckets are excluded from the series so a
        stored signal never contains a value Google intends to revise; use
        :meth:`interest_points` when the partial bucket is wanted.
        """
        points = await self.interest_points(keywords, timeframe=timeframe, geo=geo)
        out: dict[str, Series] = {}
        for point in points:
            if point.partial:
                continue
            series = out.get(point.keyword)
            if series is None:
                series = Series(
                    name=point.keyword,
                    points=[],
                    unit="index 0-100",
                    source=self.name,
                    meta={"geo": geo, "timeframe": timeframe},
                )
                out[point.keyword] = series
            series.points.append((point.ts, point.value))
        for series in out.values():
            series.points.sort()
        return out

    # --- related queries ---------------------------------------------------
    async def related_queries(
        self, keyword: str, *, timeframe: str = "today 12-m", geo: str = "US"
    ) -> dict[str, list[dict[str, object]]]:
        """Return rising and top related queries for ``keyword``."""
        text = keyword.strip()
        if not text:
            return {"top": [], "rising": []}

        def call(client: TrendReq) -> dict[str, Any]:
            """Blocking pytrends payload build + fetch."""
            client.build_payload([text], cat=0, timeframe=timeframe, geo=geo, gprop="")
            return client.related_queries()

        payload = await self._run(f"related_queries({text!r})", call)
        block = (payload or {}).get(text) or {}
        out: dict[str, list[dict[str, object]]] = {"top": [], "rising": []}
        for bucket in ("top", "rising"):
            frame = block.get(bucket)
            if isinstance(frame, pd.DataFrame) and not frame.empty:
                out[bucket] = [
                    {"query": str(row.query), "value": _scalar(row.value)}
                    for row in frame.itertuples(index=False)
                ]
        return out


def _scalar(value: Any) -> Any:
    """Render a pandas cell as a plain int/float/str."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, str):
        return value
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return int(number) if number.is_integer() else number
