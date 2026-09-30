"""Stock Research engine tests.

Two layers are covered:

1. the pure derivation layer (:func:`tradinglib.research.company.derive_metrics`
   and friends) against fixture statements whose expected values are computed
   by hand in the assertions - this is the "derived-metric correctness" test;
2. the composition layer (:class:`CompanyResearch`) against stub providers, so
   the page payloads, the store round-trip and the honest-null behaviour are
   exercised without touching a vendor.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from tradinglib.data.base import (
    Bar,
    BarInterval,
    DataProvider,
    Filing,
    Fundamentals,
    NewsItem,
    ProviderStatus,
    ProviderUnavailable,
    Quote,
    Series,
    SymbolNotFound,
)
from tradinglib.data.cache import DataCache
from tradinglib.data.providers.jobs_provider import JobPostings
from tradinglib.data.store import TimeSeriesStore
from tradinglib.research.company import (
    CompanyResearch,
    Freshness,
    aggregate_ttm,
    build_period_facts,
    derive_metrics,
    region_for,
    reverse_dcf_growth,
    score_sentiment,
    sentiment_label,
    trends_keyword,
)

UTC = timezone.utc


# ---------------------------------------------------------------------------
# fixtures: statements in the shape the yfinance provider returns
# ---------------------------------------------------------------------------
def _quarter(
    period: str,
    revenue: float,
    cost: float,
    operating_income: float,
    depreciation: float,
    net_income: float,
    ocf: float,
    capex: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build one quarterly income-statement and cash-flow record."""
    return (
        {
            "period": period,
            "Total Revenue": revenue,
            "Cost Of Revenue": cost,
            "Gross Profit": revenue - cost,
            "Operating Income": operating_income,
            "Net Income": net_income,
            "Reconciled Depreciation": depreciation,
        },
        {
            "period": period,
            "Operating Cash Flow": ocf,
            "Capital Expenditure": capex,
        },
    )


#: Eight quarters of a fictional issuer, plus one balance sheet and two years.
#: TTM revenue = 1000+1100+1200+1300 = 4600; prior TTM = 800+850+900+950 = 3500.
QUARTERS = [
    _quarter("2026-06-30", 1300, 700, 200, 40, 150, 260, -60),
    _quarter("2026-03-31", 1200, 660, 180, 38, 130, 240, -50),
    _quarter("2025-12-31", 1100, 620, 160, 36, 110, 220, -40),
    _quarter("2025-09-30", 1000, 580, 140, 34, 90, 200, -30),
    _quarter("2025-06-30", 950, 560, 120, 32, 80, 180, -25),
    _quarter("2025-03-31", 900, 540, 100, 30, 70, 170, -25),
    _quarter("2024-12-31", 850, 520, 90, 28, 60, 160, -20),
    _quarter("2024-09-30", 800, 500, 80, 26, 50, 150, -20),
]

BALANCE = [
    {
        "period": "2026-06-30",
        "Cash And Cash Equivalents": 2_000.0,
        "Other Short Term Investments": 500.0,
        "Total Debt": 900.0,
        "Stockholders Equity": 5_000.0,
        "Total Assets": 9_000.0,
        "Invested Capital": 5_900.0,
        "Ordinary Shares Number": 1_000.0,
    },
    {
        "period": "2025-12-31",
        "Cash And Cash Equivalents": 1_500.0,
        "Total Debt": 1_000.0,
        "Stockholders Equity": 4_500.0,
        "Total Assets": 8_000.0,
        "Invested Capital": 5_500.0,
    },
]

ANNUAL_INCOME = [
    {
        "period": "2025-12-31",
        "Total Revenue": 3_950.0,
        "Cost Of Revenue": 2_240.0,
        "Gross Profit": 1_710.0,
        "Operating Income": 470.0,
        "Reconciled Depreciation": 128.0,
        "Net Income": 310.0,
    },
    {
        "period": "2024-12-31",
        "Total Revenue": 3_100.0,
        "Cost Of Revenue": 1_960.0,
        "Gross Profit": 1_140.0,
        "Operating Income": 300.0,
        "Reconciled Depreciation": 100.0,
        "Net Income": 180.0,
    },
]

ANNUAL_CASHFLOW = [
    {"period": "2025-12-31", "Operating Cash Flow": 750.0, "Capital Expenditure": -120.0},
    {"period": "2024-12-31", "Operating Cash Flow": 600.0, "Capital Expenditure": -100.0},
]


def statements_fixture() -> dict[str, Any]:
    """Return a complete provider-shaped statement payload."""
    return {
        "symbol": "TEST",
        "source": "fixture",
        "annual": {
            "income_statement": ANNUAL_INCOME,
            "balance_sheet": BALANCE[1:],
            "cash_flow": ANNUAL_CASHFLOW,
        },
        "quarterly": {
            "income_statement": [income for income, _cash in QUARTERS],
            "balance_sheet": BALANCE,
            "cash_flow": [cash for _income, cash in QUARTERS],
        },
    }


def fundamentals_fixture() -> Fundamentals:
    """Return the vendor snapshot that accompanies the fixture statements."""
    return Fundamentals(
        symbol="TEST",
        currency="USD",
        name="Test Industries Limited",
        sector="Technology",
        industry="Software",
        market_cap=50_000.0,
        enterprise_value=48_000.0,
        shares_outstanding=1_000.0,
        extras={"country": "Singapore", "exchange": "NMS", "quoteType": "EQUITY"},
        source="fixture",
    )


# ---------------------------------------------------------------------------
# statement normalisation
# ---------------------------------------------------------------------------
def test_period_facts_merge_the_three_statements() -> None:
    """One period carries income, balance-sheet and cash-flow lines together."""
    facts = build_period_facts(statements_fixture(), "quarterly")
    assert [f.label for f in facts][:3] == ["2026-06-30", "2026-03-31", "2025-12-31"]
    newest = facts[0]
    assert newest.revenue == 1300
    assert newest.operating_cash_flow == 260
    assert newest.cash == 2000
    # cash + short-term investments, counted once
    assert newest.cash_and_investments == 2500
    assert newest.derived_gross_profit == 600
    assert newest.derived_ebitda == 240  # operating income 200 + D&A 40
    assert newest.derived_fcf == 200  # OCF 260 - capex 60


def test_cash_label_prevents_double_counting_investments() -> None:
    """A combined cash line is not topped up with short-term investments again."""
    payload = {
        "quarterly": {
            "balance_sheet": [
                {
                    "period": "2026-06-30",
                    "Cash Cash Equivalents And Short Term Investments": 2_500.0,
                    "Other Short Term Investments": 500.0,
                }
            ]
        }
    }
    fact = build_period_facts(payload, "quarterly")[0]
    assert fact.cash_and_investments == 2_500.0


def test_aggregate_ttm_sums_four_quarters_and_reports_its_basis() -> None:
    """The trailing window is a real sum with a traceable basis string."""
    payload = statements_fixture()
    quarterly = build_period_facts(payload, "quarterly")
    annual = build_period_facts(payload, "annual")
    current = aggregate_ttm(quarterly, annual, "revenue", label="revenue")
    prior = aggregate_ttm(quarterly, annual, "revenue", label="revenue", offset=4)
    assert current.value == 4_600
    assert prior.value == 3_500
    assert current.basis == "sum of 4 quarters 2025-09-30..2026-06-30"


def test_aggregate_ttm_falls_back_to_the_fiscal_year() -> None:
    """With fewer than four quarters the fiscal year is used, and says so."""
    payload = statements_fixture()
    payload["quarterly"]["income_statement"] = payload["quarterly"]["income_statement"][:2]
    quarterly = build_period_facts(payload, "quarterly")
    annual = build_period_facts(payload, "annual")
    result = aggregate_ttm(quarterly, annual, "revenue", label="revenue")
    assert result.value == 3_950.0
    assert "fiscal year 2025-12-31" in (result.basis or "")


def test_aggregate_ttm_reports_a_reason_when_nothing_is_published() -> None:
    """No quarters and no year yields a null with an explanation, not a zero."""
    result = aggregate_ttm([], [], "revenue", label="revenue")
    assert result.value is None
    assert "revenue is not available" in (result.reason or "")


# ---------------------------------------------------------------------------
# derived metrics
# ---------------------------------------------------------------------------
def test_derived_metrics_match_hand_computed_values() -> None:
    """Every headline metric equals the figure computed from the fixture."""
    metrics = derive_metrics(statements_fixture(), fundamentals=fundamentals_fixture())

    assert metrics["revenue_ttm"].value == 4_600
    assert metrics["revenue_prior_ttm"].value == 3_500
    assert metrics["revenue_yoy"].value == pytest.approx(4_600 / 3_500 - 1)

    gross = (1300 - 700) + (1200 - 660) + (1100 - 620) + (1000 - 580)
    assert metrics["gross_profit_ttm"].value == gross
    assert metrics["gross_margin"].value == pytest.approx(gross / 4_600)

    ebitda = (200 + 40) + (180 + 38) + (160 + 36) + (140 + 34)
    assert metrics["ebitda_ttm"].value == ebitda
    assert metrics["ebitda_margin"].value == pytest.approx(ebitda / 4_600)

    fcf = (260 - 60) + (240 - 50) + (220 - 40) + (200 - 30)
    assert metrics["fcf_ttm"].value == fcf
    assert metrics["fcf_margin"].value == pytest.approx(fcf / 4_600)

    # Balance sheet: newest quarter, cash + short-term investments less debt.
    assert metrics["cash_and_investments"].value == 2_500
    assert metrics["total_debt"].value == 900
    assert metrics["net_cash"].value == 1_600

    # EV is computed from the balance sheet, not copied from the vendor.
    assert metrics["enterprise_value"].value == 50_000 + 900 - 2_500
    assert metrics["ev_to_sales"].value == pytest.approx(48_400 / 4_600)
    assert metrics["ev_to_ebitda"].value == pytest.approx(48_400 / ebitda)
    assert metrics["net_debt_to_ebitda"].value == pytest.approx((900 - 2_500) / ebitda)
    assert metrics["return_on_invested_capital"].value == pytest.approx((200 + 180 + 160 + 140) / 5_900)


def test_growth_refuses_to_mix_a_ttm_with_a_fiscal_year() -> None:
    """With seven quarters published, YoY switches to a consistent annual basis."""
    payload = statements_fixture()
    payload["quarterly"]["income_statement"] = payload["quarterly"]["income_statement"][:7]
    payload["quarterly"]["cash_flow"] = payload["quarterly"]["cash_flow"][:7]
    metrics = derive_metrics(payload, fundamentals=fundamentals_fixture())

    assert metrics["revenue_ttm"].value == 4_600  # still four quarters
    # The prior window would have been a fiscal year, so the growth rate is
    # computed year over year instead of mixing the two bases.
    assert metrics["revenue_yoy"].basis == "fiscal year 2025-12-31 vs 2024-12-31"
    assert metrics["revenue_yoy"].value == pytest.approx(3_950.0 / 3_100.0 - 1)


def test_growth_is_null_when_only_one_year_exists() -> None:
    """One published year and three quarters cannot produce a growth rate."""
    payload = statements_fixture()
    payload["quarterly"]["income_statement"] = payload["quarterly"]["income_statement"][:3]
    payload["annual"]["income_statement"] = payload["annual"]["income_statement"][:1]
    metrics = derive_metrics(payload, fundamentals=fundamentals_fixture())
    assert metrics["revenue_yoy"].value is None
    assert metrics["revenue_yoy"].reason


def test_metrics_carry_their_basis_and_inputs() -> None:
    """A derived figure can always be traced back to what produced it."""
    metrics = derive_metrics(statements_fixture(), fundamentals=fundamentals_fixture())
    net_cash = metrics["net_cash"]
    assert net_cash.basis is not None and "balance sheet 2026-06-30" in net_cash.basis
    assert net_cash.inputs == {"cash_and_investments": 2_500.0, "total_debt": 900.0}
    assert metrics["enterprise_value"].basis == "market cap + total debt - cash & investments"


def test_missing_inputs_produce_nulls_with_reasons_never_zeros() -> None:
    """A company that publishes nothing gets nulls and explanations."""
    metrics = derive_metrics({}, fundamentals=None)
    for key in (
        "revenue_ttm",
        "gross_margin",
        "fcf_margin",
        "ev_to_sales",
        "net_cash",
        "market_cap",
    ):
        metric = metrics[key]
        assert metric.value is None, key
        assert metric.reason, key
    assert "prior-year revenue" not in (metrics["revenue_ttm"].reason or "")


def test_negative_denominator_is_refused_rather_than_signed() -> None:
    """A loss-making EBITDA does not produce a meaningless negative multiple."""
    payload = statements_fixture()
    for record in payload["quarterly"]["income_statement"]:
        record["Operating Income"] = -100.0
    payload["annual"]["income_statement"][0]["Operating Income"] = -400.0
    metrics = derive_metrics(payload, fundamentals=fundamentals_fixture())
    assert metrics["ebitda_ttm"].value is not None and metrics["ebitda_ttm"].value < 0
    assert metrics["ev_to_ebitda"].value is None
    assert "meaningless" in (metrics["ev_to_ebitda"].reason or "")


def test_market_cap_falls_back_to_shares_times_price() -> None:
    """Without a vendor market cap, shares x price is used and labelled."""
    fundamentals = fundamentals_fixture()
    fundamentals.market_cap = None
    quote = Quote(symbol="TEST", price=12.5, ts=datetime.now(UTC))
    metrics = derive_metrics(statements_fixture(), fundamentals=fundamentals, quote=quote)
    assert metrics["market_cap"].value == pytest.approx(1_000 * 12.5)
    assert metrics["market_cap"].basis == "shares outstanding x last price"


def test_enterprise_value_falls_back_to_the_vendor_figure() -> None:
    """An incomplete balance sheet falls back to the vendor EV, saying so."""
    payload = statements_fixture()
    payload["quarterly"]["balance_sheet"] = []
    payload["annual"]["balance_sheet"] = []
    metrics = derive_metrics(payload, fundamentals=fundamentals_fixture())
    assert metrics["enterprise_value"].value == 48_000.0
    assert "vendor enterprise value" in (metrics["enterprise_value"].basis or "")


def test_reverse_dcf_recovers_the_growth_rate_it_was_priced_at() -> None:
    """The solver inverts the model it documents."""
    fcf = 1_000.0
    growth = 0.12
    discount, terminal, years = 0.10, 0.025, 10
    cash, total = fcf, 0.0
    for year in range(1, years + 1):
        cash *= 1 + growth
        total += cash / (1 + discount) ** year
    total += (cash * (1 + terminal) / (discount - terminal)) / (1 + discount) ** years

    solved, basis = reverse_dcf_growth(enterprise_value=total, free_cash_flow=fcf)
    assert solved == pytest.approx(growth, abs=1e-4)
    assert "two-stage DCF" in basis


def test_reverse_dcf_refuses_negative_cash_flow() -> None:
    """No growth rate is implied for a company burning cash."""
    value, reason = reverse_dcf_growth(enterprise_value=1_000.0, free_cash_flow=-50.0)
    assert value is None
    assert "not positive" in reason


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def test_region_and_keyword_helpers() -> None:
    """Header region mapping and the Trends keyword behave as documented."""
    assert region_for("Singapore") == "Southeast Asia"
    assert region_for("Atlantis") is None
    assert region_for(None) is None
    assert trends_keyword("Grab Holdings Limited", "GRAB") == "Grab Holdings"
    assert trends_keyword(None, "aapl") == "AAPL"


def test_sentiment_scoring_is_signed_and_labelled() -> None:
    """VADER scoring returns a compound score and the standard label."""
    positive = score_sentiment("Record profits and a strong beat on margins")["compound"]
    negative = score_sentiment("Regulator opens probe; shares plunge on fraud claims")["compound"]
    assert positive > 0.05 and sentiment_label(positive) == "positive"
    assert negative < -0.05 and sentiment_label(negative) == "negative"
    assert sentiment_label(0.0) == "neutral"


def test_freshness_marks_old_entries_stale() -> None:
    """Freshness is measured from the cache entry, not asserted."""
    class _Entry:
        fetched_at = datetime.now(UTC) - timedelta(hours=5)
        age_seconds = 5 * 3600.0

    fresh = Freshness.from_entry("quote", None, source="stub")
    assert fresh.stale and fresh.fetched_at is None
    old = Freshness.from_entry("quote", _Entry(), source="stub")  # type: ignore[arg-type]
    assert old.stale and old.age_seconds == pytest.approx(18_000.0)


# ---------------------------------------------------------------------------
# composition against stub providers
# ---------------------------------------------------------------------------
class StubMarketProvider(DataProvider):
    """Deterministic stand-in for the yfinance provider (tests only)."""

    name = "stub-market"

    def __init__(self) -> None:
        """Create the stub with a fixed price history and news list."""
        self.calls: dict[str, int] = {}
        # Anchored on "today" so range windows (1m, 1y, ...) actually cover it.
        first_bar = datetime.now(UTC).replace(
            hour=21, minute=0, second=0, microsecond=0
        ) - timedelta(days=29)
        self.bars_history = [
            Bar(
                symbol="TEST",
                ts=first_bar + timedelta(days=index),
                open=10.0 + index,
                high=11.0 + index,
                low=9.5 + index,
                close=10.5 + index,
                volume=1_000_000 + index,
                interval=BarInterval.DAY_1,
                source=self.name,
            )
            for index in range(30)
        ]

    def _count(self, key: str) -> None:
        """Record a call so caching can be asserted."""
        self.calls[key] = self.calls.get(key, 0) + 1

    async def status(self) -> ProviderStatus:
        """Report the stub as configured and reachable."""
        return ProviderStatus(name=self.name, configured=True, reachable=True)

    async def get_quote(self, symbol: str) -> Quote:
        """Return a fixed quote."""
        self._count("quote")
        if symbol == "NOPE":
            raise SymbolNotFound(self.name, "NOPE: unknown")
        return Quote(
            symbol=symbol,
            price=39.5,
            ts=datetime.now(UTC),
            prev_close=39.0,
            source=self.name,
        )

    async def get_bars(
        self,
        symbol: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        interval: BarInterval = BarInterval.DAY_1,
        limit: int | None = None,
    ) -> list[Bar]:
        """Return the fixed history, filtered by ``start``."""
        self._count("bars")
        bars = [bar for bar in self.bars_history if start is None or bar.ts >= start]
        return [
            Bar(
                symbol=symbol,
                ts=bar.ts,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                interval=interval,
                source=self.name,
            )
            for bar in bars
        ]

    async def get_fundamentals(self, symbol: str) -> Fundamentals:
        """Return the fixture snapshot."""
        self._count("fundamentals")
        if symbol == "NOPE":
            raise SymbolNotFound(self.name, "NOPE: unknown")
        snapshot = fundamentals_fixture()
        snapshot.symbol = symbol
        return snapshot

    async def get_financial_statements(self, symbol: str) -> dict[str, Any]:
        """Return the fixture statements."""
        self._count("statements")
        return statements_fixture()

    async def get_estimates(self, symbol: str) -> dict[str, Any]:
        """Return one past and one future earnings date."""
        self._count("estimates")
        future = datetime.now(UTC) + timedelta(days=12)
        past = datetime(2026, 1, 10, tzinfo=UTC)
        return {
            "symbol": symbol,
            "source": self.name,
            "earnings_dates": [
                {
                    "date": future.isoformat(),
                    "EPS Estimate": 0.11,
                    "Reported EPS": None,
                    "Surprise(%)": None,
                },
                {
                    "date": past.isoformat(),
                    "EPS Estimate": 0.10,
                    "Reported EPS": 0.13,
                    "Surprise(%)": 30.0,
                },
            ],
            "earnings_estimate": [
                {"period": "0y", "avg": 0.5, "low": 0.4, "high": 0.7, "numberOfAnalysts": 6}
            ],
            "revenue_estimate": [{"period": "0y", "avg": 5_000.0, "growth": 0.18}],
            "eps_trend": [{"period": "0y", "current": 0.5, "30daysAgo": 0.4}],
        }

    async def get_news(self, symbol: str, *, limit: int = 25) -> list[NewsItem]:
        """Return two items with opposite sentiment."""
        self._count("news")
        now = datetime.now(UTC)
        return [
            NewsItem(
                symbol=symbol,
                headline="Record quarter as margins expand sharply",
                url="https://example.invalid/a",
                published_at=now - timedelta(hours=1),
                publisher="Fixture Wire",
                summary="Strong growth and improving profitability.",
                source=self.name,
            ),
            NewsItem(
                symbol=symbol,
                headline="Regulator opens an investigation into pricing",
                url="https://example.invalid/b",
                published_at=now - timedelta(days=10),
                publisher="Fixture Wire",
                summary="A probe was announced.",
                source=self.name,
            ),
        ]

    async def search(self, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
        """Return one search hit."""
        self._count("search")
        return [{"symbol": "TEST", "name": "Test Industries Limited", "source": self.name}]


class StubEdgarProvider(DataProvider):
    """EDGAR stand-in returning two filings."""

    name = "stub-edgar"

    async def status(self) -> ProviderStatus:
        """Report the stub as configured and reachable."""
        return ProviderStatus(name=self.name, configured=True, reachable=True)

    async def get_filings(self, symbol: str, *, forms: list[str] | None = None, limit: int = 40):
        """Return a 10-K and a 10-Q."""
        return [
            Filing(
                symbol=symbol,
                cik="0000000001",
                form="10-K",
                filed_at=datetime(2026, 2, 20, tzinfo=UTC).date(),
                accession="0000000001-26-000001",
                url="https://example.invalid/10k",
            ),
            Filing(
                symbol=symbol,
                cik="0000000001",
                form="10-Q",
                filed_at=datetime(2026, 5, 5, tzinfo=UTC).date(),
                accession="0000000001-26-000002",
                url="https://example.invalid/10q",
            ),
        ]


class StubTrendsProvider:
    """Google Trends stand-in returning a rising interest series."""

    name = "stub-trends"

    async def interest_over_time(
        self, keywords: list[str], *, timeframe: str = "today 12-m", geo: str = "US"
    ) -> dict[str, Series]:
        """Return one weekly series per keyword."""
        base = datetime.now(UTC) - timedelta(weeks=52)
        points = [(base + timedelta(weeks=week), 40.0 + week) for week in range(52)]
        return {
            keywords[0]: Series(
                name=keywords[0], points=points, unit="index 0-100", source=self.name
            )
        }


class StubJobsProvider:
    """Job-board stand-in: the fixture company publishes no public board."""

    name = "stub-jobs"

    async def get_postings(
        self, symbol: str, *, company_name: str | None = None, **_: object
    ) -> JobPostings:
        """Report the honest miss the real provider returns for an unlisted issuer."""
        return JobPostings(
            symbol=symbol.upper(),
            as_of=datetime.now(UTC),
            tried=["greenhouse:test", "ashby:test", "lever:test"],
            reason=(
                "no public Greenhouse, Ashby or Lever job board is published for "
                f"{company_name or symbol}"
            ),
        )


class StubHiringJobsProvider(StubJobsProvider):
    """Job-board stand-in for a company that does publish a board."""

    async def get_postings(
        self, symbol: str, *, company_name: str | None = None, **_: object
    ) -> JobPostings:
        """Return a board with 40 of 100 roles opened in the trailing window."""
        return JobPostings(
            symbol=symbol.upper(),
            board="greenhouse",
            board_label="Greenhouse",
            slug="testindustries",
            open_roles=100,
            opened_recent=40,
            opened_prior=25,
            change_pct=60.0,
            as_of=datetime.now(UTC),
        )


@pytest.fixture()
def engine(tmp_path: Path) -> CompanyResearch:
    """A research engine wired to stub providers, a temp store and a temp cache."""
    return CompanyResearch(
        market=StubMarketProvider(),
        edgar=StubEdgarProvider(),
        trends=StubTrendsProvider(),
        jobs=StubJobsProvider(),
        cache=DataCache(tmp_path / "cache", default_ttl=60.0),
        store=TimeSeriesStore(tmp_path / "ts"),
    )


async def test_overview_composes_profile_quote_and_metrics(engine: CompanyResearch) -> None:
    """The overview payload carries the header, derived metrics and freshness."""
    payload = await engine.company("test")
    assert payload["symbol"] == "TEST"
    assert payload["profile"]["name"] == "Test Industries Limited"
    assert payload["profile"]["region"] == "Southeast Asia"
    assert payload["profile"]["meta_line"] == "NMS · Technology · Southeast Asia"
    assert payload["quote"]["price"] == 39.5
    assert payload["metrics"]["revenue_ttm"]["value"] == 4_600
    assert payload["metrics"]["net_cash"]["value"] == 1_600
    assert payload["next_earnings"]["days_away"] in (11, 12)
    assert [row["period"] for row in payload["revenue_ebitda"]] == ["2024-12-31", "2025-12-31"]
    assert payload["freshness"]["statements"]["stale"] is False
    assert payload["warnings"] == []


async def test_unknown_symbol_raises_symbol_not_found(engine: CompanyResearch) -> None:
    """An unresolvable ticker is an error, not an empty page."""
    with pytest.raises(SymbolNotFound):
        await engine.overview("NOPE")


async def test_vendor_outage_is_not_reported_as_an_unknown_symbol(tmp_path: Path) -> None:
    """A provider outage degrades to 503-shaped, not "no such ticker"."""

    class DeadProvider(StubMarketProvider):
        """Market stub whose identity endpoints are down."""

        async def get_quote(self, symbol: str) -> Quote:
            """Fail the way an outage does."""
            raise ProviderUnavailable(self.name, "quote endpoint is down")

        async def get_fundamentals(self, symbol: str) -> Fundamentals:
            """Fail the way an outage does."""
            raise ProviderUnavailable(self.name, "profile endpoint is down")

    engine = CompanyResearch(
        market=DeadProvider(),
        edgar=StubEdgarProvider(),
        cache=DataCache.memory_only(),
        store=TimeSeriesStore(tmp_path / "ts"),
    )
    with pytest.raises(ProviderUnavailable) as excinfo:
        await engine.overview("TEST")
    assert not isinstance(excinfo.value, SymbolNotFound)
    assert "quote endpoint is down" in str(excinfo.value)


async def test_price_persists_bars_and_serves_them_from_the_store(
    engine: CompanyResearch,
) -> None:
    """The first call fetches and stores; the second is served locally."""
    first = await engine.price("TEST", range="1m")
    assert first["count"] == 30
    assert first["source"] == "stub-market"
    stored = engine.store.read_bars("TEST", interval=BarInterval.DAY_1)
    assert len(stored) == 30

    provider: StubMarketProvider = engine.market  # type: ignore[assignment]
    calls_before = provider.calls["bars"]
    second = await engine.price("TEST", range="1m")
    # Cached at the provider layer; either way no extra vendor call happens.
    assert provider.calls["bars"] == calls_before
    assert second["count"] == 30
    assert second["last_close"] == first["last_close"]


async def test_unknown_range_is_rejected(engine: CompanyResearch) -> None:
    """A bad range key is a caller error with a helpful message."""
    with pytest.raises(ValueError, match="unknown chart range"):
        await engine.price("TEST", range="17y")


async def test_financials_summary_carries_margins(engine: CompanyResearch) -> None:
    """Annual rows are chronological and carry per-period margins."""
    payload = await engine.financials("TEST")
    periods = [row["period"] for row in payload["annual"]]
    assert periods == ["2024-12-31", "2025-12-31"]
    latest = payload["annual"][-1]
    assert latest["revenue"] == 3_950.0
    assert latest["gross_margin"] == pytest.approx(1_710.0 / 3_950.0)
    assert latest["fcf_margin"] == pytest.approx((750.0 - 120.0) / 3_950.0)
    assert payload["quarterly"][-1]["period"] == "2026-06-30"


async def test_estimates_history_and_consensus(engine: CompanyResearch) -> None:
    """Earnings history is real, and the consensus block is derived."""
    payload = await engine.estimates("TEST")
    assert payload["history"][0]["eps_reported"] == 0.13
    assert payload["history"][0]["surprise_pct"] == 30.0
    assert payload["history"][0]["fiscal_period"] == "2025-12-31"
    assert payload["history"][0]["calendar_quarter"] == "2025 Q4"
    assert payload["history"][0]["report_quarter"] == "2026 Q1"
    assert payload["consensus"]["fy_revenue"] == 5_000.0
    assert payload["consensus"]["dispersion"] == pytest.approx((0.7 - 0.4) / 0.5)
    assert payload["consensus"]["revision_30d"] == pytest.approx((0.5 - 0.4) / 0.4)
    assert payload["next_earnings"] is not None


async def test_news_scores_every_item_and_aggregates(engine: CompanyResearch) -> None:
    """Sentiment is scored per item and aggregated with recency weighting."""
    payload = await engine.news("TEST")
    assert payload["count"] == 2
    labels = {item["sentiment_label"] for item in payload["items"]}
    assert "positive" in labels
    aggregate = payload["aggregate"]
    assert aggregate["counts"]["positive"] >= 1
    # The recent positive item dominates the ten-day-old negative one.
    assert aggregate["recency_weighted"] > aggregate["mean_compound"]


async def test_altdata_reports_unsourced_signals_as_null(engine: CompanyResearch) -> None:
    """Every alt signal is either sourced and says from what, or null with a reason."""
    payload = await engine.altdata("TEST")
    trends = payload["signals"]["google_trends"]
    assert trends["value"] == pytest.approx(91.0)
    assert trends["keyword"] == "Test Industries"
    assert payload["signals"]["news_sentiment"]["value"] is not None

    # The app-rank composite and the web-traffic tile are derived from search
    # interest, which TradingLib does have. They must therefore carry a value *and*
    # state the proxy they used - a number with no stated basis would be exactly
    # the invented figure this test exists to forbid.
    for key in ("app_rank_composite", "web_traffic_yoy"):
        signal = payload["signals"][key]
        assert signal["basis"], key
        assert "search interest" in signal["basis"] or "Google Trends" in signal["basis"], key
        if signal["value"] is None:
            assert signal["reason"], key
        else:
            assert signal["source"] == "trends", key

    # This company publishes no ATS board, so the tile is null and says why -
    # it never falls back to a plausible-looking percentage.
    postings = payload["signals"]["job_postings_90d"]
    assert postings["value"] is None
    assert "no public Greenhouse, Ashby or Lever job board" in postings["reason"]
    assert postings["source"] == "jobs"
    assert postings["basis"]


async def test_altdata_reports_job_postings_when_a_board_exists(tmp_path: Path) -> None:
    """A company with a public board gets a measured 90-day hiring rate."""
    engine = CompanyResearch(
        market=StubMarketProvider(),
        edgar=StubEdgarProvider(),
        trends=StubTrendsProvider(),
        jobs=StubHiringJobsProvider(),
        cache=DataCache(tmp_path / "cache-jobs", default_ttl=60.0),
        store=TimeSeriesStore(tmp_path / "ts-jobs"),
    )
    postings = (await engine.altdata("TEST"))["signals"]["job_postings_90d"]
    # 40 of the 100 currently listed roles were first published in 90 days.
    assert postings["value"] == pytest.approx(40.0)
    assert postings["board"] == "Greenhouse"
    assert postings["open_roles"] == 100
    assert "40 of 100 roles" in postings["basis"]
    assert postings["reason"] is None


async def test_valuation_uses_explicit_peers(engine: CompanyResearch) -> None:
    """A peer list is priced with the same derivation as the subject."""
    payload = await engine.valuation("TEST", peers=["PEER1", "PEER2"])
    assert payload["peers"][0]["is_subject"] is True
    assert {row["symbol"] for row in payload["peers"][1:]} == {"PEER1", "PEER2"}
    assert payload["multiples"]["ev_to_sales"]["value"] == pytest.approx(48_400 / 4_600)
    assert payload["peer_medians"]["fcf_margin"] is not None


async def test_filings_are_listed_and_counted(engine: CompanyResearch) -> None:
    """EDGAR filings are returned with per-form counts."""
    payload = await engine.filings("TEST")
    assert payload["count"] == 2
    assert payload["form_counts"] == {"10-K": 1, "10-Q": 1}


async def test_component_failures_degrade_instead_of_raising(tmp_path: Path) -> None:
    """A dead statements endpoint yields nulls and a warning, not an exception."""

    class BrokenStatements(StubMarketProvider):
        """Market stub whose statement endpoint always fails."""

        async def get_financial_statements(self, symbol: str) -> dict[str, Any]:
            """Fail the way a vendor outage does."""
            raise ProviderUnavailable(self.name, "statements endpoint is down")

    engine = CompanyResearch(
        market=BrokenStatements(),
        edgar=StubEdgarProvider(),
        trends=StubTrendsProvider(),
        cache=DataCache.memory_only(),
        store=TimeSeriesStore(tmp_path / "ts"),
    )
    payload = await engine.company("TEST")
    assert payload["metrics"]["revenue_ttm"]["value"] is None
    assert payload["metrics"]["revenue_ttm"]["reason"]
    assert any("statements endpoint is down" in warning for warning in payload["warnings"])
    # The quote still made it through.
    assert payload["quote"]["price"] == 39.5


async def test_freshness_report_lists_stale_components(engine: CompanyResearch) -> None:
    """The freshness endpoint reports what the page would render right now."""
    await engine.company("TEST")
    report = await engine.freshness("TEST")
    assert report["components"]["quote"]["stale"] is False
    assert report["components"]["price"]["fetched_at"] is None
    assert "price" in report["stale_components"]
    assert report["status"] == "stale"


async def test_resolve_falls_back_to_edgar(tmp_path: Path) -> None:
    """When the market search is empty the EDGAR ticker index is asked."""

    class EmptySearch(StubMarketProvider):
        """Market stub whose search returns nothing."""

        async def search(self, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
            """Return no candidates."""
            return []

    class SearchingEdgar(StubEdgarProvider):
        """EDGAR stub that resolves the query."""

        async def search(self, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
            """Return one candidate."""
            return [{"symbol": "TEST", "name": "Test Industries", "source": self.name}]

    engine = CompanyResearch(
        market=EmptySearch(),
        edgar=SearchingEdgar(),
        cache=DataCache.memory_only(),
        store=TimeSeriesStore(tmp_path / "ts"),
    )
    results = await engine.resolve("test industries")
    assert results and results[0]["source"] == "stub-edgar"


def test_math_helpers_are_finite() -> None:
    """Guard against NaN leaking into the payloads through the helpers."""
    metrics = derive_metrics(statements_fixture(), fundamentals=fundamentals_fixture())
    for metric in metrics.values():
        assert metric.value is None or math.isfinite(metric.value)
