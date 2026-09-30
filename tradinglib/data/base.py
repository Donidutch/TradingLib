"""Core data models and the provider interface.

The dataclasses here are the common currency of the whole library: every
provider returns them, the store persists them, and ``tradinglib.research`` derives
page payloads from them. All timestamps are timezone-aware UTC.
"""

from __future__ import annotations

import abc
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from enum import StrEnum
from typing import Any


class ProviderError(RuntimeError):
    """Base class for every recoverable provider failure.

    Providers raise these instead of returning empty results, so callers can
    tell "the vendor said no" apart from "there genuinely is no data".
    """

    def __init__(self, provider: str, message: str) -> None:
        super().__init__(f"{provider}: {message}")
        self.provider = provider
        self.message = message


class ProviderNotConfigured(ProviderError):
    """Raised when a provider needs credentials that are not configured."""


class ProviderRateLimited(ProviderError):
    """Raised when a vendor throttles us (HTTP 429, Google Trends quota, ...)."""

    def __init__(self, provider: str, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(provider, message)
        self.retry_after = retry_after


class ProviderUnavailable(ProviderError):
    """Raised when a vendor is reachable but failed to serve the request."""


class SymbolNotFound(ProviderError):
    """Raised when a symbol cannot be resolved by the provider."""


def utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def to_utc(value: datetime) -> datetime:
    """Coerce ``value`` to timezone-aware UTC (naive input is assumed UTC)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _json_safe(value: Any) -> Any:
    """Recursively convert a value into something ``json.dumps`` accepts."""
    if isinstance(value, datetime):
        return to_utc(value).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, StrEnum):
        return str(value)
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        return None
    return value


class BarInterval(StrEnum):
    """Supported bar intervals."""

    MIN_1 = "1m"
    MIN_5 = "5m"
    MIN_15 = "15m"
    MIN_30 = "30m"
    HOUR_1 = "1h"
    DAY_1 = "1d"
    WEEK_1 = "1wk"
    MONTH_1 = "1mo"

    @property
    def approx_seconds(self) -> int:
        """Nominal length of the interval in seconds (calendar months = 30 days).

        Used by staleness checks; it is an approximation for the calendar
        intervals and is documented as such rather than pretending precision.
        """
        return {
            BarInterval.MIN_1: 60,
            BarInterval.MIN_5: 300,
            BarInterval.MIN_15: 900,
            BarInterval.MIN_30: 1800,
            BarInterval.HOUR_1: 3600,
            BarInterval.DAY_1: 86_400,
            BarInterval.WEEK_1: 604_800,
            BarInterval.MONTH_1: 2_592_000,
        }[self]

    @property
    def intraday(self) -> bool:
        """Whether the interval is shorter than one trading day."""
        return self.approx_seconds < 86_400


class AssetClass(StrEnum):
    """Asset classes TradingLib can carry."""

    EQUITY = "equity"
    ETF = "etf"
    INDEX = "index"
    CRYPTO = "crypto"
    MACRO = "macro"


@dataclass(frozen=True, slots=True)
class Quote:
    """A point-in-time quote for a symbol."""

    symbol: str
    price: float
    ts: datetime
    bid: float | None = None
    ask: float | None = None
    bid_size: int | None = None
    ask_size: int | None = None
    volume: int | None = None
    prev_close: float | None = None
    source: str = "unknown"

    @property
    def change(self) -> float | None:
        """Absolute change against the previous close, when known."""
        if self.prev_close is None:
            return None
        return self.price - self.prev_close

    @property
    def change_pct(self) -> float | None:
        """Percentage change against the previous close, when known."""
        if not self.prev_close:
            return None
        return (self.price / self.prev_close - 1.0) * 100.0

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping, including derived change fields."""
        payload = _json_safe(asdict(self))
        payload["change"] = self.change
        payload["change_pct"] = self.change_pct
        return payload


@dataclass(frozen=True, slots=True)
class Bar:
    """A single OHLCV bar."""

    symbol: str
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    interval: BarInterval = BarInterval.DAY_1
    source: str = "unknown"

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of the bar."""
        return _json_safe(asdict(self))


@dataclass(slots=True)
class Series:
    """A named numeric time series (prices, macro series, alt-data signals)."""

    name: str
    points: list[tuple[datetime, float]] = field(default_factory=list)
    unit: str | None = None
    source: str = "unknown"
    meta: dict[str, Any] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.points)

    @property
    def last_ts(self) -> datetime | None:
        """Timestamp of the most recent point, or ``None`` when empty."""
        return self.points[-1][0] if self.points else None

    @property
    def last_value(self) -> float | None:
        """Value of the most recent point, or ``None`` when empty."""
        return self.points[-1][1] if self.points else None

    def sorted(self) -> Series:
        """Return a copy whose points are ascending in time and deduplicated."""
        merged: dict[datetime, float] = {}
        for ts, value in self.points:
            merged[to_utc(ts)] = value
        return Series(
            name=self.name,
            points=sorted(merged.items()),
            unit=self.unit,
            source=self.source,
            meta=dict(self.meta),
        )

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of the series."""
        return {
            "name": self.name,
            "unit": self.unit,
            "source": self.source,
            "meta": _json_safe(self.meta),
            "points": [[to_utc(ts).isoformat(), value] for ts, value in self.points],
        }


@dataclass(slots=True)
class Fundamentals:
    """Fundamental snapshot for one company, as reported.

    Values are absolute currency units unless the field name says otherwise.
    Any figure the vendor did not supply stays ``None`` - never zero-filled.
    """

    symbol: str
    as_of: date | None = None
    currency: str | None = None
    name: str | None = None
    sector: str | None = None
    industry: str | None = None
    market_cap: float | None = None
    enterprise_value: float | None = None
    shares_outstanding: float | None = None
    revenue_ttm: float | None = None
    gross_profit_ttm: float | None = None
    ebitda_ttm: float | None = None
    net_income_ttm: float | None = None
    free_cash_flow_ttm: float | None = None
    total_debt: float | None = None
    cash_and_equivalents: float | None = None
    extras: dict[str, Any] = field(default_factory=dict)
    source: str = "unknown"
    fetched_at: datetime | None = None

    @property
    def net_cash(self) -> float | None:
        """Cash minus total debt, when both figures are known."""
        if self.cash_and_equivalents is None or self.total_debt is None:
            return None
        return self.cash_and_equivalents - self.total_debt

    @property
    def gross_margin(self) -> float | None:
        """Gross margin as a fraction of revenue, when both figures are known."""
        if not self.revenue_ttm or self.gross_profit_ttm is None:
            return None
        return self.gross_profit_ttm / self.revenue_ttm

    @property
    def ev_to_sales(self) -> float | None:
        """Enterprise value over trailing revenue, when both are known."""
        if not self.revenue_ttm or self.enterprise_value is None:
            return None
        return self.enterprise_value / self.revenue_ttm

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping including the derived ratios."""
        payload = _json_safe(asdict(self))
        payload["net_cash"] = self.net_cash
        payload["gross_margin"] = self.gross_margin
        payload["ev_to_sales"] = self.ev_to_sales
        return payload


@dataclass(frozen=True, slots=True)
class TrendPoint:
    """One Google Trends interest reading for a keyword.

    ``value`` is Google's 0-100 relative interest index for the requested
    timeframe/geo - it is not an absolute search count, and ``partial`` marks
    the trailing bucket Google itself flags as incomplete.
    """

    keyword: str
    ts: datetime
    value: float
    partial: bool = False
    geo: str = ""
    timeframe: str = ""
    source: str = "trends"

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of the trend point."""
        return _json_safe(asdict(self))


@dataclass(frozen=True, slots=True)
class NewsItem:
    """A news article with optional sentiment scoring."""

    symbol: str | None
    headline: str
    url: str
    published_at: datetime
    publisher: str | None = None
    summary: str | None = None
    sentiment: float | None = None
    source: str = "unknown"

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of the news item."""
        return _json_safe(asdict(self))


@dataclass(frozen=True, slots=True)
class Filing:
    """An SEC EDGAR filing reference."""

    symbol: str
    cik: str
    form: str
    filed_at: date
    accession: str
    url: str
    period_of_report: date | None = None
    title: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of the filing."""
        return _json_safe(asdict(self))


@dataclass(frozen=True, slots=True)
class ProviderStatus:
    """Honest health report for a provider."""

    name: str
    configured: bool
    reachable: bool | None = None
    latency_ms: float | None = None
    last_success: datetime | None = None
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of the status."""
        return _json_safe(asdict(self))


class DataProvider(abc.ABC):
    """Abstract base for every market/fundamental/alt-data provider.

    Implementations are async. Blocking vendor SDK calls must be dispatched to
    a thread executor (``asyncio.to_thread``) so the API event loop stays free.
    Methods a provider genuinely cannot serve raise :class:`NotImplementedError`;
    callers treat that as "capability absent", never as an error to hide.
    """

    #: Stable provider identifier, e.g. ``"yfinance"``.
    name: str = "provider"

    @abc.abstractmethod
    async def status(self) -> ProviderStatus:
        """Report configuration and reachability without inventing values."""
        raise NotImplementedError

    async def get_quote(self, symbol: str) -> Quote:
        """Return the latest quote for ``symbol``."""
        raise NotImplementedError(f"{self.name} does not provide quotes")

    async def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """Return the latest quote for each of ``symbols``."""
        raise NotImplementedError(f"{self.name} does not provide quotes")

    async def get_bars(
        self,
        symbol: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        interval: BarInterval = BarInterval.DAY_1,
        limit: int | None = None,
    ) -> list[Bar]:
        """Return OHLCV bars for ``symbol`` in ``[start, end]``."""
        raise NotImplementedError(f"{self.name} does not provide bars")

    async def get_fundamentals(self, symbol: str) -> Fundamentals:
        """Return a fundamental snapshot for ``symbol``."""
        raise NotImplementedError(f"{self.name} does not provide fundamentals")

    async def get_news(self, symbol: str, *, limit: int = 25) -> list[NewsItem]:
        """Return recent news for ``symbol``."""
        raise NotImplementedError(f"{self.name} does not provide news")

    async def search(self, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
        """Resolve a free-text query to candidate symbols."""
        raise NotImplementedError(f"{self.name} does not provide symbol search")

    async def close(self) -> None:
        """Release network clients and background tasks. Default: no-op."""
        return None
