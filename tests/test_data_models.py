"""Offline tests for the data models in :mod:`tradinglib.data.base`."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from tradinglib.data.base import (
    Bar,
    BarInterval,
    Filing,
    Fundamentals,
    NewsItem,
    ProviderError,
    ProviderNotConfigured,
    ProviderRateLimited,
    ProviderStatus,
    Quote,
    Series,
    TrendPoint,
    to_utc,
    utcnow,
)

UTC_NOON = datetime(2026, 3, 4, 12, 0, tzinfo=timezone.utc)


def test_quote_derives_change_and_serialises() -> None:
    quote = Quote(symbol="AAPL", price=110.0, ts=UTC_NOON, prev_close=100.0, source="test")
    assert quote.change == 10.0
    assert quote.change_pct == pytest.approx(10.0)
    payload = quote.as_dict()
    assert payload["ts"] == "2026-03-04T12:00:00+00:00"
    assert json.dumps(payload)


def test_quote_without_prev_close_reports_none_not_zero() -> None:
    quote = Quote(symbol="AAPL", price=110.0, ts=UTC_NOON)
    assert quote.change is None
    assert quote.change_pct is None


def test_bar_interval_metadata() -> None:
    assert BarInterval.MIN_5.approx_seconds == 300
    assert BarInterval.MIN_5.intraday is True
    assert BarInterval.DAY_1.intraday is False
    assert str(BarInterval.DAY_1) == "1d"


def test_series_sorted_deduplicates_and_orders() -> None:
    later = UTC_NOON.replace(hour=13)
    series = Series(
        name="x",
        points=[(later, 2.0), (UTC_NOON, 1.0), (later, 3.0)],
        source="test",
    )
    ordered = series.sorted()
    assert ordered.points == [(UTC_NOON, 1.0), (later, 3.0)]
    assert series.points[0] == (later, 2.0), "sorted() must not mutate the original"


def test_series_as_dict_is_json_safe() -> None:
    series = Series(name="DGS10", points=[(UTC_NOON, 4.5)], unit="Percent", source="fred")
    payload = series.as_dict()
    assert payload["points"] == [["2026-03-04T12:00:00+00:00", 4.5]]
    assert json.dumps(payload)


def test_fundamentals_derived_ratios_and_missing_data() -> None:
    full = Fundamentals(
        symbol="AAPL",
        revenue_ttm=200.0,
        gross_profit_ttm=90.0,
        enterprise_value=1000.0,
        total_debt=30.0,
        cash_and_equivalents=50.0,
    )
    assert full.net_cash == 20.0
    assert full.gross_margin == 0.45
    assert full.ev_to_sales == 5.0

    empty = Fundamentals(symbol="AAPL")
    assert empty.net_cash is None
    assert empty.gross_margin is None
    assert empty.ev_to_sales is None


def test_bar_news_filing_trendpoint_serialise() -> None:
    bar = Bar("AAPL", UTC_NOON, 1.0, 2.0, 0.5, 1.5, 100.0, BarInterval.DAY_1, "test")
    news = NewsItem("AAPL", "headline", "https://example.com", UTC_NOON, source="test")
    filing = Filing("AAPL", "0000320193", "10-K", date(2026, 1, 2), "acc", "https://example.com")
    trend = TrendPoint("iphone", UTC_NOON, 55.0, partial=True, geo="US")
    for model in (bar, news, filing, trend):
        assert json.dumps(model.as_dict())
    assert bar.as_dict()["interval"] == "1d"
    assert filing.as_dict()["filed_at"] == "2026-01-02"
    assert trend.as_dict()["partial"] is True


def test_provider_status_serialises_none_fields() -> None:
    status = ProviderStatus(name="alpaca", configured=False, detail="not configured")
    payload = status.as_dict()
    assert payload["configured"] is False
    assert payload["reachable"] is None
    assert json.dumps(payload)


def test_provider_error_hierarchy_carries_provider_name() -> None:
    error = ProviderRateLimited("trends", "throttled", retry_after=15.0)
    assert isinstance(error, ProviderError)
    assert error.provider == "trends"
    assert error.retry_after == 15.0
    assert str(error) == "trends: throttled"
    assert isinstance(ProviderNotConfigured("alpaca", "no keys"), ProviderError)


def test_to_utc_and_utcnow_are_timezone_aware() -> None:
    naive = datetime(2026, 3, 4, 12, 0)
    assert to_utc(naive) == UTC_NOON

    eastern = timezone(timedelta(hours=-5))
    assert to_utc(datetime(2026, 3, 4, 7, 0, tzinfo=eastern)) == UTC_NOON
    assert utcnow().tzinfo is timezone.utc
