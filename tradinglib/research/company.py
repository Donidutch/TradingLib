"""Stock Research engine.

Composes provider output into the payloads the Stock Research page renders. It
derives metrics (TTM revenue, YoY growth, margins, EV/Sales, net cash,
freshness) from the *statements the vendor actually published* - it never
fetches from a vendor itself (everything comes through
:mod:`tradinglib.data.providers` + :mod:`tradinglib.data.cache` +
:mod:`tradinglib.data.store`) and it never fabricates a figure it cannot derive.

Two rules run through the whole module:

* **A metric is either derived or it is null with a reason.** Every derived
  figure is a :class:`Metric` carrying the inputs and the ``basis`` it was
  computed on ("sum of 4 quarters 2025-09-30..2026-06-30"); when it cannot be
  computed the value is ``None`` and ``reason`` says which input was missing.
  Nothing is zero-filled, interpolated or copied from a similar company.
* **Freshness is measured, not asserted.** Each section reports when its data
  was actually fetched (from the cache entry, which records the fetch time) and
  whether that is older than the section's tolerance.

The page's alternative-data tiles that TradingLib has no source for (app-rank
composite, web traffic, job postings) are returned as ``null`` with an
explanatory reason rather than filled with plausible numbers.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Final, Protocol

from tradinglib.data.base import (
    Bar,
    BarInterval,
    DataProvider,
    Filing,
    Fundamentals,
    NewsItem,
    ProviderError,
    ProviderUnavailable,
    Quote,
    Series,
    SymbolNotFound,
    to_utc,
    utcnow,
)
from tradinglib.data.cache import CacheEntry, DataCache, get_cache, provider_key
from tradinglib.data.store import TimeSeriesStore, get_store
from tradinglib.data.universe import UniverseRegistry, get_universe_registry, normalize_symbol

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# tunables
# ---------------------------------------------------------------------------

#: Cache TTL (seconds) per research component. Quotes are short-lived; filings
#: and statements change a few times a year.
TTL: Final[dict[str, float]] = {
    "quote": 15.0,
    "bars_intraday": 60.0,
    "bars_daily": 900.0,
    "fundamentals": 3600.0,
    "statements": 21_600.0,
    "estimates": 3600.0,
    "news": 900.0,
    "filings": 3600.0,
    "trends": 21_600.0,
    "search": 900.0,
    "jobs": 21_600.0,
}

#: How long a fundamentals-coverage measurement stays authoritative (seconds).
#: One measurement costs one provider call per universe member, so it is
#: deliberately slower than the per-symbol fundamentals TTL above.
COVERAGE_TTL: Final[float] = 3600.0

#: How old a component may be before the UI should call it stale (seconds).
MAX_AGE: Final[dict[str, float]] = {
    "quote": 120.0,
    # The newest stored daily bar is legitimately a session (and over a long
    # weekend, four days) old; anything beyond that means ingestion is behind.
    "price": 4 * 86_400.0,
    "fundamentals": 86_400.0,
    "statements": 7 * 86_400.0,
    "estimates": 86_400.0,
    "news": 3 * 3600.0,
    "filings": 86_400.0,
    "trends": 3 * 86_400.0,
    "jobs": 2 * 86_400.0,
}

#: Chart range -> (bar interval, look-back). ``None`` look-back means "all".
RANGES: Final[dict[str, tuple[BarInterval, timedelta | None]]] = {
    "1d": (BarInterval.MIN_5, timedelta(days=1)),
    "5d": (BarInterval.MIN_15, timedelta(days=5)),
    "1m": (BarInterval.DAY_1, timedelta(days=31)),
    "3m": (BarInterval.DAY_1, timedelta(days=92)),
    "6m": (BarInterval.DAY_1, timedelta(days=183)),
    "ytd": (BarInterval.DAY_1, None),  # start = 1 January, handled explicitly
    "1y": (BarInterval.DAY_1, timedelta(days=365)),
    "2y": (BarInterval.DAY_1, timedelta(days=730)),
    "5y": (BarInterval.DAY_1, timedelta(days=1826)),
    "max": (BarInterval.MONTH_1, None),
}

#: Country -> region label shown next to the exchange and sector in the header.
REGIONS: Final[dict[str, str]] = {
    "United States": "North America",
    "Canada": "North America",
    "Mexico": "North America",
    "Singapore": "Southeast Asia",
    "Malaysia": "Southeast Asia",
    "Indonesia": "Southeast Asia",
    "Thailand": "Southeast Asia",
    "Vietnam": "Southeast Asia",
    "Philippines": "Southeast Asia",
    "China": "Greater China",
    "Hong Kong": "Greater China",
    "Taiwan": "Greater China",
    "Japan": "East Asia",
    "South Korea": "East Asia",
    "India": "South Asia",
    "Australia": "Oceania",
    "New Zealand": "Oceania",
    "United Kingdom": "Europe",
    "Ireland": "Europe",
    "Germany": "Europe",
    "France": "Europe",
    "Netherlands": "Europe",
    "Switzerland": "Europe",
    "Sweden": "Europe",
    "Norway": "Europe",
    "Denmark": "Europe",
    "Finland": "Europe",
    "Spain": "Europe",
    "Italy": "Europe",
    "Belgium": "Europe",
    "Austria": "Europe",
    "Poland": "Europe",
    "Portugal": "Europe",
    "Luxembourg": "Europe",
    "Israel": "Middle East",
    "United Arab Emirates": "Middle East",
    "Saudi Arabia": "Middle East",
    "Turkey": "Middle East",
    "South Africa": "Africa",
    "Nigeria": "Africa",
    "Brazil": "Latin America",
    "Argentina": "Latin America",
    "Chile": "Latin America",
    "Colombia": "Latin America",
    "Bermuda": "North America",
    "Cayman Islands": "Latin America",
}

#: yfinance sector name <-> GICS sector name (universe members carry GICS).
GICS_TO_YF: Final[dict[str, str]] = {
    "Information Technology": "Technology",
    "Health Care": "Healthcare",
    "Consumer Discretionary": "Consumer Cyclical",
    "Consumer Staples": "Consumer Defensive",
    "Financials": "Financial Services",
    "Materials": "Basic Materials",
    "Communication Services": "Communication Services",
    "Industrials": "Industrials",
    "Energy": "Energy",
    "Utilities": "Utilities",
    "Real Estate": "Real Estate",
}

#: Discount rate and terminal growth used by the documented reverse DCF.
REVERSE_DCF_DISCOUNT: Final[float] = 0.10
REVERSE_DCF_TERMINAL_GROWTH: Final[float] = 0.025
REVERSE_DCF_YEARS: Final[int] = 10

#: Half-life (days) of the recency weighting in the news-sentiment composite.
SENTIMENT_HALF_LIFE_DAYS: Final[float] = 3.0

#: Alternative-data tiles the reference shows that TradingLib has no source for.
#: They are reported as null with this reason - never invented. Empty today:
#: every tile on the Alternative Data tab now has a real vendor behind it.
UNSOURCED_ALT_SIGNALS: Final[dict[str, str]] = {}

#: How long a job-board reading stays authoritative (seconds). Boards change a
#: few times a day at most, and Greenhouse's dated payload runs to megabytes.
JOBS_TTL: Final[float] = 21_600.0

#: Trends window used for the year-over-year web-interest comparison. A 12-month
#: window cannot express a year-over-year change, so the long window is fetched
#: separately (and cached) rather than derived from the headline series.
TRENDS_LONG_TIMEFRAME: Final[str] = "today 5-y"

#: Weeks averaged on each side of the year-over-year web-interest comparison.
#: A single weekly observation is far too noisy to quote as a trend.
TRENDS_YOY_WEEKS: Final[int] = 12

#: Days back the interest-rank composite compares today's rank against.
TRENDS_RANK_LOOKBACK_DAYS: Final[int] = 90

#: Statement line aliases, in preference order.
INCOME_ALIASES: Final[dict[str, tuple[str, ...]]] = {
    "revenue": ("Total Revenue", "Operating Revenue"),
    "cost_of_revenue": ("Cost Of Revenue", "Reconciled Cost Of Revenue"),
    "gross_profit": ("Gross Profit",),
    "operating_income": ("Operating Income", "Total Operating Income As Reported", "EBIT"),
    "ebitda": ("EBITDA", "Normalized EBITDA"),
    "depreciation": ("Reconciled Depreciation",),
    "net_income": (
        "Net Income",
        "Net Income Common Stockholders",
        "Net Income Including Noncontrolling Interests",
    ),
    "pretax_income": ("Pretax Income",),
    "tax_provision": ("Tax Provision",),
    "interest_expense": ("Interest Expense", "Interest Expense Non Operating"),
    "shares_diluted": ("Diluted Average Shares", "Basic Average Shares"),
    "eps_diluted": ("Diluted EPS", "Basic EPS"),
}

#: Balance-sheet aliases. ``cash`` records which label it came from because
#: "Cash Cash Equivalents And Short Term Investments" already includes the
#: short-term investments that would otherwise be added on top.
BALANCE_ALIASES: Final[dict[str, tuple[str, ...]]] = {
    "cash": (
        "Cash And Cash Equivalents",
        "Cash Financial",
        "Cash Cash Equivalents And Short Term Investments",
    ),
    "short_term_investments": ("Other Short Term Investments", "Available For Sale Securities"),
    "total_debt": ("Total Debt",),
    "long_term_debt": ("Long Term Debt", "Long Term Debt And Capital Lease Obligation"),
    "current_debt": ("Current Debt", "Current Debt And Capital Lease Obligation"),
    "equity": ("Stockholders Equity", "Common Stock Equity"),
    "total_assets": ("Total Assets",),
    "invested_capital": ("Invested Capital",),
    "shares_outstanding": ("Ordinary Shares Number", "Share Issued"),
}

CASHFLOW_ALIASES: Final[dict[str, tuple[str, ...]]] = {
    "operating_cash_flow": ("Operating Cash Flow", "Cash Flow From Continuing Operating Activities"),
    "capex": ("Capital Expenditure", "Purchase Of PPE"),
    "free_cash_flow": ("Free Cash Flow",),
    "depreciation_amortization": (
        "Depreciation And Amortization",
        "Depreciation Amortization Depletion",
        "Depreciation",
    ),
    "stock_based_compensation": ("Stock Based Compensation",),
    "dividends_paid": ("Cash Dividends Paid",),
}

#: Cash labels that already include short-term investments.
_CASH_INCLUDES_INVESTMENTS: Final[frozenset[str]] = frozenset(
    {"Cash Cash Equivalents And Short Term Investments"}
)

#: Legal-form tokens stripped from the *end* of a company name when deriving a
#: Google Trends keyword. Descriptive words ("Holdings", "Group") are kept:
#: "Grab Holdings Limited" -> "Grab Holdings" is a far less ambiguous search
#: term than the bare "Grab".
_NAME_SUFFIXES: Final[frozenset[str]] = frozenset(
    {
        "incorporated",
        "corporation",
        "company",
        "limited",
        "ltd",
        "inc",
        "corp",
        "plc",
        "co",
        "sa",
        "nv",
        "ag",
        "ab",
        "as",
        "oyj",
        "spa",
        "kgaa",
        "a",
        "b",
        "class",
    }
)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _num(value: Any) -> float | None:
    """Return ``value`` as a finite float, or ``None``."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def _text(value: Any) -> str | None:
    """Return ``value`` as a non-empty stripped string, or ``None``."""
    if value is None:
        return None
    out = str(value).strip()
    return out or None


def _parse_period(label: Any) -> date | None:
    """Parse a statement period label (``"2025-12-31"``, ISO datetime) to a date."""
    text = _text(label)
    if text is None:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _pick(record: dict[str, Any], aliases: Sequence[str]) -> tuple[float | None, str | None]:
    """Return the first present alias value and the label it came from."""
    for label in aliases:
        value = _num(record.get(label))
        if value is not None:
            return value, label
    return None, None


def _iso(moment: datetime | date | None) -> str | None:
    """Render a datetime/date as ISO-8601, or ``None``."""
    if moment is None:
        return None
    if isinstance(moment, datetime):
        return to_utc(moment).isoformat()
    return moment.isoformat()


def region_for(country: str | None) -> str | None:
    """Map a country to the region label shown in the page header.

    Countries outside :data:`REGIONS` return ``None`` rather than being placed
    in a bucket by guesswork; the header then falls back to the country itself.
    """
    if country is None:
        return None
    return REGIONS.get(str(country).strip())


def normalise_sector(sector: str | None) -> str | None:
    """Return a sector name in the vendor (yfinance) vocabulary.

    Universe members carry GICS sector names; provider fundamentals carry
    Yahoo's. Peer selection compares the two, so both are normalised here.
    """
    text = _text(sector)
    if text is None:
        return None
    return GICS_TO_YF.get(text, text)


def trends_keyword(name: str | None, symbol: str) -> str:
    """Return the Google Trends keyword used for a company.

    The company name with corporate suffixes removed ("Grab Holdings Limited"
    -> "Grab Holdings"); the ticker is used when no name is known. The keyword
    is reported alongside the data so the reading is interpretable.
    """
    text = _text(name)
    if text is None:
        return symbol.upper()
    words = [word.strip(",.") for word in re.split(r"[\s,]+", text) if word.strip(",.")]
    while len(words) > 1 and words[-1].lower().strip(".") in _NAME_SUFFIXES:
        words.pop()
    cleaned = " ".join(words).strip()
    return cleaned or symbol.upper()


def _percentile_rank(values: Sequence[float], target: float) -> float:
    """Return where ``target`` sits in ``values`` as a 0-100 percentile rank."""
    if not values:
        return 0.0
    below = sum(1 for value in values if value < target)
    equal = sum(1 for value in values if value == target)
    return (below + 0.5 * equal) / len(values) * 100.0


def job_postings_signal(reading: Any, *, reason: str | None = None) -> dict[str, Any]:
    """Turn a :class:`~tradinglib.data.providers.jobs_provider.JobPostings` into a tile.

    The reference's "JOB POSTINGS 90D" tile shows one percentage. The honest
    percentage that a single read of a public ATS board supports is the
    **90-day hiring rate**: what share of the roles listed right now were first
    published in the last ninety days. Comparing the trailing ninety days
    against the ninety before them would look more like a growth rate, but
    filled roles leave the board, so the older window is systematically
    undercounted and the "growth" would be an artefact - that comparison is
    deliberately not what this returns.

    Args:
        reading: the provider's reading, or ``None`` when the provider failed.
        reason: why there is no reading, when there is none.

    Returns:
        The signal mapping the Alternative Data tab renders. ``value`` is a
        percentage of the open book; ``basis`` names the board and the counts.
    """
    if reading is None or reading.board is None:
        return {
            "value": None,
            "unit": "% of listed roles opened in the last 90 days",
            "basis": (
                "share of a company's currently listed roles that were first published in the "
                "last 90 days, read from its public Greenhouse, Ashby or Lever job board"
            ),
            "reason": reason or (reading.reason if reading is not None else "no reading"),
            "source": "jobs",
        }
    open_roles = reading.open_roles or 0
    recent = reading.opened_recent
    value = (recent / open_roles * 100.0) if open_roles and recent is not None else None
    return {
        "value": value,
        "unit": "% of listed roles opened in the last 90 days",
        "board": reading.board_label,
        "slug": reading.slug,
        "open_roles": reading.open_roles,
        "opened_recent": recent,
        "basis": (
            f"{recent} of {open_roles} roles listed on the {reading.board_label} board "
            f"{reading.slug!r} were first published in the last 90 days"
            if value is not None
            else f"the {reading.board_label} board {reading.slug!r} lists no dated role"
        ),
        "reason": None if value is not None else reading.reason,
        "as_of": reading.as_of.isoformat() if reading.as_of else None,
        "source": "jobs",
    }


def interest_rank_composite(
    points: Sequence[tuple[datetime, float]],
    *,
    keyword: str,
    geo: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Rank today's search interest inside its own five-year distribution.

    The reference calls this tile "app rank composite" and shows a rank *move*
    (``↑ 4``). TradingLib has no app-store ranking vendor, so the composite is built
    from the source it does have: the percentile rank of the newest Google
    Trends observation within the whole fetched window, and the change in that
    rank over :data:`TRENDS_RANK_LOOKBACK_DAYS`. ``basis`` states plainly that
    this is a search-interest rank rather than a chart position.

    Args:
        points: the long Trends series, oldest first.
        keyword: the keyword the series was fetched for.
        geo: the Trends geography.
        reason: why the series is missing, when it is.

    Returns:
        The signal mapping the Alternative Data tab renders. ``value`` is the
        90-day rank change in percentile points; ``rank`` is the current rank.
    """
    basis = (
        f"percentile rank of the newest Google Trends interest for {keyword!r} ({geo}) within "
        f"its {TRENDS_LONG_TIMEFRAME} distribution, and the change in that rank over "
        f"{TRENDS_RANK_LOOKBACK_DAYS} days - a search-interest rank, not an app-store position"
    )
    ordered = sorted(points)
    if len(ordered) < 8:
        return {
            "value": None,
            "rank": None,
            "unit": "percentile points, 90-day change",
            "keyword": keyword,
            "geo": geo,
            "basis": basis,
            "reason": reason
            or f"Google Trends returned {len(ordered)} observation(s) for {keyword!r}",
            "source": None,
        }
    values = [value for _, value in ordered]
    rank_now = _percentile_rank(values, values[-1])
    cutoff = ordered[-1][0] - timedelta(days=TRENDS_RANK_LOOKBACK_DAYS)
    earlier = [value for ts, value in ordered if ts <= cutoff]
    rank_then = _percentile_rank(values, earlier[-1]) if earlier else None
    return {
        "value": None if rank_then is None else rank_now - rank_then,
        "rank": rank_now,
        "rank_90d_ago": rank_then,
        "unit": "percentile points, 90-day change",
        "keyword": keyword,
        "geo": geo,
        "observations": len(ordered),
        "as_of": _iso(ordered[-1][0]),
        "basis": basis,
        "reason": (
            None
            if rank_then is not None
            else f"the {TRENDS_LONG_TIMEFRAME} window holds nothing "
            f"{TRENDS_RANK_LOOKBACK_DAYS} days old to rank against"
        ),
        "source": "trends",
    }


def interest_year_over_year(
    points: Sequence[tuple[datetime, float]],
    *,
    keyword: str,
    geo: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Compare the last twelve weeks of search interest with a year earlier.

    The reference's "web traffic YoY" tile has no measured-traffic vendor behind
    it in TradingLib. Search interest is the standard keyless proxy, so that is what
    is computed - and ``basis`` says exactly that, so the reading is never
    mistaken for measured sessions.

    Args:
        points: the long Trends series, oldest first.
        keyword: the keyword the series was fetched for.
        geo: the Trends geography.
        reason: why the series is missing, when it is.

    Returns:
        The signal mapping the Alternative Data tab renders; ``value`` is a
        fractional year-over-year change (``0.118`` = +11.8%).
    """
    basis = (
        f"mean Google Trends interest for {keyword!r} ({geo}) over the last "
        f"{TRENDS_YOY_WEEKS} weekly observations against the same {TRENDS_YOY_WEEKS} a year "
        "earlier - a search-interest proxy for site traffic, not measured sessions"
    )
    ordered = sorted(points)
    empty = {
        "value": None,
        "unit": "ratio",
        "keyword": keyword,
        "geo": geo,
        "basis": basis,
        "source": None,
    }
    if len(ordered) < TRENDS_YOY_WEEKS * 2:
        empty["reason"] = reason or (
            f"a year-over-year comparison needs {TRENDS_YOY_WEEKS * 2} weekly observations; "
            f"Google Trends returned {len(ordered)}"
        )
        return empty
    recent = [value for _, value in ordered[-TRENDS_YOY_WEEKS:]]
    anchor = ordered[-1][0] - timedelta(days=365)
    prior_window = [
        value
        for ts, value in ordered
        if anchor - timedelta(weeks=TRENDS_YOY_WEEKS) < ts <= anchor
    ]
    if len(prior_window) < max(4, TRENDS_YOY_WEEKS // 2):
        empty["reason"] = (
            f"only {len(prior_window)} observation(s) fall in the same window a year ago"
        )
        return empty
    base = sum(prior_window) / len(prior_window)
    if base <= 0:
        empty["reason"] = f"search interest for {keyword!r} was zero a year ago"
        return empty
    current = sum(recent) / len(recent)
    return {
        "value": current / base - 1.0,
        "unit": "ratio",
        "keyword": keyword,
        "geo": geo,
        "current_mean": current,
        "prior_mean": base,
        "weeks": TRENDS_YOY_WEEKS,
        "as_of": _iso(ordered[-1][0]),
        "basis": basis,
        "reason": None,
        "source": "trends",
    }


@lru_cache(maxsize=1)
def _analyzer() -> Any:
    """Return the process-wide VADER analyser (lexicon load is not free)."""
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

    return SentimentIntensityAnalyzer()


def score_sentiment(text: str) -> dict[str, float]:
    """Return VADER's polarity scores for ``text`` (compound in [-1, 1])."""
    return {k: float(v) for k, v in _analyzer().polarity_scores(text).items()}


def sentiment_label(compound: float) -> str:
    """Return VADER's standard label for a compound score."""
    if compound >= 0.05:
        return "positive"
    if compound <= -0.05:
        return "negative"
    return "neutral"


# ---------------------------------------------------------------------------
# statements -> normalised facts
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class PeriodFacts:
    """One reporting period with the line items the research page needs.

    Every field is ``None`` when the filing did not report it. ``label`` is the
    vendor's period label, kept verbatim so a figure can be traced back.
    """

    label: str
    period: date | None
    revenue: float | None = None
    cost_of_revenue: float | None = None
    gross_profit: float | None = None
    operating_income: float | None = None
    ebitda: float | None = None
    depreciation: float | None = None
    net_income: float | None = None
    pretax_income: float | None = None
    tax_provision: float | None = None
    interest_expense: float | None = None
    shares_diluted: float | None = None
    eps_diluted: float | None = None
    operating_cash_flow: float | None = None
    capex: float | None = None
    free_cash_flow: float | None = None
    depreciation_amortization: float | None = None
    stock_based_compensation: float | None = None
    dividends_paid: float | None = None
    cash: float | None = None
    cash_label: str | None = None
    short_term_investments: float | None = None
    total_debt: float | None = None
    long_term_debt: float | None = None
    current_debt: float | None = None
    equity: float | None = None
    total_assets: float | None = None
    invested_capital: float | None = None
    shares_outstanding: float | None = None

    # --- derived line items ------------------------------------------------
    @property
    def derived_gross_profit(self) -> float | None:
        """Gross profit as reported, else revenue less cost of revenue."""
        if self.gross_profit is not None:
            return self.gross_profit
        if self.revenue is None or self.cost_of_revenue is None:
            return None
        return self.revenue - self.cost_of_revenue

    @property
    def derived_ebitda(self) -> float | None:
        """EBITDA as reported, else operating income plus D&A."""
        if self.ebitda is not None:
            return self.ebitda
        da = self.depreciation_amortization if self.depreciation_amortization is not None else self.depreciation
        if self.operating_income is None or da is None:
            return None
        return self.operating_income + abs(da)

    @property
    def derived_fcf(self) -> float | None:
        """Free cash flow as reported, else operating cash flow less capex."""
        if self.free_cash_flow is not None:
            return self.free_cash_flow
        if self.operating_cash_flow is None or self.capex is None:
            return None
        return self.operating_cash_flow - abs(self.capex)

    @property
    def cash_and_investments(self) -> float | None:
        """Cash plus short-term investments, avoiding double counting."""
        if self.cash is None:
            return None
        if self.cash_label in _CASH_INCLUDES_INVESTMENTS:
            return self.cash
        if self.short_term_investments is None:
            return self.cash
        return self.cash + self.short_term_investments

    @property
    def derived_total_debt(self) -> float | None:
        """Total debt as reported, else long-term plus current debt."""
        if self.total_debt is not None:
            return self.total_debt
        parts = [p for p in (self.long_term_debt, self.current_debt) if p is not None]
        return sum(parts) if parts else None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of the period."""
        return {
            "period": self.label,
            "period_date": _iso(self.period),
            "revenue": self.revenue,
            "gross_profit": self.derived_gross_profit,
            "operating_income": self.operating_income,
            "ebitda": self.derived_ebitda,
            "net_income": self.net_income,
            "eps_diluted": self.eps_diluted,
            "operating_cash_flow": self.operating_cash_flow,
            "capex": self.capex,
            "free_cash_flow": self.derived_fcf,
            "cash_and_investments": self.cash_and_investments,
            "total_debt": self.derived_total_debt,
            "equity": self.equity,
            "total_assets": self.total_assets,
            "shares_diluted": self.shares_diluted,
        }


#: Attribute -> (alias table, statement key) used when building period facts.
_FIELD_SOURCES: Final[tuple[tuple[str, dict[str, tuple[str, ...]], str], ...]] = (
    ("income_statement", INCOME_ALIASES, "income"),
    ("balance_sheet", BALANCE_ALIASES, "balance"),
    ("cash_flow", CASHFLOW_ALIASES, "cash_flow"),
)


def build_period_facts(statements: dict[str, Any], frequency: str) -> list[PeriodFacts]:
    """Normalise a provider statement payload into :class:`PeriodFacts`.

    Args:
        statements: ``{"annual": {"income_statement": [...], ...}, "quarterly": ...}``
            as returned by ``YFinanceProvider.get_financial_statements``.
        frequency: ``"annual"`` or ``"quarterly"``.

    Returns:
        Periods sorted newest first. Periods present in only one of the three
        statements still appear, with the other statements' fields ``None``.
    """
    block = (statements or {}).get(frequency) or {}
    merged: dict[str, dict[str, Any]] = {}
    for statement_key, aliases, _kind in _FIELD_SOURCES:
        for record in block.get(statement_key) or []:
            label = _text(record.get("period"))
            if label is None:
                continue
            bucket = merged.setdefault(label, {"label": label, "period": _parse_period(label)})
            for attr, alias_list in aliases.items():
                value, used = _pick(record, alias_list)
                if value is None or bucket.get(attr) is not None:
                    continue
                bucket[attr] = value
                if attr == "cash":
                    bucket["cash_label"] = used

    facts = [PeriodFacts(**bucket) for bucket in merged.values()]
    facts.sort(key=lambda f: (f.period or date.min, f.label), reverse=True)
    return facts


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Metric:
    """One derived figure, with provenance or an explanation of its absence."""

    key: str
    label: str
    value: float | None
    unit: str
    basis: str | None = None
    reason: str | None = None
    inputs: dict[str, float | None] = field(default_factory=dict)

    @property
    def available(self) -> bool:
        """Whether the metric could be derived."""
        return self.value is not None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of the metric."""
        return {
            "key": self.key,
            "label": self.label,
            "value": self.value,
            "unit": self.unit,
            "basis": self.basis,
            "reason": self.reason,
            "inputs": dict(self.inputs),
        }


def _missing(key: str, label: str, unit: str, reason: str) -> Metric:
    """Return a null metric carrying the reason it could not be derived."""
    return Metric(key=key, label=label, value=None, unit=unit, reason=reason)


def _forward_pe(fundamentals: Fundamentals | None) -> Metric:
    """Return the vendor's forward P/E (price over consensus forward EPS).

    This one multiple cannot be derived from the filed statements - it needs
    the sell-side consensus earnings the vendor publishes - so it is passed
    through from the profile payload rather than recomputed, and its basis says
    so. Absent consensus, the metric is null with that reason.
    """
    key, label = "forward_pe", "Forward P / E"
    if fundamentals is None:
        return _missing(key, label, "x", "no company profile was returned by the vendor")
    raw = (fundamentals.extras or {}).get("forwardPE")
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return _missing(
            key, label, "x", "the vendor published no forward P/E (no consensus EPS)"
        )
    if not math.isfinite(value) or value <= 0:
        return _missing(
            key, label, "x", f"the vendor's forward P/E ({raw!r}) is not a usable multiple"
        )
    return Metric(
        key=key,
        label=label,
        value=value,
        unit="x",
        basis=f"{fundamentals.source} consensus forward P/E (price / forward EPS estimate)",
        inputs={"trailing_pe": _as_float((fundamentals.extras or {}).get("trailingPE"))},
    )


def _as_float(value: Any) -> float | None:
    """Coerce a vendor scalar to a float, or ``None`` when it is not numeric."""
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


@dataclass(frozen=True, slots=True)
class _Aggregate:
    """Result of a trailing-twelve-month / annual aggregation."""

    value: float | None
    basis: str | None
    reason: str | None
    periods: tuple[str, ...] = ()
    #: ``"quarters"`` when four quarters were summed, ``"annual"`` when a
    #: fiscal year was used, ``None`` when nothing could be aggregated. Growth
    #: rates only compare two aggregates of the same kind.
    kind: str | None = None


def _flow(facts: Sequence[PeriodFacts], attr: str) -> list[float | None]:
    """Return one flow line item across ``facts`` (newest first)."""
    derived = {
        "gross_profit": "derived_gross_profit",
        "ebitda": "derived_ebitda",
        "free_cash_flow": "derived_fcf",
    }
    name = derived.get(attr, attr)
    return [getattr(fact, name) for fact in facts]


def aggregate_ttm(
    quarterly: Sequence[PeriodFacts],
    annual: Sequence[PeriodFacts],
    attr: str,
    *,
    label: str,
    offset: int = 0,
) -> _Aggregate:
    """Sum four consecutive quarters of ``attr``, falling back to a fiscal year.

    Args:
        quarterly: quarterly periods, newest first.
        annual: annual periods, newest first.
        attr: the :class:`PeriodFacts` flow attribute to aggregate.
        label: human name of the line item, used in the reason text.
        offset: 0 for the trailing twelve months, 4 for the year before that.

    Returns:
        An :class:`_Aggregate`; ``value`` is ``None`` when neither four
        quarters nor the corresponding fiscal year report the line item.
    """
    window = list(quarterly)[offset : offset + 4]
    values = _flow(window, attr)
    if len(window) == 4 and all(v is not None for v in values):
        periods = tuple(f.label for f in window)
        return _Aggregate(
            value=float(sum(v for v in values if v is not None)),
            basis=f"sum of 4 quarters {window[-1].label}..{window[0].label}",
            reason=None,
            periods=periods,
            kind="quarters",
        )

    index = offset // 4
    annual_window = list(annual)[index : index + 1]
    if annual_window:
        annual_value = _flow(annual_window, attr)[0]
        if annual_value is not None:
            fiscal = annual_window[0].label
            return _Aggregate(
                value=float(annual_value),
                basis=f"fiscal year {fiscal} (fewer than 4 quarters report {label})",
                reason=None,
                periods=(fiscal,),
                kind="annual",
            )

    reported = sum(1 for v in values if v is not None)
    return _Aggregate(
        value=None,
        basis=None,
        reason=(
            f"{label} is not available: only {reported} of the 4 quarters and no "
            "usable fiscal year report it"
        ),
    )


def latest_balance(facts: Sequence[PeriodFacts], attr: str) -> tuple[float | None, str | None]:
    """Return the newest reported balance-sheet value and its period label."""
    for fact in facts:
        value = getattr(fact, attr)
        if value is not None:
            return float(value), fact.label
    return None, None


def _ratio(
    key: str,
    label: str,
    numerator: Metric,
    denominator: Metric,
    *,
    unit: str = "ratio",
    denominator_must_be_positive: bool = True,
) -> Metric:
    """Return ``numerator / denominator`` as a metric, or a reasoned null."""
    if numerator.value is None:
        return _missing(key, label, unit, numerator.reason or f"{numerator.label} is unavailable")
    if denominator.value is None:
        return _missing(
            key, label, unit, denominator.reason or f"{denominator.label} is unavailable"
        )
    if denominator.value == 0 or (denominator_must_be_positive and denominator.value < 0):
        return _missing(
            key,
            label,
            unit,
            f"{denominator.label} is {denominator.value:,.0f}; the ratio would be meaningless",
        )
    return Metric(
        key=key,
        label=label,
        value=numerator.value / denominator.value,
        unit=unit,
        basis=f"{numerator.label} / {denominator.label}",
        inputs={numerator.key: numerator.value, denominator.key: denominator.value},
    )


def reverse_dcf_growth(
    *,
    enterprise_value: float | None,
    free_cash_flow: float | None,
    discount_rate: float = REVERSE_DCF_DISCOUNT,
    terminal_growth: float = REVERSE_DCF_TERMINAL_GROWTH,
    years: int = REVERSE_DCF_YEARS,
) -> tuple[float | None, str]:
    """Solve for the FCF growth rate the current EV implies.

    A two-stage model: ``years`` of growth at ``g`` followed by a Gordon
    terminal value at ``terminal_growth``, discounted at ``discount_rate``.
    The solution is found by bisection on ``g`` in ``[-50%, +100%]``.

    Returns:
        ``(growth, basis)``; ``growth`` is ``None`` when the inputs are missing
        or no growth rate in the bracket reproduces the enterprise value (a
        loss-making company, or one already priced beyond the bracket).
    """
    if enterprise_value is None or free_cash_flow is None:
        return None, "enterprise value and trailing free cash flow are both required"
    if free_cash_flow <= 0:
        return None, "trailing free cash flow is not positive, so no growth rate can be implied"
    if enterprise_value <= 0:
        return None, "enterprise value is not positive"
    if discount_rate <= terminal_growth:
        return None, "the discount rate must exceed the terminal growth rate"

    def present_value(growth: float) -> float:
        """Discounted value of the two-stage FCF stream at ``growth``."""
        total = 0.0
        cash = free_cash_flow
        for year in range(1, years + 1):
            cash *= 1.0 + growth
            total += cash / (1.0 + discount_rate) ** year
        terminal = cash * (1.0 + terminal_growth) / (discount_rate - terminal_growth)
        return total + terminal / (1.0 + discount_rate) ** years

    low, high = -0.50, 1.00
    if present_value(low) > enterprise_value or present_value(high) < enterprise_value:
        return None, (
            "no growth rate between -50% and +100% reproduces the enterprise value "
            f"at a {discount_rate:.0%} discount rate"
        )
    for _ in range(200):
        mid = (low + high) / 2.0
        if present_value(mid) < enterprise_value:
            low = mid
        else:
            high = mid
    basis = (
        f"{years}-year two-stage DCF on trailing FCF, {discount_rate:.0%} discount rate, "
        f"{terminal_growth:.1%} terminal growth, solved for the growth rate"
    )
    return (low + high) / 2.0, basis


@dataclass(slots=True)
class ThesisItem:
    """One catalyst or risk, with the measurement that produced it.

    Attributes:
        label: the short phrase the Research thesis card shows.
        basis: the measurement behind it, always naming the numbers used.
        kind: ``"catalyst"`` or ``"risk"``.
        metric: the metric key (or payload field) the item was derived from.
    """

    label: str
    basis: str
    kind: str
    metric: str

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of the item."""
        return {"label": self.label, "basis": self.basis, "kind": self.kind, "metric": self.metric}


#: Thresholds the thesis rules fire on. Every one is a stated policy, not a
#: tuned parameter: they exist so the card says *why* something is listed.
THESIS_RULES: Final[dict[str, float]] = {
    "earnings_days": 90.0,
    "revision_pct": 0.01,
    "revenue_growth": 0.15,
    "net_cash_share": 0.10,
    "sentiment": 0.15,
    "dispersion": 0.15,
    "ev_to_sales": 10.0,
    "net_debt_to_ebitda": 3.0,
}


def _metric_value(metrics: dict[str, Metric], key: str) -> float | None:
    """Return a derived metric's value, or ``None`` when it is absent."""
    metric = metrics.get(key)
    return metric.value if metric is not None and metric.value is not None else None


def thesis_items(
    metrics: dict[str, Metric],
    *,
    consensus: dict[str, Any] | None = None,
    next_earnings: dict[str, Any] | None = None,
    sentiment: dict[str, Any] | None = None,
    warnings: Sequence[str] = (),
) -> list[ThesisItem]:
    """Derive the Research thesis card's CATALYSTS and KEY RISKS.

    The reference fills these two rows with editorialised prose. TradingLib has no
    licence to editorialise, so the rows are instead a digest of what the
    company's own numbers say: each entry fires from one measurement, and
    carries that measurement as its ``basis``. Nothing here is a forecast, a
    recommendation, or a sentence a model wrote.

    Args:
        metrics: the derived metrics from :func:`derive_metrics`.
        consensus: the estimates payload's ``consensus`` block, if fetched.
        next_earnings: the next scheduled earnings event, if known.
        sentiment: the news payload's ``aggregate`` block, if fetched.
        warnings: coverage warnings from the overview payload.

    Returns:
        Catalysts first, then risks, each in rule order.
    """
    catalysts: list[ThesisItem] = []
    risks: list[ThesisItem] = []
    cons = consensus or {}

    if next_earnings and next_earnings.get("days_away") is not None:
        days = float(next_earnings["days_away"])
        if days <= THESIS_RULES["earnings_days"]:
            estimate = next_earnings.get("eps_estimate")
            catalysts.append(
                ThesisItem(
                    label=f"Results in {int(days)}d",
                    basis=(
                        f"next scheduled earnings {str(next_earnings.get('date'))[:10]}"
                        + (f", consensus EPS {estimate:.2f}" if estimate is not None else "")
                        + f" ({next_earnings.get('source') or 'vendor'} calendar)"
                    ),
                    kind="catalyst",
                    metric="next_earnings",
                )
            )

    revision = cons.get("revision_30d")
    if isinstance(revision, (int, float)):
        if revision >= THESIS_RULES["revision_pct"]:
            catalysts.append(
                ThesisItem(
                    label="Estimates rising",
                    basis=f"mean FY EPS estimate moved {revision:+.1%} over 30 days",
                    kind="catalyst",
                    metric="revision_30d",
                )
            )
        elif revision <= -THESIS_RULES["revision_pct"]:
            risks.append(
                ThesisItem(
                    label="Estimates falling",
                    basis=f"mean FY EPS estimate moved {revision:+.1%} over 30 days",
                    kind="risk",
                    metric="revision_30d",
                )
            )

    growth = cons.get("fy_revenue_growth")
    if isinstance(growth, (int, float)) and growth >= THESIS_RULES["revenue_growth"]:
        catalysts.append(
            ThesisItem(
                label="Consensus growth",
                basis=(
                    f"consensus FY revenue growth {growth:+.1%}"
                    + (f" across {int(cons['analysts'])} analysts" if cons.get("analysts") else "")
                ),
                kind="catalyst",
                metric="fy_revenue_growth",
            )
        )

    ebitda_margin = _metric_value(metrics, "ebitda_margin")
    revenue = _metric_value(metrics, "revenue_ttm")
    prior_revenue = _metric_value(metrics, "revenue_prior_ttm")
    ebitda = _metric_value(metrics, "ebitda_ttm")
    if (
        ebitda_margin is not None
        and revenue
        and prior_revenue
        and ebitda is not None
        and revenue > prior_revenue
    ):
        # Operating leverage: EBITDA grew faster than the revenue that carries
        # it. Stated as the growth pair, not as a margin the prior period never
        # reported - the prior TTM window has no EBITDA of its own here.
        revenue_growth = revenue / prior_revenue - 1.0
        if revenue_growth >= THESIS_RULES["revenue_growth"]:
            catalysts.append(
                ThesisItem(
                    label="Revenue compounding",
                    basis=(
                        f"trailing revenue {revenue_growth:+.1%} year over year with a "
                        f"{ebitda_margin:.1%} EBITDA margin"
                    ),
                    kind="catalyst",
                    metric="revenue_yoy",
                )
            )

    net_cash = _metric_value(metrics, "net_cash")
    market_cap = _metric_value(metrics, "market_cap")
    if net_cash is not None and market_cap:
        share = net_cash / market_cap
        if share >= THESIS_RULES["net_cash_share"]:
            catalysts.append(
                ThesisItem(
                    label="Net cash balance sheet",
                    basis=f"net cash is {share:.0%} of market capitalisation",
                    kind="catalyst",
                    metric="net_cash",
                )
            )
        elif net_cash < 0:
            leverage = _metric_value(metrics, "net_debt_to_ebitda")
            if leverage is not None and leverage >= THESIS_RULES["net_debt_to_ebitda"]:
                risks.append(
                    ThesisItem(
                        label="Leverage",
                        basis=f"net debt is {leverage:.1f}x trailing EBITDA",
                        kind="risk",
                        metric="net_debt_to_ebitda",
                    )
                )

    fcf = _metric_value(metrics, "fcf_ttm")
    fcf_margin = _metric_value(metrics, "fcf_margin")
    if fcf is not None and fcf < 0:
        risks.append(
            ThesisItem(
                label="Cash burn",
                basis=(
                    f"trailing free cash flow {fcf / 1e6:,.0f}m"
                    + (f" ({fcf_margin:.1%} of revenue)" if fcf_margin is not None else "")
                ),
                kind="risk",
                metric="fcf_ttm",
            )
        )
    elif fcf_margin is not None and fcf_margin > 0:
        catalysts.append(
            ThesisItem(
                label="Free cash generative",
                basis=f"trailing free cash flow margin {fcf_margin:.1%}",
                kind="catalyst",
                metric="fcf_margin",
            )
        )

    ev_sales = _metric_value(metrics, "ev_to_sales")
    if ev_sales is not None and ev_sales >= THESIS_RULES["ev_to_sales"]:
        risks.append(
            ThesisItem(
                label="Valuation",
                basis=f"enterprise value is {ev_sales:.1f}x trailing revenue",
                kind="risk",
                metric="ev_to_sales",
            )
        )

    dispersion = cons.get("dispersion")
    if isinstance(dispersion, (int, float)) and dispersion >= THESIS_RULES["dispersion"]:
        risks.append(
            ThesisItem(
                label="Estimate dispersion",
                basis=f"analyst EPS range spans {dispersion:.0%} of the mean",
                kind="risk",
                metric="dispersion",
            )
        )

    compound = (sentiment or {}).get("recency_weighted")
    sample = (sentiment or {}).get("sample") or (sentiment or {}).get("count")
    if isinstance(compound, (int, float)):
        detail = f"recency-weighted news sentiment {compound:+.2f}" + (
            f" over {int(sample)} items" if sample else ""
        )
        if compound >= THESIS_RULES["sentiment"]:
            catalysts.append(
                ThesisItem(
                    label="Positive news flow", basis=detail, kind="catalyst", metric="sentiment"
                )
            )
        elif compound <= -THESIS_RULES["sentiment"]:
            risks.append(
                ThesisItem(
                    label="Negative news flow", basis=detail, kind="risk", metric="sentiment"
                )
            )

    for warning in warnings:
        risks.append(
            ThesisItem(
                label="Incomplete data",
                basis=str(warning),
                kind="risk",
                metric="coverage",
            )
        )
    return catalysts + risks


def derive_metrics(
    statements: dict[str, Any],
    *,
    fundamentals: Fundamentals | None = None,
    quote: Quote | None = None,
) -> dict[str, Metric]:
    """Derive the Stock Research snapshot metrics from real statements.

    The statements are the source of truth. ``fundamentals`` supplies the live
    market capitalisation (and is the fallback for enterprise value when the
    balance sheet is incomplete); ``quote`` supplies the price used when the
    vendor did not publish a market capitalisation.

    Returns:
        A mapping of metric key -> :class:`Metric`. Keys are always present;
        values may be ``None`` with a ``reason``.
    """
    quarterly = build_period_facts(statements, "quarterly")
    annual = build_period_facts(statements, "annual")
    out: dict[str, Metric] = {}

    aggregates: dict[str, _Aggregate] = {}

    def flow_metric(key: str, label: str, attr: str, *, offset: int = 0) -> Metric:
        """Aggregate one flow item into a metric."""
        agg = aggregate_ttm(quarterly, annual, attr, label=label, offset=offset)
        aggregates[key] = agg
        if agg.value is None:
            return _missing(key, label, "usd", agg.reason or f"{label} is unavailable")
        return Metric(
            key=key,
            label=label,
            value=agg.value,
            unit="usd",
            basis=agg.basis,
            inputs={"periods_summed": float(len(agg.periods))},
        )

    out["revenue_ttm"] = flow_metric("revenue_ttm", "Revenue (TTM)", "revenue")
    out["revenue_prior_ttm"] = flow_metric(
        "revenue_prior_ttm", "Revenue (prior TTM)", "revenue", offset=4
    )
    out["gross_profit_ttm"] = flow_metric("gross_profit_ttm", "Gross profit (TTM)", "gross_profit")
    out["ebitda_ttm"] = flow_metric("ebitda_ttm", "EBITDA (TTM)", "ebitda")
    out["net_income_ttm"] = flow_metric("net_income_ttm", "Net income (TTM)", "net_income")
    out["fcf_ttm"] = flow_metric("fcf_ttm", "Free cash flow (TTM)", "free_cash_flow")
    out["operating_income_ttm"] = flow_metric(
        "operating_income_ttm", "Operating income (TTM)", "operating_income"
    )

    # --- growth -----------------------------------------------------------
    # A growth rate is only meaningful between two windows of the same kind:
    # trailing twelve months against the twelve months before it, or one fiscal
    # year against the one before it. Comparing a TTM against a fiscal year
    # (which happens when the vendor publishes seven quarters) would silently
    # mix a rolling window with a calendar one, so it is refused.
    current = out["revenue_ttm"]
    prior = out["revenue_prior_ttm"]
    growth_pair: tuple[float, float, str] | None = None
    if (
        current.value is not None
        and prior.value is not None
        and aggregates["revenue_ttm"].kind == aggregates["revenue_prior_ttm"].kind
    ):
        growth_pair = (current.value, prior.value, f"{current.basis} vs {prior.basis}")
    else:
        annual_revenue = [(fact.label, fact.revenue) for fact in annual if fact.revenue is not None]
        if len(annual_revenue) >= 2:
            growth_pair = (
                float(annual_revenue[0][1]),
                float(annual_revenue[1][1]),
                f"fiscal year {annual_revenue[0][0]} vs {annual_revenue[1][0]}",
            )

    if growth_pair is None:
        reason = (
            current.reason
            or "the prior-year revenue needed for a growth rate on the same basis is not published"
        )
        out["revenue_yoy"] = _missing("revenue_yoy", "Revenue YoY", "ratio", reason)
    elif growth_pair[1] <= 0:
        out["revenue_yoy"] = _missing(
            "revenue_yoy",
            "Revenue YoY",
            "ratio",
            "prior-year revenue is not positive, so a growth rate is meaningless",
        )
    else:
        newer, older, basis = growth_pair
        out["revenue_yoy"] = Metric(
            key="revenue_yoy",
            label="Revenue YoY",
            value=newer / older - 1.0,
            unit="ratio",
            basis=basis,
            inputs={"revenue_current": newer, "revenue_prior": older},
        )

    # --- margins ----------------------------------------------------------
    out["gross_margin"] = _ratio(
        "gross_margin", "Gross margin", out["gross_profit_ttm"], out["revenue_ttm"]
    )
    out["ebitda_margin"] = _ratio(
        "ebitda_margin", "EBITDA margin", out["ebitda_ttm"], out["revenue_ttm"]
    )
    out["fcf_margin"] = _ratio("fcf_margin", "FCF margin", out["fcf_ttm"], out["revenue_ttm"])
    out["net_margin"] = _ratio("net_margin", "Net margin", out["net_income_ttm"], out["revenue_ttm"])

    # --- balance sheet ----------------------------------------------------
    balance_source = quarterly or annual
    cash_value, cash_period = latest_balance(balance_source, "cash_and_investments")
    debt_value, debt_period = latest_balance(balance_source, "derived_total_debt")
    equity_value, equity_period = latest_balance(balance_source, "equity")

    out["cash_and_investments"] = (
        Metric(
            key="cash_and_investments",
            label="Cash & short-term investments",
            value=cash_value,
            unit="usd",
            basis=f"balance sheet {cash_period}",
        )
        if cash_value is not None
        else _missing(
            "cash_and_investments",
            "Cash & short-term investments",
            "usd",
            "no balance sheet in the vendor payload reports cash",
        )
    )
    out["total_debt"] = (
        Metric(
            key="total_debt",
            label="Total debt",
            value=debt_value,
            unit="usd",
            basis=f"balance sheet {debt_period}",
        )
        if debt_value is not None
        else _missing(
            "total_debt",
            "Total debt",
            "usd",
            "no balance sheet in the vendor payload reports debt",
        )
    )
    out["equity"] = (
        Metric(
            key="equity",
            label="Shareholders' equity",
            value=equity_value,
            unit="usd",
            basis=f"balance sheet {equity_period}",
        )
        if equity_value is not None
        else _missing(
            "equity", "Shareholders' equity", "usd", "no balance sheet reports shareholders' equity"
        )
    )

    if cash_value is None or debt_value is None:
        out["net_cash"] = _missing(
            "net_cash",
            "Net cash",
            "usd",
            "net cash needs both cash and total debt from the same balance sheet",
        )
    else:
        out["net_cash"] = Metric(
            key="net_cash",
            label="Net cash",
            value=cash_value - debt_value,
            unit="usd",
            basis=f"cash & investments less total debt, balance sheet {cash_period}",
            inputs={"cash_and_investments": cash_value, "total_debt": debt_value},
        )

    # --- market figures ---------------------------------------------------
    market_cap = _num(fundamentals.market_cap) if fundamentals else None
    market_cap_basis = "vendor market capitalisation"
    if market_cap is None:
        shares = _num(fundamentals.shares_outstanding) if fundamentals else None
        if shares is None:
            shares, _ = latest_balance(balance_source, "shares_outstanding")
        price = _num(quote.price) if quote else None
        if shares is not None and price is not None:
            market_cap = shares * price
            market_cap_basis = "shares outstanding x last price"
    out["market_cap"] = (
        Metric(
            key="market_cap",
            label="Market cap",
            value=market_cap,
            unit="usd",
            basis=market_cap_basis,
        )
        if market_cap is not None
        else _missing(
            "market_cap",
            "Market cap",
            "usd",
            "neither a vendor market capitalisation nor shares x price is available",
        )
    )

    if market_cap is not None and cash_value is not None and debt_value is not None:
        out["enterprise_value"] = Metric(
            key="enterprise_value",
            label="Enterprise value",
            value=market_cap + debt_value - cash_value,
            unit="usd",
            basis="market cap + total debt - cash & investments",
            inputs={
                "market_cap": market_cap,
                "total_debt": debt_value,
                "cash_and_investments": cash_value,
            },
        )
    else:
        vendor_ev = _num(fundamentals.enterprise_value) if fundamentals else None
        out["enterprise_value"] = (
            Metric(
                key="enterprise_value",
                label="Enterprise value",
                value=vendor_ev,
                unit="usd",
                basis="vendor enterprise value (balance-sheet inputs incomplete)",
            )
            if vendor_ev is not None
            else _missing(
                "enterprise_value",
                "Enterprise value",
                "usd",
                "market cap, debt or cash is missing and the vendor published no enterprise value",
            )
        )

    out["ev_to_sales"] = _ratio(
        "ev_to_sales",
        "EV / Sales",
        out["enterprise_value"],
        out["revenue_ttm"],
        unit="x",
    )
    out["ev_to_ebitda"] = _ratio(
        "ev_to_ebitda",
        "EV / EBITDA",
        out["enterprise_value"],
        out["ebitda_ttm"],
        unit="x",
    )
    out["price_to_earnings"] = _ratio(
        "price_to_earnings",
        "P / E",
        out["market_cap"],
        out["net_income_ttm"],
        unit="x",
    )
    out["forward_pe"] = _forward_pe(fundamentals)
    out["price_to_book"] = _ratio(
        "price_to_book", "P / B", out["market_cap"], out["equity"], unit="x"
    )
    out["fcf_yield"] = _ratio(
        "fcf_yield",
        "FCF yield",
        out["fcf_ttm"],
        out["market_cap"],
        unit="ratio",
    )

    # --- returns ----------------------------------------------------------
    out["return_on_equity"] = _ratio(
        "return_on_equity", "Return on equity", out["net_income_ttm"], out["equity"]
    )
    invested, invested_period = latest_balance(balance_source, "invested_capital")
    nopat = out["operating_income_ttm"]
    if nopat.value is None or invested is None or invested <= 0:
        out["return_on_invested_capital"] = _missing(
            "return_on_invested_capital",
            "Return on invested capital",
            "ratio",
            "ROIC needs trailing operating income and a positive invested-capital balance",
        )
    else:
        out["return_on_invested_capital"] = Metric(
            key="return_on_invested_capital",
            label="Return on invested capital",
            value=nopat.value / invested,
            unit="ratio",
            basis=(
                "trailing operating income / invested capital "
                f"(balance sheet {invested_period}); no tax adjustment is applied"
            ),
            inputs={"operating_income_ttm": nopat.value, "invested_capital": invested},
        )

    if out["ebitda_ttm"].value is None or out["ebitda_ttm"].value <= 0 or debt_value is None:
        out["net_debt_to_ebitda"] = _missing(
            "net_debt_to_ebitda",
            "Net debt / EBITDA",
            "x",
            "net debt / EBITDA needs total debt and positive trailing EBITDA",
        )
    else:
        net_debt = debt_value - (cash_value or 0.0)
        out["net_debt_to_ebitda"] = Metric(
            key="net_debt_to_ebitda",
            label="Net debt / EBITDA",
            value=net_debt / out["ebitda_ttm"].value,
            unit="x",
            basis=(
                "(total debt - cash & investments) / trailing EBITDA"
                if cash_value is not None
                else "total debt / trailing EBITDA (cash not reported)"
            ),
            inputs={"net_debt": net_debt, "ebitda_ttm": out["ebitda_ttm"].value},
        )

    growth, dcf_basis = reverse_dcf_growth(
        enterprise_value=out["enterprise_value"].value,
        free_cash_flow=out["fcf_ttm"].value,
    )
    out["reverse_dcf_growth"] = (
        Metric(
            key="reverse_dcf_growth",
            label="Reverse-DCF implied growth",
            value=growth,
            unit="ratio",
            basis=dcf_basis,
        )
        if growth is not None
        else _missing(
            "reverse_dcf_growth", "Reverse-DCF implied growth", "ratio", dcf_basis
        )
    )
    return out


# ---------------------------------------------------------------------------
# freshness + page payload types
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class Freshness:
    """How current a section's underlying data is."""

    component: str
    source: str
    fetched_at: datetime | None
    age_seconds: float | None
    stale: bool
    max_age_seconds: float | None = None
    detail: str | None = None

    @classmethod
    def from_entry(
        cls, component: str, entry: CacheEntry | None, *, source: str, detail: str | None = None
    ) -> Freshness:
        """Build freshness from the cache entry a fetch produced."""
        max_age = MAX_AGE.get(component)
        if entry is None:
            return cls(
                component=component,
                source=source,
                fetched_at=None,
                age_seconds=None,
                stale=True,
                max_age_seconds=max_age,
                detail=detail or "not fetched",
            )
        age = entry.age_seconds
        return cls(
            component=component,
            source=source,
            fetched_at=entry.fetched_at,
            age_seconds=age,
            stale=bool(max_age is not None and age > max_age),
            max_age_seconds=max_age,
            detail=detail,
        )

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of the freshness record."""
        return {
            "component": self.component,
            "source": self.source,
            "fetched_at": _iso(self.fetched_at),
            "age_seconds": self.age_seconds,
            "stale": self.stale,
            "max_age_seconds": self.max_age_seconds,
            "detail": self.detail,
        }


@dataclass(slots=True)
class CompanyProfile:
    """Identity block of the Stock Research header."""

    symbol: str
    name: str | None
    exchange: str | None
    sector: str | None
    industry: str | None
    country: str | None
    region: str | None
    currency: str | None
    quote_type: str | None = None
    website: str | None = None
    employees: int | None = None
    summary: str | None = None

    @property
    def meta_line(self) -> str:
        """The header's second line: ``NASDAQ · Technology · Southeast Asia``."""
        parts = [p for p in (self.exchange, self.sector, self.region or self.country) if p]
        return " · ".join(parts)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of the profile."""
        return {
            "symbol": self.symbol,
            "name": self.name,
            "exchange": self.exchange,
            "sector": self.sector,
            "industry": self.industry,
            "country": self.country,
            "region": self.region,
            "currency": self.currency,
            "quote_type": self.quote_type,
            "website": self.website,
            "employees": self.employees,
            "summary": self.summary,
            "meta_line": self.meta_line,
        }


@dataclass(slots=True)
class CompanyOverview:
    """Header + snapshot payload for the Overview tab."""

    symbol: str
    profile: CompanyProfile
    quote: Quote | None
    metrics: dict[str, Metric] = field(default_factory=dict)
    next_earnings: dict[str, Any] | None = None
    revenue_ebitda: list[dict[str, Any]] = field(default_factory=list)
    freshness: dict[str, Freshness] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON payload the Stock Research page renders."""
        return {
            "symbol": self.symbol,
            "profile": self.profile.as_dict(),
            "quote": self.quote.as_dict() if self.quote else None,
            "metrics": {key: metric.as_dict() for key, metric in self.metrics.items()},
            "next_earnings": self.next_earnings,
            "revenue_ebitda": self.revenue_ebitda,
            "freshness": {key: value.as_dict() for key, value in self.freshness.items()},
            "warnings": list(self.warnings),
        }


def _collection_name(registry: UniverseRegistry, ref: str) -> str | None:
    """Display name of a universe or watchlist id, or ``None`` when unknown."""
    plain = ref.split(":", 1)[1] if ":" in ref else ref
    try:
        return registry.get(plain).name
    except KeyError:
        pass
    try:
        return registry.watchlists.get(plain).name
    except KeyError:
        return None


@dataclass(slots=True, frozen=True)
class FundamentalsCoverage:
    """How much of a universe TradingLib actually holds fundamentals for.

    "Covered" is deliberately strict: a symbol counts only when the vendor
    returned a snapshot carrying the figures the Fundamentals panel renders (a
    market cap *and* trailing revenue). A snapshot that exists but is empty is a
    gap, and is reported as one.
    """

    universe_id: str | None
    universe_name: str | None
    covered: int
    total: int
    missing: list[str]
    measured_at: datetime
    duration_s: float

    @property
    def pct(self) -> float | None:
        """Covered share of the universe, or ``None`` for an empty universe."""
        if not self.total:
            return None
        return 100.0 * self.covered / self.total

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping for the API layer."""
        return {
            "universe_id": self.universe_id,
            "universe_name": self.universe_name,
            "covered": self.covered,
            "total": self.total,
            "pct": self.pct,
            # Capped: the datastrip wants the count and a few examples, not a
            # 200-symbol list on every poll.
            "missing": list(self.missing[:20]),
            "missing_total": len(self.missing),
            "measured_at": self.measured_at.isoformat(),
            "duration_s": round(self.duration_s, 3),
        }


class _StatementCapableProvider(Protocol):
    """The extra surface ``CompanyResearch`` needs beyond :class:`DataProvider`."""

    async def get_financial_statements(self, symbol: str) -> dict[str, Any]:
        """Return income statement, balance sheet and cash-flow records."""
        ...

    async def get_estimates(self, symbol: str) -> dict[str, Any]:
        """Return analyst estimates, recommendations and earnings dates."""
        ...


# ---------------------------------------------------------------------------
# the engine
# ---------------------------------------------------------------------------
class CompanyResearch:
    """Builds every Stock Research tab payload for any ticker.

    All vendor access goes through injected providers and the shared
    :class:`~tradinglib.data.cache.DataCache`, so a page render costs at most one
    request per component per TTL, and the fetch timestamps the cache records
    are what the page reports as data freshness.
    """

    def __init__(
        self,
        *,
        market: DataProvider | None = None,
        edgar: DataProvider | None = None,
        trends: Any | None = None,
        jobs: Any | None = None,
        cache: DataCache | None = None,
        store: TimeSeriesStore | None = None,
        universes: UniverseRegistry | None = None,
        peer_candidates: int = 12,
        peer_count: int = 5,
    ) -> None:
        """Create the engine.

        Args:
            market: quotes/bars/fundamentals/statements provider (yfinance by
                default; built lazily so constructing the engine is cheap).
            edgar: SEC EDGAR provider used for filings and symbol resolution.
            trends: Google Trends provider used for alternative signals.
            jobs: public job-board provider behind the job-postings tile.
            cache: shared data cache; defaults to the process-wide one.
            store: time-series store used to serve and persist price history.
            universes: registry used to pick peers from the configured
                universes.
            peer_candidates: how many same-sector names to price before
                ranking them by market-cap proximity.
            peer_count: how many peers the valuation tab shows.
        """
        self._market = market
        self._edgar = edgar
        self._trends = trends
        self._jobs = jobs
        self._cache = cache
        self._store = store
        self._universes = universes
        self.peer_candidates = max(1, peer_candidates)
        self.peer_count = max(1, peer_count)
        self._peer_lock = asyncio.Semaphore(4)
        # Fundamentals coverage is measured in the background: it costs one
        # provider call per universe member, which must never sit in the path
        # of a datastrip poll. Readers get the last measurement (or None until
        # the first one lands) and a refresh is scheduled when it goes stale.
        self._coverage: FundamentalsCoverage | None = None
        self._coverage_task: asyncio.Task[FundamentalsCoverage] | None = None

    # --- lazily built collaborators ---------------------------------------
    @property
    def market(self) -> DataProvider:
        """The market-data provider (yfinance unless one was injected)."""
        if self._market is None:
            from tradinglib.data.providers import build_provider

            self._market = build_provider("yfinance")
        return self._market

    @property
    def edgar(self) -> DataProvider:
        """The EDGAR provider (built on first use)."""
        if self._edgar is None:
            from tradinglib.data.providers import build_provider

            self._edgar = build_provider("edgar")
        return self._edgar

    @property
    def trends(self) -> Any:
        """The Google Trends provider (built on first use)."""
        if self._trends is None:
            from tradinglib.data.providers import build_provider

            self._trends = build_provider("trends")
        return self._trends

    @property
    def jobs(self) -> Any:
        """The public job-board provider (built on first use)."""
        if self._jobs is None:
            from tradinglib.data.providers import build_provider

            self._jobs = build_provider("jobs")
        return self._jobs

    @property
    def cache(self) -> DataCache:
        """The shared data cache."""
        if self._cache is None:
            self._cache = get_cache()
        return self._cache

    @property
    def store(self) -> TimeSeriesStore:
        """The time-series store."""
        if self._store is None:
            self._store = get_store()
        return self._store

    @property
    def universes(self) -> UniverseRegistry:
        """The universe registry used for peer selection."""
        if self._universes is None:
            self._universes = get_universe_registry()
        return self._universes

    # --- cached fetch helpers ---------------------------------------------
    async def _fetch(
        self, component: str, key: str, coro_factory: Any, *, source: str
    ) -> tuple[Any, CacheEntry | None, ProviderError | None]:
        """Fetch through the cache, returning ``(value, entry, error)``.

        Provider failures are returned, not raised: one dead component must not
        take the whole page down, and the caller can tell an unknown ticker
        (:class:`SymbolNotFound`) apart from a vendor outage - which decides
        whether the page is a 404 or a degraded 200. Non-provider exceptions
        are wrapped so callers only ever deal with the provider hierarchy.
        """
        try:
            entry = await self.cache.get_or_fetch(
                key, coro_factory, ttl=TTL.get(component, 300.0), source=source
            )
        except ProviderError as exc:
            logger.info("%s unavailable: %s", component, exc.message)
            return None, None, exc
        except Exception as exc:  # noqa: BLE001 - a vendor SDK may raise anything
            logger.warning("%s failed: %s", component, exc, exc_info=True)
            return None, None, ProviderUnavailable(source, f"{type(exc).__name__}: {exc}")
        return entry.value, entry, None

    async def _quote(
        self, symbol: str
    ) -> tuple[Quote | None, CacheEntry | None, ProviderError | None]:
        """Fetch the latest quote through the cache."""
        return await self._fetch(
            "quote",
            provider_key(self.market.name, "quote", symbol),
            lambda: self.market.get_quote(symbol),
            source=self.market.name,
        )

    async def _fundamentals(
        self, symbol: str
    ) -> tuple[Fundamentals | None, CacheEntry | None, ProviderError | None]:
        """Fetch the fundamental snapshot through the cache."""
        return await self._fetch(
            "fundamentals",
            provider_key(self.market.name, "fundamentals", symbol),
            lambda: self.market.get_fundamentals(symbol),
            source=self.market.name,
        )

    # --- fundamentals coverage --------------------------------------------
    @staticmethod
    def _fundamentals_usable(snapshot: Fundamentals | None) -> bool:
        """Whether a snapshot carries the figures the Fundamentals panel needs."""
        if snapshot is None:
            return False
        return snapshot.market_cap is not None and snapshot.revenue_ttm is not None

    async def measure_fundamentals_coverage(
        self, universe_id: str | None = None, *, concurrency: int = 6
    ) -> FundamentalsCoverage:
        """Count how many members of a universe have usable fundamentals.

        Every member is resolved through the ordinary cached fetch path, so a
        measurement costs at most one vendor call per symbol per
        ``TTL["fundamentals"]`` and warms the cache the Stock Research page
        reads afterwards.

        Args:
            universe_id: universe to measure; defaults to the active one.
            concurrency: how many symbols to resolve at once. Kept low because
                the default vendor rate-limits aggressively.

        Returns:
            The measurement. An unknown or empty universe yields a zero total
            rather than an error - "nothing to cover" is a real answer.
        """
        started = time.monotonic()
        registry = self.universes
        name: str | None = None
        raw_symbols: list[str] = []
        if universe_id:
            resolved_id = universe_id
            try:
                raw_symbols = await asyncio.to_thread(registry.resolve, universe_id)
                name = await asyncio.to_thread(_collection_name, registry, universe_id)
            except KeyError:
                logger.info("fundamentals coverage: unknown collection %r", universe_id)
        else:
            # No explicit collection: measure whatever the topbar has selected,
            # which may be a watchlist rather than a universe.
            scope = await asyncio.to_thread(registry.active_scope)
            resolved_id = scope["id"]
            name = scope["name"]
            raw_symbols = list(scope["symbols"])
        symbols: list[str] = []
        for raw in raw_symbols:
            symbol = normalize_symbol(str(raw))
            if symbol and symbol not in symbols:
                symbols.append(symbol)

        gate = asyncio.Semaphore(max(1, concurrency))

        async def covered(symbol: str) -> tuple[str, bool]:
            async with gate:
                snapshot, _entry, error = await self._fundamentals(symbol)
            if error is not None:
                logger.debug("fundamentals coverage: %s unavailable: %s", symbol, error)
            return symbol, self._fundamentals_usable(snapshot)

        results = await asyncio.gather(*(covered(symbol) for symbol in symbols))
        missing = sorted(symbol for symbol, ok in results if not ok)
        coverage = FundamentalsCoverage(
            universe_id=resolved_id,
            universe_name=name,
            covered=len(symbols) - len(missing),
            total=len(symbols),
            missing=missing,
            measured_at=utcnow(),
            duration_s=time.monotonic() - started,
        )
        self._coverage = coverage
        logger.info(
            "fundamentals coverage for %s: %d/%d in %.1fs",
            resolved_id or "(no active universe)",
            coverage.covered,
            coverage.total,
            coverage.duration_s,
        )
        return coverage

    def fundamentals_coverage(
        self, universe_id: str | None = None, *, max_age_s: float = COVERAGE_TTL
    ) -> FundamentalsCoverage | None:
        """Return the last coverage measurement, refreshing it in the background.

        Never blocks: callers on the datastrip path get ``None`` until the first
        measurement completes, which the UI renders as "not measured yet" rather
        than as a number nobody counted.

        Args:
            universe_id: universe of interest; a change of universe invalidates
                the memo immediately.
            max_age_s: how old a measurement may be before a refresh is queued.
        """
        current = self._coverage
        fresh = (
            current is not None
            and (universe_id is None or current.universe_id == universe_id)
            and (utcnow() - current.measured_at).total_seconds() < max_age_s
        )
        if not fresh:
            self._schedule_coverage(universe_id)
        if current is not None and universe_id is not None and current.universe_id != universe_id:
            # A stale measurement of a *different* universe would be a wrong
            # answer, not an old one.
            return None
        return current

    def _schedule_coverage(self, universe_id: str | None) -> None:
        """Start a background coverage measurement unless one is already running."""
        if self._coverage_task is not None and not self._coverage_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Synchronous caller (CLI, tests): measuring is the caller's job.
            return

        async def run() -> FundamentalsCoverage:
            try:
                return await self.measure_fundamentals_coverage(universe_id)
            except Exception:
                logger.warning("fundamentals coverage measurement failed", exc_info=True)
                raise

        self._coverage_task = loop.create_task(run(), name="fundamentals-coverage")
        # A task nobody awaits must still not raise "exception was never
        # retrieved" into the log at GC time.
        self._coverage_task.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)

    async def _statements(
        self, symbol: str
    ) -> tuple[dict[str, Any] | None, CacheEntry | None, ProviderError | None]:
        """Fetch financial statements through the cache."""
        provider = self.market
        if not hasattr(provider, "get_financial_statements"):
            return None, None, ProviderUnavailable(
                provider.name, "this provider does not publish financial statements"
            )
        typed: _StatementCapableProvider = provider  # type: ignore[assignment]
        return await self._fetch(
            "statements",
            provider_key(provider.name, "statements", symbol),
            lambda: typed.get_financial_statements(symbol),
            source=provider.name,
        )

    async def _estimates(
        self, symbol: str
    ) -> tuple[dict[str, Any] | None, CacheEntry | None, ProviderError | None]:
        """Fetch analyst estimates through the cache."""
        provider = self.market
        if not hasattr(provider, "get_estimates"):
            return None, None, ProviderUnavailable(
                provider.name, "this provider does not publish analyst estimates"
            )
        typed: _StatementCapableProvider = provider  # type: ignore[assignment]
        return await self._fetch(
            "estimates",
            provider_key(provider.name, "estimates", symbol),
            lambda: typed.get_estimates(symbol),
            source=provider.name,
        )

    async def _news_items(
        self, symbol: str, limit: int
    ) -> tuple[list[NewsItem] | None, CacheEntry | None, ProviderError | None]:
        """Fetch raw news items through the cache."""
        return await self._fetch(
            "news",
            provider_key(self.market.name, "news", symbol, limit=limit),
            lambda: self.market.get_news(symbol, limit=limit),
            source=self.market.name,
        )

    # --- symbol resolution -------------------------------------------------
    async def resolve(self, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
        """Resolve free text to candidate symbols.

        The reference UI only knew a hardcoded handful of tickers; this asks
        the vendor's symbol search and falls back to the EDGAR ticker index
        when Yahoo is unavailable, so any listed ticker loads.
        """
        text = query.strip()
        if not text:
            return []
        results, _entry, error = await self._fetch(
            "search",
            provider_key(self.market.name, "search", text.lower(), limit=limit),
            lambda: self.market.search(text, limit=limit),
            source=self.market.name,
        )
        candidates: list[dict[str, Any]] = list(results or [])
        if candidates:
            return candidates[:limit]

        edgar_results, _e, edgar_error = await self._fetch(
            "search",
            provider_key("edgar", "search", text.lower(), limit=limit),
            lambda: self.edgar.search(text, limit=limit),
            source="edgar",
        )
        if edgar_results:
            return list(edgar_results)[:limit]
        if error is not None or edgar_error is not None:
            logger.info(
                "symbol search for %r found nothing (%s / %s)",
                text,
                error.message if error else None,
                edgar_error.message if edgar_error else None,
            )
        return []

    async def profile(self, symbol: str) -> tuple[CompanyProfile, CacheEntry | None, str | None]:
        """Return the identity block, plus the cache entry behind it."""
        sym = normalize_symbol(symbol)
        fundamentals, entry, error = await self._fundamentals(sym)
        if fundamentals is None:
            return (
                CompanyProfile(
                    symbol=sym,
                    name=None,
                    exchange=None,
                    sector=None,
                    industry=None,
                    country=None,
                    region=None,
                    currency=None,
                ),
                entry,
                error,
            )
        extras = fundamentals.extras or {}
        country = _text(extras.get("country"))
        employees = _num(extras.get("fullTimeEmployees"))
        return (
            CompanyProfile(
                symbol=sym,
                name=fundamentals.name,
                exchange=_text(extras.get("exchange")),
                sector=fundamentals.sector,
                industry=fundamentals.industry,
                country=country,
                region=region_for(country),
                currency=fundamentals.currency,
                quote_type=_text(extras.get("quoteType")),
                website=_text(extras.get("website")),
                employees=int(employees) if employees is not None else None,
                summary=_text(extras.get("longBusinessSummary")),
            ),
            entry,
            error,
        )

    # --- price -------------------------------------------------------------
    @staticmethod
    def _range_window(range_key: str) -> tuple[BarInterval, datetime | None]:
        """Return the interval and start datetime for a chart range key."""
        key = (range_key or "1y").strip().lower()
        if key not in RANGES:
            raise ValueError(f"unknown chart range {range_key!r}; expected one of {sorted(RANGES)}")
        interval, span = RANGES[key]
        now = utcnow()
        if key == "ytd":
            return interval, datetime(now.year, 1, 1, tzinfo=timezone.utc)
        if span is None:
            return interval, None
        return interval, now - span

    async def _bars(
        self, symbol: str, interval: BarInterval, start: datetime | None
    ) -> tuple[list[Bar], CacheEntry | None, ProviderError | None, str]:
        """Return bars for a window, preferring the store and back-filling it.

        The store is authoritative when it already covers the window and its
        newest bar is fresh; otherwise the provider is asked (through the
        cache) and the result is written back so the next request is local.
        """
        stored = await asyncio.to_thread(
            self.store.read_bar_objects, symbol, interval=interval, start=start
        )
        if stored:
            newest = stored[-1].ts
            tolerance = timedelta(seconds=max(interval.approx_seconds * 3, 4 * 86_400))
            covers_start = start is None or stored[0].ts <= start + timedelta(
                seconds=interval.approx_seconds * 5
            )
            if covers_start and utcnow() - newest <= tolerance:
                return stored, None, None, "store"

        component = "bars_intraday" if interval.intraday else "bars_daily"
        bars, entry, error = await self._fetch(
            component,
            provider_key(
                self.market.name,
                "bars",
                symbol,
                interval=str(interval),
                start=start,
            ),
            lambda: self.market.get_bars(symbol, start=start, interval=interval),
            source=self.market.name,
        )
        if bars:
            written = await self.store.awrite_bars(list(bars), interval=interval)
            logger.debug("persisted %d %s bars for %s", written, interval, symbol)
            return list(bars), entry, None, self.market.name
        if stored:
            return stored, None, error, "store"
        return [], entry, error, self.market.name

    async def price(self, symbol: str, *, range: str = "1y") -> dict[str, Any]:
        """Return the OHLC series the lightweight-charts price chart renders."""
        sym = normalize_symbol(symbol)
        interval, start = self._range_window(range)
        bars, entry, error, source = await self._bars(sym, interval, start)
        points = [
            {
                "time": _iso(bar.ts),
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
            }
            for bar in bars
        ]
        first_close = bars[0].close if bars else None
        last_close = bars[-1].close if bars else None
        change_pct = (
            (last_close / first_close - 1.0) * 100.0
            if first_close not in (None, 0) and last_close is not None
            else None
        )
        freshness = Freshness.from_entry("price", entry, source=source)
        if entry is None and bars:
            # Served from the store: freshness is the newest stored bar.
            newest = bars[-1].ts
            age = (utcnow() - newest).total_seconds()
            freshness = Freshness(
                component="price",
                source=source,
                fetched_at=newest,
                age_seconds=age,
                stale=age > MAX_AGE["price"] and not interval.intraday,
                max_age_seconds=MAX_AGE["price"],
                detail="newest stored bar",
            )
        return {
            "symbol": sym,
            "range": range,
            "interval": str(interval),
            "source": source,
            "points": points,
            "count": len(points),
            "first_close": first_close,
            "last_close": last_close,
            "change_pct": change_pct,
            "error": error.message if error else None,
            "freshness": freshness.as_dict(),
        }

    # --- overview ----------------------------------------------------------
    @staticmethod
    def _next_earnings(estimates: dict[str, Any] | None) -> dict[str, Any] | None:
        """Return the next scheduled earnings date from the estimates payload."""
        if not estimates:
            return None
        now = utcnow()
        upcoming: list[tuple[datetime, dict[str, Any]]] = []
        for record in estimates.get("earnings_dates") or []:
            moment = _parse_datetime(record.get("date"))
            if moment is None or moment < now:
                continue
            upcoming.append((moment, record))
        if not upcoming:
            return None
        moment, record = min(upcoming, key=lambda item: item[0])
        return {
            "date": _iso(moment),
            "days_away": max(0, (moment - now).days),
            "eps_estimate": _num(record.get("EPS Estimate")),
            "source": estimates.get("source"),
            "basis": "vendor earnings calendar",
        }

    @staticmethod
    def _revenue_ebitda_bars(statements: dict[str, Any] | None) -> list[dict[str, Any]]:
        """Return the annual revenue/EBITDA pairs the overview bar chart draws."""
        if not statements:
            return []
        annual = build_period_facts(statements, "annual")
        rows = [
            {
                "period": fact.label,
                "year": fact.period.year if fact.period else None,
                "revenue": fact.revenue,
                "ebitda": fact.derived_ebitda,
                "free_cash_flow": fact.derived_fcf,
            }
            for fact in annual
            if fact.revenue is not None or fact.derived_ebitda is not None
        ]
        rows.sort(key=lambda row: row["period"])
        return rows

    async def overview(self, symbol: str) -> CompanyOverview:
        """Return the Overview tab payload with derived metrics and freshness."""
        sym = normalize_symbol(symbol)
        (profile, profile_entry, profile_error), quote_result, statement_result, estimate_result = (
            await asyncio.gather(
                self.profile(sym),
                self._quote(sym),
                self._statements(sym),
                self._estimates(sym),
            )
        )
        quote, quote_entry, quote_error = quote_result
        statements, statement_entry, statement_error = statement_result
        estimates, estimate_entry, estimate_error = estimate_result

        fundamentals, fundamental_entry, _f_err = await self._fundamentals(sym)
        metrics = derive_metrics(statements or {}, fundamentals=fundamentals, quote=quote)

        errors = [
            error
            for error in (profile_error, quote_error, statement_error, estimate_error)
            if error is not None
        ]
        warnings = [error.message for error in errors]
        if not statements:
            warnings.append(
                "no financial statements were returned, so every statement-derived metric is null"
            )
        if profile.name is None and quote is None:
            # Nothing identifies the ticker. If the vendor said "no such
            # symbol" that is a 404; if it merely failed (rate limit, outage)
            # the caller must not be told the company does not exist.
            detail = "; ".join(warnings) or "the provider returned no data"
            if any(isinstance(error, SymbolNotFound) for error in errors):
                raise SymbolNotFound("research", f"{sym}: {detail}")
            raise ProviderUnavailable("research", f"{sym}: {detail}")

        freshness = {
            "profile": Freshness.from_entry(
                "fundamentals", profile_entry, source=self.market.name
            ),
            "quote": Freshness.from_entry("quote", quote_entry, source=self.market.name),
            "fundamentals": Freshness.from_entry(
                "fundamentals", fundamental_entry, source=self.market.name
            ),
            "statements": Freshness.from_entry(
                "statements", statement_entry, source=self.market.name
            ),
            "estimates": Freshness.from_entry(
                "estimates", estimate_entry, source=self.market.name
            ),
        }
        return CompanyOverview(
            symbol=sym,
            profile=profile,
            quote=quote,
            metrics=metrics,
            next_earnings=self._next_earnings(estimates),
            revenue_ebitda=self._revenue_ebitda_bars(statements),
            freshness=freshness,
            warnings=warnings,
        )

    async def company(self, symbol: str) -> dict[str, Any]:
        """Return :meth:`overview` as the JSON payload of ``/api/research/company``."""
        return (await self.overview(symbol)).as_dict()

    # --- financials --------------------------------------------------------
    async def financials(self, symbol: str) -> dict[str, Any]:
        """Return income statement, balance sheet and cash-flow history.

        Alongside the raw vendor records (which the Financials tab's statement
        tables render verbatim) this returns a normalised annual and quarterly
        summary - revenue, gross profit, EBITDA, free cash flow and the margin
        progression - all derived from those same records.
        """
        sym = normalize_symbol(symbol)
        statements, entry, error = await self._statements(sym)
        annual = build_period_facts(statements or {}, "annual")
        quarterly = build_period_facts(statements or {}, "quarterly")

        def summary(facts: Sequence[PeriodFacts]) -> list[dict[str, Any]]:
            """Chronological summary rows with margins derived per period."""
            rows: list[dict[str, Any]] = []
            for fact in reversed(list(facts)):
                revenue = fact.revenue
                gross = fact.derived_gross_profit
                ebitda = fact.derived_ebitda
                fcf = fact.derived_fcf
                rows.append(
                    {
                        **fact.as_dict(),
                        "gross_margin": gross / revenue if revenue and gross is not None else None,
                        "ebitda_margin": (
                            ebitda / revenue if revenue and ebitda is not None else None
                        ),
                        "fcf_margin": fcf / revenue if revenue and fcf is not None else None,
                    }
                )
            return rows

        fundamentals, _entry, _err = await self._fundamentals(sym)
        return {
            "symbol": sym,
            "currency": fundamentals.currency if fundamentals else None,
            "source": (statements or {}).get("source", self.market.name),
            "error": error.message if error else None,
            "annual": summary(annual),
            "quarterly": summary(quarterly),
            "statements": (statements or {}).get("annual", {}),
            "quarterly_statements": (statements or {}).get("quarterly", {}),
            "freshness": Freshness.from_entry(
                "statements", entry, source=self.market.name
            ).as_dict(),
        }

    # --- earnings & estimates ---------------------------------------------
    async def _price_reaction(self, symbol: str, when: datetime) -> float | None:
        """Return the next session's percentage move after an earnings date.

        Uses stored/vendor daily bars: the close-to-close return of the first
        session that ends after the report. ``None`` when the bars needed are
        not available (a report older than the stored history, or one that has
        not traded yet).
        """
        start = when - timedelta(days=10)
        bars, _entry, _error, _source = await self._bars(symbol, BarInterval.DAY_1, start)
        if len(bars) < 2:
            return None
        for index in range(1, len(bars)):
            if bars[index].ts >= when:
                previous = bars[index - 1].close
                if not previous:
                    return None
                return (bars[index].close / previous - 1.0) * 100.0
        return None

    async def estimates(self, symbol: str) -> dict[str, Any]:
        """Return earnings history, analyst estimates and revisions.

        The "reaction" column is computed from real price bars around each
        report, not taken from the vendor.
        """
        sym = normalize_symbol(symbol)
        payload, entry, error = await self._estimates(sym)
        payload = payload or {}

        statements, _s_entry, _s_error = await self._statements(sym)
        fiscal_periods = [
            fact.period
            for fact in build_period_facts(statements or {}, "quarterly")
            if fact.period is not None
        ]

        history: list[dict[str, Any]] = []
        for record in payload.get("earnings_dates") or []:
            moment = _parse_datetime(record.get("date"))
            reported = _num(record.get("Reported EPS"))
            if moment is None or reported is None:
                continue
            estimate = _num(record.get("EPS Estimate"))
            surprise_pct = _num(record.get("Surprise(%)"))
            if surprise_pct is None and estimate not in (None, 0) and reported is not None:
                surprise_pct = (reported - estimate) / abs(estimate) * 100.0
            fiscal = _fiscal_period_for(moment, fiscal_periods)
            history.append(
                {
                    "date": _iso(moment),
                    # The reporting period the announcement covers, matched to
                    # the newest quarterly statement that ended before it.
                    # ``calendar_quarter`` is the calendar quarter that period
                    # ends in - NOT the issuer's own fiscal quarter number,
                    # which the vendor does not publish - and
                    # ``report_quarter`` is when the results were announced.
                    "fiscal_period": fiscal.isoformat() if fiscal else None,
                    "calendar_quarter": (
                        f"{fiscal.year} Q{(fiscal.month - 1) // 3 + 1}" if fiscal else None
                    ),
                    "report_quarter": f"{moment.year} Q{(moment.month - 1) // 3 + 1}",
                    "eps_estimate": estimate,
                    "eps_reported": reported,
                    "surprise_pct": surprise_pct,
                    "reaction_pct": None,
                }
            )
        history.sort(key=lambda row: row["date"], reverse=True)
        history = history[:8]
        reactions = await asyncio.gather(
            *(
                self._price_reaction(sym, datetime.fromisoformat(row["date"]))
                for row in history
            ),
            return_exceptions=True,
        )
        for row, reaction in zip(history, reactions, strict=True):
            row["reaction_pct"] = reaction if isinstance(reaction, float) else None

        consensus = _consensus_block(payload)
        return {
            "symbol": sym,
            "source": payload.get("source", self.market.name),
            "error": error.message if error else None,
            "history": history,
            "next_earnings": self._next_earnings(payload),
            "consensus": consensus,
            "earnings_estimate": payload.get("earnings_estimate") or [],
            "revenue_estimate": payload.get("revenue_estimate") or [],
            "eps_trend": payload.get("eps_trend") or [],
            "eps_revisions": payload.get("eps_revisions") or [],
            "growth_estimates": payload.get("growth_estimates") or [],
            "recommendations": payload.get("recommendations") or [],
            "upgrades_downgrades": (payload.get("upgrades_downgrades") or [])[:20],
            "price_targets": payload.get("price_targets"),
            "freshness": Freshness.from_entry(
                "estimates", entry, source=self.market.name
            ).as_dict(),
        }

    # --- valuation & peers -------------------------------------------------
    async def _peer_candidates(self, symbol: str, sector: str | None) -> list[str]:
        """Return same-sector candidate symbols drawn from the universes."""
        if sector is None:
            return []
        target = normalise_sector(sector)
        seen: dict[str, None] = {}
        try:
            universes = self.universes.list_universes(include_members=True)
        except Exception:  # noqa: BLE001 - peer selection must not break the tab
            logger.warning("could not read universes for peer selection", exc_info=True)
            return []
        # Active universe first, then the built-ins, so a user's own selection
        # decides the peer set when it can.
        universes.sort(key=lambda u: (not u.active, not u.builtin, u.id))
        for universe in universes:
            for member in universe.members:
                if member.symbol == symbol or member.removed_on is not None:
                    continue
                if normalise_sector(member.sector) != target:
                    continue
                seen.setdefault(member.symbol, None)
                if len(seen) >= self.peer_candidates:
                    return list(seen)
        return list(seen)

    async def _peer_row(self, symbol: str) -> dict[str, Any] | None:
        """Return one peer's comparative metrics, or ``None`` when unavailable."""
        async with self._peer_lock:
            fundamentals, _entry, _error = await self._fundamentals(symbol)
            statements, _s_entry, _s_error = await self._statements(symbol)
        if fundamentals is None and statements is None:
            return None
        metrics = derive_metrics(statements or {}, fundamentals=fundamentals)
        return {
            "symbol": symbol,
            "name": fundamentals.name if fundamentals else None,
            "sector": fundamentals.sector if fundamentals else None,
            "industry": fundamentals.industry if fundamentals else None,
            "market_cap": metrics["market_cap"].value,
            "revenue_ttm": metrics["revenue_ttm"].value,
            "revenue_yoy": metrics["revenue_yoy"].value,
            "gross_margin": metrics["gross_margin"].value,
            "fcf_margin": metrics["fcf_margin"].value,
            "ev_to_sales": metrics["ev_to_sales"].value,
            "ev_to_ebitda": metrics["ev_to_ebitda"].value,
        }

    async def valuation(self, symbol: str, *, peers: Sequence[str] | None = None) -> dict[str, Any]:
        """Return valuation multiples and a peer comparison table.

        Peers are same-sector members of the configured universes (the active
        universe first), priced and then ranked by market-cap proximity to the
        subject. When no configured universe holds a same-sector name, the peer
        table is empty and says so - it is never padded with unrelated tickers.
        """
        sym = normalize_symbol(symbol)
        (profile, _p_entry, _p_error), statement_result = await asyncio.gather(
            self.profile(sym), self._statements(sym)
        )
        statements, entry, error = statement_result
        fundamentals, _f_entry, _f_error = await self._fundamentals(sym)
        quote, _q_entry, _q_error = await self._quote(sym)
        metrics = derive_metrics(statements or {}, fundamentals=fundamentals, quote=quote)

        if peers is not None:
            candidates = [normalize_symbol(p) for p in peers if _text(p)]
            peer_basis = "explicit peer list"
        else:
            candidates = await self._peer_candidates(sym, profile.sector)
            peer_basis = (
                f"same-sector members of the configured universes (sector {profile.sector!r}), "
                "ranked by industry match then market-cap proximity"
                if candidates
                else "no configured universe holds another company in this sector"
            )

        rows = [row for row in await asyncio.gather(*(self._peer_row(p) for p in candidates)) if row]
        subject = {
            "symbol": sym,
            "name": profile.name,
            "sector": profile.sector,
            "industry": profile.industry,
            "market_cap": metrics["market_cap"].value,
            "revenue_ttm": metrics["revenue_ttm"].value,
            "revenue_yoy": metrics["revenue_yoy"].value,
            "gross_margin": metrics["gross_margin"].value,
            "fcf_margin": metrics["fcf_margin"].value,
            "ev_to_sales": metrics["ev_to_sales"].value,
            "ev_to_ebitda": metrics["ev_to_ebitda"].value,
            "is_subject": True,
        }
        # Closest comparables first: same industry beats same sector, then the
        # smallest distance in log market cap. Names whose market cap is
        # unknown sort last rather than being dropped silently.
        target_cap = subject["market_cap"]
        target_industry = _text(profile.industry)

        def closeness(row: dict[str, Any]) -> tuple[int, float]:
            """Rank key: industry match, then log market-cap distance."""
            same_industry = 0 if target_industry and row.get("industry") == target_industry else 1
            cap = row.get("market_cap")
            if not target_cap or not cap:
                return same_industry, float("inf")
            return same_industry, abs(math.log(cap / target_cap))

        rows.sort(key=closeness)
        rows = rows[: self.peer_count]

        multiples = {
            key: metrics[key].as_dict()
            for key in (
                "ev_to_sales",
                "ev_to_ebitda",
                "price_to_earnings",
                "forward_pe",
                "price_to_book",
                "fcf_yield",
                "reverse_dcf_growth",
                "market_cap",
                "enterprise_value",
                "net_cash",
            )
        }
        return {
            "symbol": sym,
            "error": error.message if error else None,
            "multiples": multiples,
            "peer_basis": peer_basis,
            "peers": [subject, *rows],
            "peer_medians": _medians(rows),
            "freshness": Freshness.from_entry(
                "statements", entry, source=self.market.name
            ).as_dict(),
        }

    # --- news --------------------------------------------------------------
    async def news(self, symbol: str, *, limit: int = 25) -> dict[str, Any]:
        """Return recent news with VADER sentiment scores and an aggregate.

        Each item is scored on its headline plus summary. The aggregate carries
        both the plain mean compound score and a recency-weighted composite
        (exponential decay, 3-day half-life), so a week-old rally does not read
        as today's sentiment.
        """
        sym = normalize_symbol(symbol)
        items, entry, error = await self._news_items(sym, limit)
        now = utcnow()
        scored: list[dict[str, Any]] = []
        weight_sum = 0.0
        weighted_total = 0.0
        for item in items or []:
            text = " ".join(part for part in (item.headline, item.summary) if part)
            scores = score_sentiment(text)
            compound = scores["compound"]
            age_hours = max(0.0, (now - to_utc(item.published_at)).total_seconds() / 3600.0)
            weight = 0.5 ** (age_hours / (SENTIMENT_HALF_LIFE_DAYS * 24.0))
            weight_sum += weight
            weighted_total += weight * compound
            payload = item.as_dict()
            payload.update(
                {
                    "sentiment": compound,
                    "sentiment_label": sentiment_label(compound),
                    "sentiment_scores": scores,
                    "age_hours": age_hours,
                }
            )
            scored.append(payload)

        counts = {"positive": 0, "neutral": 0, "negative": 0}
        for row in scored:
            counts[row["sentiment_label"]] += 1
        mean = sum(row["sentiment"] for row in scored) / len(scored) if scored else None
        composite = weighted_total / weight_sum if weight_sum else None
        return {
            "symbol": sym,
            "source": self.market.name,
            "error": error.message if error else None,
            "scorer": "vaderSentiment",
            "items": scored,
            "count": len(scored),
            "aggregate": {
                "mean_compound": mean,
                "recency_weighted": composite,
                "label": sentiment_label(composite) if composite is not None else None,
                "counts": counts,
                "half_life_days": SENTIMENT_HALF_LIFE_DAYS,
            },
            "freshness": Freshness.from_entry("news", entry, source=self.market.name).as_dict(),
        }

    async def thesis(self, symbol: str) -> dict[str, Any]:
        """Return the Research thesis card's measured catalysts and key risks.

        Composed from data already on the page - the derived metrics, the
        consensus block, the next scheduled earnings date and the news
        sentiment aggregate - so the two rows never say anything the rest of
        the tab does not also show. See :func:`thesis_items` for the rules.
        """
        sym = normalize_symbol(symbol)
        overview = await self.overview(sym)
        estimates, _entry, estimate_error = await self._estimates(sym)
        news = await self.news(sym, limit=25)
        consensus = _consensus_block(estimates) if estimates else None
        items = thesis_items(
            overview.metrics,
            consensus=consensus,
            next_earnings=overview.next_earnings,
            sentiment=news.get("aggregate"),
            warnings=overview.warnings,
        )
        return {
            "symbol": sym,
            "catalysts": [item.as_dict() for item in items if item.kind == "catalyst"],
            "risks": [item.as_dict() for item in items if item.kind == "risk"],
            "rules": dict(THESIS_RULES),
            "error": estimate_error.message if estimate_error else None,
            "basis": (
                "measured from this company's own filings, consensus and news - not written prose"
            ),
        }

    # --- alternative data --------------------------------------------------
    async def altdata(self, symbol: str, *, timeframe: str = "today 12-m") -> dict[str, Any]:
        """Return alternative-data signals (Google Trends, news sentiment).

        Four of the reference's tiles are served here:

        * *Google Trends* - the raw interest index for the brand keyword;
        * *App rank composite* - the percentile rank of today's interest within
          its own five-year distribution, and how that rank moved over 90 days.
          It is a search-interest rank, **not** an app-store chart position;
          every payload carries the ``basis`` string saying so;
        * *Web traffic YoY* - the trailing twelve weeks of search interest
          against the same twelve weeks a year earlier. Search interest is a
          proxy for site traffic, and the ``basis`` names it as one;
        * *News sentiment* - the recency-weighted VADER composite.

        * *Job postings 90d* - the share of a company's currently listed roles
          that were first published in the last ninety days, read from its
          public Greenhouse / Ashby / Lever board. A company that publishes no
          such board has no reading, and the payload says which slugs were
          tried rather than substituting a plausible number.
        """
        sym = normalize_symbol(symbol)
        (profile, _entry, _error) = await self.profile(sym)
        keyword = trends_keyword(profile.name, sym)
        geo = "US"

        series_map, trends_entry, trends_error = await self._fetch(
            "trends",
            provider_key("trends", "interest", keyword, timeframe=timeframe, geo=geo),
            lambda: self.trends.interest_over_time([keyword], timeframe=timeframe, geo=geo),
            source="trends",
        )
        series = (series_map or {}).get(keyword)
        points = [
            {"time": _iso(ts), "value": value} for ts, value in (series.points if series else [])
        ]
        latest = points[-1]["value"] if points else None
        change_90d: float | None = None
        if series and len(series.points) >= 2:
            cutoff = utcnow() - timedelta(days=90)
            baseline = [value for ts, value in series.points if ts <= cutoff]
            if baseline and baseline[-1]:
                change_90d = (series.points[-1][1] / baseline[-1] - 1.0) * 100.0

        long_series, long_entry, long_error = await self._fetch(
            "trends",
            provider_key("trends", "interest", keyword, timeframe=TRENDS_LONG_TIMEFRAME, geo=geo),
            lambda: self.trends.interest_over_time(
                [keyword], timeframe=TRENDS_LONG_TIMEFRAME, geo=geo
            ),
            source="trends",
        )
        long_points = ((long_series or {}).get(keyword) or Series(name=keyword)).points
        long_reason = long_error.message if long_error else None

        news_payload = await self.news(sym, limit=25)
        aggregate = news_payload["aggregate"]
        signals: dict[str, Any] = {
            "google_trends": {
                "value": latest,
                "keyword": keyword,
                "geo": geo,
                "timeframe": timeframe,
                "unit": "index 0-100 (Google-relative)",
                "change_90d_pct": change_90d,
                "points": points,
                "reason": (trends_error.message if trends_error else None)
                if latest is None
                else None,
                "source": "trends",
            },
            "news_sentiment": {
                "value": aggregate["recency_weighted"],
                "unit": "VADER compound, recency weighted",
                "label": aggregate["label"],
                "sample": news_payload["count"],
                "reason": news_payload.get("error") if aggregate["recency_weighted"] is None else None,
                "source": self.market.name,
            },
        }
        signals["app_rank_composite"] = interest_rank_composite(
            long_points, keyword=keyword, geo=geo, reason=long_reason
        )
        signals["web_traffic_yoy"] = interest_year_over_year(
            long_points, keyword=keyword, geo=geo, reason=long_reason
        )

        jobs, jobs_entry, jobs_error = await self._fetch(
            "jobs",
            provider_key("jobs", "postings", sym, name=profile.name or ""),
            lambda: self.jobs.get_postings(sym, company_name=profile.name),
            source="jobs",
        )
        signals["job_postings_90d"] = job_postings_signal(
            jobs, reason=jobs_error.message if jobs_error else None
        )
        for key, reason in UNSOURCED_ALT_SIGNALS.items():
            signals[key] = {"value": None, "reason": reason, "source": None}
        return {
            "symbol": sym,
            "signals": signals,
            "freshness": {
                "trends": Freshness.from_entry("trends", trends_entry, source="trends").as_dict(),
                "trends_long": Freshness.from_entry(
                    "trends", long_entry, source="trends"
                ).as_dict(),
                "news": news_payload["freshness"],
                "jobs": Freshness.from_entry("jobs", jobs_entry, source="jobs").as_dict(),
            },
        }

    # --- filings -----------------------------------------------------------
    async def filings(self, symbol: str, *, limit: int = 40) -> dict[str, Any]:
        """Return recent EDGAR filings for the Research tab."""
        sym = normalize_symbol(symbol)
        filings, entry, error = await self._fetch(
            "filings",
            provider_key("edgar", "filings", sym, limit=limit),
            lambda: self.edgar.get_filings(sym, limit=limit),  # type: ignore[attr-defined]
            source="edgar",
        )
        items: list[Filing] = list(filings or [])
        counts: dict[str, int] = {}
        for filing in items:
            counts[filing.form] = counts.get(filing.form, 0) + 1
        return {
            "symbol": sym,
            "source": "edgar",
            "error": error.message if error else None,
            "count": len(items),
            "form_counts": dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))),
            "items": [filing.as_dict() for filing in items],
            "freshness": Freshness.from_entry("filings", entry, source="edgar").as_dict(),
        }

    # --- freshness ---------------------------------------------------------
    async def freshness(self, symbol: str) -> dict[str, Any]:
        """Return the per-component data-freshness report for a symbol.

        Nothing is fetched here beyond what the cache already holds for the
        cheap components; the report describes the data the page would render
        right now.
        """
        sym = normalize_symbol(symbol)
        components = {
            "quote": (provider_key(self.market.name, "quote", sym), self.market.name),
            "fundamentals": (
                provider_key(self.market.name, "fundamentals", sym),
                self.market.name,
            ),
            "statements": (provider_key(self.market.name, "statements", sym), self.market.name),
            "estimates": (provider_key(self.market.name, "estimates", sym), self.market.name),
            "news": (provider_key(self.market.name, "news", sym, limit=25), self.market.name),
            "filings": (provider_key("edgar", "filings", sym, limit=40), "edgar"),
        }
        out: dict[str, Any] = {}
        for component, (key, source) in components.items():
            entry = await self.cache.get(key)
            out[component] = Freshness.from_entry(component, entry, source=source).as_dict()

        latest_bar = await asyncio.to_thread(self.store.latest_timestamp, sym, BarInterval.DAY_1)
        age = (utcnow() - latest_bar).total_seconds() if latest_bar else None
        out["price"] = Freshness(
            component="price",
            source="store",
            fetched_at=latest_bar,
            age_seconds=age,
            # No stored bar at all is stale: the chart has nothing to draw.
            stale=age is None or age > MAX_AGE["price"],
            max_age_seconds=MAX_AGE["price"],
            detail="newest stored daily bar" if latest_bar else "no daily bars stored yet",
        ).as_dict()
        stale = [name for name, block in out.items() if block["stale"]]
        return {
            "symbol": sym,
            "components": out,
            "stale_components": stale,
            "status": "current" if not stale else "stale",
        }


# ---------------------------------------------------------------------------
# module-level helpers used by the engine
# ---------------------------------------------------------------------------
def _parse_datetime(value: Any) -> datetime | None:
    """Parse an ISO timestamp (or date) into an aware UTC datetime."""
    text = _text(value)
    if text is None:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = _parse_period(text)
        if parsed is None:
            return None
        moment = datetime.combine(parsed, datetime.min.time())
    return to_utc(moment)


def _fiscal_period_for(
    reported_at: datetime, periods: Sequence[date], *, max_lag_days: int = 150
) -> date | None:
    """Return the fiscal period a report published at ``reported_at`` covers.

    The newest quarterly statement period that ended before the announcement
    and no more than ``max_lag_days`` earlier. ``None`` when no statement fits,
    because guessing "the previous calendar quarter" would be wrong for every
    issuer whose fiscal year is offset.
    """
    published = reported_at.date()
    candidates = [
        period
        for period in periods
        if period < published and (published - period).days <= max_lag_days
    ]
    return max(candidates) if candidates else None


def _consensus_block(payload: dict[str, Any]) -> dict[str, Any]:
    """Summarise the consensus tiles from a vendor estimates payload.

    ``+1y``/``0y`` rows carry the analyst mean, the analyst count and the
    high/low range; the 30-day revision comes from the EPS trend table and the
    dispersion is ``(high - low) / |mean|`` on the current-year EPS estimate.
    Anything the vendor did not publish stays ``None``.
    """
    by_period = {
        _text(row.get("period")): row for row in payload.get("earnings_estimate") or []
    }
    revenue_by_period = {
        _text(row.get("period")): row for row in payload.get("revenue_estimate") or []
    }
    trend_by_period = {_text(row.get("period")): row for row in payload.get("eps_trend") or []}

    current_year = by_period.get("0y") or {}
    next_year = by_period.get("+1y") or {}
    current_revenue = revenue_by_period.get("0y") or {}
    next_revenue = revenue_by_period.get("+1y") or {}
    trend = trend_by_period.get("0y") or {}

    mean = _num(current_year.get("avg"))
    high = _num(current_year.get("high"))
    low = _num(current_year.get("low"))
    dispersion = (
        (high - low) / abs(mean) if mean not in (None, 0) and high is not None and low is not None
        else None
    )
    current_eps = _num(trend.get("current"))
    thirty = _num(trend.get("30daysAgo"))
    revision_30d = (
        (current_eps - thirty) / abs(thirty)
        if current_eps is not None and thirty not in (None, 0)
        else None
    )
    return {
        "fy_revenue": _num(current_revenue.get("avg")),
        "fy_revenue_growth": _num(current_revenue.get("growth")),
        "fy_next_revenue": _num(next_revenue.get("avg")),
        "fy_eps": mean,
        "fy_next_eps": _num(next_year.get("avg")),
        "analysts": _num(current_year.get("numberOfAnalysts")),
        "eps_high": high,
        "eps_low": low,
        "dispersion": dispersion,
        "revision_30d": revision_30d,
        "basis": (
            "vendor consensus for the current fiscal year; dispersion is "
            "(high - low) / |mean| EPS, revision is the 30-day change in the mean EPS estimate"
        ),
    }


def _medians(rows: Sequence[dict[str, Any]]) -> dict[str, float | None]:
    """Return the median of each comparative column across peer rows."""
    keys = ("revenue_yoy", "gross_margin", "fcf_margin", "ev_to_sales", "ev_to_ebitda")
    out: dict[str, float | None] = {}
    for key in keys:
        values = sorted(
            value for value in (row.get(key) for row in rows) if isinstance(value, float)
        )
        if not values:
            out[key] = None
            continue
        middle = len(values) // 2
        out[key] = (
            values[middle]
            if len(values) % 2
            else (values[middle - 1] + values[middle]) / 2.0
        )
    return out
