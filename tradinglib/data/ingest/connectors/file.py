"""File connector: ingests local files matched by a glob pattern."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tradinglib.data.ingest.connectors.base import (
    ConnectionTestResult,
    Connector,
    FetchResult,
)
from tradinglib.data.ingest.source import PayloadFormat, SourceConfig, SourceStatus

logger = logging.getLogger(__name__)

#: Formats read as bytes rather than text.
_BINARY_FORMATS: frozenset[PayloadFormat] = frozenset(
    {PayloadFormat.PARQUET, PayloadFormat.ARROW}
)


class FileConnector(Connector):
    """Reads CSV/JSON/Parquet files from disk, tracking which are already ingested.

    The ledger of ingested files lives at
    ``<data_dir>/ingest/<source_id>.files.json`` and records each file's size
    and modification time, so a file that is rewritten is re-ingested while an
    untouched one is skipped.

    Advanced configuration keys::

        glob        discovery pattern (overrides the endpoint)
        recursive   ``True`` to expand ``**`` (implied when the pattern has it)
        ledger      ``False`` to re-read every matching file on every fetch
    """

    protocol = "file"

    def __init__(self, config: SourceConfig, *, ledger_path: Path | None = None) -> None:
        """Bind the connector; ``ledger_path`` is overridable for tests."""
        super().__init__(config)
        self._ledger_path = ledger_path
        self._ledger: dict[str, dict[str, Any]] = {}
        self._loaded = False

    # --- layout -----------------------------------------------------------
    @property
    def pattern(self) -> str:
        """The discovery pattern this source reads."""
        return str(self.config.advanced.glob or self.config.endpoint or "").strip()

    @property
    def ledger_path(self) -> Path:
        """Path of the ingested-file ledger."""
        if self._ledger_path is not None:
            return self._ledger_path
        from tradinglib.config import get_settings

        return get_settings().data_dir / "ingest" / f"{self.config.id}.files.json"

    @property
    def use_ledger(self) -> bool:
        """Whether already-ingested files are skipped."""
        return bool(self.config.advanced.get("ledger", True))

    def matches(self) -> list[Path]:
        """Return every file matching the pattern, oldest modification first.

        Raises:
            ValueError: when no pattern is configured.
        """
        pattern = self.pattern
        if not pattern:
            raise ValueError("file source has no path or glob pattern")
        path = Path(pattern).expanduser()
        if path.is_file():
            return [path]
        if path.is_dir():
            candidates = sorted(p for p in path.iterdir() if p.is_file())
        else:
            anchor = path.anchor
            if anchor:
                root = Path(anchor)
                relative = str(path.relative_to(anchor))
            else:
                root = Path.cwd()
                relative = str(path)
            candidates = [p for p in root.glob(relative) if p.is_file()]
        return sorted(candidates, key=lambda p: (p.stat().st_mtime, str(p)))

    # --- ledger -----------------------------------------------------------
    def _load_ledger(self) -> None:
        """Load the ingested-file ledger from disk (missing file = empty ledger)."""
        if self._loaded:
            return
        self._loaded = True
        path = self.ledger_path
        if not path.is_file():
            self._ledger = {}
            return
        try:
            self._ledger = json.loads(path.read_text())
        except (OSError, ValueError):
            logger.warning("ingest %s: unreadable ledger %s; starting empty", self.config.id, path)
            self._ledger = {}

    def _save_ledger(self) -> None:
        """Write the ledger atomically."""
        path = self.ledger_path
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".json.tmp")
        os.close(fd)
        try:
            Path(tmp).write_text(json.dumps(self._ledger, indent=1, sort_keys=True))
            os.chmod(tmp, 0o644)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    @staticmethod
    def _stamp(path: Path) -> dict[str, Any]:
        """Return the ledger entry describing a file's current state."""
        stat = path.stat()
        return {
            "size": stat.st_size,
            "mtime": stat.st_mtime,
            "ingested_at": datetime.now(timezone.utc).isoformat(),
        }

    def _is_new(self, path: Path) -> bool:
        """Whether a file has not been ingested in its current state."""
        if not self.use_ledger:
            return True
        entry = self._ledger.get(str(path))
        if entry is None:
            return True
        stat = path.stat()
        return entry.get("size") != stat.st_size or entry.get("mtime") != stat.st_mtime

    # --- lifecycle --------------------------------------------------------
    async def connect(self) -> None:
        """Resolve the glob root and load the ingested-file ledger."""
        await asyncio.to_thread(self._load_ledger)

    async def close(self) -> None:
        """Flush the ledger. No network resources are held."""
        if self._loaded and self.use_ledger:
            await asyncio.to_thread(self._save_ledger)

    # --- reading ----------------------------------------------------------
    def _read(self, path: Path) -> list[dict[str, Any]]:
        """Parse one file into mapped records."""
        body: bytes | str
        if self.config.format in _BINARY_FORMATS:
            body = path.read_bytes()
        else:
            body = path.read_text(encoding=str(self.config.advanced.get("encoding") or "utf-8"))
        records = self.decode(body)
        source_field = str(self.config.advanced.get("source_field") or "")
        if source_field:
            for record in records:
                record.setdefault(source_field, str(path))
        return records

    async def fetch(self, **params: Any) -> FetchResult:
        """Read files not yet ingested and return their records.

        Raises:
            ValueError: when the pattern is missing or a file does not parse.
        """
        await self.connect()
        started = time.perf_counter()
        limit = params.get("limit")
        try:
            candidates = await asyncio.to_thread(self.matches)
        except (OSError, ValueError) as exc:
            self._mark_failure(str(exc))
            raise
        pending = [p for p in candidates if self._is_new(p)]
        if limit:
            pending = pending[: int(limit)]

        records: list[dict[str, Any]] = []
        for path in pending:
            try:
                records.extend(await asyncio.to_thread(self._read, path))
            except (OSError, ValueError) as exc:
                self._mark_failure(f"{path}: {exc}")
                raise
            self._ledger[str(path)] = self._stamp(path)
        if pending and self.use_ledger:
            await asyncio.to_thread(self._save_ledger)

        latency_ms = (time.perf_counter() - started) * 1000.0
        self._mark_success(latency_ms)
        return FetchResult(
            records=records,
            fetched_at=datetime.now(timezone.utc),
            latency_ms=latency_ms,
            complete=True,
            cursor=str(pending[-1]) if pending else None,
        )

    async def backfill(
        self, *, range: str = "1y", batch_size: int = 500, end: datetime | None = None
    ) -> AsyncIterator[FetchResult]:
        """Re-read every matching file in chronological order.

        ``range`` selects how far back to go by file modification time, ``end``
        bounds that window above, and ``batch_size`` caps the number of records
        per yielded batch.
        """
        from tradinglib.data.ingest.source import BackfillConfig

        await self.connect()
        cutoff = BackfillConfig(enabled=True, range=range).range_start(now=end)
        paths = await asyncio.to_thread(self.matches)
        if cutoff is not None or end is not None:
            def in_window(path: Path) -> bool:
                """Whether the file's mtime falls inside the requested window."""
                stamp = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
                if cutoff is not None and stamp < cutoff:
                    return False
                return not (end is not None and stamp > end)

            paths = [p for p in paths if in_window(p)]

        batch: list[dict[str, Any]] = []
        for path in paths:
            batch.extend(await asyncio.to_thread(self._read, path))
            self._ledger[str(path)] = self._stamp(path)
            if len(batch) >= batch_size:
                yield FetchResult(
                    records=batch,
                    fetched_at=datetime.now(timezone.utc),
                    complete=False,
                    cursor=str(path),
                )
                batch = []
        if self.use_ledger:
            await asyncio.to_thread(self._save_ledger)
        yield FetchResult(
            records=batch,
            fetched_at=datetime.now(timezone.utc),
            complete=True,
            cursor=str(paths[-1]) if paths else None,
        )

    # --- test / status ----------------------------------------------------
    async def test(self) -> ConnectionTestResult:
        """Match the glob, parse the first file and report a real sample."""
        started = time.perf_counter()
        try:
            paths = await asyncio.to_thread(self.matches)
        except (OSError, ValueError) as exc:
            self._mark_failure(str(exc))
            return ConnectionTestResult.failure(str(exc), reachable=False)
        if not paths:
            message = f"no files match {self.pattern!r}"
            self._mark_failure(message)
            return ConnectionTestResult.failure(message, reachable=True, authenticated=True)
        try:
            records = await asyncio.to_thread(self._read, paths[0])
        except (OSError, ValueError) as exc:
            message = f"{paths[0]} did not parse as {self.config.format.label}: {exc}"
            self._mark_failure(message)
            return ConnectionTestResult.failure(message, reachable=True, authenticated=True)
        latency_ms = (time.perf_counter() - started) * 1000.0
        self._mark_success(latency_ms)
        return ConnectionTestResult.success(
            f"{len(paths)} file(s) matched · {paths[0].name} parsed into "
            f"{len(records)} records",
            latency_ms=latency_ms,
            authenticated=True,
            records=records,
        )

    async def status(self) -> SourceStatus:
        """Report whether the configured path exists and matches files."""
        if not self.pattern:
            return SourceStatus.NOT_CONFIGURED
        try:
            paths = await asyncio.to_thread(self.matches)
        except (OSError, ValueError):
            return SourceStatus.DISCONNECTED
        if not paths:
            return SourceStatus.DEGRADED
        return SourceStatus.CONNECTED


__all__ = ["FileConnector"]
