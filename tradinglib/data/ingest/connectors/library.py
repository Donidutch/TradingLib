"""Library connector: wraps an in-process Python provider as a catalog source.

This is how the built-in keyless providers (yfinance, EDGAR, FRED, Trends) and
credentialed SDK providers (Alpaca) appear in the Source Catalog as peers of
user-created HTTP sources. The provider owns the vendor's protocol details
(rate limits, User-Agent quirks, pagination); this connector only turns its
typed results into the record batches the ingestion layer moves around.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

from tradinglib.data.base import (
    BarInterval,
    DataProvider,
    ProviderError,
    ProviderNotConfigured,
    ProviderRateLimited,
    ProviderStatus,
)
from tradinglib.data.ingest.connectors.base import (
    ConnectionTestResult,
    Connector,
    FetchResult,
)
from tradinglib.data.ingest.formats import json_safe_records
from tradinglib.data.ingest.source import DataDomain, SourceConfig, SourceStatus

logger = logging.getLogger(__name__)

#: Dataset produced by each built-in provider when the source does not say.
DEFAULT_DATASETS: dict[str, str] = {
    "yfinance": "bars",
    "alpaca": "quotes",
    "fred": "series",
    "trends": "trends",
    "edgar": "filings",
}

#: Dataset implied by the data domain, used for providers not listed above.
DOMAIN_DATASETS: dict[DataDomain, str] = {
    DataDomain.MARKET: "bars",
    DataDomain.FUNDAMENTALS: "fundamentals",
    DataDomain.MACRO: "series",
    DataDomain.NEWS: "news",
    DataDomain.ALTERNATIVE: "trends",
    DataDomain.REFERENCE: "quotes",
}

#: Datasets this connector knows how to fetch.
DATASETS: frozenset[str] = frozenset(
    {"bars", "quotes", "fundamentals", "news", "series", "trends", "filings", "options", "jobs"}
)

#: Namespace the ``options`` dataset writes its implied-volatility series into.
OPTIONS_NAMESPACE: str = "options"

#: Namespace the ``jobs`` dataset writes its job-board counts into.
JOBS_NAMESPACE: str = "jobs"


def effective_dataset(config: SourceConfig) -> str:
    """Return the dataset a library source really fetches.

    ``advanced.dataset`` wins when it is set; otherwise the provider's own
    default is used, falling back to the one implied by the data domain. This
    is the single place that resolution lives, so a caller outside the
    connector (the API deciding whether a subscription's bar size can reach the
    fetch) asks the same question the fetch itself answers.

    Raises:
        ValueError: when the configured dataset is not one this connector can
            fetch.
    """
    configured = config.advanced.get("dataset")
    if configured:
        name = str(configured).strip().lower()
    else:
        provider_id = str(config.provider or config.endpoint or "").strip().lower()
        name = DEFAULT_DATASETS.get(
            provider_id, DOMAIN_DATASETS.get(config.data_domain, "bars")
        )
    if name not in DATASETS:
        raise ValueError(f"unknown library dataset {name!r}; expected one of {sorted(DATASETS)}")
    return name


class LibraryConnector(Connector):
    """Adapts a :class:`~tradinglib.data.base.DataProvider` to the connector interface.

    Advanced configuration keys::

        dataset     one of bars/quotes/fundamentals/news/series/trends/filings
        interval    bar interval for the ``bars`` dataset (default ``1d``)
        limit       per-item row cap
        forms       EDGAR form filter for the ``filings`` dataset
        timeframe   Google Trends timeframe for the ``trends`` dataset
        geo         Google Trends geography (default ``US``)
        target_days tenor of the ``options`` dataset's IV point (default 30)
    """

    protocol = "library"

    def __init__(self, config: SourceConfig, provider: DataProvider | None = None) -> None:
        """Bind the connector to a config and, optionally, an existing provider.

        When ``provider`` is ``None`` it is constructed from
        ``config.provider`` (falling back to ``config.endpoint``), which names
        the provider (``"yfinance"``, ...).
        """
        super().__init__(config)
        self.provider = provider
        self._owns_provider = provider is None

    # --- configuration ----------------------------------------------------
    @property
    def provider_id(self) -> str:
        """The provider this source wraps."""
        return str(self.config.provider or self.config.endpoint or "").strip().lower()

    @property
    def dataset(self) -> str:
        """The dataset this source ingests.

        Raises:
            ValueError: when the configured dataset is not one this connector
                can fetch.
        """
        return effective_dataset(self.config)

    @property
    def interval(self) -> BarInterval:
        """The bar interval used by the ``bars`` dataset."""
        return BarInterval(str(self.config.advanced.get("interval", "1d")))

    def scope(self, override: Any = None) -> list[str]:
        """Return the items (symbols, series ids, keywords) to fetch."""
        if override:
            if isinstance(override, str):
                return [override]
            return [str(item) for item in override]
        return list(self.config.scope)

    # --- lifecycle --------------------------------------------------------
    async def connect(self) -> None:
        """Instantiate the wrapped provider if it was not supplied.

        Raises:
            KeyError: when the provider id is not registered - a missing vendor
                is never silently substituted with another one.
        """
        if self.provider is not None:
            return
        from tradinglib.data.providers import build_provider

        self.provider = build_provider(self.provider_id)

    async def close(self) -> None:
        """Close the wrapped provider (only when this connector created it)."""
        if self.provider is not None and self._owns_provider:
            provider, self.provider = self.provider, None
            await provider.close()

    async def _require(self) -> DataProvider:
        """Return the connected provider."""
        await self.connect()
        assert self.provider is not None  # noqa: S101 - established by connect()
        return self.provider

    # --- fetching ---------------------------------------------------------
    async def fetch(self, **params: Any) -> FetchResult:
        """Fetch the configured scope through the wrapped provider.

        Keyword arguments: ``scope`` (override the configured items), ``limit``
        (rows per item), ``start``/``end`` (window for the time-series
        datasets), ``interval`` (bar size for the ``bars`` dataset, which is how
        a subscription's granularity reaches the vendor call).

        Raises:
            ProviderError: whatever the vendor adapter raises - the scheduler
                records it as a real failure rather than swallowing it.
            ValueError: when ``interval`` is not a supported bar interval.
        """
        provider = await self._require()
        dataset = self.dataset
        items = self.scope(params.get("scope"))
        limit = params.get("limit") or self.config.advanced.get("limit")
        start: datetime | None = params.get("start")
        end: datetime | None = params.get("end")
        override = params.get("interval")
        interval = BarInterval(str(override)) if override else self.interval

        started = time.perf_counter()
        try:
            records = await self._fetch_dataset(
                provider, dataset, items, limit=limit, start=start, end=end, interval=interval
            )
        except ProviderError as exc:
            self._mark_failure(exc.message)
            raise
        latency_ms = (time.perf_counter() - started) * 1000.0
        self._mark_success(latency_ms)
        return FetchResult(
            records=self.map_records(records),
            fetched_at=datetime.now(timezone.utc),
            latency_ms=latency_ms,
            complete=True,
            dataset=dataset,
        )

    async def _fetch_dataset(
        self,
        provider: DataProvider,
        dataset: str,
        items: list[str],
        *,
        limit: Any = None,
        start: datetime | None = None,
        end: datetime | None = None,
        interval: BarInterval | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch one dataset and return canonical records."""
        if not items and dataset != "quotes":
            return []
        row_limit = int(limit) if limit else None
        bar_interval = interval or self.interval

        if dataset == "bars":
            out: list[dict[str, Any]] = []
            for symbol in items:
                bars = await provider.get_bars(
                    symbol, start=start, end=end, interval=bar_interval, limit=row_limit
                )
                out.extend(bar.as_dict() for bar in bars)
            return out

        if dataset == "quotes":
            if not items:
                return []
            quotes = await provider.get_quotes(items)
            return [quote.as_dict() for quote in quotes.values()]

        if dataset == "fundamentals":
            return [(await provider.get_fundamentals(symbol)).as_dict() for symbol in items]

        if dataset == "news":
            out = []
            for symbol in items:
                news = await provider.get_news(symbol, limit=row_limit or 25)
                out.extend(item.as_dict() for item in news)
            return out

        if dataset == "series":
            out = []
            for series_id in items:
                series = await provider.get_series(  # type: ignore[attr-defined]
                    series_id,
                    start=start.date() if start else None,
                    end=end.date() if end else None,
                )
                out.extend(_series_records(series, namespace=self.provider_id))
            return out

        if dataset == "trends":
            timeframe = str(self.config.advanced.get("timeframe", "today 12-m"))
            geo = str(self.config.advanced.get("geo", "US"))
            series_map = await provider.interest_over_time(  # type: ignore[attr-defined]
                items, timeframe=timeframe, geo=geo
            )
            out = []
            for series in series_map.values():
                out.extend(_series_records(series, namespace="trends"))
            return out

        if dataset == "options":
            target_days = int(self.config.advanced.get("target_days", 30) or 30)
            out = []
            for symbol in items:
                quote = await provider.get_option_iv(  # type: ignore[attr-defined]
                    symbol, target_days=target_days
                )
                stamp = quote.get("as_of")
                value = quote.get("implied_volatility")
                if value is None:
                    continue
                out.append(
                    {
                        "namespace": OPTIONS_NAMESPACE,
                        # Underscore, not a colon: the quality layer addresses datasets as
                        # "series:<namespace>:<name>", so a colon in the name
                        # would make the key ambiguous.
                        "name": f"iv{target_days}_{str(symbol).upper()}",
                        "ts": stamp,
                        "value": float(value),
                        "unit": "annualised volatility",
                        "source": getattr(provider, "name", self.provider_id),
                    }
                )
            return json_safe_records(out)

        if dataset == "jobs":
            # Scope items are board slugs ("datadog") or tickers; a slug that
            # looks like a ticker is still tried as a slug first, which is what
            # the provider does anyway. Two series per board accrue history the
            # single-snapshot research tile cannot have.
            out = []
            for item in items:
                token = str(item).strip()
                reading = await provider.get_postings(  # type: ignore[attr-defined]
                    token, slug=token.lower()
                )
                if reading.board is None or reading.open_roles is None:
                    continue
                key = (reading.slug or token).lower().replace(":", "_")
                stamp = reading.as_of
                out.append(
                    {
                        "namespace": JOBS_NAMESPACE,
                        "name": f"open_roles_{key}",
                        "ts": stamp,
                        "value": float(reading.open_roles),
                        "unit": "listed roles",
                        "source": getattr(provider, "name", self.provider_id),
                    }
                )
                if reading.opened_recent is not None:
                    out.append(
                        {
                            "namespace": JOBS_NAMESPACE,
                            "name": f"opened_90d_{key}",
                            "ts": stamp,
                            "value": float(reading.opened_recent),
                            "unit": "roles opened in 90 days",
                            "source": getattr(provider, "name", self.provider_id),
                        }
                    )
            return json_safe_records(out)

        if dataset == "filings":
            forms = self.config.advanced.get("forms")
            out = []
            for symbol in items:
                filings = await provider.get_filings(  # type: ignore[attr-defined]
                    symbol,
                    forms=[str(f) for f in forms] if forms else None,
                    limit=row_limit or 40,
                )
                out.extend(filing.as_dict() for filing in filings)
            return out

        raise ValueError(f"unsupported dataset {dataset!r}")  # pragma: no cover - guarded above

    async def backfill(
        self, *, range: str = "1y", batch_size: int = 500, end: datetime | None = None
    ) -> AsyncIterator[FetchResult]:
        """Backfill history for the configured scope through the provider.

        One batch is yielded per scope item: providers return a symbol's or a
        series' whole window in one call, so splitting it further would only
        add round-trips. ``end`` is handed to the provider adapters, which
        already take a window.
        """
        from tradinglib.data.ingest.source import BackfillConfig

        start = BackfillConfig(enabled=True, range=range).range_start(now=end)
        items = self.scope()
        if not items:
            yield FetchResult.empty(dataset=self.dataset)
            return
        for index, item in enumerate(items):
            batch = await self.fetch(scope=[item], start=start, end=end, limit=batch_size)
            yield FetchResult(
                records=batch.records,
                fetched_at=batch.fetched_at,
                latency_ms=batch.latency_ms,
                complete=index == len(items) - 1,
                cursor=item,
                dataset=batch.dataset,
            )

    # --- test / status ----------------------------------------------------
    async def test(self) -> ConnectionTestResult:
        """Call the provider's ``status()`` and turn it into a test result.

        When the probe succeeds the connector additionally fetches one real
        record from the configured scope, so the UI's "sample schema detected"
        is an actual sample rather than a claim.
        """
        started = time.perf_counter()
        try:
            provider = await self._require()
        except KeyError as exc:
            return ConnectionTestResult.failure(f"unknown provider: {exc}", reachable=None)
        try:
            status: ProviderStatus = await provider.status()
        except ProviderNotConfigured as exc:
            return ConnectionTestResult.failure(
                f"not configured: {exc.message}", authenticated=False, reachable=None
            )
        except ProviderRateLimited as exc:
            return ConnectionTestResult.failure(
                f"rate limited: {exc.message}", reachable=True, authenticated=True
            )
        except ProviderError as exc:
            self._mark_failure(exc.message)
            return ConnectionTestResult.failure(exc.message, reachable=False)

        latency_ms = status.latency_ms
        if latency_ms is None:
            latency_ms = (time.perf_counter() - started) * 1000.0

        if not status.configured:
            self._mark_failure(status.detail or "not configured")
            return ConnectionTestResult.failure(
                f"not configured: {status.detail or 'credentials are missing'}",
                reachable=status.reachable,
                authenticated=False,
            )
        if status.reachable is not True:
            self._mark_failure(status.detail or "unreachable")
            return ConnectionTestResult.failure(
                status.detail or f"{self.provider_id} is unreachable",
                reachable=False,
                authenticated=True,
                latency_ms=latency_ms,
            )

        records: list[dict[str, Any]] = []
        sample_note = ""
        items = self.scope()
        if items:
            try:
                sample = await self.fetch(scope=items[:1], limit=1)
                records = sample.records
                sample_note = f" · sampled {len(records)} record(s) for {items[0]}"
            except (ProviderError, ValueError) as exc:
                sample_note = f" · sample fetch failed: {exc}"
        else:
            sample_note = " · no scope configured, so nothing was sampled"

        self._mark_success(latency_ms)
        return ConnectionTestResult.success(
            f"{self.provider_id}: {status.detail or 'reachable'}{sample_note}",
            latency_ms=latency_ms,
            authenticated=True,
            records=records,
        )

    async def status(self) -> SourceStatus:
        """Map the provider's status onto a catalog status."""
        try:
            provider = await self._require()
        except KeyError:
            return SourceStatus.NOT_CONFIGURED
        try:
            status = await provider.status()
        except ProviderNotConfigured:
            return SourceStatus.NOT_CONFIGURED
        except ProviderError:
            return SourceStatus.DISCONNECTED
        return map_provider_status(status)


def map_provider_status(status: ProviderStatus) -> SourceStatus:
    """Translate a :class:`~tradinglib.data.base.ProviderStatus` into a catalog status."""
    if not status.configured:
        return SourceStatus.NOT_CONFIGURED
    if status.reachable is True:
        return SourceStatus.CONNECTED
    if status.reachable is False:
        return SourceStatus.DISCONNECTED
    return SourceStatus.UNKNOWN


def _series_records(series: Any, *, namespace: str) -> list[dict[str, Any]]:
    """Convert a :class:`~tradinglib.data.base.Series` into canonical records."""
    records = [
        {
            "namespace": namespace,
            "name": series.name,
            "ts": ts,
            "value": value,
            "unit": series.unit,
            "source": series.source,
        }
        for ts, value in series.points
    ]
    return json_safe_records(records)


__all__ = [
    "DATASETS",
    "DEFAULT_DATASETS",
    "DOMAIN_DATASETS",
    "LibraryConnector",
    "map_provider_status",
]
