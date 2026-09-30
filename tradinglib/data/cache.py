"""Two-tier cache in front of the providers.

An in-process TTL layer absorbs bursts (a page render asking for the same quote
several times); a disk layer under ``<data_dir>/cache`` survives restarts and
keeps vendor rate limits comfortable. Cache entries always carry the time they
were fetched so the UI can show real data-freshness values.

The disk tier stores one pickle per key (the values are our own dataclasses and
pandas frames, which JSON cannot round-trip). A file that cannot be unpickled -
after a dependency upgrade, say - is treated as a miss and deleted rather than
raised, because a cache must never be able to break a request.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import pickle
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Suffix used for disk-tier files.
CACHE_SUFFIX = ".pkl"


def provider_key(provider: str, call: str, *args: Any, **kwargs: Any) -> str:
    """Build the canonical cache key for one provider call.

    ``provider_key("yfinance", "bars", "AAPL", interval="1d")`` yields
    ``"yfinance:bars:AAPL:interval=1d"``. Keyword arguments are sorted so the
    same call always produces the same key, and ``None`` values are dropped so
    an explicit default does not split the cache entry.
    """
    parts = [str(provider), str(call)]
    parts.extend(_key_part(arg) for arg in args if arg is not None)
    parts.extend(
        f"{name}={_key_part(value)}"
        for name, value in sorted(kwargs.items())
        if value is not None
    )
    return ":".join(parts)


def _key_part(value: Any) -> str:
    """Render one key component deterministically."""
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, (list, tuple, set, frozenset)):
        return ",".join(sorted(_key_part(item) for item in value))
    return str(value)


@dataclass(frozen=True, slots=True)
class CacheEntry:
    """A cached value together with its provenance."""

    key: str
    value: Any
    fetched_at: datetime
    ttl_seconds: float
    source: str

    @property
    def age_seconds(self) -> float:
        """Seconds elapsed since the value was fetched."""
        return (datetime.now(timezone.utc) - self.fetched_at).total_seconds()

    @property
    def expired(self) -> bool:
        """Whether the entry has outlived its TTL.

        A non-positive TTL means "never cache"; such an entry is born expired.
        """
        if self.ttl_seconds <= 0:
            return True
        return self.age_seconds >= self.ttl_seconds

    def as_dict(self) -> dict[str, Any]:
        """Return the entry's metadata (not its value) for health endpoints."""
        return {
            "key": self.key,
            "fetched_at": self.fetched_at.isoformat(),
            "age_seconds": self.age_seconds,
            "ttl_seconds": self.ttl_seconds,
            "source": self.source,
            "expired": self.expired,
        }


class DataCache:
    """Async memory+disk cache keyed by provider-qualified strings.

    Keys are free-form strings, by convention ``"<provider>:<call>:<args>"``.
    They are hashed for the filesystem, so any characters are allowed.
    """

    def __init__(self, cache_dir: Path | None = None, *, default_ttl: float = 60.0) -> None:
        """Create the cache.

        Args:
            cache_dir: disk tier location; defaults to the configured cache dir.
                Pass ``None`` explicitly only when the configured location is
                wanted - use :meth:`memory_only` for a tier-less cache.
            default_ttl: TTL applied when a caller does not specify one.
        """
        if cache_dir is None:
            from tradinglib.config import get_settings

            cache_dir = get_settings().cache_dir
        self.cache_dir: Path | None = Path(cache_dir)
        self.default_ttl = float(default_ttl)
        self._memory: dict[str, CacheEntry] = {}
        self._lock = asyncio.Lock()
        self._key_locks: dict[str, asyncio.Lock] = {}
        self._counters: dict[str, int] = {
            "memory_hits": 0,
            "disk_hits": 0,
            "misses": 0,
            "sets": 0,
            "evictions": 0,
            "errors": 0,
        }
        if self.cache_dir is not None:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def memory_only(cls, *, default_ttl: float = 60.0) -> DataCache:
        """Return a cache with the disk tier disabled (tests, ephemeral use)."""
        cache = cls.__new__(cls)
        cache.cache_dir = None
        cache.default_ttl = float(default_ttl)
        cache._memory = {}
        cache._lock = asyncio.Lock()
        cache._key_locks = {}
        cache._counters = {
            "memory_hits": 0,
            "disk_hits": 0,
            "misses": 0,
            "sets": 0,
            "evictions": 0,
            "errors": 0,
        }
        return cache

    # --- paths ------------------------------------------------------------
    def path_for(self, key: str) -> Path | None:
        """Return the disk-tier path for ``key``, or ``None`` when disabled."""
        if self.cache_dir is None:
            return None
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{digest}{CACHE_SUFFIX}"

    # --- reads ------------------------------------------------------------
    async def get(self, key: str) -> CacheEntry | None:
        """Return a live entry for ``key``, or ``None`` when absent/expired."""
        async with self._lock:
            entry = self._memory.get(key)
        if entry is not None:
            if not entry.expired:
                self._counters["memory_hits"] += 1
                return entry
            await self.invalidate(key)
            self._counters["evictions"] += 1

        entry = await asyncio.to_thread(self._read_disk, key)
        if entry is None:
            self._counters["misses"] += 1
            return None
        if entry.expired:
            self._counters["evictions"] += 1
            self._counters["misses"] += 1
            await asyncio.to_thread(self._delete_disk, key)
            return None
        async with self._lock:
            self._memory[key] = entry
        self._counters["disk_hits"] += 1
        return entry

    def _read_disk(self, key: str) -> CacheEntry | None:
        """Load an entry from the disk tier (blocking; runs in a thread)."""
        path = self.path_for(key)
        if path is None or not path.is_file():
            return None
        try:
            with path.open("rb") as handle:
                entry = pickle.load(handle)
        except Exception:  # noqa: BLE001 - a corrupt cache file is a miss
            self._counters["errors"] += 1
            logger.warning("discarding unreadable cache file %s", path)
            path.unlink(missing_ok=True)
            return None
        if not isinstance(entry, CacheEntry):
            path.unlink(missing_ok=True)
            return None
        return entry

    def _delete_disk(self, key: str) -> bool:
        """Remove one disk-tier file. Returns whether a file was removed."""
        path = self.path_for(key)
        if path is None or not path.is_file():
            return False
        path.unlink(missing_ok=True)
        return True

    # --- writes -----------------------------------------------------------
    async def set(
        self, key: str, value: Any, *, ttl: float | None = None, source: str = "unknown"
    ) -> CacheEntry:
        """Store ``value`` under ``key`` and return the stored entry."""
        entry = CacheEntry(
            key=key,
            value=value,
            fetched_at=datetime.now(timezone.utc),
            ttl_seconds=self.default_ttl if ttl is None else float(ttl),
            source=source,
        )
        async with self._lock:
            self._memory[key] = entry
        self._counters["sets"] += 1
        if self.cache_dir is not None and entry.ttl_seconds > 0:
            await asyncio.to_thread(self._write_disk, entry)
        return entry

    def _write_disk(self, entry: CacheEntry) -> None:
        """Persist an entry atomically (blocking; runs in a thread)."""
        path = self.path_for(entry.key)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as handle:
                    pickle.dump(entry, handle, protocol=pickle.HIGHEST_PROTOCOL)
                os.replace(tmp_name, path)
            except BaseException:
                Path(tmp_name).unlink(missing_ok=True)
                raise
        except Exception:  # noqa: BLE001 - the memory tier still holds the value
            self._counters["errors"] += 1
            logger.warning("could not persist cache entry %s", entry.key, exc_info=True)

    async def get_or_fetch(
        self,
        key: str,
        fetch: Callable[[], Awaitable[Any]],
        *,
        ttl: float | None = None,
        source: str = "unknown",
    ) -> CacheEntry:
        """Return the cached entry for ``key``, calling ``fetch`` on a miss.

        Concurrent callers asking for the same key share one ``fetch`` call, so
        a page rendering ten panels never triggers ten identical vendor hits.
        """
        entry = await self.get(key)
        if entry is not None:
            return entry

        async with self._lock:
            lock = self._key_locks.setdefault(key, asyncio.Lock())
        try:
            async with lock:
                entry = await self.get(key)
                if entry is not None:
                    return entry
                value = await fetch()
                return await self.set(key, value, ttl=ttl, source=source)
        finally:
            # Drop the per-key lock once nobody is waiting on it, so a long-lived
            # process does not accumulate one lock per key it ever fetched.
            async with self._lock:
                existing = self._key_locks.get(key)
                if existing is not None and not existing.locked():
                    self._key_locks.pop(key, None)

    # --- invalidation -----------------------------------------------------
    async def invalidate(self, key: str) -> bool:
        """Drop one key. Returns whether anything was removed."""
        async with self._lock:
            removed = self._memory.pop(key, None) is not None
        if self.cache_dir is not None:
            removed = await asyncio.to_thread(self._delete_disk, key) or removed
        return removed

    async def invalidate_prefix(self, prefix: str) -> int:
        """Drop every key starting with ``prefix``. Returns the count removed.

        Only keys this process has seen can be matched by prefix, because the
        disk tier is keyed by hash; their files are removed alongside.
        """
        async with self._lock:
            keys = [k for k in self._memory if k.startswith(prefix)]
        count = 0
        for key in keys:
            if await self.invalidate(key):
                count += 1
        return count

    async def clear(self) -> int:
        """Drop every entry in both tiers. Returns the number of keys removed."""
        async with self._lock:
            keys = list(self._memory)
            self._memory.clear()
        removed = len(keys)
        if self.cache_dir is not None:
            removed = max(removed, await asyncio.to_thread(self._clear_disk))
        return removed

    def _clear_disk(self) -> int:
        """Delete every disk-tier file. Returns how many were removed."""
        if self.cache_dir is None or not self.cache_dir.is_dir():
            return 0
        count = 0
        for path in self.cache_dir.glob(f"*{CACHE_SUFFIX}"):
            path.unlink(missing_ok=True)
            count += 1
        return count

    # --- introspection ----------------------------------------------------
    async def stats(self) -> dict[str, Any]:
        """Return hit/miss counters and entry counts for the health endpoints."""
        async with self._lock:
            live = sum(1 for entry in self._memory.values() if not entry.expired)
            total = len(self._memory)
        disk_entries = 0
        if self.cache_dir is not None:
            disk_entries = await asyncio.to_thread(
                lambda: sum(1 for _ in self.cache_dir.glob(f"*{CACHE_SUFFIX}"))  # type: ignore[union-attr]
            )
        lookups = (
            self._counters["memory_hits"] + self._counters["disk_hits"] + self._counters["misses"]
        )
        hits = self._counters["memory_hits"] + self._counters["disk_hits"]
        return {
            **self._counters,
            "hit_rate": (hits / lookups) if lookups else None,
            "memory_entries": total,
            "memory_entries_live": live,
            "disk_entries": disk_entries,
            "disk_enabled": self.cache_dir is not None,
            "cache_dir": str(self.cache_dir) if self.cache_dir else None,
            "default_ttl": self.default_ttl,
        }


_cache: DataCache | None = None


def get_cache() -> DataCache:
    """Return the process-wide cache, creating it on first use."""
    global _cache
    if _cache is None:
        _cache = DataCache()
    return _cache


def reset_cache(cache: DataCache | None = None) -> DataCache:
    """Replace the process-wide cache. Intended for tests and reconfiguration."""
    global _cache
    _cache = cache if cache is not None else DataCache()
    return _cache
