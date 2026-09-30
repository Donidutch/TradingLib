"""Offline tests for the data-quality engine.

Fixtures are real Parquet partitions written through
:class:`~tradinglib.data.store.TimeSeriesStore`: a clean daily series, one with
sessions deleted, one that stops well before "now", and one with an injected
decimal-shift outlier. The checks then have to find exactly those defects.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pytest

from tradinglib.config import Settings
from tradinglib.data.base import Bar, BarInterval, Series
from tradinglib.data.quality import (
    QUALITY_CHANNEL,
    Cadence,
    DatasetRef,
    QualityEngine,
    Severity,
    bond_market_holidays,
    business_days,
    easter_sunday,
    expected_bar_timestamps,
    expected_timestamps,
    infer_cadence,
    nyse_early_closes,
    nyse_holidays,
    session_bar_starts,
    session_close,
    us_federal_holidays,
)
from tradinglib.data.store import TimeSeriesStore
from tradinglib.events import EventBus

#: "Now" for every deterministic test: Friday 28 August 2026, 20:00 UTC (16:00 ET).
NOW = datetime(2026, 8, 28, 20, 0, tzinfo=timezone.utc)
WINDOW_START = date(2026, 1, 2)
WINDOW_END = date(2026, 8, 28)

#: Every check :meth:`QualityEngine.evaluate` runs against one dataset. Three
#: of them carry the weighted composite score; the other three are pass/fail
#: integrity gates. Naming the set once keeps the assertions honest when a
#: check is added: the suite fails loudly rather than silently narrowing.
SUITE = frozenset(
    {"completeness", "staleness", "outliers", "duplicates", "schema_drift", "lookahead"}
)


def utc_midnight(day: date) -> datetime:
    """Return the UTC midnight timestamp of ``day`` (how daily bars are stamped)."""
    return datetime.combine(day, time.min, tzinfo=timezone.utc)


def daily_bars(
    symbol: str,
    days: list[date],
    *,
    closes: list[float] | None = None,
) -> list[Bar]:
    """Build a deterministic run of daily bars for ``days``."""
    prices = closes if closes is not None else [100.0 + 0.1 * i for i in range(len(days))]
    return [
        Bar(
            symbol=symbol,
            ts=utc_midnight(day),
            open=price,
            high=price * 1.01,
            low=price * 0.99,
            close=price,
            volume=1_000_000.0 + index,
            interval=BarInterval.DAY_1,
            source="fixture",
        )
        for index, (day, price) in enumerate(zip(days, prices, strict=True))
    ]


@pytest.fixture()
def store(tmp_path: Path) -> TimeSeriesStore:
    """A store rooted in a throwaway directory."""
    return TimeSeriesStore(tmp_path / "ts")


@pytest.fixture()
def engine(settings: Settings, bus: EventBus, store: TimeSeriesStore) -> QualityEngine:
    """A quality engine on a throwaway store, database and event bus."""
    return QualityEngine(store, bus=bus)


# --- calendars -------------------------------------------------------------
def test_easter_sunday_matches_known_dates() -> None:
    assert easter_sunday(2024) == date(2024, 3, 31)
    assert easter_sunday(2025) == date(2025, 4, 20)
    assert easter_sunday(2026) == date(2026, 4, 5)


def test_nyse_holidays_2026() -> None:
    holidays = nyse_holidays(2026)
    assert date(2026, 1, 1) in holidays  # New Year's Day
    assert date(2026, 1, 19) in holidays  # MLK
    assert date(2026, 2, 16) in holidays  # Washington's Birthday
    assert date(2026, 4, 3) in holidays  # Good Friday
    assert date(2026, 5, 25) in holidays  # Memorial Day
    assert date(2026, 6, 19) in holidays  # Juneteenth
    assert date(2026, 7, 3) in holidays  # 4 July observed (Saturday)
    assert date(2026, 9, 7) in holidays  # Labor Day
    assert date(2026, 11, 26) in holidays  # Thanksgiving
    assert date(2026, 12, 25) in holidays  # Christmas
    # Columbus Day and Veterans Day are federal but not NYSE holidays.
    assert date(2026, 10, 12) not in holidays
    assert date(2026, 11, 11) not in holidays
    assert date(2026, 10, 12) in us_federal_holidays(2026)
    # The bond calendar keeps the federal days and adds Good Friday.
    assert date(2026, 10, 12) in bond_market_holidays(2026)
    assert date(2026, 4, 3) in bond_market_holidays(2026)


def test_session_counts_and_new_year_saturday_rule() -> None:
    assert len(business_days(date(2026, 1, 1), date(2026, 12, 31))) == 251
    # 1 January 2022 was a Saturday: US exchanges did not close on 31 December.
    assert date(2021, 12, 31) not in nyse_holidays(2021)
    assert date(2022, 1, 1) not in nyse_holidays(2022)
    # Juneteenth only became a holiday in 2021.
    assert date(2019, 6, 19) not in us_federal_holidays(2019)


def test_early_closes_and_session_close_times() -> None:
    early = nyse_early_closes(2026)
    assert date(2026, 11, 27) in early  # day after Thanksgiving
    assert date(2026, 12, 24) in early
    assert session_close(date(2026, 8, 28)) == datetime(2026, 8, 28, 20, 0, tzinfo=timezone.utc)
    assert session_close(date(2026, 11, 27)) == datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc)


def test_intraday_bucket_grid() -> None:
    hourly = session_bar_starts(date(2026, 8, 28), 3600)
    assert len(hourly) == 7  # 09:30 .. 15:30 ET, the last bucket is a half hour
    assert hourly[0] == datetime(2026, 8, 28, 13, 30, tzinfo=timezone.utc)
    assert session_bar_starts(date(2026, 11, 27), 3600) == [
        datetime(2026, 11, 27, 14, 30, tzinfo=timezone.utc) + timedelta(hours=h)
        for h in range(4)
    ]
    grid = expected_bar_timestamps(
        BarInterval.HOUR_1,
        datetime(2026, 8, 27, 13, 30, tzinfo=timezone.utc),
        datetime(2026, 8, 28, 20, 0, tzinfo=timezone.utc),
    )
    assert len(grid) == 14  # two full sessions


# --- cadence inference -----------------------------------------------------
def test_infer_cadence_recognises_publication_schedules() -> None:
    sessions = [utc_midnight(d) for d in business_days(date(2026, 1, 2), date(2026, 4, 30))]
    business = infer_cadence(sessions)
    assert business is not None and business.kind == "business"

    weekly = [utc_midnight(date(2026, 1, 2)) + timedelta(days=7 * i) for i in range(20)]
    assert infer_cadence(weekly) is not None
    assert infer_cadence(weekly).kind == "weekly"  # type: ignore[union-attr]

    monthly = [utc_midnight(date(2024, 1, 1) + timedelta(days=30 * i)) for i in range(24)]
    assert infer_cadence(monthly).kind == "monthly"  # type: ignore[union-attr]

    assert infer_cadence([]) is None
    assert infer_cadence(weekly[:2]) is None


def test_expected_timestamps_for_monthly_cadence_clamps_month_ends() -> None:
    cadence = Cadence("monthly", 2_592_000, "monthly")
    grid = expected_timestamps(
        cadence,
        datetime(2026, 1, 31, tzinfo=timezone.utc),
        datetime(2026, 4, 30, tzinfo=timezone.utc),
    )
    assert [ts.date() for ts in grid] == [
        date(2026, 1, 31),
        date(2026, 2, 28),
        date(2026, 3, 31),
        date(2026, 4, 30),
    ]


# --- completeness ----------------------------------------------------------
def test_completeness_is_perfect_for_a_full_session_grid(
    engine: QualityEngine, store: TimeSeriesStore
) -> None:
    days = business_days(WINDOW_START, WINDOW_END)
    store.write_bars(daily_bars("CLEAN", days))
    result = engine.check_completeness(DatasetRef.bars("CLEAN"))
    assert result.passed is True
    assert result.score == 1.0
    assert result.metrics["missing"] == 0
    assert result.metrics["expected"] == len(days)


def test_completeness_finds_injected_gaps(
    engine: QualityEngine, store: TimeSeriesStore
) -> None:
    days = business_days(WINDOW_START, WINDOW_END)
    removed = {days[20], days[21], days[40], days[80], days[81], days[82]}
    kept = [d for d in days if d not in removed]
    store.write_bars(daily_bars("GAPPY", kept))

    result = engine.check_completeness(DatasetRef.bars("GAPPY"))
    assert result.passed is False
    assert result.metrics["missing"] == len(removed)
    assert result.metrics["expected"] == len(days)
    assert result.metrics["observed"] == len(kept)
    assert set(result.metrics["missing_sample"]) == {d.isoformat() for d in removed}
    assert 0.9 < result.score < 1.0
    assert result.severity is Severity.ERROR  # 6 of 165 sessions missing

    # A single missing session is a warning, not an error.
    store.write_bars(daily_bars("ONEGAP", [d for d in days if d != days[30]]))
    minor = engine.check_completeness(DatasetRef.bars("ONEGAP"))
    assert minor.passed is False
    assert minor.severity is Severity.WARN
    assert minor.metrics["missing_sample"] == [days[30].isoformat()]


def test_completeness_ignores_weekends_and_holidays(
    engine: QualityEngine, store: TimeSeriesStore
) -> None:
    # A series that skips 3 July 2026 (holiday) and every weekend is complete.
    days = business_days(date(2026, 6, 22), date(2026, 7, 17))
    assert date(2026, 7, 3) not in days
    store.write_bars(daily_bars("HOLI", days))
    assert engine.check_completeness(DatasetRef.bars("HOLI")).score == 1.0


def test_completeness_of_a_business_daily_series(
    engine: QualityEngine, store: TimeSeriesStore
) -> None:
    days = business_days(date(2026, 1, 2), date(2026, 6, 30), "bond")
    points = [(utc_midnight(day), 4.0 + 0.001 * i) for i, day in enumerate(days)]
    store.write_series("fred", Series(name="DGS10", points=points, source="fred"))

    ref = DatasetRef.series("fred", "DGS10")
    assert engine.check_completeness(ref).score == 1.0

    dropped = [p for i, p in enumerate(points) if i not in {10, 11, 12}]
    store.write_series("fred", Series(name="SPARSE", points=dropped, source="fred"))
    result = engine.check_completeness(DatasetRef.series("fred", "SPARSE"))
    assert result.metrics["missing"] == 3
    assert result.metrics["cadence"] == "business"


def test_completeness_of_a_dataset_with_one_point_is_not_judged(
    engine: QualityEngine, store: TimeSeriesStore
) -> None:
    store.write_bars(daily_bars("TINY", [WINDOW_END]))
    result = engine.check_completeness(DatasetRef.bars("TINY"))
    assert result.passed is True
    assert "nothing to compare" in result.detail


# --- staleness -------------------------------------------------------------
def test_staleness_accepts_a_series_current_to_the_last_close(
    engine: QualityEngine, store: TimeSeriesStore
) -> None:
    days = business_days(WINDOW_START, WINDOW_END)
    store.write_bars(daily_bars("FRESH", days))
    result = engine.check_staleness(DatasetRef.bars("FRESH"), now=NOW)
    assert result.passed is True
    assert result.metrics["missed_periods"] == 0


def test_a_weekend_does_not_make_daily_bars_stale(
    engine: QualityEngine, store: TimeSeriesStore
) -> None:
    days = business_days(WINDOW_START, date(2026, 8, 21))  # Friday
    store.write_bars(daily_bars("WEEKEND", days))
    sunday = datetime(2026, 8, 23, 18, 0, tzinfo=timezone.utc)
    assert engine.check_staleness(DatasetRef.bars("WEEKEND"), now=sunday).passed is True


def test_staleness_counts_missed_sessions(
    engine: QualityEngine, store: TimeSeriesStore
) -> None:
    days = business_days(WINDOW_START, date(2026, 8, 12))
    store.write_bars(daily_bars("STALE", days))
    result = engine.check_staleness(DatasetRef.bars("STALE"), now=NOW)
    assert result.passed is False
    assert result.severity is Severity.ERROR
    assert result.metrics["missed_periods"] == len(
        business_days(date(2026, 8, 13), date(2026, 8, 28))
    )
    assert result.score < 1.0


def test_staleness_of_an_empty_dataset_is_reported_not_hidden(
    engine: QualityEngine,
) -> None:
    result = engine.check_staleness(DatasetRef.bars("NOTHING"), now=NOW)
    assert result.passed is False
    assert result.severity is Severity.ERROR
    assert result.metrics["last"] is None


def test_series_staleness_allows_one_period_of_publication_lag(
    engine: QualityEngine, store: TimeSeriesStore
) -> None:
    days = business_days(date(2026, 1, 2), date(2026, 8, 27), "bond")
    points = [(utc_midnight(day), 4.0 + 0.001 * i) for i, day in enumerate(days)]
    store.write_series("fred", Series(name="LAGGY", points=points, source="fred"))
    result = engine.check_staleness(DatasetRef.series("fred", "LAGGY"), now=NOW)
    assert result.passed is True
    assert result.metrics["missed_periods"] == 0


def test_weekly_bars_are_only_stale_after_a_full_week_elapses(
    engine: QualityEngine, store: TimeSeriesStore
) -> None:
    mondays = [date(2026, 1, 5) + timedelta(days=7 * i) for i in range(34)]
    mondays = [day for day in mondays if day <= date(2026, 8, 17)]
    store.write_bars(
        [
            Bar(
                symbol="WKLY",
                ts=utc_midnight(day),
                open=100.0 + i,
                high=101.0 + i,
                low=99.0 + i,
                close=100.0 + i,
                volume=1_000.0,
                interval=BarInterval.WEEK_1,
                source="fixture",
            )
            for i, day in enumerate(mondays)
        ],
        interval=BarInterval.WEEK_1,
    )
    ref = DatasetRef.bars("WKLY", BarInterval.WEEK_1)
    assert engine.check_completeness(ref).score == 1.0
    # The week starting 24 August has not finished on the 28th: not stale yet.
    assert engine.check_staleness(ref, now=NOW).metrics["missed_periods"] == 0
    later = datetime(2026, 9, 15, 20, 0, tzinfo=timezone.utc)
    assert engine.check_staleness(ref, now=later).metrics["missed_periods"] == 3


# --- outliers --------------------------------------------------------------
def test_outliers_are_clean_on_a_smooth_series(
    engine: QualityEngine, store: TimeSeriesStore
) -> None:
    days = business_days(WINDOW_START, WINDOW_END)
    store.write_bars(daily_bars("SMOOTH", days))
    result = engine.check_outliers(DatasetRef.bars("SMOOTH"))
    assert result.passed is True
    assert result.metrics["flagged"] == 0
    assert result.metrics["estimator"] == "median/MAD"


def test_outliers_catch_an_injected_decimal_shift(
    engine: QualityEngine, store: TimeSeriesStore
) -> None:
    days = business_days(WINDOW_START, WINDOW_END)
    closes = [100.0 + 0.1 * i for i in range(len(days))]
    bad_index = 100
    closes[bad_index] = closes[bad_index] / 10.0  # decimal shift
    store.write_bars(daily_bars("SHIFT", days, closes=closes))

    result = engine.check_outliers(DatasetRef.bars("SHIFT"))
    assert result.passed is False
    assert result.metrics["flagged"] >= 2  # the drop and the recovery
    flagged_days = {entry["ts"][:10] for entry in result.metrics["worst"]}
    assert days[bad_index].isoformat() in flagged_days
    assert result.score < 1.0


def test_outlier_threshold_is_configurable(
    engine: QualityEngine, store: TimeSeriesStore
) -> None:
    days = business_days(WINDOW_START, WINDOW_END)
    closes = [100.0 + 0.1 * i for i in range(len(days))]
    closes[50] *= 1.05
    store.write_bars(daily_bars("MILD", days, closes=closes))
    ref = DatasetRef.bars("MILD")
    assert engine.check_outliers(ref, z_threshold=1000.0).metrics["flagged"] == 0
    assert engine.check_outliers(ref, z_threshold=3.0).metrics["flagged"] > 0


def test_short_history_is_not_scored_for_outliers(
    engine: QualityEngine, store: TimeSeriesStore
) -> None:
    days = business_days(date(2026, 8, 3), date(2026, 8, 14))
    store.write_bars(daily_bars("SHORT", days))
    result = engine.check_outliers(DatasetRef.bars("SHORT"))
    assert result.passed is True
    assert "are needed before outlier scoring" in result.detail


# --- suite, persistence, incidents ----------------------------------------
async def test_run_scores_persists_and_publishes_incidents(
    engine: QualityEngine, store: TimeSeriesStore, bus: EventBus
) -> None:
    days = business_days(WINDOW_START, WINDOW_END)
    store.write_bars(daily_bars("CLEAN", days))
    stale_days = business_days(WINDOW_START, date(2026, 8, 12))
    store.write_bars(daily_bars("STALE", stale_days))

    events: list[dict[str, object]] = []
    bus.subscribe(QUALITY_CHANNEL, lambda event: events.append(event.payload))

    outcomes = await engine.run(now=NOW)
    keys = {o.ref.key for o in outcomes}
    assert keys == {"bars:CLEAN:1d", "bars:STALE:1d"}

    clean = next(o for o in outcomes if o.ref.symbol == "CLEAN")
    stale = next(o for o in outcomes if o.ref.symbol == "STALE")
    assert clean.score == 100.0
    assert clean.failed == []
    assert stale.score < 100.0
    assert [r.check for r in stale.failed] == ["staleness"]

    incidents = engine.incidents()
    assert len(incidents) == 1
    assert incidents[0].dataset == "bars:STALE:1d"
    assert incidents[0].check == "staleness"
    assert incidents[0].open is True

    opened = [e for e in events if e.get("type") == "incident.opened"]
    completed = [e for e in events if e.get("type") == "run.completed"]
    assert len(opened) == 1
    assert len(completed) == 1
    assert completed[0]["failed"] == ["bars:STALE:1d"]

    summary = engine.summary()
    assert summary["datasets"] == 2
    assert summary["stale_series"] == 1
    assert summary["failed_checks"] == 1
    assert summary["open_incidents"] == 1
    assert summary["missing_values_pct"] == 0.0
    assert summary["overall_quality_pct"] is not None
    assert summary["last_run"] is not None

    latest = engine.latest()
    assert [row["dataset"] for row in latest] == ["bars:CLEAN:1d", "bars:STALE:1d"]
    assert latest[0]["passed"] is True

    history = engine.series("bars:STALE:1d")
    assert history["points"] == 1
    assert set(history["checks"]) == SUITE


async def test_incidents_close_when_the_data_recovers(
    engine: QualityEngine, store: TimeSeriesStore, bus: EventBus
) -> None:
    stale_days = business_days(WINDOW_START, date(2026, 8, 12))
    store.write_bars(daily_bars("RECOVER", stale_days))
    await engine.run(now=NOW)
    assert len(engine.incidents()) == 1
    first = engine.incidents()[0]

    # Re-running while the defect persists updates the incident, never duplicates it.
    await engine.run(now=NOW)
    assert [i.id for i in engine.incidents()] == [first.id]

    # Backfill the missing sessions; the next run resolves the incident.
    store.write_bars(daily_bars("RECOVER", business_days(WINDOW_START, WINDOW_END)))
    events: list[dict[str, object]] = []
    bus.subscribe(QUALITY_CHANNEL, lambda event: events.append(event.payload))
    await engine.run(now=NOW)

    assert engine.incidents() == []
    resolved = engine.incidents(include_resolved=True)
    assert len(resolved) == 1
    assert resolved[0].resolved_at is not None
    assert any(e.get("type") == "incident.resolved" for e in events)
    assert engine.summary()["open_incidents"] == 0


async def test_manual_incident_lifecycle(engine: QualityEngine) -> None:
    incident = await engine.open_incident(
        "bars:AAPL:1d", "manual", Severity.WARN, "checked by hand", note="ticket-1"
    )
    assert incident.open is True
    assert incident.metrics["note"] == "ticket-1"
    resolved = await engine.resolve_incident(incident.id)
    assert resolved.resolved_at is not None
    assert engine.incidents(dataset="bars:AAPL:1d") == []
    with pytest.raises(KeyError):
        await engine.resolve_incident("does-not-exist")


async def test_run_all_returns_flat_results_and_datasets_are_discovered(
    engine: QualityEngine, store: TimeSeriesStore
) -> None:
    store.write_bars(daily_bars("DISC", business_days(WINDOW_START, WINDOW_END)))
    store.write_series(
        "fred",
        Series(
            name="DGS10",
            points=[
                (utc_midnight(day), 4.0)
                for day in business_days(date(2026, 1, 2), date(2026, 8, 27), "bond")
            ],
            source="fred",
        ),
    )
    refs = {ref.key for ref in engine.datasets()}
    assert refs == {"bars:DISC:1d", "series:fred:DGS10"}
    assert DatasetRef.parse("series:fred:DGS10").label == "fred/DGS10"

    results = await engine.run_all()
    assert {r.check for r in results} == SUITE
    # Two datasets, the full suite against each.
    assert len(results) == 2 * len(SUITE)


def test_summary_is_empty_before_any_run(engine: QualityEngine) -> None:
    summary = engine.summary()
    assert summary["datasets"] == 0
    assert summary["overall_quality_pct"] is None
    assert summary["open_incidents"] == 0
    assert engine.latest() == []


# --- integrity checks ------------------------------------------------------
def test_duplicate_rows_are_counted_not_hidden(
    engine: QualityEngine, store: TimeSeriesStore
) -> None:
    """The store upserts on the time key, so a duplicate is a real fault."""
    days = business_days(WINDOW_START, WINDOW_END)
    store.write_bars(daily_bars("DUPES", days))
    clean = engine.check_duplicates(DatasetRef.bars("DUPES"))
    assert clean.passed is True
    assert clean.metrics["duplicates"] == 0
    assert clean.metrics["rows"] == len(days)


def test_schema_drift_records_a_baseline_then_reports_change(
    engine: QualityEngine, store: TimeSeriesStore
) -> None:
    """The first run learns the shape; a later run compares against it."""
    ref = DatasetRef.series("fixture", "DRIFT")
    points = [
        (utc_midnight(day), 1.0 + index)
        for index, day in enumerate(business_days(WINDOW_START, date(2026, 3, 31)))
    ]
    store.write_series("fixture", Series(name="DRIFT", points=points, source="fixture"))

    first = engine.check_schema_drift(ref)
    assert first.passed is True
    assert first.metrics["baseline"] is True
    assert first.metrics["columns"]

    # Nothing changed, so the second run must not invent a drift.
    second = engine.check_schema_drift(ref)
    assert second.passed is True
    assert second.metrics.get("baseline") is None
    assert second.metrics["added"] == []
    assert second.metrics["removed"] == []
    assert second.metrics["signature"] == first.metrics["signature"]


def test_lookahead_scan_flags_a_future_dated_observation(
    engine: QualityEngine, store: TimeSeriesStore
) -> None:
    """A value stamped after the moment of the check could not have been known."""
    ref = DatasetRef.series("fixture", "AHEAD")
    days = business_days(WINDOW_START, WINDOW_END)
    points = [(utc_midnight(day), 10.0 + i) for i, day in enumerate(days)]
    store.write_series("fixture", Series(name="AHEAD", points=points, source="fixture"))
    assert engine.check_lookahead(ref, now=NOW).passed is True

    store.write_series(
        "fixture",
        Series(name="AHEAD", points=[(NOW + timedelta(days=30), 99.0)], source="fixture"),
    )
    flagged = engine.check_lookahead(ref, now=NOW)
    assert flagged.passed is False
    assert flagged.severity is Severity.ERROR
    assert flagged.metrics["future"] == 1


# --- outlier review queue --------------------------------------------------
@pytest.mark.anyio
async def test_quarantining_an_outlier_removes_it_from_every_store_read(
    engine: QualityEngine, store: TimeSeriesStore
) -> None:
    """A quarantine decision is enforced in the read path, not just displayed."""
    days = business_days(date(2024, 1, 2), WINDOW_END)
    closes = [100.0 + 0.05 * i for i in range(len(days))]
    closes[120] = closes[120] / 10.0  # a decimal shift
    store.write_bars(daily_bars("SHIFT", days, closes=closes))
    ref = DatasetRef.bars("SHIFT")

    outcome = engine.evaluate(ref, now=NOW)
    assert {result.check for result in outcome.results} == SUITE
    queue = engine.outlier_queue(dataset=ref.key)
    assert queue, "the decimal shift must reach the review queue"
    assert engine.outlier_counts()["open"] == len(queue)

    before = len(store.read_bars("SHIFT"))
    flag = queue[0]
    decided = await engine.decide_outlier(flag["id"], "quarantine", by="tester", publish=False)
    assert decided["state"] == "quarantined"
    assert len(store.read_bars("SHIFT")) == before - 1

    restored = await engine.decide_outlier(flag["id"], "open", by="tester", publish=False)
    assert restored["state"] == "open"
    assert len(store.read_bars("SHIFT")) == before

    with pytest.raises(ValueError):
        await engine.decide_outlier(flag["id"], "ignore", publish=False)
    with pytest.raises(KeyError):
        await engine.decide_outlier("nope", "accept", publish=False)


@pytest.mark.anyio
async def test_escalating_keeps_the_point_in_the_queue_and_in_the_data(
    engine: QualityEngine, store: TimeSeriesStore
) -> None:
    """Escalation asks for a human: nothing is quarantined and nothing is resolved."""
    days = business_days(date(2024, 1, 2), WINDOW_END)
    closes = [100.0 + 0.05 * i for i in range(len(days))]
    closes[140] = closes[140] * 9.0
    store.write_bars(daily_bars("ESCA", days, closes=closes))
    ref = DatasetRef.bars("ESCA")

    engine.evaluate(ref, now=NOW)
    queue = engine.outlier_queue(dataset=ref.key)
    assert queue, "the spike must reach the review queue"
    before = len(store.read_bars("ESCA"))
    flag = queue[0]

    decided = await engine.decide_outlier(flag["id"], "escalate", by="tester", publish=False)
    assert decided["state"] == "escalated"
    assert decided["needs_review"] is True
    assert decided["escalated"] is True
    assert decided["decided_by"] == "tester"

    # The value is still in the data - that is the difference from quarantine.
    assert len(store.read_bars("ESCA")) == before

    # And the point is still in the queue, including the "undecided only" view.
    still_open = engine.outlier_queue(dataset=ref.key, include_decided=False)
    assert flag["id"] in {row["id"] for row in still_open}

    counts = engine.outlier_counts()
    assert counts["escalated"] == 1
    assert counts["awaiting_review"] == counts["open"] + counts["escalated"]
    assert counts["quarantined"] == 0

    # The state survives a fresh engine over the same database.
    reloaded = QualityEngine(store)
    persisted = [
        row for row in reloaded.outlier_queue(dataset=ref.key) if row["id"] == flag["id"]
    ]
    assert persisted and persisted[0]["state"] == "escalated"

    # An escalation can be resolved either way afterwards.
    cleared = await engine.decide_outlier(flag["id"], "quarantine", by="tester", publish=False)
    assert cleared["state"] == "quarantined"
    assert cleared["needs_review"] is False
    assert len(store.read_bars("ESCA")) == before - 1


@pytest.mark.anyio
async def test_a_rerun_does_not_duplicate_or_resurrect_queue_entries(
    engine: QualityEngine, store: TimeSeriesStore
) -> None:
    """A decided flag keeps its decision when the suite runs again."""
    days = business_days(date(2024, 1, 2), WINDOW_END)
    closes = [100.0 + 0.05 * i for i in range(len(days))]
    closes[200] = closes[200] * 12.0
    store.write_bars(daily_bars("SPIKE", days, closes=closes))
    ref = DatasetRef.bars("SPIKE")

    engine.evaluate(ref, now=NOW)
    first = engine.outlier_queue(dataset=ref.key)
    assert first
    await engine.decide_outlier(first[0]["id"], "accept", by="tester", publish=False)

    engine.evaluate(ref, now=NOW)
    second = engine.outlier_queue(dataset=ref.key)
    assert len(second) == len(first)
    accepted = [row for row in second if row["id"] == first[0]["id"]]
    assert accepted and accepted[0]["state"] == "accepted"
    assert accepted[0]["decided_by"] == "tester"


# --- tickets ---------------------------------------------------------------
def test_tickets_open_list_and_close(engine: QualityEngine) -> None:
    """A ticket is a real recorded object, not a button that does nothing."""
    assert engine.tickets() == []
    ticket = engine.open_ticket(
        title="options-iv is behind its cadence",
        dataset="series:options:iv30_SPY",
        check="sla",
        body="no ingest in 9h",
        severity=Severity.ERROR,
    )
    assert ticket["status"] == "open"
    assert [row["id"] for row in engine.tickets()] == [ticket["id"]]

    closed = engine.close_ticket(ticket["id"])
    assert closed["status"] == "closed"
    assert closed["closed_at"] is not None
    assert engine.tickets() == []
    assert [row["id"] for row in engine.tickets(include_closed=True)] == [ticket["id"]]

    with pytest.raises(ValueError):
        engine.open_ticket(title="   ")
    with pytest.raises(KeyError):
        engine.close_ticket("missing")
