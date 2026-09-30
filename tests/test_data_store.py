"""Offline tests for the Parquet + DuckDB time-series store."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from tradinglib.data.base import Bar, BarInterval, Series
from tradinglib.data.store import TimeSeriesStore, partition_value

START = datetime(2026, 1, 5, tzinfo=timezone.utc)


@pytest.fixture()
def store(tmp_path: Path) -> TimeSeriesStore:
    """A store rooted in a throwaway directory."""
    return TimeSeriesStore(tmp_path / "ts")


def make_bars(symbol: str = "AAPL", count: int = 5, *, offset: float = 0.0) -> list[Bar]:
    """Build a deterministic run of daily bars."""
    return [
        Bar(
            symbol=symbol,
            ts=START + timedelta(days=i),
            open=100.0 + i + offset,
            high=101.0 + i + offset,
            low=99.0 + i + offset,
            close=100.5 + i + offset,
            volume=1_000_000 + i,
            interval=BarInterval.DAY_1,
            source="fixture",
        )
        for i in range(count)
    ]


def test_partition_value_escapes_unsafe_characters() -> None:
    assert partition_value("AAPL") == "AAPL"
    assert partition_value("BRK.B") == "BRK.B"
    assert partition_value("^GSPC") == "_GSPC"
    assert partition_value(" ") == "_"


def test_layout_matches_the_documented_paths(store: TimeSeriesStore) -> None:
    bars_path = store.bars_path("aapl", BarInterval.DAY_1)
    assert bars_path.parent.name == "symbol=AAPL"
    assert bars_path.parent.parent.name == "interval=1d"
    assert bars_path.parent.parent.parent.name == "bars"

    series_path = store.series_path("fred", "DGS10")
    assert series_path.parent.name == "name=DGS10"
    assert series_path.parent.parent.name == "namespace=fred"


def test_reading_an_unknown_symbol_returns_an_empty_frame(store: TimeSeriesStore) -> None:
    frame = store.read_bars("NOSUCH")
    assert frame.empty
    assert list(frame.columns) == ["open", "high", "low", "close", "volume", "source"]
    assert store.latest_timestamp("NOSUCH") is None
    assert store.symbols() == []


def test_write_then_read_bars_round_trip(store: TimeSeriesStore) -> None:
    assert store.write_bars(make_bars()) == 5
    frame = store.read_bars("AAPL")
    assert len(frame) == 5
    assert frame.index[0] == pd.Timestamp(START)
    assert str(frame.index.tz) == "UTC"
    assert frame["close"].iloc[-1] == pytest.approx(104.5)
    assert store.symbols() == ["AAPL"]
    assert store.latest_timestamp("AAPL") == START + timedelta(days=4)


def test_write_bars_is_an_upsert_not_an_append(store: TimeSeriesStore) -> None:
    store.write_bars(make_bars())
    store.write_bars(make_bars(count=3, offset=1000.0))

    frame = store.read_bars("AAPL")
    assert len(frame) == 5, "overlapping timestamps must be replaced, not duplicated"
    assert frame["close"].iloc[0] == pytest.approx(1100.5), "the newest write wins"
    assert frame["close"].iloc[-1] == pytest.approx(104.5), "untouched rows survive"


def test_read_bars_honours_the_time_window(store: TimeSeriesStore) -> None:
    store.write_bars(make_bars(count=10))
    frame = store.read_bars(
        "AAPL", start=START + timedelta(days=2), end=START + timedelta(days=4)
    )
    assert len(frame) == 3
    assert frame.index[0] == pd.Timestamp(START + timedelta(days=2))


def test_intervals_are_stored_separately(store: TimeSeriesStore) -> None:
    store.write_bars(make_bars())
    hourly = [
        Bar("AAPL", START + timedelta(hours=i), 1, 2, 0.5, 1.5, 10, BarInterval.HOUR_1, "fixture")
        for i in range(4)
    ]
    store.write_bars(hourly, interval=BarInterval.HOUR_1)

    assert len(store.read_bars("AAPL", interval=BarInterval.DAY_1)) == 5
    assert len(store.read_bars("AAPL", interval=BarInterval.HOUR_1)) == 4
    assert store.symbols(BarInterval.HOUR_1) == ["AAPL"]


def test_multiple_symbols_land_in_their_own_partitions(store: TimeSeriesStore) -> None:
    store.write_bars(make_bars("AAPL") + make_bars("MSFT", offset=50.0))
    assert store.symbols() == ["AAPL", "MSFT"]
    assert store.bars_path("AAPL", BarInterval.DAY_1).is_file()
    assert store.bars_path("MSFT", BarInterval.DAY_1).is_file()
    assert len(store.read_bars("MSFT")) == 5


def test_bar_objects_round_trip_with_utc_timestamps(store: TimeSeriesStore) -> None:
    store.write_bars(make_bars(count=2))
    bars = store.read_bar_objects("AAPL")
    assert [b.ts for b in bars] == [START, START + timedelta(days=1)]
    assert all(b.ts.tzinfo is timezone.utc for b in bars)
    assert bars[0].interval is BarInterval.DAY_1
    assert bars[0].source == "fixture"


def test_naive_timestamps_are_stored_as_utc(store: TimeSeriesStore) -> None:
    naive = datetime(2026, 2, 2, 0, 0)
    store.write_bars([Bar("AAPL", naive, 1, 1, 1, 1, 1, BarInterval.DAY_1, "fixture")])
    assert store.latest_timestamp("AAPL") == naive.replace(tzinfo=timezone.utc)


def test_series_round_trip_and_upsert(store: TimeSeriesStore) -> None:
    series = Series(
        name="DGS10",
        points=[(START, 4.5), (START + timedelta(days=1), 4.6)],
        unit="Percent",
        source="fred",
    )
    assert store.write_series("fred", series) == 2

    revised = Series(name="DGS10", points=[(START, 9.9)], unit="Percent", source="fred")
    assert store.write_series("fred", revised) == 2

    stored = store.read_series("fred", "DGS10")
    assert stored.unit == "Percent"
    assert stored.source == "fred"
    assert stored.points[0] == (START, 9.9)
    assert len(stored) == 2
    assert store.series_names() == [{"namespace": "fred", "name": "DGS10"}]
    assert store.series_latest_timestamp("fred", "DGS10") == START + timedelta(days=1)


def test_reading_an_unknown_series_is_empty_not_an_error(store: TimeSeriesStore) -> None:
    empty = store.read_series("fred", "NOPE")
    assert len(empty) == 0
    assert empty.last_value is None
    assert store.read_series_frame("fred", "NOPE").empty


def test_empty_writes_are_no_ops(store: TimeSeriesStore) -> None:
    assert store.write_bars([]) == 0
    assert store.write_series("fred", Series(name="EMPTY")) == 0
    assert store.datasets() == []


def test_query_exposes_bars_and_series_views(store: TimeSeriesStore) -> None:
    store.write_bars(make_bars())
    store.write_series("fred", Series(name="DGS10", points=[(START, 4.5)], source="fred"))

    counted = store.query("SELECT count(*) AS n FROM bars WHERE symbol = $symbol", symbol="AAPL")
    assert int(counted["n"].iloc[0]) == 5
    assert int(store.query("SELECT count(*) AS n FROM series")["n"].iloc[0]) == 1


def test_query_works_before_anything_is_written(store: TimeSeriesStore) -> None:
    assert store.query("SELECT * FROM bars").empty
    assert store.query("SELECT * FROM series").empty


def test_coverage_and_stats_report_real_numbers(store: TimeSeriesStore) -> None:
    store.write_bars(make_bars(count=7))
    coverage = store.coverage("AAPL")
    assert coverage["rows"] == 7
    assert coverage["distinct_timestamps"] == 7
    assert coverage["first_ts"] == START.isoformat()
    assert coverage["bytes"] > 0

    stats = store.stats()
    assert stats["total_rows"] == 7
    assert stats["total_files"] == 1
    assert stats["total_bytes"] == coverage["bytes"]


def test_coverage_of_unknown_symbol_is_zeroed(store: TimeSeriesStore) -> None:
    coverage = store.coverage("NOSUCH")
    assert coverage["rows"] == 0
    assert coverage["first_ts"] is None
    assert coverage["bytes"] == 0


def test_write_frame_and_drop_symbol(store: TimeSeriesStore) -> None:
    frame = pd.DataFrame({"symbol": ["AAPL", "MSFT"], "score": [1.0, 2.0]})
    assert store.write_frame("screens", frame, partition_cols=["symbol"]) == 2
    names = {entry["dataset"] for entry in store.datasets()}
    assert "screens" in names

    store.write_bars(make_bars())
    assert store.drop_symbol("AAPL") == 1
    assert store.read_bars("AAPL").empty


async def test_async_helpers_use_worker_threads(store: TimeSeriesStore) -> None:
    assert await store.awrite_bars(make_bars(count=3)) == 3
    frame = await store.aread_bars("AAPL")
    assert len(frame) == 3

    await store.awrite_series("fred", Series(name="DGS10", points=[(START, 4.5)], source="fred"))
    assert len(await store.aread_series("fred", "DGS10")) == 1
    assert int((await store.aquery("SELECT count(*) AS n FROM bars"))["n"].iloc[0]) == 3
