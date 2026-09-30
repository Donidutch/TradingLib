"""Offline tests for universes, watchlists and point-in-time snapshots.

Constituent fetching is exercised through an ``httpx.MockTransport`` serving a
synthetic Wikipedia-shaped table, so the real fetch/parse/cache/store path runs
end to end without touching the network.
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest

from tradinglib.config import Settings
from tradinglib.data.base import ProviderUnavailable
from tradinglib.data.universe import (
    BUILTIN_UNIVERSES,
    CONSTITUENT_SOURCES,
    US_QUALITY_50,
    ConstituentCache,
    UniverseMember,
    UniverseRegistry,
    WatchlistRegistry,
    constituents_from_records,
    fetch_constituents,
    normalize_symbol,
    parse_wikipedia_table,
    us_quality_50_members,
)

SP500_HTML = """
<html><body>
<h1>List of S&amp;P 500 companies</h1>
<table id="constituents" class="wikitable sortable">
<tr><th>Symbol</th><th>Security</th><th>GICS Sector</th><th>GICS Sub-Industry</th>
    <th>Date added</th></tr>
<tr><td><a href="/x">MMM</a></td><td>3M</td><td>Industrials</td>
    <td>Industrial Conglomerates</td><td>1957-03-04</td></tr>
<tr><td>BRK.B</td><td>Berkshire Hathaway</td><td>Financials</td>
    <td>Multi-Sector Holdings</td><td>2010-02-16</td></tr>
<tr><td>NVDA[1]</td><td>Nvidia</td><td>Information Technology</td>
    <td>Semiconductors</td><td>2001-11-30</td></tr>
<tr><td>MMM</td><td>3M duplicate row</td><td>Industrials</td><td>x</td><td>1957-03-04</td></tr>
<tr><td>&nbsp;</td><td>broken row</td><td></td><td></td><td></td></tr>
</table>
</body></html>
"""


@pytest.fixture()
def registry(settings: Settings, tmp_path: Path) -> UniverseRegistry:
    """A universe registry on a throwaway database and constituent cache."""
    return UniverseRegistry(cache=ConstituentCache(tmp_path / "universe-cache"))


@pytest.fixture()
def watchlists(settings: Settings) -> WatchlistRegistry:
    """A watchlist registry on a throwaway database."""
    return WatchlistRegistry()


def make_client(html: str, counter: list[int], *, fail: bool = False) -> httpx.AsyncClient:
    """Return a client serving ``html`` and counting requests."""

    def handler(request: httpx.Request) -> httpx.Response:
        counter.append(1)
        if fail:
            raise httpx.ConnectError("simulated network failure", request=request)
        return httpx.Response(200, text=html)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --- symbols ---------------------------------------------------------------
def test_normalize_symbol_canonicalises_class_shares() -> None:
    assert normalize_symbol(" aapl ") == "AAPL"
    assert normalize_symbol("BRK.B") == "BRK-B"
    assert normalize_symbol("BF.B") == "BF-B"
    assert normalize_symbol("BRK-B") == "BRK-B"
    with pytest.raises(ValueError):
        normalize_symbol("   ")
    with pytest.raises(ValueError):
        normalize_symbol("not a ticker!")


# --- the built-in quality list --------------------------------------------
def test_us_quality_50_is_fifty_unique_sector_capped_names() -> None:
    members = us_quality_50_members()
    assert len(members) == 50
    assert len({m.symbol for m in members}) == 50
    assert all(m.name and m.sector for m in members)
    counts: dict[str, int] = {}
    for _, _, sector in US_QUALITY_50:
        counts[sector] = counts.get(sector, 0) + 1
    assert max(counts.values()) <= 8, counts
    # The rule excludes these sectors by construction.
    assert not {"Utilities", "Energy", "Real Estate"} & set(counts)


# --- table parsing ---------------------------------------------------------
def test_parse_wikipedia_table_reads_headers_and_rows() -> None:
    records = parse_wikipedia_table(SP500_HTML, "constituents")
    assert len(records) == 5
    assert records[0]["Symbol"] == "MMM"
    assert records[0]["GICS Sector"] == "Industrials"
    assert records[2]["Symbol"] == "NVDA"  # footnote marker stripped


def test_parse_wikipedia_table_fails_loudly_on_a_missing_table() -> None:
    with pytest.raises(ValueError):
        parse_wikipedia_table("<html><body><p>nothing</p></body></html>", "constituents")


def test_constituents_from_records_normalises_dedupes_and_skips_junk() -> None:
    records = parse_wikipedia_table(SP500_HTML, "constituents")
    members = constituents_from_records(CONSTITUENT_SOURCES["sp500"], records)
    symbols = [m.symbol for m in members]
    assert symbols == ["MMM", "BRK-B", "NVDA"]
    assert members[1].source_symbol == "BRK.B"
    assert members[0].added_on == date(1957, 3, 4)
    assert members[2].sector == "Information Technology"


async def test_fetch_constituents_uses_the_real_parse_path() -> None:
    calls: list[int] = []
    async with make_client(SP500_HTML, calls) as client:
        members = await fetch_constituents(CONSTITUENT_SOURCES["sp500"], client=client)
    assert len(calls) == 1
    assert [m.symbol for m in members] == ["MMM", "BRK-B", "NVDA"]


async def test_fetch_constituents_reports_network_failure() -> None:
    calls: list[int] = []
    async with make_client(SP500_HTML, calls, fail=True) as client:
        with pytest.raises(ProviderUnavailable):
            await fetch_constituents(CONSTITUENT_SOURCES["sp500"], client=client)


# --- cache -----------------------------------------------------------------
def test_constituent_cache_round_trips(tmp_path: Path) -> None:
    cache = ConstituentCache(tmp_path / "cache")
    members = [UniverseMember(symbol="AAPL", name="Apple", added_on=date(2020, 1, 2))]
    saved = cache.save("sp500", members, source_url="https://example.invalid")
    loaded = cache.load("sp500")
    assert loaded is not None
    assert loaded.members[0].symbol == "AAPL"
    assert loaded.members[0].added_on == date(2020, 1, 2)
    assert loaded.fetched_at == saved.fetched_at
    assert loaded.age.total_seconds() >= 0


def test_constituent_cache_treats_a_damaged_file_as_a_miss(tmp_path: Path) -> None:
    cache = ConstituentCache(tmp_path / "cache")
    cache.path("sp500").write_text("{not json", encoding="utf-8")
    assert cache.load("sp500") is None
    assert cache.load("never-written") is None


# --- built-ins -------------------------------------------------------------
def test_ensure_builtins_creates_every_builtin_and_an_active_universe(
    registry: UniverseRegistry,
) -> None:
    universes = registry.ensure_builtins()
    assert {u.id for u in universes} == set(BUILTIN_UNIVERSES)

    quality = registry.get("us-quality-50")
    assert quality.builtin is True
    assert len(quality.members) == 50
    assert quality.rule is not None and "capped at 8 names" in quality.rule
    assert registry.active_universe_id() == "us-quality-50"

    # No constituents have been fetched yet: the row says so instead of faking.
    index = registry.get("sp500")
    assert index.members == []
    assert index.members_fetched_at is None

    registry.ensure_builtins()  # idempotent
    assert len(registry.get("us-quality-50").members) == 50


async def test_refresh_builtin_fetches_caches_and_reuses(registry: UniverseRegistry) -> None:
    registry.ensure_builtins()
    calls: list[int] = []
    async with make_client(SP500_HTML, calls) as client:
        universe = await registry.refresh_builtin("sp500", force=True, client=client)
        assert len(calls) == 1
        assert universe.symbols() == ["MMM", "BRK-B", "NVDA"]
        assert universe.members_fetched_at is not None

        # A fresh cache is reused rather than re-fetched...
        await registry.refresh_builtin("sp500", client=client)
        assert len(calls) == 1
        # ...unless the caller forces it or the cache is older than max_age.
        await registry.refresh_builtin("sp500", max_age=timedelta(seconds=0), client=client)
        assert len(calls) == 2

    assert registry.cache.load("sp500") is not None


async def test_refresh_builtin_falls_back_to_cache_when_the_fetch_fails(
    registry: UniverseRegistry,
) -> None:
    registry.ensure_builtins()
    calls: list[int] = []
    async with make_client(SP500_HTML, calls) as client:
        await registry.refresh_builtin("sp500", force=True, client=client)

    failing: list[int] = []
    async with make_client(SP500_HTML, failing, fail=True) as client:
        universe = await registry.refresh_builtin("sp500", force=True, client=client)
    assert failing  # the fetch really was attempted
    assert universe.symbols() == ["MMM", "BRK-B", "NVDA"]


async def test_refresh_builtin_raises_without_cache_or_network(
    registry: UniverseRegistry,
) -> None:
    registry.ensure_builtins()
    calls: list[int] = []
    async with make_client(SP500_HTML, calls, fail=True) as client:
        with pytest.raises(ProviderUnavailable):
            await registry.refresh_builtin("nasdaq100", force=True, client=client)


async def test_refresh_builtin_rejects_non_builtins(registry: UniverseRegistry) -> None:
    with pytest.raises(KeyError):
        await registry.refresh_builtin("not-a-builtin")


async def test_refresh_builtin_rematerialises_the_quality_list(
    registry: UniverseRegistry,
) -> None:
    registry.ensure_builtins()
    universe = await registry.refresh_builtin("us-quality-50")
    assert len(universe.members) == 50
    assert universe.members_fetched_at is not None


# --- CRUD ------------------------------------------------------------------
def test_create_update_delete_user_universe(registry: UniverseRegistry) -> None:
    universe = registry.create("AI Infrastructure", ["nvda", "avgo", "nvda"], description="theme")
    assert universe.id == "ai-infrastructure"
    assert universe.symbols() == ["NVDA", "AVGO"]

    updated = registry.update(universe.id, name="AI Infra", symbols=["NVDA", "AMD"])
    assert updated.name == "AI Infra"
    assert updated.symbols() == ["NVDA", "AMD"]

    with pytest.raises(ValueError):
        registry.update(universe.id, nonsense=1)

    registry.delete(universe.id)
    with pytest.raises(KeyError):
        registry.get(universe.id)


def test_duplicate_names_get_distinct_ids(registry: UniverseRegistry) -> None:
    first = registry.create("Peers", ["AAPL"])
    second = registry.create("Peers", ["MSFT"])
    assert first.id == "peers"
    assert second.id == "peers-2"


def test_builtin_universes_are_protected(registry: UniverseRegistry) -> None:
    registry.ensure_builtins()
    with pytest.raises(ValueError):
        registry.update("us-quality-50", symbols=["AAPL"])
    with pytest.raises(ValueError):
        registry.delete("us-quality-50")


def test_creating_with_an_empty_name_is_rejected(registry: UniverseRegistry) -> None:
    with pytest.raises(ValueError):
        registry.create("   ", ["AAPL"])


# --- point in time ---------------------------------------------------------
def test_snapshots_make_membership_point_in_time(registry: UniverseRegistry) -> None:
    universe = registry.create(
        "Index",
        [
            UniverseMember(symbol="AAPL", name="Apple"),
            UniverseMember(symbol="ATVI", name="Activision"),
        ],
    )
    first = registry.snapshot(universe.id, as_of=date(2022, 6, 30))
    assert first["symbol_count"] == 2

    # Activision leaves the index; today's membership is only AAPL + MSFT.
    registry.update(universe.id, symbols=["AAPL", "MSFT"])
    registry.snapshot(universe.id, as_of=date(2024, 6, 28))

    old = registry.membership(universe.id, as_of=date(2023, 1, 31))
    assert old["basis"] == "snapshot"
    assert old["snapshot_as_of"] == "2022-06-30"
    assert [m["symbol"] for m in old["members"]] == ["AAPL", "ATVI"]

    recent = registry.symbols(universe.id, as_of=date(2025, 1, 1))
    assert recent == ["AAPL", "MSFT"]
    assert registry.symbols(universe.id) == ["AAPL", "MSFT"]

    assert [s["as_of"] for s in registry.snapshots(universe.id)] == [
        "2024-06-28",
        "2022-06-30",
    ]
    assert registry.snapshot_dates(universe.id) == [date(2022, 6, 30), date(2024, 6, 28)]


def test_snapshotting_the_same_date_twice_replaces_it(registry: UniverseRegistry) -> None:
    universe = registry.create("Snap", ["AAPL"])
    registry.snapshot(universe.id, as_of=date(2026, 1, 5))
    registry.update(universe.id, symbols=["AAPL", "MSFT"])
    record = registry.snapshot(universe.id, as_of=date(2026, 1, 5))
    assert record["symbol_count"] == 2
    assert len(registry.snapshots(universe.id)) == 1
    assert registry.delete_snapshot(universe.id, date(2026, 1, 5)) is True
    assert registry.snapshots(universe.id) == []


def test_membership_dates_are_used_when_no_snapshot_exists(registry: UniverseRegistry) -> None:
    universe = registry.create(
        "Dated",
        [
            UniverseMember(symbol="OLD", added_on=date(2015, 1, 1), removed_on=date(2020, 1, 1)),
            UniverseMember(symbol="NEW", added_on=date(2021, 1, 1)),
            UniverseMember(symbol="ALWAYS"),
        ],
    )
    payload = registry.membership(universe.id, as_of=date(2018, 6, 30))
    assert payload["basis"] == "membership-dates"
    assert [m["symbol"] for m in payload["members"]] == ["OLD", "ALWAYS"]
    assert registry.symbols(universe.id, as_of=date(2022, 1, 1)) == ["NEW", "ALWAYS"]


# --- resolution and active selection ---------------------------------------
def test_resolve_handles_universes_watchlists_and_ad_hoc_lists(
    registry: UniverseRegistry, watchlists: WatchlistRegistry
) -> None:
    registry.create("Peer Set", ["AAPL", "MSFT"])
    watchlists.create("Semis", ["NVDA", "AMD"])

    assert registry.resolve("peer-set") == ["AAPL", "MSFT"]
    assert registry.resolve("universe:peer-set") == ["AAPL", "MSFT"]
    assert registry.resolve("watchlist:semis") == ["NVDA", "AMD"]
    assert registry.resolve("semis") == ["NVDA", "AMD"]
    assert registry.resolve("aapl, brk.b") == ["AAPL", "BRK-B"]
    with pytest.raises(KeyError):
        registry.resolve("no such thing at all")


def test_active_universe_selection(registry: UniverseRegistry) -> None:
    registry.ensure_builtins()
    custom = registry.create("Mine", ["AAPL"])
    assert registry.set_active(custom.id).active is True
    assert registry.active_universe_id() == custom.id
    active = registry.get_active()
    assert active is not None and active.id == custom.id
    # Exactly one universe stays active.
    assert [u.id for u in registry.list_universes(include_members=False) if u.active] == [
        custom.id
    ]
    with pytest.raises(KeyError):
        registry.set_active("nope")


# --- watchlists ------------------------------------------------------------
def test_watchlist_crud_and_default_handling(watchlists: WatchlistRegistry) -> None:
    first = watchlists.create("Primary research", ["aapl", "msft", "aapl"])
    assert first.symbols == ["AAPL", "MSFT"]
    assert first.is_default is True  # the first one becomes the default

    second = watchlists.create("Earnings", ["nvda"])
    assert second.is_default is False

    promoted = watchlists.update(second.id, is_default=True)
    assert promoted.is_default is True
    assert watchlists.get(first.id).is_default is False
    default = watchlists.default_watchlist()
    assert default is not None and default.id == second.id

    assert watchlists.add_symbol(second.id, "brk.b").symbols == ["NVDA", "BRK-B"]
    assert watchlists.add_symbol(second.id, "NVDA").symbols == ["NVDA", "BRK-B"]
    assert watchlists.remove_symbol(second.id, "nvda").symbols == ["BRK-B"]

    renamed = watchlists.update(first.id, name="Core", symbols=["TSLA"])
    assert renamed.name == "Core" and renamed.symbols == ["TSLA"]

    watchlists.delete(second.id)
    assert [w.id for w in watchlists.list_watchlists()] == [first.id]
    assert watchlists.get(first.id).is_default is True  # default was handed over

    with pytest.raises(KeyError):
        watchlists.get(second.id)
    with pytest.raises(ValueError):
        watchlists.create("")
    with pytest.raises(ValueError):
        watchlists.update(first.id, colour="red")


# --- live constituent sources ---------------------------------------------
@pytest.mark.network
@pytest.mark.skipif(
    os.environ.get("TRADINGLIB_NETWORK_TESTS") != "1",
    reason="live Wikipedia fetch; set TRADINGLIB_NETWORK_TESTS=1 to run it",
)
async def test_live_index_constituents_are_real(registry: UniverseRegistry) -> None:
    registry.ensure_builtins()
    outcomes = await registry.refresh_all_builtins(force=True)
    assert all(not text.startswith("error") for text in outcomes.values()), outcomes

    sp500 = registry.get("sp500")
    assert 480 <= len(sp500.members) <= 520
    assert "AAPL" in sp500.symbols()
    assert "BRK-B" in sp500.symbols()  # normalised from Wikipedia's "BRK.B"
    assert any(m.sector for m in sp500.members)
    assert any(m.added_on for m in sp500.members)

    ndx = registry.get("nasdaq100")
    assert 90 <= len(ndx.members) <= 110
    assert "MSFT" in ndx.symbols()

    cached = registry.cache.load("sp500")
    assert cached is not None and len(cached.members) == len(sp500.members)


def test_watchlist_as_dict_is_json_shaped(watchlists: WatchlistRegistry) -> None:
    payload = watchlists.create("Shorts", ["AAPL"]).as_dict()
    assert payload["symbol_count"] == 1
    assert payload["symbols"] == ["AAPL"]
    assert isinstance(payload["created_at"], str)


async def test_seeding_fills_only_the_empty_builtins(registry: UniverseRegistry) -> None:
    """The first start fetches the index lists; a populated universe is left alone."""
    registry.ensure_builtins()
    # us-quality-50 is materialised from the static list, so it is never empty.
    assert registry.unpopulated_builtins() == ["sp500", "nasdaq100"]

    calls: list[int] = []
    async with make_client(SP500_HTML, calls) as client:
        outcome = await registry.seed_unpopulated_builtins(client=client)
    # The stub only serves the S&P table, so that universe is filled and the
    # other reports its failure instead of being quietly left as "done".
    assert set(outcome) == {"sp500", "nasdaq100"}
    assert outcome["sp500"] == "3 members"
    assert outcome["nasdaq100"].startswith("error:")
    assert registry.unpopulated_builtins() == ["nasdaq100"]
    assert registry.get("sp500").symbols() == ["MMM", "BRK-B", "NVDA"]
    fetched = len(calls)

    # Seeding again only retries what is still empty; the filled one is not
    # re-fetched, because this is a first-run seed and not a refresh.
    async with make_client(SP500_HTML, calls) as client:
        again = await registry.seed_unpopulated_builtins(client=client)
    assert set(again) == {"nasdaq100"}
    assert len(calls) == fetched + 1


async def test_seeding_reports_a_failure_without_raising(registry: UniverseRegistry) -> None:
    """A vendor that is down leaves the universe empty and says so."""
    registry.ensure_builtins()
    calls: list[int] = []
    async with make_client(SP500_HTML, calls, fail=True) as client:
        outcome = await registry.seed_unpopulated_builtins(client=client)
    assert calls, "the fetch really was attempted"
    assert set(outcome) == {"sp500", "nasdaq100"}
    assert all(value.startswith("error:") for value in outcome.values())
    # Nothing was invented to paper over the failure.
    assert registry.unpopulated_builtins() == ["sp500", "nasdaq100"]
