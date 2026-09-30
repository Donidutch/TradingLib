"""Offline tests for :mod:`tradinglib.data.cache` (memory + disk tiers, TTL)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tradinglib.data.cache import CacheEntry, DataCache, provider_key


@pytest.fixture()
def cache(tmp_path: Path) -> DataCache:
    """A disk-backed cache in a throwaway directory."""
    return DataCache(tmp_path / "cache", default_ttl=60.0)


async def test_set_then_get_round_trips_value(cache: DataCache) -> None:
    await cache.set("yfinance:quote:AAPL", {"price": 1.0}, source="yfinance")
    entry = await cache.get("yfinance:quote:AAPL")
    assert entry is not None
    assert entry.value == {"price": 1.0}
    assert entry.source == "yfinance"
    assert entry.age_seconds >= 0.0
    assert entry.expired is False


async def test_miss_returns_none(cache: DataCache) -> None:
    assert await cache.get("absent") is None
    stats = await cache.stats()
    assert stats["misses"] == 1


async def test_entry_expires_after_ttl(cache: DataCache) -> None:
    await cache.set("k", 1, ttl=0.05)
    assert await cache.get("k") is not None
    await asyncio.sleep(0.06)
    assert await cache.get("k") is None


async def test_zero_ttl_is_never_cached(cache: DataCache) -> None:
    entry = await cache.set("k", 1, ttl=0)
    assert entry.expired is True
    assert await cache.get("k") is None
    assert cache.path_for("k") is not None
    assert not (cache.path_for("k") or Path("/nonexistent")).exists()


async def test_disk_tier_survives_a_new_cache_instance(tmp_path: Path) -> None:
    first = DataCache(tmp_path / "cache", default_ttl=600.0)
    await first.set("shared", [1, 2, 3], source="test")

    second = DataCache(tmp_path / "cache", default_ttl=600.0)
    entry = await second.get("shared")
    assert entry is not None
    assert entry.value == [1, 2, 3]
    stats = await second.stats()
    assert stats["disk_hits"] == 1


async def test_memory_only_cache_writes_no_files(tmp_path: Path) -> None:
    cache = DataCache.memory_only(default_ttl=60.0)
    await cache.set("k", "v")
    assert (await cache.get("k")) is not None
    assert list(tmp_path.iterdir()) == []
    stats = await cache.stats()
    assert stats["disk_enabled"] is False


async def test_get_or_fetch_calls_fetch_once_per_miss(cache: DataCache) -> None:
    calls = 0

    async def fetch() -> int:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return 42

    entries = await asyncio.gather(
        *(cache.get_or_fetch("k", fetch, ttl=60.0, source="test") for _ in range(5))
    )
    assert {entry.value for entry in entries} == {42}
    assert calls == 1, "concurrent callers must share one fetch"

    again = await cache.get_or_fetch("k", fetch)
    assert again.value == 42
    assert calls == 1


async def test_get_or_fetch_propagates_fetch_errors(cache: DataCache) -> None:
    async def boom() -> int:
        raise RuntimeError("vendor down")

    with pytest.raises(RuntimeError, match="vendor down"):
        await cache.get_or_fetch("k", boom)
    assert await cache.get("k") is None


async def test_invalidate_and_prefix(cache: DataCache) -> None:
    await cache.set("yf:quote:AAPL", 1)
    await cache.set("yf:quote:MSFT", 2)
    await cache.set("edgar:cik:AAPL", 3)

    assert await cache.invalidate("yf:quote:AAPL") is True
    assert await cache.invalidate("yf:quote:AAPL") is False
    assert await cache.invalidate_prefix("yf:") == 1
    assert await cache.get("edgar:cik:AAPL") is not None


async def test_clear_empties_both_tiers(cache: DataCache) -> None:
    await cache.set("a", 1)
    await cache.set("b", 2)
    assert await cache.clear() == 2
    stats = await cache.stats()
    assert stats["memory_entries"] == 0
    assert stats["disk_entries"] == 0


async def test_corrupt_disk_file_is_treated_as_a_miss(cache: DataCache) -> None:
    await cache.set("k", 1, ttl=600.0)
    path = cache.path_for("k")
    assert path is not None
    path.write_bytes(b"not a pickle")
    cache._memory.clear()  # force the disk tier to be consulted

    assert await cache.get("k") is None
    assert not path.exists()
    stats = await cache.stats()
    assert stats["errors"] == 1


async def test_stats_report_hit_rate(cache: DataCache) -> None:
    await cache.set("k", 1)
    await cache.get("k")
    await cache.get("missing")
    stats = await cache.stats()
    assert stats["memory_hits"] == 1
    assert stats["misses"] == 1
    assert stats["hit_rate"] == pytest.approx(0.5)


def test_provider_key_is_canonical_and_stable() -> None:
    from datetime import datetime, timezone

    assert provider_key("yfinance", "bars", "AAPL", interval="1d") == (
        "yfinance:bars:AAPL:interval=1d"
    )
    assert provider_key("yfinance", "bars", "AAPL", interval="1d", limit=None) == (
        "yfinance:bars:AAPL:interval=1d"
    ), "None arguments must not split the cache"
    assert provider_key("yf", "q", b="2", a="1") == provider_key("yf", "q", a="1", b="2")
    assert provider_key("yf", "quotes", ["MSFT", "AAPL"]) == "yf:quotes:AAPL,MSFT"
    assert provider_key(
        "yf", "bars", start=datetime(2026, 1, 1, tzinfo=timezone.utc)
    ) == "yf:bars:start=2026-01-01T00:00:00+00:00"


def test_cache_entry_metadata_excludes_the_value() -> None:
    from datetime import datetime, timezone

    entry = CacheEntry(
        key="k",
        value={"secret": 1},
        fetched_at=datetime.now(timezone.utc),
        ttl_seconds=30.0,
        source="test",
    )
    payload = entry.as_dict()
    assert "value" not in payload
    assert payload["ttl_seconds"] == 30.0
