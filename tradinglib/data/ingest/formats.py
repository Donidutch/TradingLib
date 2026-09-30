"""Payload parsers and schema mapping for ingested records.

Every connector produces the same thing: a list of flat-ish ``dict`` records.
This module is where a vendor payload becomes that list, and where the source's
``schema_mapping`` renames vendor fields into TradingLib's canonical names
(``symbol``, ``ts``, ``open``, ``high``, ``low``, ``close``, ``volume``,
``value``, ``name``...).

Nothing here invents values: a field the payload does not contain is simply
absent from the mapped record rather than being filled with ``None`` or zero.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from tradinglib.data.ingest.source import PayloadFormat, resolve_timezone

logger = logging.getLogger(__name__)

#: Column names treated as the record timestamp, in priority order.
TIMESTAMP_FIELDS: tuple[str, ...] = (
    "ts",
    "timestamp",
    "time",
    "datetime",
    "date",
    "observation_date",
    "published_at",
    "filed_at",
)

#: Values that mean "no observation" in the feeds TradingLib reads.
_NULL_TOKENS: frozenset[str] = frozenset({"", ".", "na", "n/a", "nan", "null", "none", "-"})

_TRUE_TOKENS: frozenset[str] = frozenset({"true", "yes"})
_FALSE_TOKENS: frozenset[str] = frozenset({"false", "no"})

_NUMBER_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")


def _text(body: bytes | str, encoding: str) -> str:
    """Decode ``body`` to text, replacing undecodable bytes rather than failing."""
    if isinstance(body, bytes):
        return body.decode(encoding, errors="replace")
    return body


def coerce_scalar(value: Any) -> Any:
    """Convert a text scalar into ``bool``/``int``/``float``/``None`` when unambiguous.

    Text formats (CSV, XML, query strings) carry no types. Values that are not
    unambiguously numeric or boolean are returned as the original string.
    """
    if not isinstance(value, str):
        return value
    text = value.strip()
    if text.lower() in _NULL_TOKENS:
        return None
    if text.lower() in _TRUE_TOKENS:
        return True
    if text.lower() in _FALSE_TOKENS:
        return False
    if _NUMBER_RE.match(text):
        try:
            if "." not in text and "e" not in text.lower():
                return int(text)
            return float(text)
        except ValueError:  # pragma: no cover - guarded by the regex
            return text
    return text


def dig(record: Any, path: str) -> Any:
    """Read a dotted path out of nested dicts/lists.

    ``"data.0.close"`` reads ``record["data"][0]["close"]``. A missing element
    yields :data:`MISSING` so callers can tell "absent" from "present but null".
    """
    node: Any = record
    for part in path.split("."):
        if isinstance(node, dict):
            if part not in node:
                return MISSING
            node = node[part]
        elif isinstance(node, (list, tuple)):
            try:
                node = node[int(part)]
            except (ValueError, IndexError):
                return MISSING
        else:
            return MISSING
    return node


class _Missing:
    """Sentinel type for "the payload does not contain this field"."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "MISSING"

    def __bool__(self) -> bool:
        return False


#: Sentinel returned by :func:`dig` when a path is absent.
MISSING = _Missing()


def parse_json(body: bytes | str, *, records_path: str | None = None) -> list[dict[str, Any]]:
    """Parse JSON, optionally descending a dotted ``records_path`` first.

    A payload that resolves to a list becomes the record list; a payload that
    resolves to an object becomes a single record.

    Raises:
        ValueError: when the body is not JSON or ``records_path`` is absent.
    """
    text = _text(body, "utf-8")
    try:
        data: Any = json.loads(text) if text.strip() else []
    except json.JSONDecodeError as exc:
        raise ValueError(f"payload is not valid JSON: {exc.msg} (line {exc.lineno})") from exc
    if records_path:
        data = dig(data, records_path)
        if data is MISSING:
            raise ValueError(f"records_path {records_path!r} is not present in the payload")
    if data is None:
        return []
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [row if isinstance(row, dict) else {"value": row} for row in data]
    return [{"value": data}]


def parse_ndjson(body: bytes | str) -> list[dict[str, Any]]:
    """Parse newline-delimited JSON, one record per non-empty line.

    Raises:
        ValueError: when a line is not valid JSON.
    """
    out: list[dict[str, Any]] = []
    for number, line in enumerate(_text(body, "utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            row = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(f"line {number} is not valid JSON: {exc.msg}") from exc
        out.append(row if isinstance(row, dict) else {"value": row})
    return out


def parse_csv(
    body: bytes | str, *, delimiter: str = ",", header: int | None = 0
) -> list[dict[str, Any]]:
    """Parse delimited text into records.

    Args:
        body: the raw CSV document.
        delimiter: field separator.
        header: row index of the header, or ``None`` for a headerless file
            (fields are then named ``col_0``, ``col_1``, ...).
    """
    text = _text(body, "utf-8")
    if not text.strip():
        return []
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = [row for row in reader if row]
    if not rows:
        return []
    if header is None:
        names = [f"col_{i}" for i in range(max(len(row) for row in rows))]
        data_rows = rows
    else:
        if header >= len(rows):
            raise ValueError(f"header row {header} is beyond the end of the document")
        names = [name.strip() for name in rows[header]]
        data_rows = rows[header + 1 :]
    out: list[dict[str, Any]] = []
    for row in data_rows:
        record: dict[str, Any] = {}
        for index, name in enumerate(names):
            key = name or f"col_{index}"
            record[key] = coerce_scalar(row[index]) if index < len(row) else None
        out.append(record)
    return out


def parse_xml(body: bytes | str) -> list[dict[str, Any]]:
    """Parse XML into one record per child of the document root.

    Each record carries the child's attributes plus the text of its own leaf
    children. Deeper nesting is not flattened - use a JSON feed for that.

    Raises:
        ValueError: when the document cannot be parsed.
    """
    from xml.etree import ElementTree

    raw = body.encode() if isinstance(body, str) else body
    try:
        root = ElementTree.fromstring(raw)  # noqa: S314 - local, non-adversarial feeds
    except ElementTree.ParseError as exc:
        raise ValueError(f"payload is not valid XML: {exc}") from exc
    out: list[dict[str, Any]] = []
    for child in root:
        record: dict[str, Any] = {k: coerce_scalar(v) for k, v in child.attrib.items()}
        for leaf in child:
            if len(leaf) == 0:
                record[leaf.tag] = coerce_scalar(leaf.text or "")
        if not record and child.text and child.text.strip():
            record = {child.tag: coerce_scalar(child.text)}
        out.append(record)
    return out


def parse_parquet(body: bytes) -> list[dict[str, Any]]:
    """Parse a Parquet document into records.

    Raises:
        ValueError: when the bytes are not a readable Parquet file.
    """
    import pyarrow.parquet as pq

    try:
        table = pq.read_table(io.BytesIO(body))
    except Exception as exc:  # noqa: BLE001 - pyarrow raises several types
        raise ValueError(f"payload is not readable Parquet: {exc}") from exc
    return [dict(row) for row in table.to_pylist()]


def parse_arrow(body: bytes) -> list[dict[str, Any]]:
    """Parse an Arrow IPC stream/file into records.

    Raises:
        ValueError: when the bytes are not a readable Arrow document.
    """
    import pyarrow as pa

    buffer = pa.py_buffer(body)
    try:
        with pa.ipc.open_stream(buffer) as reader:
            table = reader.read_all()
    except Exception:  # noqa: BLE001 - fall back to the file format
        try:
            with pa.ipc.open_file(buffer) as reader:
                table = reader.read_all()
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"payload is not a readable Arrow document: {exc}") from exc
    return [dict(row) for row in table.to_pylist()]


def parse_text(body: bytes | str) -> list[dict[str, Any]]:
    """Parse plain text into one ``{"line", "text"}`` record per non-empty line."""
    return [
        {"line": number, "text": line}
        for number, line in enumerate(_text(body, "utf-8").splitlines(), start=1)
        if line.strip()
    ]


def parse_payload(
    body: bytes | str, fmt: PayloadFormat, *, encoding: str = "utf-8", **options: Any
) -> list[dict[str, Any]]:
    """Parse a raw payload into a list of records.

    Args:
        body: raw response body.
        fmt: declared payload format.
        encoding: text encoding for byte payloads.
        options: format-specific options (``records_path`` for JSON,
            ``delimiter``/``header`` for CSV).

    Raises:
        ValueError: when the payload cannot be parsed as ``fmt``.
    """
    if fmt is PayloadFormat.JSON:
        return parse_json(
            _text(body, encoding), records_path=options.get("records_path")
        )
    if fmt is PayloadFormat.NDJSON:
        return parse_ndjson(_text(body, encoding))
    if fmt is PayloadFormat.CSV:
        return parse_csv(
            _text(body, encoding),
            delimiter=str(options.get("delimiter", ",")),
            header=options.get("header", 0),
        )
    if fmt is PayloadFormat.XML:
        return parse_xml(body)
    if fmt is PayloadFormat.TEXT:
        return parse_text(_text(body, encoding))
    if fmt is PayloadFormat.PARQUET:
        return parse_parquet(body if isinstance(body, bytes) else body.encode(encoding))
    if fmt is PayloadFormat.ARROW:
        return parse_arrow(body if isinstance(body, bytes) else body.encode(encoding))
    if fmt is PayloadFormat.ROWS:
        # Database rows arrive already decoded; a raw body cannot be one.
        raise ValueError(
            "the 'Database rows' format has no wire representation; "
            "it is produced by the database connector"
        )
    raise ValueError(f"unsupported payload format: {fmt}")  # pragma: no cover


def apply_schema_mapping(
    records: list[dict[str, Any]], mapping: dict[str, str]
) -> list[dict[str, Any]]:
    """Rename/select source fields into TradingLib's canonical field names.

    ``mapping`` maps canonical name -> source field path (dotted paths allowed).
    An empty mapping returns the records unchanged. A source path that is absent
    from a record leaves the canonical field out of the mapped record - it is
    never filled with a placeholder.
    """
    if not mapping:
        return records
    out: list[dict[str, Any]] = []
    for record in records:
        mapped: dict[str, Any] = {}
        for canonical, path in mapping.items():
            value = dig(record, path)
            if value is not MISSING:
                mapped[canonical] = value
        out.append(mapped)
    return out


def parse_timestamp(value: Any, *, tz: ZoneInfo | None = None) -> datetime | None:
    """Parse a record timestamp into an aware UTC datetime, or ``None``.

    Accepts ISO-8601 text (with or without offset), ``datetime``/``date``
    objects, and epoch numbers (seconds, milliseconds, microseconds or
    nanoseconds, chosen by magnitude). Naive values are interpreted in ``tz``,
    or as UTC when the source has no real timezone.
    """
    if value is None or value is MISSING:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime(value.year, value.month, value.day)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value)
        magnitude = abs(seconds)
        if magnitude >= 1e17:
            seconds /= 1e9
        elif magnitude >= 1e14:
            seconds /= 1e6
        elif magnitude >= 1e11:
            seconds /= 1e3
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    else:
        text = str(value).strip()
        if not text or text.lower() in _NULL_TOKENS:
            return None
        candidate = text.replace("Z", "+00:00") if text.endswith("Z") else text
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            try:
                stamp = pd.Timestamp(text)
            except (ValueError, TypeError):
                return None
            if stamp is pd.NaT:  # pragma: no cover - defensive
                return None
            parsed = stamp.to_pydatetime()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz or timezone.utc)
    return parsed.astimezone(timezone.utc)


def timestamp_field(record: dict[str, Any]) -> str | None:
    """Return the record key holding its timestamp, or ``None`` when there is none."""
    lowered = {key.lower(): key for key in record}
    for candidate in TIMESTAMP_FIELDS:
        if candidate in lowered:
            return lowered[candidate]
    return None


def records_to_frame(records: list[dict[str, Any]], *, tz: str = "UTC") -> pd.DataFrame:
    """Convert mapped records into a dataframe with a UTC timestamp index.

    When no record carries a recognisable timestamp field the frame is returned
    with its default range index - an ingested payload without time is still
    data, and inventing a timestamp would corrupt it.
    """
    if not records:
        return pd.DataFrame()
    frame = pd.DataFrame(records)
    zone = resolve_timezone(tz)
    field = timestamp_field(records[0])
    if field is None:
        return frame
    stamps = [parse_timestamp(value, tz=zone) for value in frame[field].tolist()]
    if all(stamp is None for stamp in stamps):
        return frame
    frame = frame.drop(columns=[field])
    frame.index = pd.DatetimeIndex(pd.Series(stamps, dtype="object"), name="ts")
    return frame.sort_index()


def infer_schema(records: list[dict[str, Any]], *, sample: int = 50) -> dict[str, str]:
    """Infer a field -> dtype description, used to preview a test connection.

    Types seen across the sample are unified: a field that is sometimes an int
    and sometimes a float is ``number``; genuinely mixed fields are ``mixed``;
    a field that is always null is ``null``.
    """
    seen: dict[str, set[str]] = {}
    for record in records[: max(1, sample)]:
        for key, value in record.items():
            seen.setdefault(str(key), set()).add(_type_name(value))
    schema: dict[str, str] = {}
    for key, kinds in seen.items():
        concrete = {k for k in kinds if k != "null"}
        if not concrete:
            schema[key] = "null"
        elif len(concrete) == 1:
            schema[key] = concrete.pop()
        elif concrete <= {"integer", "number"}:
            schema[key] = "number"
        else:
            schema[key] = "mixed"
    return schema


def _type_name(value: Any) -> str:
    """Return the schema type name of one value."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, (datetime, date)):
        return "timestamp"
    if isinstance(value, (list, tuple)):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "string"


def json_safe(value: Any) -> Any:
    """Return a JSON-serialisable copy of a record value.

    Vendor payloads carry ``datetime``, ``Decimal``, ``bytes`` and numpy scalars;
    every one of those has to survive the trip to the browser.
    """
    import decimal

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if value == value and value not in (float("inf"), float("-inf")) else None
    if isinstance(value, datetime):
        stamp = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return stamp.astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(v) for v in value]
    if hasattr(value, "item") and hasattr(value, "dtype"):  # numpy scalar
        try:
            return json_safe(value.item())
        except Exception:  # noqa: BLE001 - fall through to str()
            return str(value)
    if value is pd.NaT:
        return None
    return str(value)


def json_safe_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Apply :func:`json_safe` to every value of every record."""
    return [{str(k): json_safe(v) for k, v in record.items()} for record in records]


__all__ = [
    "MISSING",
    "TIMESTAMP_FIELDS",
    "apply_schema_mapping",
    "coerce_scalar",
    "dig",
    "infer_schema",
    "json_safe",
    "json_safe_records",
    "parse_arrow",
    "parse_csv",
    "parse_json",
    "parse_ndjson",
    "parse_parquet",
    "parse_payload",
    "parse_text",
    "parse_timestamp",
    "parse_xml",
    "records_to_frame",
    "timestamp_field",
]
