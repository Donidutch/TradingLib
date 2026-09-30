"""Macro series and index breadth - the two Overview figures with no route before.

Both run offline: the macro service is given a stub provider and a real Parquet
store, and the breadth calculation reads bars written into that store. Nothing
here contacts FRED.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest

from tradinglib.data.base import Bar, BarInterval, Series
from tradinglib.data.macro import (
    MACRO_PANEL,
    MacroSeriesSpec,
    MacroService,
)
from tradinglib.data.store import TimeSeriesStore
from tradinglib.data.universe import MIN_BREADTH_COVERAGE, moving_average_breadth


def _month_starts(count: int, *, end: date) -> list[datetime]:
    """Return ``count`` monthly timestamps ending at ``end``, oldest first."""
    out: list[datetime] = []
    year, month = end.year, end.month
    for _ in range(count):
        out.append(datetime(year, month, 1, tzinfo=timezone.utc))
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return sorted(out)


class StubFred:
    """A FRED provider that returns a scripted series and records the calls."""

    def __init__(self, series: dict[str, Series]) -> None:
        self.series = series
        self.requested: list[str] = []
        self.closed = False

    async def get_series(
        self, series_id: str, *, start: date | None = None, end: date | None = None
    ) -> Series:
        """Return the scripted series for ``series_id`` (empty when unscripted)."""
        self.requested.append(series_id)
        return self.series.get(series_id, Series(name=series_id, points=[]))

    async def close(self) -> None:
        """Record that the service closed the client."""
        self.closed = True


class BrokenFred:
    """A FRED provider that always fails, to prove the failure is reported."""

    async def get_series(self, series_id: str, **_: Any) -> Series:
        """Fail the way an unreachable vendor does."""
        raise RuntimeError("fredgraph.stlouisfed.org is unreachable")

    async def close(self) -> None:
        """Nothing to close."""


@pytest.fixture()
def store(settings: Any) -> TimeSeriesStore:
    """A time-series store rooted in the temporary data directory."""
    return TimeSeriesStore()


# --------------------------------------------------------------------------
# macro series
# --------------------------------------------------------------------------
async def test_a_level_series_is_read_and_its_change_reported(store: TimeSeriesStore) -> None:
    """A daily level comes back with its previous observation and the delta."""
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    points = [(today - timedelta(days=n), 4.0 + n * 0.01) for n in range(5)]
    provider = StubFred({"DGS10": Series(name="DGS10", points=points, source="fred")})
    service = MacroService(store, provider_factory=lambda: provider)

    reading = await service.reading(MACRO_PANEL[0])
    assert reading.series_id == "DGS10"
    assert reading.value == pytest.approx(4.0)
    assert reading.previous == pytest.approx(4.01)
    assert reading.change == pytest.approx(-0.01)
    assert reading.stale is False
    assert reading.origin == "fred"
    assert provider.closed is True

    # It was persisted, so the next read needs no vendor at all.
    second = MacroService(store, provider_factory=lambda: BrokenFred())
    again = await second.reading(MACRO_PANEL[0])
    assert again.value == pytest.approx(4.0)
    assert again.origin == "store"


async def test_a_year_over_year_transform_is_computed_not_assumed(
    store: TimeSeriesStore,
) -> None:
    """Core CPI is served as a percentage change on twelve months, not a level."""
    stamps = _month_starts(26, end=date.today().replace(day=1))
    # A clean 3% annual rise: each month is 1.03**(1/12) above the previous.
    values = [100.0 * (1.03 ** (index / 12.0)) for index in range(len(stamps))]
    provider = StubFred(
        {
            "CPILFESL": Series(
                name="CPILFESL",
                points=list(zip(stamps, values, strict=True)),
                source="fred",
            )
        }
    )
    service = MacroService(store, provider_factory=lambda: provider)
    spec = next(item for item in MACRO_PANEL if item.transform == "yoy")

    reading = await service.reading(spec)
    assert reading.transform == "yoy"
    assert reading.value == pytest.approx(3.0, abs=0.05)
    assert reading.observations >= 12


async def test_an_unreachable_vendor_is_reported_not_invented(
    store: TimeSeriesStore,
) -> None:
    """With nothing stored and FRED down, the reading is null plus the reason."""
    service = MacroService(store, provider_factory=lambda: BrokenFred())
    reading = await service.reading(
        MacroSeriesSpec(series_id="NOTHING", label="NOTHING", description="test")
    )
    assert reading.value is None
    assert reading.stale is True
    assert "FRED refresh failed" in (reading.reason or "")
    assert "unreachable" in (reading.reason or "")


async def test_the_panel_answers_for_every_series(store: TimeSeriesStore) -> None:
    """One failing series never blanks the others."""
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    provider = StubFred(
        {
            "DGS10": Series(
                name="DGS10",
                points=[(today - timedelta(days=n), 4.5) for n in range(3)],
                source="fred",
            )
        }
    )
    service = MacroService(store, provider_factory=lambda: provider)
    readings = await service.panel()
    assert [reading.series_id for reading in readings] == [
        spec.series_id for spec in MACRO_PANEL
    ]
    by_id = {reading.series_id: reading for reading in readings}
    assert by_id["DGS10"].value == pytest.approx(4.5)
    # The unscripted ones are honest about having nothing.
    assert by_id["UNRATE"].value is None
    assert by_id["UNRATE"].reason


# --------------------------------------------------------------------------
# index breadth
# --------------------------------------------------------------------------
def _write_trend(store: TimeSeriesStore, symbol: str, *, rising: bool, bars: int = 260) -> None:
    """Write a monotonic price path so the SMA comparison is unambiguous."""
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    rows = []
    for index in range(bars):
        price = 100.0 + index * 0.5 if rising else 200.0 - index * 0.5
        rows.append(
            Bar(
                symbol=symbol,
                ts=start + timedelta(days=index),
                open=price,
                high=price,
                low=price,
                close=price,
                volume=1000.0,
                interval=BarInterval.DAY_1,
                source="test",
            )
        )
    store.write_bars(rows, interval=BarInterval.DAY_1)


def test_breadth_counts_constituents_above_their_own_average(
    store: TimeSeriesStore,
) -> None:
    """Breadth is a constituent count, not the index against its own average."""
    symbols = []
    for index in range(MIN_BREADTH_COVERAGE + 5):
        symbol = f"UP{index}" if index % 4 else f"DN{index}"
        _write_trend(store, symbol, rising=not symbol.startswith("DN"))
        symbols.append(symbol)

    result = moving_average_breadth(store, symbols, window=200)
    assert result["covered"] == len(symbols)
    assert result["above"] + result["below"] == len(symbols)
    # A monotonically rising path is always above its trailing mean, and a
    # falling one always below, so the split is exactly the construction.
    assert result["below"] == sum(1 for s in symbols if s.startswith("DN"))
    assert result["percent"] == pytest.approx(result["above"] / result["covered"])
    assert result["as_of"] is not None
    assert result["uncovered"] == []


def test_thin_coverage_refuses_to_publish_a_number(store: TimeSeriesStore) -> None:
    """Too few priced constituents means no reading, with the reason."""
    _write_trend(store, "ONLYONE", rising=True)
    result = moving_average_breadth(store, ["ONLYONE", "NOPRICE"], window=200)
    assert result["percent"] is None
    assert result["covered"] == 1
    assert result["uncovered"] == ["NOPRICE"]
    assert "at least" in (result["reason"] or "")


def test_a_symbol_without_enough_history_is_excluded_not_assumed(
    store: TimeSeriesStore,
) -> None:
    """A name with fewer than ``window`` closes cannot be counted either way."""
    _write_trend(store, "SHORT", rising=True, bars=50)
    result = moving_average_breadth(store, ["SHORT"], window=200)
    assert result["covered"] == 0
    assert result["uncovered"] == ["SHORT"]
    assert result["percent"] is None
