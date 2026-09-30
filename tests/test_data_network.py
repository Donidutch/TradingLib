"""Live network tests for the data providers.

These hit real vendors, so they are marked ``network`` and skipped unless
``TRADINGLIB_NETWORK_TESTS=1`` is set. They assert on shapes, freshness and
provenance - never on specific prices, which change by the second.

Run them with::

    TRADINGLIB_NETWORK_TESTS=1 .venv/bin/pytest tests/test_data_network.py -v
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tradinglib.data.base import BarInterval, ProviderError, ProviderRateLimited
from tradinglib.data.store import TimeSeriesStore

pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(
        os.environ.get("TRADINGLIB_NETWORK_TESTS") != "1",
        reason="live vendor tests; set TRADINGLIB_NETWORK_TESTS=1 to run them",
    ),
]

SYMBOL = "AAPL"


async def test_yfinance_quote_is_live_and_recent() -> None:
    from tradinglib.data.providers.yfinance_provider import YFinanceProvider

    provider = YFinanceProvider()
    status = await provider.status()
    assert status.configured is True
    assert status.reachable is True

    quote = await provider.get_quote(SYMBOL)
    assert quote.symbol == SYMBOL
    assert quote.price > 0
    assert quote.source == "yfinance"
    assert quote.ts.tzinfo is not None
    age = datetime.now(timezone.utc) - quote.ts
    assert age < timedelta(days=7), f"quote is stale by {age}"


async def test_yfinance_daily_bars_are_ordered_and_consistent() -> None:
    from tradinglib.data.providers.yfinance_provider import YFinanceProvider

    bars = await YFinanceProvider().get_bars(
        SYMBOL, start=datetime.now(timezone.utc) - timedelta(days=45)
    )
    assert len(bars) > 10
    timestamps = [bar.ts for bar in bars]
    assert timestamps == sorted(timestamps)
    for bar in bars:
        assert bar.low <= bar.open <= bar.high
        assert bar.low <= bar.close <= bar.high
        assert bar.volume >= 0
        assert bar.interval is BarInterval.DAY_1


async def test_yfinance_fundamentals_statements_news_and_search() -> None:
    from tradinglib.data.providers.yfinance_provider import YFinanceProvider

    provider = YFinanceProvider()

    fundamentals = await provider.get_fundamentals(SYMBOL)
    assert fundamentals.name
    assert fundamentals.market_cap and fundamentals.market_cap > 0
    assert fundamentals.revenue_ttm and fundamentals.revenue_ttm > 0

    statements = await provider.get_financial_statements(SYMBOL)
    annual = statements["annual"]["income_statement"]
    assert annual and "period" in annual[0]

    estimates = await provider.get_estimates(SYMBOL)
    assert isinstance(estimates["earnings_estimate"], list)

    news = await provider.get_news(SYMBOL, limit=5)
    assert news
    assert all(item.headline for item in news)

    results = await provider.search("apple", limit=5)
    assert any(row["symbol"] == SYMBOL for row in results)


async def test_edgar_filings_and_facts_are_live() -> None:
    from tradinglib.data.providers.edgar_provider import EdgarProvider

    provider = EdgarProvider()
    try:
        status = await provider.status()
        assert status.reachable is True

        assert await provider.resolve_cik(SYMBOL) == "0000320193"

        filings = await provider.get_filings(SYMBOL, forms=["10-K", "10-Q"], limit=5)
        assert filings
        assert all(f.url.startswith("https://www.sec.gov/Archives/") for f in filings)
        assert filings[0].filed_at <= date.today()

        facts = await provider.get_company_facts(SYMBOL)
        assert "us-gaap" in facts["facts"]

        points = await provider.get_concept(
            SYMBOL, "us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"
        )
        assert points
        assert points == sorted(points)
    finally:
        await provider.close()


async def test_fred_series_is_live_without_a_key() -> None:
    from tradinglib.data.providers.fred_provider import FredProvider

    provider = FredProvider(api_key=None)
    try:
        status = await provider.status()
        assert status.configured is True
        assert status.reachable is True

        series = await provider.get_series("DGS10", start=date.today() - timedelta(days=120))
        assert len(series) > 20
        assert series.source == "fred"
        assert series.meta["mode"] == "csv"
        timestamps = [ts for ts, _ in series.points]
        assert timestamps == sorted(timestamps)
    finally:
        await provider.close()


async def test_google_trends_interest_over_time_is_live() -> None:
    from tradinglib.data.providers.trends_provider import TrendsProvider

    provider = TrendsProvider()
    try:
        series = await provider.interest_over_time(["iphone"], timeframe="today 3-m")
    except ProviderRateLimited as exc:
        pytest.skip(f"Google Trends throttled this run: {exc}")
    assert "iphone" in series
    assert len(series["iphone"]) > 10
    assert all(0.0 <= value <= 100.0 for _, value in series["iphone"].points)


async def test_alpaca_reports_its_real_configuration_state() -> None:
    from tradinglib.data.providers.alpaca_provider import AlpacaProvider

    provider = AlpacaProvider()
    status = await provider.status()
    if not provider.configured:
        assert status.configured is False
        assert status.reachable is None
        pytest.skip("Alpaca keys are not configured; honest 'not configured' status verified")
    assert status.configured is True
    quote = await provider.get_quote(SYMBOL)
    assert quote.price > 0
    assert quote.source == "alpaca"


async def test_end_to_end_fetch_store_and_query(tmp_path: Path) -> None:
    """The acceptance path: yfinance -> Parquet -> DuckDB."""
    from tradinglib.data.providers.yfinance_provider import YFinanceProvider

    store = TimeSeriesStore(tmp_path / "ts")
    bars = await YFinanceProvider().get_bars(
        SYMBOL, start=datetime.now(timezone.utc) - timedelta(days=120)
    )
    assert bars

    written = await store.awrite_bars(bars)
    assert written == len(bars)
    assert store.bars_path(SYMBOL, BarInterval.DAY_1).is_file()

    frame = store.read_bars(SYMBOL)
    assert len(frame) == len(bars)
    assert str(frame.index.tz) == "UTC"

    aggregate = store.query(
        "SELECT count(*) AS n, min(ts) AS first_ts, max(close) AS high "
        "FROM bars WHERE symbol = $symbol",
        symbol=SYMBOL,
    )
    assert int(aggregate["n"].iloc[0]) == len(bars)
    assert float(aggregate["high"].iloc[0]) == pytest.approx(max(b.close for b in bars))

    coverage = store.coverage(SYMBOL)
    assert coverage["rows"] == len(bars)
    assert coverage["bytes"] > 0

    # Re-writing the same bars must not duplicate rows.
    assert await store.awrite_bars(bars) == len(bars)


async def test_fred_series_round_trips_through_the_store(tmp_path: Path) -> None:
    from tradinglib.data.providers.fred_provider import FredProvider

    store = TimeSeriesStore(tmp_path / "ts")
    provider = FredProvider(api_key=None)
    try:
        series = await provider.get_series("DGS10", start=date.today() - timedelta(days=90))
    except ProviderError as exc:  # pragma: no cover - vendor outage
        pytest.skip(f"FRED unavailable: {exc}")
    finally:
        await provider.close()

    assert await store.awrite_series("fred", series) == len(series)
    stored = store.read_series("fred", "DGS10")
    assert len(stored) == len(series)
    assert stored.points[-1] == series.points[-1]
