"""Offline provider tests.

These exercise every parsing and policy path with local fixtures - no socket is
opened. The live counterparts live in ``tests/test_data_network.py``.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

import pandas as pd
import pytest

from tradinglib.data.base import (
    BarInterval,
    ProviderNotConfigured,
    ProviderRateLimited,
    ProviderUnavailable,
    SymbolNotFound,
)

# --- yfinance ------------------------------------------------------------


class _StubFastInfo(dict[str, Any]):
    """Stand-in for ``yfinance``'s fast_info mapping."""


class _StubTicker:
    """Minimal stand-in for :class:`yfinance.Ticker` built from fixture data."""

    def __init__(self, **attrs: Any) -> None:
        self._attrs = attrs

    def __getattr__(self, item: str) -> Any:
        if item in self._attrs:
            return self._attrs[item]
        raise AttributeError(item)

    def get_news(self, count: int = 10) -> list[dict[str, Any]]:
        """Return the fixture news payload, honouring ``count``."""
        return list(self._attrs.get("news", []))[:count]


def _install_ticker(monkeypatch: pytest.MonkeyPatch, ticker: _StubTicker) -> None:
    """Point the yfinance provider at ``ticker`` instead of the live vendor."""
    from tradinglib.data.providers.yfinance_provider import YFinanceProvider

    monkeypatch.setattr(YFinanceProvider, "_ticker", staticmethod(lambda symbol: ticker))


async def test_yfinance_quote_uses_the_vendor_market_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tradinglib.data.providers.yfinance_provider import YFinanceProvider

    market_time = pd.Timestamp("2026-08-28 14:41:19-04:00")
    _install_ticker(
        monkeypatch,
        _StubTicker(
            fast_info=_StubFastInfo(
                lastPrice=319.69, previousClose=314.94, lastVolume=22_733_341
            ),
            history_metadata={"regularMarketTime": market_time},
        ),
    )
    quote = await YFinanceProvider().get_quote("aapl")
    assert quote.symbol == "AAPL"
    assert quote.price == pytest.approx(319.69)
    assert quote.prev_close == pytest.approx(314.94)
    assert quote.ts == datetime(2026, 8, 28, 18, 41, 19, tzinfo=timezone.utc)
    assert quote.bid is None and quote.ask is None, "Yahoo's free feed has no NBBO"
    assert quote.source == "yfinance"


async def test_yfinance_quote_without_a_price_raises_symbol_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tradinglib.data.providers.yfinance_provider import YFinanceProvider

    _install_ticker(
        monkeypatch, _StubTicker(fast_info=_StubFastInfo(lastPrice=None), history_metadata={})
    )
    with pytest.raises(SymbolNotFound):
        await YFinanceProvider().get_quote("NOPE")


async def test_yfinance_bars_map_the_history_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    from tradinglib.data.providers.yfinance_provider import YFinanceProvider

    index = pd.DatetimeIndex(
        [pd.Timestamp("2026-08-26 00:00:00-04:00"), pd.Timestamp("2026-08-27 00:00:00-04:00")],
        name="Date",
    )
    frame = pd.DataFrame(
        {
            "Open": [310.3, 310.55],
            "High": [315.43, 315.4],
            "Low": [309.0, 310.0],
            "Close": [314.9, 315.0],
            "Volume": [1000, 2000],
        },
        index=index,
    )
    _install_ticker(monkeypatch, _StubTicker(history=lambda **kwargs: frame))

    bars = await YFinanceProvider().get_bars("AAPL")
    assert len(bars) == 2
    assert bars[0].ts == datetime(2026, 8, 26, 4, 0, tzinfo=timezone.utc)
    assert bars[0].close == pytest.approx(314.9)
    assert bars[0].interval is BarInterval.DAY_1
    assert bars[-1].volume == pytest.approx(2000)


async def test_yfinance_bars_limit_takes_the_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    from tradinglib.data.providers.yfinance_provider import YFinanceProvider

    index = pd.date_range("2026-01-01", periods=6, freq="D", tz="UTC")
    frame = pd.DataFrame(
        {
            "Open": range(6),
            "High": range(6),
            "Low": range(6),
            "Close": [float(i) for i in range(6)],
            "Volume": [10] * 6,
        },
        index=index,
    )
    _install_ticker(monkeypatch, _StubTicker(history=lambda **kwargs: frame))

    bars = await YFinanceProvider().get_bars("AAPL", limit=2)
    assert [bar.close for bar in bars] == [4.0, 5.0]


async def test_yfinance_empty_history_returns_no_bars(monkeypatch: pytest.MonkeyPatch) -> None:
    from tradinglib.data.providers.yfinance_provider import YFinanceProvider

    _install_ticker(monkeypatch, _StubTicker(history=lambda **kwargs: pd.DataFrame()))
    assert await YFinanceProvider().get_bars("AAPL") == []


async def test_yfinance_fundamentals_keep_missing_fields_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tradinglib.data.providers.yfinance_provider import YFinanceProvider

    _install_ticker(
        monkeypatch,
        _StubTicker(
            info={
                "longName": "Apple Inc.",
                "sector": "Technology",
                "currency": "USD",
                "marketCap": 4_665_467_469_824,
                "totalRevenue": 466_822_987_776,
                "grossProfits": 227_123_003_392,
                "mostRecentQuarter": 1_782_518_400,
                "trailingPE": 36.7,
                # no enterpriseValue / totalDebt in this payload
            }
        ),
    )
    fundamentals = await YFinanceProvider().get_fundamentals("AAPL")
    assert fundamentals.name == "Apple Inc."
    assert fundamentals.as_of == date(2026, 6, 27)
    assert fundamentals.enterprise_value is None
    assert fundamentals.total_debt is None
    assert fundamentals.net_cash is None
    assert fundamentals.gross_margin == pytest.approx(0.4865, rel=1e-3)
    assert fundamentals.extras["trailingPE"] == pytest.approx(36.7)
    assert "enterpriseValue" not in fundamentals.extras


async def test_yfinance_unknown_company_raises_symbol_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tradinglib.data.providers.yfinance_provider import YFinanceProvider

    _install_ticker(monkeypatch, _StubTicker(info={}))
    with pytest.raises(SymbolNotFound):
        await YFinanceProvider().get_fundamentals("NOPE")


async def test_yfinance_statements_are_grouped_by_period(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tradinglib.data.providers.yfinance_provider import YFinanceProvider

    statement = pd.DataFrame(
        {
            pd.Timestamp("2025-09-30"): [416_161_000_000.0, 112_010_000_000.0],
            pd.Timestamp("2024-09-30"): [391_035_000_000.0, 93_736_000_000.0],
        },
        index=["Total Revenue", "Net Income"],
    )
    empty = pd.DataFrame()
    _install_ticker(
        monkeypatch,
        _StubTicker(
            income_stmt=statement,
            balance_sheet=empty,
            cash_flow=empty,
            quarterly_income_stmt=empty,
            quarterly_balance_sheet=empty,
            quarterly_cash_flow=empty,
        ),
    )
    payload = await YFinanceProvider().get_financial_statements("AAPL")
    annual = payload["annual"]["income_statement"]
    assert [row["period"] for row in annual] == ["2025-09-30", "2024-09-30"]
    assert annual[0]["Total Revenue"] == pytest.approx(416_161_000_000.0)
    assert payload["annual"]["balance_sheet"] == [], "absent statements stay empty"


async def test_yfinance_news_maps_the_content_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tradinglib.data.providers.yfinance_provider import YFinanceProvider

    _install_ticker(
        monkeypatch,
        _StubTicker(
            news=[
                {
                    "content": {
                        "title": "Older story",
                        "summary": "s1",
                        "pubDate": "2026-08-27T10:00:00Z",
                        "provider": {"displayName": "Reuters"},
                        "canonicalUrl": {"url": "https://example.com/1"},
                    }
                },
                {
                    "content": {
                        "title": "Newer story",
                        "pubDate": "2026-08-28T10:00:00Z",
                        "provider": {"displayName": "Quartz"},
                        "clickThroughUrl": {"url": "https://example.com/2"},
                    }
                },
                {"not_content": True},
            ]
        ),
    )
    items = await YFinanceProvider().get_news("AAPL", limit=5)
    assert [item.headline for item in items] == ["Newer story", "Older story"]
    assert items[0].url == "https://example.com/2"
    assert items[0].publisher == "Quartz"
    assert items[0].sentiment is None, "the provider must not invent sentiment"
    assert items[1].published_at == datetime(2026, 8, 27, 10, 0, tzinfo=timezone.utc)


# --- SEC EDGAR ------------------------------------------------------------

TICKER_INDEX = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 1067983, "ticker": "BRK-B", "title": "BERKSHIRE HATHAWAY INC"},
    "2": {"cik_str": 63330, "ticker": "MLP", "title": "MAUI LAND & PINEAPPLE CO INC"},
}

SUBMISSIONS = {
    "cik": "320193",
    "name": "Apple Inc.",
    "filings": {
        "recent": {
            "accessionNumber": ["0000320193-26-000020", "0000320193-25-000079"],
            "form": ["10-Q", "10-K"],
            "filingDate": ["2026-07-31", "2025-10-31"],
            "reportDate": ["2026-06-27", "2025-09-27"],
            "primaryDocument": ["aapl-20260627.htm", ""],
            "primaryDocDescription": ["10-Q", "10-K"],
        }
    },
}

CONCEPT = {
    "units": {
        "USD": [
            {"end": "2025-09-27", "val": 100.0, "filed": "2025-10-31"},
            {"end": "2025-09-27", "val": 110.0, "filed": "2026-01-30"},
            {"end": "2026-06-27", "val": 120.0, "filed": "2026-07-31"},
            {"end": None, "val": 1.0, "filed": "2026-07-31"},
        ]
    }
}


def _install_edgar_json(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Serve EDGAR fixtures instead of HTTP; returns the list of URLs asked for."""
    from tradinglib.data.providers import edgar_provider

    seen: list[str] = []

    async def fake_get_json(self: Any, url: str) -> Any:
        seen.append(url)
        if url == edgar_provider.TICKERS_URL:
            return TICKER_INDEX
        if "/submissions/" in url:
            return SUBMISSIONS
        if "/companyconcept/" in url:
            return CONCEPT
        if "/companyfacts/" in url:
            return {"cik": 320193, "entityName": "Apple Inc.", "facts": {"us-gaap": {}}}
        raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr(edgar_provider.EdgarProvider, "_get_json", fake_get_json)
    return seen


def test_edgar_uses_the_spec_user_agent() -> None:
    from tradinglib.data.providers.edgar_provider import SPEC_USER_AGENT, EdgarProvider

    assert SPEC_USER_AGENT == "TradingLib research mantelrs@gmail.com"
    assert EdgarProvider().user_agent == SPEC_USER_AGENT
    assert EdgarProvider(user_agent="Custom UA me@example.com").user_agent == (
        "Custom UA me@example.com"
    )


def test_edgar_normalise_cik() -> None:
    from tradinglib.data.providers.edgar_provider import normalise_cik

    assert normalise_cik(320193) == "0000320193"
    assert normalise_cik("0000320193") == "0000320193"
    assert normalise_cik("CIK0000320193") == "0000320193"
    with pytest.raises(ValueError):
        normalise_cik("not-a-cik")


async def test_edgar_resolves_ciks_including_dotted_tickers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tradinglib.data.providers.edgar_provider import EdgarProvider

    urls = _install_edgar_json(monkeypatch)
    provider = EdgarProvider()
    assert await provider.resolve_cik("aapl") == "0000320193"
    assert await provider.resolve_cik("BRK.B") == "0001067983"
    assert len(urls) == 1, "the ticker index must be cached after the first fetch"
    with pytest.raises(SymbolNotFound):
        await provider.resolve_cik("NOPE")


async def test_edgar_filings_build_real_archive_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    from tradinglib.data.providers.edgar_provider import EdgarProvider

    _install_edgar_json(monkeypatch)
    filings = await EdgarProvider().get_filings("AAPL")
    assert [f.form for f in filings] == ["10-Q", "10-K"]
    assert filings[0].filed_at == date(2026, 7, 31)
    assert filings[0].period_of_report == date(2026, 6, 27)
    assert filings[0].url == (
        "https://www.sec.gov/Archives/edgar/data/320193/000032019326000020/aapl-20260627.htm"
    )
    assert filings[1].url.endswith("0000320193-25-000079-index.htm"), (
        "a filing without a primary document falls back to its index page"
    )


async def test_edgar_filings_filter_by_form(monkeypatch: pytest.MonkeyPatch) -> None:
    from tradinglib.data.providers.edgar_provider import EdgarProvider

    _install_edgar_json(monkeypatch)
    filings = await EdgarProvider().get_filings("AAPL", forms=["10-k"])
    assert [f.form for f in filings] == ["10-K"]


async def test_edgar_concept_keeps_the_latest_restatement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tradinglib.data.providers.edgar_provider import EdgarProvider

    _install_edgar_json(monkeypatch)
    points = await EdgarProvider().get_concept("AAPL", "us-gaap", "Revenues")
    assert points == [(date(2025, 9, 27), 110.0), (date(2026, 6, 27), 120.0)]

    series = await EdgarProvider().get_concept_series("AAPL", "us-gaap", "Revenues")
    assert series.unit == "USD"
    assert series.points[0][0] == datetime(2025, 9, 27, tzinfo=timezone.utc)


async def test_edgar_search_ranks_exact_tickers_first(monkeypatch: pytest.MonkeyPatch) -> None:
    from tradinglib.data.providers.edgar_provider import EdgarProvider

    _install_edgar_json(monkeypatch)
    results = await EdgarProvider().search("apple", limit=5)
    assert results[0]["symbol"] == "AAPL", "a name-prefix match must outrank PINEAPPLE"
    assert {row["symbol"] for row in results} == {"AAPL", "MLP"}


# --- FRED -----------------------------------------------------------------

CSV_NEW_HEADER = "observation_date,DGS10\n2026-08-03,4.70\n2026-08-04,.\n2026-08-05,4.63\n"
CSV_OLD_HEADER = "DATE,DGS10\n2026-08-03,4.70\n2026-08-04,\n"


def test_fred_csv_parser_handles_both_header_styles_and_gaps() -> None:
    from tradinglib.data.providers.fred_provider import FredProvider

    new_style = FredProvider.parse_csv("DGS10", CSV_NEW_HEADER)
    assert new_style == [(date(2026, 8, 3), 4.70), (date(2026, 8, 5), 4.63)]

    old_style = FredProvider.parse_csv("DGS10", CSV_OLD_HEADER)
    assert old_style == [(date(2026, 8, 3), 4.70)]

    assert FredProvider.parse_csv("DGS10", "") == []
    assert FredProvider.parse_csv("DGS10", "observation_date\n") == []


def test_fred_user_agent_keeps_a_client_token() -> None:
    from tradinglib.data.providers.fred_provider import USER_AGENT

    assert USER_AGENT.startswith("TradingLib research (python-httpx/")


async def test_fred_series_stores_dates_as_utc_midnight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tradinglib.data.providers import fred_provider

    async def fake_request(self: Any, url: str, params: dict[str, Any]) -> Any:
        assert url == fred_provider.CSV_URL
        assert params["id"] == "DGS10"

        class _Response:
            text = CSV_NEW_HEADER

        return _Response()

    monkeypatch.setattr(fred_provider.FredProvider, "_request", fake_request)
    series = await fred_provider.FredProvider(api_key=None).get_series("dgs10")
    assert series.name == "DGS10"
    assert series.meta["mode"] == "csv"
    assert series.points[0] == (datetime(2026, 8, 3, tzinfo=timezone.utc), 4.70)
    assert len(series) == 2


async def test_fred_metadata_and_search_need_a_key() -> None:
    from tradinglib.data.providers.fred_provider import FredProvider

    provider = FredProvider(api_key=None)
    assert provider.uses_api is False
    with pytest.raises(ProviderNotConfigured):
        await provider.get_series_meta("DGS10")
    with pytest.raises(ProviderNotConfigured):
        await provider.search("inflation")


async def test_fred_html_error_page_is_reported_as_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tradinglib.data.providers import fred_provider

    async def fake_request(self: Any, url: str, params: dict[str, Any]) -> Any:
        class _Response:
            text = "<html><body>Bad Request</body></html>"

        return _Response()

    monkeypatch.setattr(fred_provider.FredProvider, "_request", fake_request)
    with pytest.raises(SymbolNotFound):
        await fred_provider.FredProvider(api_key=None).get_series("NOPE")


# --- Google Trends --------------------------------------------------------


def test_trends_keyword_list_is_capped_and_deduplicated() -> None:
    from tradinglib.data.providers.trends_provider import MAX_KEYWORDS, TrendsProvider

    cleaned = TrendsProvider._normalise_keywords(
        [" a ", "a", "b", "c", "d", "e", "f", "", "   "]
    )
    assert cleaned == ["a", "b", "c", "d", "e"]
    assert len(cleaned) == MAX_KEYWORDS


async def test_trends_series_excludes_partial_buckets(monkeypatch: pytest.MonkeyPatch) -> None:
    from tradinglib.data.providers import trends_provider

    index = pd.DatetimeIndex(
        [pd.Timestamp("2026-08-26"), pd.Timestamp("2026-08-27"), pd.Timestamp("2026-08-28")]
    )
    frame = pd.DataFrame(
        {"iphone": [70.0, 72.0, 5.0], "isPartial": [False, False, True]}, index=index
    )

    async def fake_frame(self: Any, keywords: list[str], timeframe: str, geo: str) -> pd.DataFrame:
        return frame

    monkeypatch.setattr(trends_provider.TrendsProvider, "_interest_frame", fake_frame)
    provider = trends_provider.TrendsProvider()

    points = await provider.interest_points(["iphone"])
    assert len(points) == 3
    assert points[-1].partial is True

    series = await provider.interest_over_time(["iphone"])
    assert len(series["iphone"]) == 2, "the partial trailing bucket is not stored"
    assert series["iphone"].unit == "index 0-100"
    assert series["iphone"].points[0] == (
        datetime(2026, 8, 26, tzinfo=timezone.utc),
        70.0,
    )


async def test_trends_rate_limit_is_retried_then_surfaced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pytrends import exceptions as pytrends_exceptions

    from tradinglib.data.providers import trends_provider

    attempts = 0

    class _ThrottlingClient:
        def build_payload(self, *args: Any, **kwargs: Any) -> None:
            nonlocal attempts
            attempts += 1
            raise pytrends_exceptions.TooManyRequestsError("429", response=None)

    monkeypatch.setattr(trends_provider, "BACKOFF_SECONDS", (0.0, 0.0))
    monkeypatch.setattr(
        trends_provider.TrendsProvider, "_build_client", lambda self: _ThrottlingClient()
    )

    provider = trends_provider.TrendsProvider()
    with pytest.raises(ProviderRateLimited) as excinfo:
        await provider.interest_points(["iphone"])
    assert attempts == 3, "two retries plus the initial attempt"
    assert excinfo.value.retry_after == 0.0

    status = await provider.status()
    assert status.reachable is False
    assert "rate limited" in (status.detail or "")


async def test_trends_response_error_becomes_provider_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pytrends import exceptions as pytrends_exceptions

    from tradinglib.data.providers import trends_provider

    class _BrokenClient:
        def build_payload(self, *args: Any, **kwargs: Any) -> None:
            raise pytrends_exceptions.ResponseError("boom", response=None)

    monkeypatch.setattr(
        trends_provider.TrendsProvider, "_build_client", lambda self: _BrokenClient()
    )
    with pytest.raises(ProviderUnavailable):
        await trends_provider.TrendsProvider().interest_points(["iphone"])


# --- Alpaca ---------------------------------------------------------------


async def test_alpaca_without_keys_is_honestly_unconfigured() -> None:
    from tradinglib.data.providers.alpaca_provider import AlpacaProvider

    provider = AlpacaProvider(api_key=None, secret_key=None)
    provider.api_key = None
    provider.secret_key = None
    assert provider.configured is False

    status = await provider.status()
    assert status.configured is False
    assert status.reachable is None, "an unconfigured provider must not claim reachability"
    assert "not configured" in (status.detail or "")

    for call in (provider.get_quote("AAPL"), provider.get_bars("AAPL")):
        with pytest.raises(ProviderNotConfigured):
            await call


async def test_alpaca_stream_without_keys_refuses_to_start() -> None:
    from tradinglib.data.providers.alpaca_provider import AlpacaStream
    from tradinglib.events import EventBus

    stream = AlpacaStream(EventBus(), api_key=None, secret_key=None)
    stream.api_key = None
    stream.secret_key = None

    assert stream.configured is False
    assert stream.running is False
    assert stream.status()["messages_published"] == 0
    with pytest.raises(ProviderNotConfigured):
        await stream.start()
    await stream.stop()  # must be safe even when nothing started


async def test_alpaca_stream_publishes_tape_events_to_the_bus() -> None:
    from tradinglib.data.providers.alpaca_provider import AlpacaStream
    from tradinglib.events import Event, EventBus

    bus = EventBus()
    received: list[Event] = []
    bus.subscribe("tape", received.append)

    stream = AlpacaStream(bus, api_key="k", secret_key="s")

    class _Trade:
        symbol = "AAPL"
        price = 319.7
        size = 100
        timestamp = datetime(2026, 8, 28, 18, 41, tzinfo=timezone.utc)
        exchange = "V"

    class _Quote:
        symbol = "AAPL"
        bid_price = 319.6
        ask_price = 319.8
        bid_size = 2
        ask_size = 3
        timestamp = datetime(2026, 8, 28, 18, 41, 1, tzinfo=timezone.utc)

    await stream._on_trade(_Trade())
    await stream._on_quote(_Quote())

    assert [event.channel for event in received] == ["tape.AAPL", "tape.AAPL"]
    assert received[0].payload["kind"] == "trade"
    assert received[0].payload["price"] == pytest.approx(319.7)
    assert received[0].payload["ts"] == "2026-08-28T18:41:00+00:00"
    assert received[1].payload["kind"] == "quote"
    assert received[1].payload["bid"] == pytest.approx(319.6)
    assert stream.status()["messages_published"] == 2


def test_alpaca_interval_map_covers_every_bar_interval() -> None:
    from tradinglib.data.providers.alpaca_provider import TIMEFRAMES

    assert set(TIMEFRAMES) == set(BarInterval)


# --- registry -------------------------------------------------------------


def test_provider_registry_resolves_every_configured_vendor() -> None:
    from tradinglib.data import providers

    for provider_id, class_name in providers.PROVIDER_NAMES.items():
        assert providers.get_provider_class(provider_id).__name__ == class_name
    with pytest.raises(KeyError):
        providers.get_provider_class("does-not-exist")
