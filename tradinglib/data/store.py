"""Parquet + DuckDB time-series store (``docs/SPEC.md`` decision D4).

Bars, macro series and alt-data signals are written as partitioned Parquet
under ``<data_dir>/ts`` and queried with DuckDB, which reads the Parquet files
directly - there is no second copy of the data.

Layout (Hive-style directory names, so DuckDB globs stay readable)::

    data/ts/bars/interval=1d/symbol=AAPL/part-0000.parquet
    data/ts/series/namespace=fred/name=DGS10/part-0000.parquet
    data/ts/<dataset>/...                    (arbitrary frames, write_frame)

Every partition holds exactly one file which is rewritten on upsert: the file
is read, merged with the incoming rows, deduplicated on the time key (incoming
rows win), sorted and written back atomically. Daily bars for one symbol are a
few thousand rows, so a rewrite is cheaper than maintaining a delta log, and it
gives real upsert semantics rather than append-with-duplicates.

All timestamps are stored timezone-aware in UTC (``docs/SPEC.md`` section 4).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
import threading
from collections.abc import Sequence
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Final, Protocol

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from tradinglib.data.base import Bar, BarInterval, Series, to_utc

logger = logging.getLogger(__name__)

#: Directory name of the time-series dataset inside the configured data dir.
TS_DIRNAME: Final[str] = "ts"

#: Dataset (domain) names owned by this module.
BARS_DATASET: Final[str] = "bars"
SERIES_DATASET: Final[str] = "series"

#: Single file name written inside each partition directory.
PART_FILENAME: Final[str] = "part-0000.parquet"

#: Characters kept verbatim in a partition value; everything else is escaped.
_SAFE_PARTITION = re.compile(r"[^A-Za-z0-9._+-]")

#: Optional callback the quality engine installs so quarantined observations
#: never reach a feature, a screen or a backtest. It takes the dataset key
#: (``bars:AAPL:1d``, ``series:fred:DGS10``) and the timestamps about to be
#: returned, and answers with the subset that is quarantined.
#:
#: The hook lives here rather than in :mod:`tradinglib.data.quality` because the
#: read path is the only place an exclusion can be enforced for *every*
#: consumer; the quality engine imports the store, so the dependency has to
#: point this way round.
_quarantine_filter: QuarantineFilter | None = None


class QuarantineFilter(Protocol):
    """Callable that reports which stored timestamps are quarantined."""

    def __call__(self, dataset: str, timestamps: Sequence[pd.Timestamp]) -> set[pd.Timestamp]:
        """Return the subset of ``timestamps`` that must not be served."""


def set_quarantine_filter(hook: QuarantineFilter | None) -> QuarantineFilter | None:
    """Install (or clear) the quarantine hook and return the previous one."""
    global _quarantine_filter
    previous = _quarantine_filter
    _quarantine_filter = hook
    return previous


def _apply_quarantine(dataset: str, frame: pd.DataFrame) -> pd.DataFrame:
    """Drop quarantined rows from ``frame`` (indexed by timestamp).

    A failing hook is logged and ignored: quality tooling must never be able to
    make the data layer unreadable.
    """
    hook = _quarantine_filter
    if hook is None or frame.empty:
        return frame
    try:
        excluded = hook(dataset, list(frame.index))
    except Exception:  # noqa: BLE001 - a broken hook must not break reads
        logger.warning("quarantine filter failed for %s", dataset, exc_info=True)
        return frame
    if not excluded:
        return frame
    keep = [ts not in excluded for ts in frame.index]
    return frame[keep]


#: Arrow schema of the bars dataset.
BARS_SCHEMA: Final[pa.Schema] = pa.schema(
    [
        pa.field("symbol", pa.string(), nullable=False),
        pa.field("ts", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("open", pa.float64()),
        pa.field("high", pa.float64()),
        pa.field("low", pa.float64()),
        pa.field("close", pa.float64()),
        pa.field("volume", pa.float64()),
        pa.field("interval", pa.string(), nullable=False),
        pa.field("source", pa.string()),
    ]
)

#: Arrow schema of the named-series dataset.
SERIES_SCHEMA: Final[pa.Schema] = pa.schema(
    [
        pa.field("namespace", pa.string(), nullable=False),
        pa.field("name", pa.string(), nullable=False),
        pa.field("ts", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("value", pa.float64()),
        pa.field("unit", pa.string()),
        pa.field("source", pa.string()),
    ]
)

#: DuckDB type list used to build empty views when a dataset has no files yet.
_EMPTY_VIEW_COLUMNS: Final[dict[str, list[tuple[str, str]]]] = {
    BARS_DATASET: [
        ("symbol", "VARCHAR"),
        ("ts", "TIMESTAMP WITH TIME ZONE"),
        ("open", "DOUBLE"),
        ("high", "DOUBLE"),
        ("low", "DOUBLE"),
        ("close", "DOUBLE"),
        ("volume", "DOUBLE"),
        ("interval", "VARCHAR"),
        ("source", "VARCHAR"),
    ],
    SERIES_DATASET: [
        ("namespace", "VARCHAR"),
        ("name", "VARCHAR"),
        ("ts", "TIMESTAMP WITH TIME ZONE"),
        ("value", "DOUBLE"),
        ("unit", "VARCHAR"),
        ("source", "VARCHAR"),
    ],
}


def partition_value(value: str) -> str:
    """Return a filesystem-safe rendering of a partition value.

    ``BRK.B`` stays ``BRK.B``; ``^GSPC`` becomes ``_GSPC``. The mapping is
    deterministic and applied consistently on read and write, so a symbol whose
    escaped form collides with another (``^X`` and ``.X``) would share a
    partition - callers therefore also store the raw value in a column, which
    is what every read filters on.
    """
    cleaned = _SAFE_PARTITION.sub("_", value.strip())
    return cleaned or "_"


def _to_utc_timestamp(value: datetime | date) -> datetime:
    """Coerce a date/datetime to an aware UTC datetime (dates -> midnight UTC)."""
    if isinstance(value, datetime):
        return to_utc(value)
    return datetime.combine(value, time.min, tzinfo=timezone.utc)


class TimeSeriesStore:
    """Partitioned Parquet dataset with a DuckDB query surface."""

    def __init__(self, root: Path | None = None) -> None:
        """Create the store.

        Args:
            root: dataset root; defaults to ``<data_dir>/ts``.
        """
        if root is None:
            from tradinglib.config import get_settings

            root = get_settings().data_dir / TS_DIRNAME
        self.root: Path = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.RLock()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"TimeSeriesStore(root={str(self.root)!r})"

    # --- layout -----------------------------------------------------------
    def dataset_dir(self, dataset: str) -> Path:
        """Return the directory holding ``dataset``."""
        return self.root / partition_value(dataset)

    def bars_path(self, symbol: str, interval: BarInterval) -> Path:
        """Return the Parquet path holding ``symbol`` bars at ``interval``."""
        return (
            self.dataset_dir(BARS_DATASET)
            / f"interval={partition_value(str(interval))}"
            / f"symbol={partition_value(symbol.upper())}"
            / PART_FILENAME
        )

    def series_path(self, namespace: str, name: str) -> Path:
        """Return the Parquet path holding a named non-price series."""
        return (
            self.dataset_dir(SERIES_DATASET)
            / f"namespace={partition_value(namespace)}"
            / f"name={partition_value(name)}"
            / PART_FILENAME
        )

    # --- low-level parquet helpers ---------------------------------------
    @staticmethod
    def _write_table_atomic(table: pa.Table, path: Path) -> None:
        """Write ``table`` to ``path`` via a temporary file and an atomic rename."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".parquet.tmp")
        os.close(fd)
        try:
            pq.write_table(table, tmp_name, compression="zstd")
            # mkstemp creates 0600; data files should follow the usual default
            # so other tooling (duckdb CLI, notebooks) can read them.
            os.chmod(tmp_name, 0o644)
            os.replace(tmp_name, path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise

    @staticmethod
    def _read_table(path: Path, schema: pa.Schema) -> pa.Table:
        """Read a partition file, returning an empty table when it is absent."""
        if not path.is_file():
            return schema.empty_table()
        table = pq.read_table(path)
        return table.cast(schema) if table.schema != schema else table

    def _upsert(self, path: Path, incoming: pa.Table, schema: pa.Schema, keys: list[str]) -> int:
        """Merge ``incoming`` into the partition at ``path``. Returns rows written."""
        with self._write_lock:
            existing = self._read_table(path, schema)
            combined = (
                incoming if existing.num_rows == 0 else pa.concat_tables([existing, incoming])
            )
            frame = combined.to_pandas(types_mapper=None)
            frame = frame.drop_duplicates(subset=keys, keep="last")
            frame = frame.sort_values(keys, kind="stable").reset_index(drop=True)
            merged = pa.Table.from_pandas(frame, schema=schema, preserve_index=False)
            self._write_table_atomic(merged, path)
            return int(merged.num_rows)

    # --- writes -----------------------------------------------------------
    def write_bars(self, bars: list[Bar], *, interval: BarInterval = BarInterval.DAY_1) -> int:
        """Upsert ``bars`` (deduplicated on symbol+timestamp). Returns rows written.

        ``interval`` is the partition to write into; a bar carrying a different
        interval is written to its own partition, so a mixed list is fine.
        """
        if not bars:
            return 0
        groups: dict[tuple[str, BarInterval], list[Bar]] = {}
        for bar in bars:
            key = (bar.symbol.upper(), bar.interval or interval)
            groups.setdefault(key, []).append(bar)

        written = 0
        for (symbol, bar_interval), group in groups.items():
            table = pa.Table.from_pydict(
                {
                    "symbol": [symbol] * len(group),
                    "ts": [_to_utc_timestamp(b.ts) for b in group],
                    "open": [float(b.open) for b in group],
                    "high": [float(b.high) for b in group],
                    "low": [float(b.low) for b in group],
                    "close": [float(b.close) for b in group],
                    "volume": [float(b.volume) for b in group],
                    "interval": [str(bar_interval)] * len(group),
                    "source": [b.source for b in group],
                },
                schema=BARS_SCHEMA,
            )
            path = self.bars_path(symbol, bar_interval)
            written += self._upsert(path, table, BARS_SCHEMA, ["symbol", "ts"])
        return written

    def write_series(self, namespace: str, series: Series) -> int:
        """Upsert a named series. Returns the number of rows written."""
        points = series.sorted().points
        if not points:
            return 0
        table = pa.Table.from_pydict(
            {
                "namespace": [namespace] * len(points),
                "name": [series.name] * len(points),
                "ts": [_to_utc_timestamp(ts) for ts, _ in points],
                "value": [float(value) for _, value in points],
                "unit": [series.unit] * len(points),
                "source": [series.source] * len(points),
            },
            schema=SERIES_SCHEMA,
        )
        path = self.series_path(namespace, series.name)
        return self._upsert(path, table, SERIES_SCHEMA, ["namespace", "name", "ts"])

    def write_frame(
        self, dataset: str, frame: pd.DataFrame, *, partition_cols: list[str] | None = None
    ) -> int:
        """Write an arbitrary dataframe into ``dataset``. Returns rows written.

        Unlike the bar/series writers this is a whole-partition overwrite
        handled by :func:`pyarrow.parquet.write_to_dataset`: data already
        present for a partition being written is deleted first
        (``delete_matching``). Without ``partition_cols`` the partition is the
        entire dataset, so the call replaces it.
        """
        if frame.empty:
            return 0
        table = pa.Table.from_pandas(frame, preserve_index=False)
        target = self.dataset_dir(dataset)
        with self._write_lock:
            target.mkdir(parents=True, exist_ok=True)
            pq.write_to_dataset(
                table,
                root_path=str(target),
                partition_cols=partition_cols or None,
                existing_data_behavior="delete_matching",
                compression="zstd",
            )
        return int(table.num_rows)

    # --- duckdb -----------------------------------------------------------
    def _dataset_files(self, dataset: str) -> list[str]:
        """Return every Parquet file belonging to ``dataset``."""
        base = self.dataset_dir(dataset)
        if not base.is_dir():
            return []
        return sorted(str(p) for p in base.rglob("*.parquet"))

    @staticmethod
    def _sql_literal(value: str) -> str:
        """Quote a string as a SQL literal (DDL cannot take bound parameters)."""
        return "'" + value.replace("'", "''") + "'"

    def connect(self) -> duckdb.DuckDBPyConnection:
        """Return a DuckDB connection with ``bars`` and ``series`` views bound.

        The views are rebuilt on every call from the files currently on disk, so
        a connection never serves a stale file list. Callers must close it.
        """
        con = duckdb.connect(database=":memory:")
        # Results must come back in UTC regardless of the host's timezone
        # (SPEC.md section 4: all timestamps are stored and served as UTC).
        con.execute("SET TimeZone='UTC'")
        for dataset in (BARS_DATASET, SERIES_DATASET):
            files = self._dataset_files(dataset)
            if files:
                # DDL statements cannot be prepared, so the (internally
                # generated) paths are quoted rather than bound.
                file_list = ", ".join(self._sql_literal(f) for f in files)
                con.execute(
                    f"CREATE OR REPLACE VIEW {dataset} AS "
                    f"SELECT * FROM read_parquet([{file_list}])"
                )
            else:
                cols = ", ".join(
                    f"CAST(NULL AS {sql_type}) AS {name}"
                    for name, sql_type in _EMPTY_VIEW_COLUMNS[dataset]
                )
                con.execute(f"CREATE OR REPLACE VIEW {dataset} AS SELECT {cols} WHERE 1=0")
        return con

    def query(self, sql: str, **params: Any) -> pd.DataFrame:
        """Run a DuckDB query over the stored Parquet datasets.

        The ``bars`` and ``series`` views are available. Pass parameters as
        keyword arguments and reference them as ``$name`` in ``sql``; never
        interpolate user input into the statement text.
        """
        con = self.connect()
        try:
            relation = con.execute(sql, params) if params else con.execute(sql)
            return relation.fetch_df()
        finally:
            con.close()

    # --- reads ------------------------------------------------------------
    def read_bars(
        self,
        symbol: str,
        *,
        interval: BarInterval = BarInterval.DAY_1,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> pd.DataFrame:
        """Return stored bars as a dataframe indexed by UTC timestamp.

        Columns: ``open``, ``high``, ``low``, ``close``, ``volume``, ``source``.
        An unknown symbol yields an empty frame with those columns, never an
        exception - "no rows" is a legitimate answer the UI must be able to show.
        """
        clauses = ["symbol = $symbol", "interval = $interval"]
        params: dict[str, Any] = {
            "symbol": symbol.upper(),
            "interval": str(interval),
        }
        if start is not None:
            clauses.append("ts >= $start")
            params["start"] = _to_utc_timestamp(start)
        if end is not None:
            clauses.append("ts <= $end")
            params["end"] = _to_utc_timestamp(end)
        sql = (
            "SELECT ts, open, high, low, close, volume, source FROM bars "
            f"WHERE {' AND '.join(clauses)} ORDER BY ts"
        )
        frame = self.query(sql, **params)
        if frame.empty:
            frame = pd.DataFrame(
                {
                    "ts": pd.Series(dtype="datetime64[us, UTC]"),
                    "open": pd.Series(dtype="float64"),
                    "high": pd.Series(dtype="float64"),
                    "low": pd.Series(dtype="float64"),
                    "close": pd.Series(dtype="float64"),
                    "volume": pd.Series(dtype="float64"),
                    "source": pd.Series(dtype="object"),
                }
            )
        return _apply_quarantine(
            f"bars:{symbol.upper()}:{interval}", frame.set_index("ts")
        )

    def read_bar_objects(
        self,
        symbol: str,
        *,
        interval: BarInterval = BarInterval.DAY_1,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Bar]:
        """Return stored bars as :class:`~tradinglib.data.base.Bar` objects."""
        frame = self.read_bars(symbol, interval=interval, start=start, end=end)
        return [
            Bar(
                symbol=symbol.upper(),
                ts=to_utc(ts.to_pydatetime()),
                open=float(row.open),
                high=float(row.high),
                low=float(row.low),
                close=float(row.close),
                volume=float(row.volume),
                interval=interval,
                source=str(row.source),
            )
            for ts, row in frame.iterrows()
        ]

    def read_series(
        self, namespace: str, name: str, *, start: date | None = None, end: date | None = None
    ) -> Series:
        """Return a stored named series (empty when nothing is stored yet)."""
        clauses = ["namespace = $namespace", "name = $name"]
        params: dict[str, Any] = {"namespace": namespace, "name": name}
        if start is not None:
            clauses.append("ts >= $start")
            params["start"] = _to_utc_timestamp(start)
        if end is not None:
            clauses.append("ts <= $end")
            params["end"] = _to_utc_timestamp(end)
        sql = (
            "SELECT ts, value, any_value(unit) OVER () AS unit, any_value(source) OVER () AS source "
            f"FROM series WHERE {' AND '.join(clauses)} ORDER BY ts"
        )
        frame = self.query(sql, **params)
        if frame.empty:
            return Series(name=name, points=[], meta={"namespace": namespace})
        unit = frame["unit"].iloc[0]
        source = frame["source"].iloc[0]
        excluded: set[Any] = set()
        if _quarantine_filter is not None:
            try:
                excluded = _quarantine_filter(
                    f"series:{namespace}:{name}", list(frame["ts"])
                )
            except Exception:  # noqa: BLE001 - a broken hook must not break reads
                logger.warning("quarantine filter failed for %s:%s", namespace, name, exc_info=True)
        points = [
            (to_utc(ts.to_pydatetime()), float(value))
            for ts, value in zip(frame["ts"], frame["value"], strict=True)
            if ts not in excluded
        ]
        return Series(
            name=name,
            points=points,
            unit=None if unit is None or pd.isna(unit) else str(unit),
            source="unknown" if source is None or pd.isna(source) else str(source),
            meta={"namespace": namespace},
        )

    def read_series_frame(
        self, namespace: str, name: str, *, start: date | None = None, end: date | None = None
    ) -> pd.DataFrame:
        """Return a stored named series as a dataframe indexed by UTC timestamp."""
        series = self.read_series(namespace, name, start=start, end=end)
        frame = pd.DataFrame(
            {
                "ts": [ts for ts, _ in series.points],
                "value": [value for _, value in series.points],
            }
        )
        if frame.empty:
            frame = pd.DataFrame(
                {
                    "ts": pd.Series(dtype="datetime64[us, UTC]"),
                    "value": pd.Series(dtype="float64"),
                }
            )
        return frame.set_index("ts")

    # --- introspection ----------------------------------------------------
    def latest_timestamp(
        self, symbol: str, interval: BarInterval = BarInterval.DAY_1
    ) -> datetime | None:
        """Return the newest stored bar timestamp for a symbol, or ``None``."""
        frame = self.query(
            "SELECT max(ts) AS last_ts FROM bars WHERE symbol = $symbol AND interval = $interval",
            symbol=symbol.upper(),
            interval=str(interval),
        )
        if frame.empty:
            return None
        value = frame["last_ts"].iloc[0]
        if value is None or pd.isna(value):
            return None
        return to_utc(pd.Timestamp(value).to_pydatetime())

    def series_latest_timestamp(self, namespace: str, name: str) -> datetime | None:
        """Return the newest stored timestamp of a named series, or ``None``."""
        frame = self.query(
            "SELECT max(ts) AS last_ts FROM series WHERE namespace = $namespace AND name = $name",
            namespace=namespace,
            name=name,
        )
        if frame.empty:
            return None
        value = frame["last_ts"].iloc[0]
        if value is None or pd.isna(value):
            return None
        return to_utc(pd.Timestamp(value).to_pydatetime())

    def coverage(self, symbol: str, interval: BarInterval = BarInterval.DAY_1) -> dict[str, Any]:
        """Return first/last timestamp, row count and file size for a symbol."""
        frame = self.query(
            "SELECT count(*) AS rows, min(ts) AS first_ts, max(ts) AS last_ts, "
            "count(DISTINCT ts) AS distinct_ts FROM bars "
            "WHERE symbol = $symbol AND interval = $interval",
            symbol=symbol.upper(),
            interval=str(interval),
        )
        rows = int(frame["rows"].iloc[0]) if not frame.empty else 0
        first_ts = frame["first_ts"].iloc[0] if not frame.empty else None
        last_ts = frame["last_ts"].iloc[0] if not frame.empty else None
        path = self.bars_path(symbol, interval)
        return {
            "symbol": symbol.upper(),
            "interval": str(interval),
            "rows": rows,
            "distinct_timestamps": int(frame["distinct_ts"].iloc[0]) if rows else 0,
            "first_ts": None if first_ts is None or pd.isna(first_ts) else to_utc(
                pd.Timestamp(first_ts).to_pydatetime()
            ).isoformat(),
            "last_ts": None if last_ts is None or pd.isna(last_ts) else to_utc(
                pd.Timestamp(last_ts).to_pydatetime()
            ).isoformat(),
            "path": str(path),
            "bytes": path.stat().st_size if path.is_file() else 0,
        }

    def symbols(self, interval: BarInterval | None = None) -> list[str]:
        """Return every symbol with stored bars, optionally for one interval."""
        if interval is None:
            frame = self.query("SELECT DISTINCT symbol FROM bars ORDER BY symbol")
        else:
            frame = self.query(
                "SELECT DISTINCT symbol FROM bars WHERE interval = $interval ORDER BY symbol",
                interval=str(interval),
            )
        return [str(s) for s in frame["symbol"].tolist()]

    def series_names(self, namespace: str | None = None) -> list[dict[str, str]]:
        """Return the stored named series, optionally filtered by namespace."""
        if namespace is None:
            frame = self.query(
                "SELECT DISTINCT namespace, name FROM series ORDER BY namespace, name"
            )
        else:
            frame = self.query(
                "SELECT DISTINCT namespace, name FROM series WHERE namespace = $namespace "
                "ORDER BY name",
                namespace=namespace,
            )
        return [
            {"namespace": str(row.namespace), "name": str(row.name)}
            for row in frame.itertuples(index=False)
        ]

    def datasets(self) -> list[dict[str, Any]]:
        """List stored datasets with file/row counts and byte sizes."""
        out: list[dict[str, Any]] = []
        if not self.root.is_dir():
            return out
        for child in sorted(self.root.iterdir()):
            if not child.is_dir():
                continue
            files = sorted(child.rglob("*.parquet"))
            rows = 0
            for path in files:
                try:
                    rows += pq.ParquetFile(path).metadata.num_rows
                except Exception:  # noqa: BLE001 - a broken file must not hide the rest
                    logger.warning("unreadable parquet file %s", path, exc_info=True)
            out.append(
                {
                    "dataset": child.name,
                    "files": len(files),
                    "rows": rows,
                    "bytes": sum(p.stat().st_size for p in files),
                    "path": str(child),
                }
            )
        return out

    def stats(self) -> dict[str, Any]:
        """Return an aggregate summary of the whole store."""
        datasets = self.datasets()
        return {
            "root": str(self.root),
            "datasets": datasets,
            "total_files": sum(d["files"] for d in datasets),
            "total_rows": sum(d["rows"] for d in datasets),
            "total_bytes": sum(d["bytes"] for d in datasets),
        }

    # --- destructive ------------------------------------------------------
    def drop_symbol(self, symbol: str, interval: BarInterval | None = None) -> int:
        """Delete stored bars for a symbol. Returns the number of files removed."""
        intervals = list(BarInterval) if interval is None else [interval]
        removed = 0
        with self._write_lock:
            for iv in intervals:
                path = self.bars_path(symbol, iv)
                if path.is_file():
                    shutil.rmtree(path.parent, ignore_errors=True)
                    removed += 1
        return removed

    # --- async convenience -------------------------------------------------
    async def awrite_bars(
        self, bars: list[Bar], *, interval: BarInterval = BarInterval.DAY_1
    ) -> int:
        """Await :meth:`write_bars` on a worker thread."""
        return await asyncio.to_thread(self.write_bars, bars, interval=interval)

    async def awrite_series(self, namespace: str, series: Series) -> int:
        """Await :meth:`write_series` on a worker thread."""
        return await asyncio.to_thread(self.write_series, namespace, series)

    async def aread_bars(
        self,
        symbol: str,
        *,
        interval: BarInterval = BarInterval.DAY_1,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> pd.DataFrame:
        """Await :meth:`read_bars` on a worker thread."""
        return await asyncio.to_thread(
            self.read_bars, symbol, interval=interval, start=start, end=end
        )

    async def aread_series(
        self, namespace: str, name: str, *, start: date | None = None, end: date | None = None
    ) -> Series:
        """Await :meth:`read_series` on a worker thread."""
        return await asyncio.to_thread(self.read_series, namespace, name, start=start, end=end)

    async def aquery(self, sql: str, **params: Any) -> pd.DataFrame:
        """Await :meth:`query` on a worker thread."""
        return await asyncio.to_thread(lambda: self.query(sql, **params))


_store: TimeSeriesStore | None = None


def get_store() -> TimeSeriesStore:
    """Return the process-wide store, creating it on first use."""
    global _store
    if _store is None:
        _store = TimeSeriesStore()
    return _store


def reset_store(store: TimeSeriesStore | None = None) -> TimeSeriesStore:
    """Replace the process-wide store. Intended for tests and reconfiguration."""
    global _store
    _store = store if store is not None else TimeSeriesStore()
    return _store
