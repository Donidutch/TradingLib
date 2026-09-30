"""Ingestion-layer tests.

Every connector is exercised against a *local* fixture - an httpx
``MockTransport`` for HTTP, a real ``websockets`` server on localhost, a
temporary SQLite file and temporary CSV files on disk - so the suite proves the
real code paths without depending on a vendor being up.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from tradinglib.config import Settings
from tradinglib.data.base import BarInterval, ProviderStatus, Quote
from tradinglib.data.ingest.auth import ApiKeyAuth, AuthError, BasicAuth, OAuthAuth, build_auth
from tradinglib.data.ingest.catalog import (
    SourceCatalog,
    SourceState,
    Subscription,
    bar_interval_for,
    builtin_sources,
    granularity_options,
    granularity_reaches_fetch,
    parse_granularity,
    status_from_test,
)
from tradinglib.data.ingest.connectors.base import (
    ConnectionTestResult,
    Connector,
    FetchResult,
    build_connector,
)
from tradinglib.data.ingest.connectors.database import DatabaseConnector
from tradinglib.data.ingest.connectors.file import FileConnector
from tradinglib.data.ingest.connectors.library import LibraryConnector, map_provider_status
from tradinglib.data.ingest.connectors.polling import PollingConnector
from tradinglib.data.ingest.connectors.rest import RestConnector
from tradinglib.data.ingest.connectors.websocket import WebSocketConnector
from tradinglib.data.ingest.formats import (
    apply_schema_mapping,
    infer_schema,
    parse_csv,
    parse_json,
    parse_ndjson,
    parse_payload,
    parse_timestamp,
    parse_xml,
    records_to_frame,
)
from tradinglib.data.ingest.gateway import IngestGateway
from tradinglib.data.ingest.scheduler import (
    IngestScheduler,
    classify_records,
    records_to_bars,
    records_to_series,
)
from tradinglib.data.ingest.source import (
    AdvancedConfig,
    AuthConfig,
    AuthType,
    BackfillConfig,
    DataDomain,
    PayloadFormat,
    Protocol,
    RetryMode,
    RetryPolicy,
    ScheduleKind,
    SourceConfig,
    SourceStatus,
    parse_schedule,
)
from tradinglib.data.store import TimeSeriesStore
from tradinglib.events import EventBus

UTC = timezone.utc

#: Ids of the pre-registered provider sources, derived from the catalog itself
#: so adding a built-in vendor does not mean editing a count in five tests.
BUILTIN_SOURCE_IDS = {source.id for source in builtin_sources()}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def make_config(**overrides: Any) -> SourceConfig:
    """Build a REST source config with sensible defaults for tests."""
    payload: dict[str, Any] = {
        "id": "test-source",
        "name": "Test source",
        "data_domain": DataDomain.MARKET,
        "protocol": Protocol.REST,
        "endpoint": "https://vendor.test/v1/bars",
        "format": PayloadFormat.JSON,
        "schedule": "5m",
    }
    payload.update(overrides)
    return SourceConfig(**payload)


class StubConnector(Connector):
    """A connector that returns canned records, for scheduler/gateway tests."""

    protocol = "stub"

    def __init__(
        self,
        config: SourceConfig,
        *,
        records: list[dict[str, Any]] | None = None,
        error: Exception | None = None,
        test_result: ConnectionTestResult | None = None,
    ) -> None:
        """Create the stub."""
        super().__init__(config)
        self.records = records or []
        self.error = error
        self.test_result = test_result
        self.closed = False
        self.fetches = 0

    async def connect(self) -> None:
        """No resources to open."""
        return None

    async def close(self) -> None:
        """Record that the caller closed the connector."""
        self.closed = True

    async def test(self) -> ConnectionTestResult:
        """Return the canned test result."""
        if self.test_result is not None:
            return self.test_result
        return ConnectionTestResult.success("stub ok", latency_ms=1.5, records=self.records)

    async def fetch(self, **params: Any) -> FetchResult:
        """Return the canned records, or raise the canned error."""
        self.fetches += 1
        if self.error is not None:
            raise self.error
        return FetchResult(
            records=list(self.records),
            fetched_at=datetime.now(UTC),
            latency_ms=2.0,
            dataset="stub",
        )

    async def backfill(
        self, *, range: str = "1y", batch_size: int = 500, end: datetime | None = None
    ) -> AsyncIterator[FetchResult]:
        """Yield the canned records as a single batch."""
        yield FetchResult(records=list(self.records), fetched_at=datetime.now(UTC))

    async def status(self) -> SourceStatus:
        """Report the stub as connected."""
        return SourceStatus.CONNECTED


@pytest.fixture()
def catalog(settings: Settings, bus: EventBus) -> SourceCatalog:
    """A catalog backed by the temporary per-test database."""
    return SourceCatalog(bus)


# ---------------------------------------------------------------------------
# SourceConfig / form mapping
# ---------------------------------------------------------------------------
def test_from_form_maps_every_ui_control() -> None:
    """The REFERENCE form payload maps 1:1 onto SourceConfig fields."""
    config = SourceConfig.from_form(
        {
            "sourceName": "Alpaca live bars",
            "sourceDomain": "Prices & quotes",
            "sourceProtocol": "WebSocket",
            "sourceEndpoint": "wss://vendor.test/v2/stream",
            "sourceAuth": "API key",
            "auth_key_name": "X-Key",
            "auth_key_value": "secret-value",
            "sourceFormat": "NDJSON",
            "sourceFrequency": "Streaming",
            "sourceScope": "Custom symbols…",
            "scope": "AAPL, MSFT",
            "sourceTimezone": "America/New_York",
            "sourceRetries": "Fixed 30 seconds",
            "backfillEnabled": True,
            "backfillRange": "Last 10 years",
            "backfillBatch": "10,000 rows",
            "advancedConfig": json.dumps(
                {"subscriptions": ["trades.AAPL"], "headers": {"X-Trace": "1"}}
            ),
        }
    )
    assert config.name == "Alpaca live bars"
    assert config.data_domain is DataDomain.MARKET
    assert config.protocol is Protocol.WEBSOCKET
    assert config.endpoint == "wss://vendor.test/v2/stream"
    assert config.auth.type is AuthType.API_KEY
    assert config.auth.key_value == "secret-value"
    assert config.format is PayloadFormat.NDJSON
    assert config.parsed_schedule.kind is ScheduleKind.STREAMING
    assert config.scope == ["AAPL", "MSFT"]
    assert config.source_tz == "America/New_York"
    assert config.retry_policy.mode is RetryMode.FIXED_30S
    assert config.retry_policy.backoff_seconds == 30.0
    assert config.backfill.enabled and config.backfill.range == "10y"
    assert config.backfill.batch_size == 10_000
    assert config.advanced.subscriptions == ["trades.AAPL"]
    assert config.advanced.headers == {"X-Trace": "1"}


def test_to_dict_masks_secrets() -> None:
    """Serialising a source never leaks a credential."""
    config = make_config(
        auth=AuthConfig(type=AuthType.API_KEY, key_name="X-Key", key_value="supersecret")
    )
    masked = config.to_dict()
    assert masked["auth"]["key_value"] == "*******cret"
    assert masked["auth"]["key_set"] is True
    assert "supersecret" not in json.dumps(masked)
    assert config.to_dict(mask_secrets=False)["auth"]["key_value"] == "supersecret"


def test_from_form_rejects_inconsistent_protocol_fields() -> None:
    """A protocol whose required field is missing is refused, not defaulted."""
    with pytest.raises(ValueError, match="ws://"):
        SourceConfig.from_form(
            {
                "sourceName": "Bad stream",
                "sourceProtocol": "WebSocket",
                "sourceEndpoint": "https://vendor.test",
            }
        )
    with pytest.raises(ValueError, match="sql"):
        SourceConfig.from_form(
            {
                "sourceName": "Bad db",
                "sourceProtocol": "Database",
                "sourceEndpoint": "sqlite:///tmp.db",
            }
        )
    with pytest.raises(ValueError, match="name is required"):
        SourceConfig.from_form({"sourceProtocol": "REST", "sourceEndpoint": "https://x.test"})


@pytest.mark.parametrize(
    ("value", "kind", "seconds"),
    [
        ("Streaming", ScheduleKind.STREAMING, None),
        ("Every second", ScheduleKind.INTERVAL, 1.0),
        ("Every 5 minutes", ScheduleKind.INTERVAL, 300.0),
        ("Hourly", ScheduleKind.INTERVAL, 3600.0),
        ("Daily", ScheduleKind.INTERVAL, 86_400.0),
        ("15s", ScheduleKind.INTERVAL, 15.0),
        ("On file change", ScheduleKind.ON_CHANGE, None),
        ("manual", ScheduleKind.MANUAL, None),
    ],
)
def test_parse_schedule(value: str, kind: ScheduleKind, seconds: float | None) -> None:
    """Every frequency the form offers parses into a runnable schedule."""
    schedule = parse_schedule(value)
    assert schedule.kind is kind
    assert schedule.interval_seconds == seconds


def test_parse_schedule_cron_and_errors() -> None:
    """Cron schedules are validated and unknown text is refused."""
    schedule = parse_schedule("cron:*/5 * * * *")
    assert schedule.kind is ScheduleKind.CRON
    first = schedule.next_run_after(None, now=datetime(2026, 1, 1, 0, 1, tzinfo=UTC))
    assert first == datetime(2026, 1, 1, 0, 5, tzinfo=UTC)
    with pytest.raises(ValueError):
        parse_schedule("cron:not a cron")
    with pytest.raises(ValueError):
        parse_schedule("every blue moon")


def test_backfill_range_start() -> None:
    """Backfill ranges resolve to real start timestamps (or 'everything')."""
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert BackfillConfig(range="all").range_start(now=now) is None
    assert BackfillConfig(range="1y").range_start(now=now) == now - timedelta(days=365)
    custom = BackfillConfig(range="custom", start_date="2020-03-01")
    assert custom.range_start(now=now) == datetime(2020, 3, 1, tzinfo=UTC)
    with pytest.raises(ValueError):
        BackfillConfig.from_form(
            enabled=True, range_value="Custom start date…", batch_value="Automatic"
        )


def test_retry_policy_backoff() -> None:
    """Exponential backoff grows and is capped."""
    policy = RetryPolicy.from_mode(RetryMode.EXPONENTIAL)
    assert policy.delay_for(1) == 2.0
    assert policy.delay_for(2) == 4.0
    assert policy.delay_for(20) == policy.max_backoff_seconds
    assert RetryPolicy.from_mode(RetryMode.IMMEDIATE_5).delay_for(1) == 0.0


# ---------------------------------------------------------------------------
# formats
# ---------------------------------------------------------------------------
def test_parse_json_records_path() -> None:
    """A dotted records path descends into the payload."""
    body = json.dumps({"data": {"rows": [{"a": 1}, {"a": 2}]}})
    assert parse_json(body, records_path="data.rows") == [{"a": 1}, {"a": 2}]
    with pytest.raises(ValueError, match="not present"):
        parse_json(body, records_path="data.missing")
    with pytest.raises(ValueError, match="not valid JSON"):
        parse_json("{oops")


def test_parse_csv_and_ndjson_and_xml() -> None:
    """The text formats parse into typed records."""
    rows = parse_csv("symbol,close\nAAPL,1.5\nMSFT,.\n")
    assert rows == [{"symbol": "AAPL", "close": 1.5}, {"symbol": "MSFT", "close": None}]
    assert parse_ndjson('{"a":1}\n\n{"a":2}\n') == [{"a": 1}, {"a": 2}]
    xml = "<rows><row symbol='AAPL'><close>2.5</close></row></rows>"
    assert parse_xml(xml) == [{"symbol": "AAPL", "close": 2.5}]


def test_apply_schema_mapping_omits_absent_fields() -> None:
    """A source field that is absent leaves the canonical field out."""
    records = [{"t": "2026-01-01", "c": 10, "nested": {"v": 3}}, {"t": "2026-01-02"}]
    mapped = apply_schema_mapping(
        records, {"ts": "t", "close": "c", "extra": "nested.v", "volume": "vol"}
    )
    assert mapped[0] == {"ts": "2026-01-01", "close": 10, "extra": 3}
    assert mapped[1] == {"ts": "2026-01-02"}
    assert "volume" not in mapped[0]


def test_infer_schema_unifies_types() -> None:
    """Mixed int/float is a number; genuinely mixed types are reported as mixed."""
    schema = infer_schema([{"a": 1, "b": "x", "c": None}, {"a": 1.5, "b": 2, "c": None}])
    assert schema == {"a": "number", "b": "mixed", "c": "null"}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-01-02T03:04:05Z", datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)),
        ("2026-01-02", datetime(2026, 1, 2, tzinfo=UTC)),
        (1767322800, datetime.fromtimestamp(1767322800, tz=UTC)),
        (1767322800000, datetime.fromtimestamp(1767322800, tz=UTC)),
        ("", None),
        (None, None),
    ],
)
def test_parse_timestamp(value: Any, expected: datetime | None) -> None:
    """Timestamps parse from ISO text and epoch numbers of any resolution."""
    assert parse_timestamp(value) == expected


def test_records_to_frame_indexes_on_utc() -> None:
    """A record timestamp becomes a UTC index; a naive one uses the source zone."""
    frame = records_to_frame(
        [{"ts": "2026-01-02 09:30:00", "value": 1.0}], tz="America/New_York"
    )
    assert list(frame.columns) == ["value"]
    assert frame.index[0] == datetime(2026, 1, 2, 14, 30, tzinfo=UTC)


def test_parse_binary_formats_round_trip() -> None:
    """Parquet and Arrow payloads parse back into the records they were built from."""
    import io

    import pyarrow as pa
    import pyarrow.parquet as pq

    table = pa.table({"symbol": ["AAPL", "MSFT"], "close": [1.5, 2.5]})
    parquet_buffer = io.BytesIO()
    pq.write_table(table, parquet_buffer)
    assert parse_payload(parquet_buffer.getvalue(), PayloadFormat.PARQUET) == [
        {"symbol": "AAPL", "close": 1.5},
        {"symbol": "MSFT", "close": 2.5},
    ]

    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    assert parse_payload(sink.getvalue().to_pybytes(), PayloadFormat.ARROW)[0] == {
        "symbol": "AAPL",
        "close": 1.5,
    }
    with pytest.raises(ValueError, match="readable Parquet"):
        parse_payload(b"not parquet", PayloadFormat.PARQUET)


def test_parse_payload_rejects_rows_format() -> None:
    """'Database rows' has no wire representation and says so."""
    with pytest.raises(ValueError, match="no wire representation"):
        parse_payload(b"", PayloadFormat.ROWS)


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------
async def test_api_key_auth_header_and_query() -> None:
    """The API key lands in the header or the query string, as configured."""
    request: dict[str, Any] = {"url": "https://x.test", "headers": {}, "params": {}}
    await ApiKeyAuth("X-Key", "abc").apply(request)
    assert request["headers"]["X-Key"] == "abc"
    request = {"url": "https://x.test", "headers": {}, "params": {}}
    await ApiKeyAuth("token", "abc", in_header=False).apply(request)
    assert request["params"]["token"] == "abc"
    assert ApiKeyAuth("X", "abcdef").describe()["key_value"] == "**cdef"


async def test_basic_auth_header() -> None:
    """Basic auth produces a real base64 Authorization header."""
    request: dict[str, Any] = {"url": "https://x.test", "headers": {}, "params": {}}
    await BasicAuth("user", "pass").apply(request)
    assert request["headers"]["Authorization"] == "Basic dXNlcjpwYXNz"


async def test_oauth_client_credentials_flow() -> None:
    """OAuth fetches a token once and reuses it until it expires."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert b"client_credentials" in request.content
        return httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})

    strategy = OAuthAuth(
        "https://auth.test/token",
        "cid",
        "csecret",
        scopes=["read"],
        transport=httpx.MockTransport(handler),
    )
    request: dict[str, Any] = {"url": "https://x.test", "headers": {}, "params": {}}
    await strategy.apply(request)
    await strategy.apply(request)
    assert request["headers"]["Authorization"] == "Bearer tok-1"
    assert calls == 1


async def test_oauth_reports_rejection() -> None:
    """A rejected token request raises instead of silently going unauthenticated."""
    strategy = OAuthAuth(
        "https://auth.test/token",
        "cid",
        "bad",
        transport=httpx.MockTransport(lambda r: httpx.Response(401, text="nope")),
    )
    with pytest.raises(AuthError, match="401"):
        await strategy.refresh()


def test_build_auth_requires_complete_credentials() -> None:
    """An incomplete credential set is an error, not a silent anonymous request."""
    with pytest.raises(AuthError):
        build_auth(AuthConfig(type=AuthType.API_KEY, key_name="X-Key"))
    with pytest.raises(AuthError):
        build_auth(AuthConfig(type=AuthType.OAUTH, token_url="https://t.test"))
    assert build_auth(AuthConfig()).type == "none"


# ---------------------------------------------------------------------------
# REST connector (httpx MockTransport fixture)
# ---------------------------------------------------------------------------
async def test_rest_connector_test_returns_real_sample() -> None:
    """A successful test carries the status code, latency and a real record."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("X-Key")
        return httpx.Response(200, json={"rows": [{"symbol": "AAPL", "close": 1.0}]})

    config = make_config(
        auth=AuthConfig(type=AuthType.API_KEY, key_name="X-Key", key_value="abc"),
        advanced=AdvancedConfig(extra={"records_path": "rows"}),
    )
    connector = RestConnector(config, transport=httpx.MockTransport(handler))
    result = await connector.test()
    await connector.close()

    assert result.ok is True
    assert result.reachable is True
    assert result.authenticated is True
    assert result.status_code == 200
    assert result.sample_record == {"symbol": "AAPL", "close": 1.0}
    assert result.inferred_schema == {"symbol": "string", "close": "number"}
    assert result.latency_ms is not None and result.latency_ms >= 0
    assert seen["key"] == "abc"


async def test_rest_connector_reports_auth_rejection() -> None:
    """HTTP 401 is reported as reachable-but-unauthenticated."""
    connector = RestConnector(
        make_config(auth=AuthConfig(type=AuthType.API_KEY, key_name="X", key_value="bad")),
        transport=httpx.MockTransport(lambda r: httpx.Response(401, text="denied")),
    )
    result = await connector.test()
    await connector.close()
    assert result.ok is False
    assert result.reachable is True
    assert result.authenticated is False
    assert result.status_code == 401
    assert status_from_test(result) is SourceStatus.DEGRADED


async def test_rest_connector_reports_unreachable_endpoint() -> None:
    """A transport failure is reported as unreachable, with the real reason."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    connector = RestConnector(
        make_config(retry_policy=RetryPolicy.from_mode(RetryMode.IMMEDIATE_5)),
        transport=httpx.MockTransport(handler),
    )
    result = await connector.test()
    await connector.close()
    assert result.ok is False
    assert result.reachable is False
    assert "connection refused" in (result.error or "")
    assert status_from_test(result) is SourceStatus.DISCONNECTED


async def test_rest_connector_retries_server_errors() -> None:
    """A 500 is retried according to the retry policy, then succeeds."""
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json=[{"a": 1}])

    config = make_config(
        retry_policy=RetryPolicy(
            mode=RetryMode.IMMEDIATE_5, max_attempts=3, backoff_seconds=0.0
        )
    )
    connector = RestConnector(config, transport=httpx.MockTransport(handler))
    batch = await connector.fetch()
    await connector.close()
    assert attempts == 3
    assert batch.records == [{"a": 1}]


async def test_rest_connector_paginates() -> None:
    """Page-style pagination walks until a short page ends it."""
    pages = {
        1: [{"i": 1}, {"i": 2}],
        2: [{"i": 3}, {"i": 4}],
        3: [{"i": 5}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", 1))
        assert request.url.params.get("limit") == "2"
        return httpx.Response(200, json=pages[page])

    config = make_config(
        advanced=AdvancedConfig(pagination={"style": "page", "param": "page", "size": 2})
    )
    connector = RestConnector(config, transport=httpx.MockTransport(handler))
    batch = await connector.fetch()
    await connector.close()
    assert [row["i"] for row in batch.records] == [1, 2, 3, 4, 5]
    assert batch.complete is True


async def test_rest_connector_backfill_walks_windows() -> None:
    """Backfill steps through history using the configured window parameters."""
    windows: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        windows.append((params["from"], params["to"]))
        return httpx.Response(200, json=[{"ts": params["from"], "v": 1}])

    config = make_config(
        backfill=BackfillConfig(enabled=True, range="1y", batch="1mo"),
        advanced=AdvancedConfig(
            extra={"window_params": {"start": "from", "end": "to", "format": "date"}}
        ),
    )
    connector = RestConnector(config, transport=httpx.MockTransport(handler))
    batches = [batch async for batch in connector.backfill(range="1y")]
    await connector.close()
    assert len(windows) >= 12
    assert len(batches) == len(windows)
    assert all(start < end for start, end in windows)


async def test_rest_connector_reports_bad_payload() -> None:
    """A body that is not the declared format fails honestly."""
    connector = RestConnector(
        make_config(),
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text="not json")),
    )
    result = await connector.test()
    await connector.close()
    assert result.ok is False
    assert "did not parse" in result.message


# ---------------------------------------------------------------------------
# polling connector
# ---------------------------------------------------------------------------
async def test_polling_connector_emits_only_new_records() -> None:
    """Change detection keeps a watermark and emits only newer rows."""
    batches = [
        [{"ts": "2026-01-01T00:00:00Z", "v": 1}, {"ts": "2026-01-01T00:01:00Z", "v": 2}],
        [{"ts": "2026-01-01T00:01:00Z", "v": 2}, {"ts": "2026-01-01T00:02:00Z", "v": 3}],
    ]
    index = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal index
        payload = batches[min(index, len(batches) - 1)]
        index += 1
        return httpx.Response(200, json=payload)

    config = make_config(protocol=Protocol.POLLING, schedule="1m")
    connector = PollingConnector(config, transport=httpx.MockTransport(handler))
    first = await connector.fetch()
    second = await connector.fetch()
    third = await connector.fetch()
    await connector.close()

    assert len(first.records) == 2
    assert [row["v"] for row in second.records] == [3]
    assert third.records == []
    assert connector.poll_seconds == 60.0


async def test_polling_connector_fingerprint_mode() -> None:
    """Without timestamps the connector dedupes on record content."""
    payload = [{"a": 1}, {"a": 2}]
    connector = PollingConnector(
        make_config(protocol=Protocol.POLLING, advanced=AdvancedConfig(extra={"dedupe": "fingerprint"})),
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload)),
    )
    assert len((await connector.fetch()).records) == 2
    assert (await connector.fetch()).records == []
    await connector.close()


# ---------------------------------------------------------------------------
# file connector
# ---------------------------------------------------------------------------
@pytest.fixture()
def csv_source(tmp_path: Path) -> tuple[SourceConfig, Path]:
    """A temporary CSV drop directory with one file in it."""
    drop = tmp_path / "drop"
    drop.mkdir()
    (drop / "day1.csv").write_text("symbol,ts,close\nAAPL,2026-01-02,10.5\nAAPL,2026-01-03,11.0\n")
    config = make_config(
        id="csv-drop",
        name="CSV drop",
        protocol=Protocol.FILE,
        endpoint=str(drop / "*.csv"),
        format=PayloadFormat.CSV,
        schedule="on_change",
    )
    return config, drop


async def test_file_connector_test_and_ledger(csv_source: tuple[SourceConfig, Path], tmp_path: Path) -> None:
    """Files are parsed once; an unchanged file is not ingested twice."""
    config, drop = csv_source
    ledger = tmp_path / "ledger.json"
    connector = FileConnector(config, ledger_path=ledger)

    result = await connector.test()
    assert result.ok is True
    assert result.sample_record == {"symbol": "AAPL", "ts": "2026-01-02", "close": 10.5}

    first = await connector.fetch()
    assert len(first.records) == 2
    second = await connector.fetch()
    assert second.records == []

    (drop / "day2.csv").write_text("symbol,ts,close\nMSFT,2026-01-02,20.0\n")
    third = await connector.fetch()
    assert [row["symbol"] for row in third.records] == ["MSFT"]

    await connector.close()
    assert ledger.is_file()
    assert len(json.loads(ledger.read_text())) == 2


async def test_file_connector_reports_missing_pattern(tmp_path: Path) -> None:
    """A pattern that matches nothing is reported, not silently empty."""
    config = make_config(
        protocol=Protocol.FILE,
        endpoint=str(tmp_path / "nothing" / "*.csv"),
        format=PayloadFormat.CSV,
    )
    connector = FileConnector(config, ledger_path=tmp_path / "l.json")
    result = await connector.test()
    assert result.ok is False
    assert "no files match" in result.message
    assert await connector.status() is SourceStatus.DEGRADED


async def test_file_connector_backfill_rereads_everything(
    csv_source: tuple[SourceConfig, Path], tmp_path: Path
) -> None:
    """Backfill re-reads matching files regardless of the ledger."""
    config, _drop = csv_source
    connector = FileConnector(config, ledger_path=tmp_path / "l2.json")
    await connector.fetch()
    rows = 0
    async for batch in connector.backfill(range="all", batch_size=1):
        rows += len(batch.records)
    await connector.close()
    assert rows == 2


# ---------------------------------------------------------------------------
# database connector
# ---------------------------------------------------------------------------
@pytest.fixture()
def sqlite_source(tmp_path: Path) -> SourceConfig:
    """A temporary SQLite database with a small prices table."""
    path = tmp_path / "market.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE prices (symbol TEXT, ts TEXT, close REAL)")
    connection.executemany(
        "INSERT INTO prices VALUES (?, ?, ?)",
        [
            ("AAPL", "2026-01-02T00:00:00Z", 10.0),
            ("AAPL", "2026-01-03T00:00:00Z", 11.0),
            ("MSFT", "2026-01-02T00:00:00Z", 20.0),
        ],
    )
    connection.commit()
    connection.close()
    return make_config(
        id="market-db",
        name="Market DuckDB",
        protocol=Protocol.DATABASE,
        endpoint=f"sqlite:///{path}",
        format=PayloadFormat.ROWS,
        schedule="1h",
        advanced=AdvancedConfig(
            sql="SELECT symbol, ts, close FROM prices", extra={"watermark_column": "ts"}
        ),
    )


async def test_database_connector_test_and_fetch(sqlite_source: SourceConfig) -> None:
    """The configured SQL really runs and its rows come back as records."""
    connector = DatabaseConnector(sqlite_source)
    result = await connector.test()
    assert result.ok is True
    assert result.sample_record is not None
    assert result.sample_record["symbol"] == "AAPL"

    batch = await connector.fetch()
    assert len(batch.records) == 3
    assert await connector.status() is SourceStatus.CONNECTED
    await connector.close()


async def test_database_connector_backfill_keyset(sqlite_source: SourceConfig) -> None:
    """Backfill pages through history on the watermark column."""
    connector = DatabaseConnector(sqlite_source)
    batches = [b async for b in connector.backfill(range="all", batch_size=2)]
    await connector.close()
    total = sum(len(b.records) for b in batches)
    assert total == 3
    assert batches[-1].complete is True


async def test_database_connector_reports_bad_query(sqlite_source: SourceConfig) -> None:
    """A broken query fails with the database's own message."""
    sqlite_source.advanced.sql = "SELECT * FROM does_not_exist"
    connector = DatabaseConnector(sqlite_source)
    result = await connector.test()
    await connector.close()
    assert result.ok is False
    assert "does_not_exist" in (result.error or "")


async def test_database_connector_requires_sql() -> None:
    """A database source without SQL says so instead of guessing a table."""
    config = SourceConfig(
        id="no-sql",
        name="No SQL",
        data_domain=DataDomain.MARKET,
        protocol=Protocol.DATABASE,
        endpoint="sqlite:///:memory:",
    )
    result = await DatabaseConnector(config).test()
    assert result.ok is False
    assert "sql" in result.message


# ---------------------------------------------------------------------------
# websocket connector (real localhost server)
# ---------------------------------------------------------------------------
async def _serve(handler: Any) -> Any:
    """Start a websockets server on an ephemeral localhost port."""
    from websockets.asyncio.server import serve

    server = await serve(handler, "127.0.0.1", 0)
    return server


def _server_port(server: Any) -> int:
    """Return the port an ephemeral server bound to."""
    return int(next(iter(server.sockets)).getsockname()[1])


def _ws_config(port: int, *, key: str | None, secret: str | None) -> SourceConfig:
    """A websocket source pointed at the local test server."""
    return make_config(
        id="ws-source",
        name="Local stream",
        protocol=Protocol.WEBSOCKET,
        endpoint=f"ws://127.0.0.1:{port}",
        format=PayloadFormat.JSON,
        schedule="streaming",
        scope=["AAPL"],
        auth=AuthConfig(
            type=AuthType.API_KEY, key_name="key", key_value=key, client_secret=secret
        ),
        advanced=AdvancedConfig(
            subscriptions=["AAPL"],
            extra={
                "auth_message": {"action": "auth", "key": "{key}", "secret": "{secret}"},
                "subscribe_message": {"action": "subscribe", "trades": "{channels}"},
                "test_wait": 2.0,
                "auth_timeout": 2.0,
            },
        ),
    )


async def test_websocket_connector_authenticates_and_receives() -> None:
    """A real socket handshake, auth frame, subscription and data frame."""
    received: list[dict[str, Any]] = []

    async def handler(websocket: Any) -> None:
        auth = json.loads(await websocket.recv())
        received.append(auth)
        if auth.get("key") != "k" or auth.get("secret") != "s":
            await websocket.send(json.dumps([{"T": "error", "msg": "auth failed"}]))
            return
        await websocket.send(json.dumps([{"T": "success", "msg": "authenticated"}]))
        subscribe = json.loads(await websocket.recv())
        received.append(subscribe)
        await websocket.send(json.dumps([{"T": "t", "S": "AAPL", "p": 101.5}]))
        await asyncio.sleep(5)

    server = await _serve(handler)
    try:
        config = _ws_config(_server_port(server), key="k", secret="s")
        connector = WebSocketConnector(config)
        result = await connector.test()
    finally:
        server.close()
        await server.wait_closed()

    assert result.ok is True
    assert result.authenticated is True
    assert result.sample_record == {"T": "t", "S": "AAPL", "p": 101.5}
    assert received[0]["action"] == "auth"
    assert received[1] == {"action": "subscribe", "trades": ["AAPL"]}


async def test_websocket_connector_reports_rejected_credentials() -> None:
    """A rejected auth frame is reported as reachable but unauthenticated."""

    async def handler(websocket: Any) -> None:
        await websocket.recv()
        await websocket.send(json.dumps([{"T": "error", "code": 402, "msg": "auth failed"}]))

    server = await _serve(handler)
    try:
        config = _ws_config(_server_port(server), key="wrong", secret="wrong")
        result = await WebSocketConnector(config).test()
    finally:
        server.close()
        await server.wait_closed()

    assert result.ok is False
    assert result.reachable is True
    assert result.authenticated is False
    assert status_from_test(result) is SourceStatus.DEGRADED


async def test_websocket_connector_without_credentials_is_not_configured() -> None:
    """Missing credentials produce a 'not configured' result, and no connection."""
    config = _ws_config(1, key=None, secret=None)
    connector = WebSocketConnector(config)
    result = await connector.test()
    assert result.ok is False
    assert result.reachable is None
    assert result.authenticated is False
    assert "not configured" in result.message
    assert status_from_test(result) is SourceStatus.NOT_CONFIGURED
    assert await connector.status() is SourceStatus.NOT_CONFIGURED


async def test_websocket_connector_unreachable_endpoint() -> None:
    """An endpoint nothing listens on is reported as unreachable."""
    config = _ws_config(9, key="k", secret="s")
    result = await WebSocketConnector(config).test()
    assert result.ok is False
    assert result.reachable is False


# ---------------------------------------------------------------------------
# library connector
# ---------------------------------------------------------------------------
class FakeProvider:
    """A minimal in-process provider used to test the library connector."""

    name = "fake"

    def __init__(self, *, configured: bool = True, reachable: bool | None = True) -> None:
        """Create the fake."""
        self._status = ProviderStatus(
            name="fake",
            configured=configured,
            reachable=reachable,
            latency_ms=12.0,
            detail="fixture provider",
        )
        self.closed = False

    async def status(self) -> ProviderStatus:
        """Return the canned status."""
        return self._status

    async def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """Return one quote per requested symbol."""
        return {
            symbol: Quote(
                symbol=symbol,
                price=100.0,
                ts=datetime(2026, 1, 2, tzinfo=UTC),
                prev_close=99.0,
                source="fake",
            )
            for symbol in symbols
        }

    async def close(self) -> None:
        """Record that the connector closed the provider."""
        self.closed = True


async def test_library_connector_test_and_fetch() -> None:
    """A library source reports the provider's real status and a real sample."""
    config = make_config(
        id="fake-provider",
        protocol=Protocol.LIBRARY,
        endpoint="fake",
        provider="fake",
        format=PayloadFormat.ROWS,
        scope=["AAPL"],
        advanced=AdvancedConfig(extra={"dataset": "quotes"}),
    )
    provider = FakeProvider()
    connector = LibraryConnector(config, provider)  # type: ignore[arg-type]
    result = await connector.test()
    assert result.ok is True
    assert result.authenticated is True
    assert result.sample_record is not None
    assert result.sample_record["symbol"] == "AAPL"
    assert result.latency_ms == 12.0

    batch = await connector.fetch()
    assert batch.dataset == "quotes"
    assert batch.records[0]["price"] == 100.0
    assert await connector.status() is SourceStatus.CONNECTED


async def test_library_connector_reports_unconfigured_provider() -> None:
    """An unconfigured provider yields NOT_CONFIGURED, never a fake 'connected'."""
    config = make_config(
        id="fake-unconfigured",
        protocol=Protocol.LIBRARY,
        endpoint="fake",
        provider="fake",
        advanced=AdvancedConfig(extra={"dataset": "quotes"}),
    )
    connector = LibraryConnector(config, FakeProvider(configured=False, reachable=None))  # type: ignore[arg-type]
    result = await connector.test()
    assert result.ok is False
    assert result.authenticated is False
    assert "not configured" in result.message
    assert status_from_test(result) is SourceStatus.NOT_CONFIGURED
    assert await connector.status() is SourceStatus.NOT_CONFIGURED


def test_map_provider_status() -> None:
    """Provider statuses map onto catalog statuses without inventing anything."""
    assert map_provider_status(ProviderStatus("x", True, True)) is SourceStatus.CONNECTED
    assert map_provider_status(ProviderStatus("x", True, False)) is SourceStatus.DISCONNECTED
    assert map_provider_status(ProviderStatus("x", True, None)) is SourceStatus.UNKNOWN
    assert map_provider_status(ProviderStatus("x", False)) is SourceStatus.NOT_CONFIGURED


def test_build_connector_dispatches_on_protocol_and_provider() -> None:
    """The protocol selects the class; a named provider selects the library one."""
    assert isinstance(build_connector(make_config()), RestConnector)
    assert isinstance(
        build_connector(make_config(protocol=Protocol.POLLING)), PollingConnector
    )
    assert isinstance(
        build_connector(
            make_config(protocol=Protocol.FILE, endpoint="/tmp/*.csv")  # noqa: S108
        ),
        FileConnector,
    )
    library = build_connector(make_config(protocol=Protocol.REST, provider="edgar"))
    assert isinstance(library, LibraryConnector)
    assert library.provider_id == "edgar"


# ---------------------------------------------------------------------------
# catalog
# ---------------------------------------------------------------------------
def test_builtin_sources_are_the_spec_sources() -> None:
    """Every built-in provider is pre-registered with its real protocol."""
    by_id = {source.id: source for source in builtin_sources()}
    assert set(by_id) == BUILTIN_SOURCE_IDS
    assert by_id["yfinance"].protocol is Protocol.LIBRARY
    assert by_id["alpaca"].protocol is Protocol.WEBSOCKET
    assert by_id["edgar"].protocol is Protocol.REST
    assert by_id["fred"].protocol is Protocol.POLLING
    assert by_id["trends"].protocol is Protocol.POLLING
    assert by_id["jobs"].protocol is Protocol.LIBRARY
    assert all(source.builtin for source in by_id.values())


def test_register_builtins_is_idempotent(catalog: SourceCatalog) -> None:
    """Seeding twice keeps one row per built-in and does not reset user changes."""
    catalog.register_builtins()
    fred = catalog.get("fred")
    fred.scope = ["DGS10"]
    fred.schedule = "1d"
    catalog.save(fred)

    catalog.register_builtins()
    assert len(catalog.list_sources()) == len(BUILTIN_SOURCE_IDS)
    reloaded = catalog.get("fred")
    assert reloaded.scope == ["DGS10"]
    assert reloaded.schedule == "1d"


def test_builtin_statuses_start_unknown_not_connected(catalog: SourceCatalog) -> None:
    """A freshly seeded catalog claims nothing about connectivity."""
    catalog.register_builtins()
    assert catalog.connected_count() == (0, len(BUILTIN_SOURCE_IDS))
    assert all(
        state.status is SourceStatus.UNKNOWN for state in catalog.states().values()
    )


async def test_refresh_status_records_a_real_probe(settings: Settings, bus: EventBus) -> None:
    """A probe's outcome - and only that - becomes the source's status."""
    events: list[dict[str, Any]] = []
    bus.subscribe("ingest", lambda event: events.append(event.payload))

    stub = StubConnector(make_config(), records=[{"symbol": "AAPL", "close": 1.0}])
    catalog = SourceCatalog(bus, connector_factory=lambda config: stub)
    catalog.save(make_config())

    state = await catalog.refresh_status("test-source")
    assert state.status is SourceStatus.CONNECTED
    assert state.latency_ms == 1.5
    assert stub.closed is True
    assert catalog.connected_count() == (1, 1)
    assert any(event.get("event") == "source_status" for event in events)


async def test_refresh_status_records_failure(settings: Settings, bus: EventBus) -> None:
    """A failed probe degrades the source and keeps the vendor's message."""
    failing = StubConnector(
        make_config(),
        test_result=ConnectionTestResult.failure("vendor down", reachable=False),
    )
    catalog = SourceCatalog(bus, connector_factory=lambda config: failing)
    catalog.save(make_config())
    state = await catalog.refresh_status("test-source")
    assert state.status is SourceStatus.DISCONNECTED
    assert state.last_error == "vendor down"
    assert catalog.connected_count() == (0, 1)


def test_catalog_crud_and_builtin_protection(catalog: SourceCatalog) -> None:
    """User sources round-trip; built-ins cannot be deleted."""
    catalog.register_builtins()
    config = make_config(id="user-rest", name="User REST")
    catalog.save(config)
    assert catalog.get("user-rest").name == "User REST"

    config.name = "Renamed"
    catalog.save(config)
    assert catalog.get("user-rest").name == "Renamed"

    with pytest.raises(ValueError, match="built-in"):
        catalog.delete("fred")
    catalog.delete("user-rest")
    with pytest.raises(KeyError):
        catalog.get("user-rest")


def test_catalog_persists_across_instances(settings: Settings, bus: EventBus) -> None:
    """Sources and state survive a restart (they live in the app database)."""
    first = SourceCatalog(bus)
    first.save(make_config(id="persisted"))
    first.record_ingest("persisted", rows=42, latency_ms=8.0)

    second = SourceCatalog(bus)
    assert second.get("persisted").id == "persisted"
    state = second.state("persisted")
    assert state.rows_ingested == 42
    assert state.status is SourceStatus.CONNECTED
    assert state.last_ingest_at is not None


def test_catalog_subscriptions(catalog: SourceCatalog) -> None:
    """Subscriptions are stored against their source and can be removed."""
    catalog.save(make_config(id="sub-source"))
    saved = catalog.save_subscription(
        Subscription(id="", source_id="sub-source", channel="bars", scope=["AAPL"])
    )
    assert saved.id
    assert [s.channel for s in catalog.list_subscriptions()] == ["bars"]
    with pytest.raises(KeyError):
        catalog.save_subscription(Subscription(id="", source_id="ghost", channel="x"))
    catalog.delete_subscription(saved.id)
    assert catalog.list_subscriptions() == []


def test_catalog_summary_counts_real_states(catalog: SourceCatalog) -> None:
    """The catalog header counters come from stored state, not assumptions."""
    catalog.register_builtins()
    catalog.record_ingest("fred", rows=10)
    summary = catalog.summary()
    assert summary["sources"] == len(BUILTIN_SOURCE_IDS)
    assert summary["builtin"] == len(BUILTIN_SOURCE_IDS)
    assert summary["connected"] == 1
    assert summary["rows_ingested"] == 10
    assert summary["by_status"]["unknown"] == len(BUILTIN_SOURCE_IDS) - 1


def test_stale_sources_detects_delay(catalog: SourceCatalog) -> None:
    """A source that missed two of its own cycles is reported as delayed."""
    config = make_config(id="hourly", schedule="1h")
    catalog.save(config)
    catalog.record_ingest(
        "hourly", rows=1, at=datetime.now(UTC) - timedelta(hours=5)
    )
    stale = catalog.stale_sources()
    assert [row["source_id"] for row in stale] == ["hourly"]
    assert stale[0]["tolerance_seconds"] == 7200.0
    assert stale[0]["never_ingested"] is False

    catalog.record_ingest("hourly", rows=1)
    assert catalog.stale_sources() == []


def test_stale_sources_ignores_manual_sources(catalog: SourceCatalog) -> None:
    """Nothing promised a cadence for a manual source, so it is never late."""
    catalog.save(make_config(id="manual-source", schedule="manual"))
    assert catalog.stale_sources() == []


# ---------------------------------------------------------------------------
# scheduler
# ---------------------------------------------------------------------------
def bar_records(symbol: str = "AAPL", days: int = 3) -> list[dict[str, Any]]:
    """Return canonical bar records for the scheduler tests."""
    return [
        {
            "symbol": symbol,
            "ts": datetime(2026, 1, day + 1, tzinfo=UTC).isoformat(),
            "open": 10.0 + day,
            "high": 11.0 + day,
            "low": 9.0 + day,
            "close": 10.5 + day,
            "volume": 1000.0 + day,
            "interval": "1d",
            "source": "fixture",
        }
        for day in range(days)
    ]


def test_classify_and_convert_records() -> None:
    """Record shape decides the store destination; bad rows are dropped."""
    assert classify_records(bar_records()) == "bars"
    assert classify_records([{"name": "DGS10", "ts": "2026-01-01", "value": 4.2}]) == "series"
    assert classify_records([{"headline": "x"}]) == "frame"

    bars = records_to_bars(bar_records() + [{"symbol": "X", "ts": None, "close": None}], default_source="s")
    assert len(bars) == 3
    assert bars[0].interval is BarInterval.DAY_1

    pairs = records_to_series(
        [
            {"namespace": "fred", "name": "DGS10", "ts": "2026-01-01", "value": 4.2},
            {"namespace": "fred", "name": "DGS10", "ts": "2026-01-02", "value": 4.3},
            {"name": "BAD", "ts": "2026-01-02", "value": None},
        ],
        default_namespace="fred",
    )
    assert len(pairs) == 1
    namespace, series = pairs[0]
    assert namespace == "fred"
    assert len(series.points) == 2


async def test_scheduler_tick_runs_due_jobs_and_persists(
    settings: Settings, bus: EventBus, tmp_path: Path
) -> None:
    """A due job fetches, writes to the store and records last_ingest."""
    events: list[dict[str, Any]] = []
    bus.subscribe("ingest", lambda event: events.append(event.payload))

    stub = StubConnector(make_config(), records=bar_records())
    catalog = SourceCatalog(bus, connector_factory=lambda config: stub)
    catalog.save(make_config(id="bars-source", schedule="5m"))

    store = TimeSeriesStore(tmp_path / "ts")
    scheduler = IngestScheduler(catalog, bus, store=store, tick_seconds=0.01)
    await scheduler.reload()

    job = scheduler.job("bars-source")
    assert job is not None and job.next_run_at is not None

    outcomes = await scheduler.tick()
    assert len(outcomes) == 1
    assert outcomes[0].ok is True
    assert outcomes[0].rows_written == 3
    assert outcomes[0].dataset == "bars"

    frame = store.read_bars("AAPL", interval=BarInterval.DAY_1)
    assert len(frame) == 3
    assert scheduler.last_ingest_at() is not None
    assert catalog.state("bars-source").rows_ingested == 3
    assert any(event.get("event") == "ingest_run" for event in events)

    # rescheduled five minutes out, so a second tick does nothing
    assert scheduler.due_jobs() == []
    assert await scheduler.tick() == []


async def test_scheduler_records_failures_without_stopping(
    settings: Settings, bus: EventBus, tmp_path: Path
) -> None:
    """A failing source is marked degraded and the scheduler keeps running."""
    stub = StubConnector(make_config(), error=RuntimeError("vendor exploded"))
    catalog = SourceCatalog(bus, connector_factory=lambda config: stub)
    catalog.save(make_config(id="broken", schedule="1m"))
    scheduler = IngestScheduler(catalog, bus, store=TimeSeriesStore(tmp_path / "ts2"))
    await scheduler.reload()

    outcomes = await scheduler.tick()
    assert outcomes[0].ok is False
    assert "vendor exploded" in (outcomes[0].error or "")
    state = catalog.state("broken")
    assert state.status is SourceStatus.DEGRADED
    assert state.last_ingest_at is None
    job = scheduler.job("broken")
    assert job is not None and job.last_status == "error"
    assert job.next_run_at is not None


async def test_scheduler_skips_streaming_and_empty_scope(
    settings: Settings, bus: EventBus, tmp_path: Path
) -> None:
    """Streaming sources and provider sources without a scope report skipped."""
    catalog = SourceCatalog(bus, connector_factory=lambda config: StubConnector(config))
    catalog.save(
        make_config(
            id="stream",
            protocol=Protocol.WEBSOCKET,
            endpoint="wss://vendor.test/stream",
            schedule="streaming",
        )
    )
    catalog.save(
        make_config(id="no-scope", protocol=Protocol.LIBRARY, endpoint="fred", provider="fred")
    )
    scheduler = IngestScheduler(catalog, bus, store=TimeSeriesStore(tmp_path / "ts3"))

    streamed = await scheduler.run_now("stream")
    assert streamed.skipped is not None and streamed.ok is False
    empty = await scheduler.run_now("no-scope")
    assert empty.skipped == "no scope configured"

    # a streaming source is not put on the periodic job table at all
    await scheduler.reload()
    assert scheduler.job("stream") is None


async def test_scheduler_start_stop_runs_the_loop(
    settings: Settings, bus: EventBus, tmp_path: Path
) -> None:
    """start() really drives ticks in the background; stop() cancels cleanly."""
    stub = StubConnector(make_config(), records=bar_records("MSFT", days=1))
    catalog = SourceCatalog(bus, connector_factory=lambda config: stub)
    catalog.save(make_config(id="fast", schedule="1s"))
    scheduler = IngestScheduler(
        catalog, bus, store=TimeSeriesStore(tmp_path / "ts4"), tick_seconds=0.01
    )
    await scheduler.start()
    assert scheduler.running is True
    for _ in range(100):
        if stub.fetches:
            break
        await asyncio.sleep(0.01)
    await scheduler.stop()
    assert scheduler.running is False
    assert stub.fetches >= 1


async def test_scheduler_backfill_reports_progress(
    settings: Settings, bus: EventBus, tmp_path: Path
) -> None:
    """Backfill streams batches, persists them and publishes progress events."""
    events: list[dict[str, Any]] = []
    bus.subscribe("ingest", lambda event: events.append(event.payload))
    stub = StubConnector(make_config(), records=bar_records("NVDA", days=2))
    catalog = SourceCatalog(bus, connector_factory=lambda config: stub)
    catalog.save(make_config(id="backfillable", schedule="1d"))
    scheduler = IngestScheduler(catalog, bus, store=TimeSeriesStore(tmp_path / "ts5"))

    summary = await scheduler.backfill("backfillable", range="1y")
    assert summary["ok"] is True
    assert summary["rows_written"] == 2
    kinds = [event.get("event") for event in events]
    assert "backfill_started" in kinds and "backfill_finished" in kinds


async def test_scheduler_backfill_reports_unsupported(
    settings: Settings, bus: EventBus, tmp_path: Path
) -> None:
    """A connector that cannot backfill says so rather than claiming success."""

    class NoBackfill(StubConnector):
        """A stub whose backfill is genuinely unimplemented."""

        async def backfill(
            self, *, range: str = "1y", batch_size: int = 500, end: datetime | None = None
        ) -> AsyncIterator[FetchResult]:
            """Refuse to backfill."""
            raise NotImplementedError("stub sources cannot backfill")
            yield FetchResult.empty()  # pragma: no cover

    catalog = SourceCatalog(bus, connector_factory=lambda config: NoBackfill(config))
    catalog.save(make_config(id="nobf", schedule="1d"))
    scheduler = IngestScheduler(catalog, bus, store=TimeSeriesStore(tmp_path / "ts6"))
    summary = await scheduler.backfill("nobf")
    assert summary["ok"] is False
    assert "cannot backfill" in (summary["error"] or "")


async def test_scheduler_persists_series_and_frames(
    settings: Settings, bus: EventBus, tmp_path: Path
) -> None:
    """Series records go to the series dataset; other shapes to a frame dataset."""
    series_records = [
        {"namespace": "fred", "name": "DGS10", "ts": "2026-01-01T00:00:00Z", "value": 4.2},
        {"namespace": "fred", "name": "DGS10", "ts": "2026-01-02T00:00:00Z", "value": 4.3},
    ]
    news_records = [{"symbol": "AAPL", "headline": "Something happened", "url": "https://x.test"}]
    store = TimeSeriesStore(tmp_path / "ts7")
    catalog = SourceCatalog(bus)
    catalog.save(make_config(id="macro", provider="fred", protocol=Protocol.POLLING))
    scheduler = IngestScheduler(catalog, bus, store=store)

    rows, dataset = await scheduler.persist(
        catalog.get("macro"),
        FetchResult(records=series_records, fetched_at=datetime.now(UTC)),
    )
    assert (rows, dataset) == (2, "series")
    assert len(store.read_series("fred", "DGS10").points) == 2

    rows, dataset = await scheduler.persist(
        catalog.get("macro"),
        FetchResult(records=news_records, fetched_at=datetime.now(UTC)),
    )
    assert rows == 1
    assert dataset == "ingest_macro"


# ---------------------------------------------------------------------------
# gateway
# ---------------------------------------------------------------------------
async def test_gateway_health_reports_observed_values(
    settings: Settings, bus: EventBus
) -> None:
    """Health counts real connected sources and publishes on 'status'."""
    status_events: list[dict[str, Any]] = []
    bus.subscribe("status", lambda event: status_events.append(event.payload))

    stub = StubConnector(make_config(), records=[{"a": 1}])
    catalog = SourceCatalog(bus, connector_factory=lambda config: stub)
    catalog.save(make_config(id="ok-source", schedule="5m"))
    catalog.save(make_config(id="quiet-source", schedule="5m"))
    await catalog.refresh_status("ok-source")
    catalog.record_ingest("ok-source", rows=3, latency_ms=4.0)

    gateway = IngestGateway(catalog, bus)
    health = await gateway.health()
    assert health.sources_connected == 1
    assert health.sources_total == 2
    assert health.latency_ms is not None
    assert health.last_ingest_at is not None
    assert any(event.get("event") == "gateway_health" for event in status_events)


async def test_gateway_latency_is_measured_not_invented(
    settings: Settings, bus: EventBus
) -> None:
    """Latency is None until something was actually measured."""
    catalog = SourceCatalog(bus, connector_factory=lambda config: StubConnector(config))
    gateway = IngestGateway(catalog, bus)
    assert gateway.observed_latency() is None
    health = await gateway.health(publish=False)
    assert health.latency_ms is None
    assert health.sources_total == 0
    assert health.detail == "no sources are configured"

    catalog.save(make_config(id="probe-me", schedule="5m"))
    measured = await gateway.measure_latency()
    assert measured is not None and measured >= 0
    assert gateway.latency_fresh is True


async def test_gateway_counts_delayed_series(settings: Settings, bus: EventBus) -> None:
    """Each scope item of an overdue source counts as one delayed series."""
    catalog = SourceCatalog(bus)
    config = make_config(
        id="fred-like",
        protocol=Protocol.POLLING,
        provider="fred",
        schedule="1h",
        scope=["DGS10", "CPIAUCSL"],
    )
    catalog.save(config)
    catalog.record_ingest("fred-like", rows=1, at=datetime.now(UTC) - timedelta(hours=6))

    gateway = IngestGateway(catalog, bus)
    delayed = gateway.delayed_series()
    assert [entry["series"] for entry in delayed] == ["DGS10", "CPIAUCSL"]
    health = await gateway.health(publish=False)
    assert health.delayed_series == 2
    assert health.healthy is False
    assert "delayed" in (health.detail or "")


async def test_gateway_report_includes_source_table(
    settings: Settings, bus: EventBus
) -> None:
    """The gateway payload carries the per-source table the UI renders."""
    catalog = SourceCatalog(bus)
    catalog.register_builtins()
    report = await IngestGateway(catalog, bus).report()
    assert {row["id"] for row in report["sources"]} == BUILTIN_SOURCE_IDS
    assert report["summary"]["sources"] == len(BUILTIN_SOURCE_IDS)
    assert report["latency_ms"] is None
    row = next(row for row in report["sources"] if row["id"] == "alpaca")
    assert row["protocol"] == "websocket"
    assert row["state"]["status"] == "unknown"


# ---------------------------------------------------------------------------
# end-to-end: catalog + connector + scheduler over local fixtures
# ---------------------------------------------------------------------------
async def test_end_to_end_file_source_ingests_into_the_store(
    settings: Settings, bus: EventBus, tmp_path: Path
) -> None:
    """A saved CSV source is probed, scheduled, ingested and stored for real."""
    drop = tmp_path / "e2e"
    drop.mkdir()
    (drop / "bars.csv").write_text(
        "symbol,ts,open,high,low,close,volume\n"
        "AAPL,2026-01-02T00:00:00Z,10,11,9,10.5,1000\n"
        "AAPL,2026-01-03T00:00:00Z,11,12,10,11.5,1200\n"
    )
    config = SourceConfig.from_form(
        {
            "sourceName": "E2E CSV bars",
            "id": "e2e-csv",
            "sourceDomain": "Prices & quotes",
            "sourceProtocol": "Database file / flat file",
            "sourceEndpoint": str(drop / "*.csv"),
            "sourceFormat": "CSV",
            "sourceFrequency": "Every minute",
            "sourceAuth": "None",
            "sourceTimezone": "UTC",
            "sourceRetries": "Exponential backoff",
            "backfillEnabled": False,
        }
    )
    catalog = SourceCatalog(bus)
    catalog.save(config)

    state = await catalog.refresh_status("e2e-csv")
    assert state.status is SourceStatus.CONNECTED

    store = TimeSeriesStore(tmp_path / "ts-e2e")
    scheduler = IngestScheduler(catalog, bus, store=store, tick_seconds=0.01)
    await scheduler.reload()
    outcomes = await scheduler.tick()

    assert outcomes[0].rows_written == 2
    assert len(store.read_bars("AAPL")) == 2
    gateway = IngestGateway(catalog, bus)
    health = await gateway.health(publish=False)
    assert health.sources_connected == 1
    assert health.delayed_series == 0


# ---------------------------------------------------------------------------
# subscription feed mode and point-in-time
# ---------------------------------------------------------------------------
def test_subscription_carries_feed_mode_and_pit(catalog: SourceCatalog) -> None:
    """Both per-subscription controls survive a save/load round trip."""
    catalog.save(make_config(id="sub-source"))
    saved = catalog.save_subscription(
        Subscription(
            id="",
            source_id="sub-source",
            channel="bars",
            scope=["AAPL"],
            feed_mode="live",
            preserve_pit=True,
        )
    )
    assert saved.feed_mode == "live"
    assert saved.preserve_pit is True
    payload = saved.as_dict()
    assert payload["feed_mode"] == "live"
    assert payload["preserve_pit"] is True

    reloaded = catalog.list_subscriptions(source_id="sub-source")
    assert [(s.feed_mode, s.preserve_pit) for s in reloaded] == [("live", True)]

    # Defaults stay conservative for a subscription that does not ask.
    plain = catalog.save_subscription(
        Subscription(id="", source_id="sub-source", channel="quotes")
    )
    assert plain.feed_mode == "scheduled"
    assert plain.preserve_pit is False


async def test_point_in_time_subscription_refuses_a_restatement(
    settings: Settings, bus: EventBus, tmp_path: Path
) -> None:
    """With PIT on, an already-stored timestamp is kept and only new ones land."""
    records = bar_records()
    stub = StubConnector(make_config(), records=records)
    catalog = SourceCatalog(bus, connector_factory=lambda config: stub)
    catalog.save(make_config(id="pit-source", schedule="5m"))
    store = TimeSeriesStore(tmp_path / "ts-pit")
    scheduler = IngestScheduler(catalog, bus, store=store, tick_seconds=0.01)
    await scheduler.reload()

    first = await scheduler.tick()
    assert first[0].rows_written == 3
    original = store.read_bars("AAPL", interval=BarInterval.DAY_1)["close"].tolist()

    catalog.save_subscription(
        Subscription(
            id="", source_id="pit-source", channel="bars", preserve_pit=True
        )
    )
    # The vendor restates every close and adds one new bar.
    restated = [dict(row, close=row["close"] * 2) for row in records]
    restated.append(
        {
            "symbol": "AAPL",
            "ts": datetime(2026, 1, 6, tzinfo=UTC).isoformat(),
            "open": 20.0,
            "high": 21.0,
            "low": 19.0,
            "close": 20.5,
            "volume": 1500.0,
            "interval": "1d",
            "source": "fixture",
        }
    )
    stub.records = restated
    job = scheduler.job("pit-source")
    assert job is not None
    job.next_run_at = datetime.now(UTC) - timedelta(seconds=1)

    await scheduler.tick()
    frame = store.read_bars("AAPL", interval=BarInterval.DAY_1)
    assert len(frame) == 4, "the genuinely new bar must still be written"
    assert frame["close"].tolist()[:3] == original, "stored history must not be restated"


# ---------------------------------------------------------------------------
# per-dataset granularity (F43b) and custom poll intervals (F43a)
# ---------------------------------------------------------------------------
def test_granularity_options_follow_the_data_domain() -> None:
    """A price feed offers bar sizes; a fundamentals feed offers report cadences."""
    assert "1m" in granularity_options(DataDomain.MARKET)
    assert "quarterly" in granularity_options(DataDomain.FUNDAMENTALS)
    assert "1m" not in granularity_options(DataDomain.FUNDAMENTALS)

    assert parse_granularity(" 5M ") == "5m"
    assert parse_granularity(None) == ""
    with pytest.raises(ValueError, match="unknown granularity"):
        parse_granularity("1 fortnight")

    # Only a real bar size can reach a fetch; a reporting cadence cannot.
    assert bar_interval_for("5m") == "5m"
    assert bar_interval_for("quarterly") is None


def test_subscription_granularity_round_trips_and_is_domain_checked(
    catalog: SourceCatalog,
) -> None:
    """The granularity is persisted, reloaded, and refused when the domain says so."""
    catalog.save(make_config(id="gran-source", data_domain=DataDomain.MARKET))
    saved = catalog.save_subscription(
        Subscription(id="", source_id="gran-source", channel="bars", granularity="5m")
    )
    assert saved.granularity == "5m"
    assert saved.as_dict()["granularity"] == "5m"

    # Reloaded from the database, not from the object that was just saved.
    fresh = SourceCatalog()
    assert [s.granularity for s in fresh.list_subscriptions(source_id="gran-source")] == ["5m"]

    # A price feed cannot be subscribed at a reporting cadence.
    with pytest.raises(ValueError, match="not 'quarterly'"):
        catalog.save_subscription(
            Subscription(
                id="", source_id="gran-source", channel="bars", granularity="quarterly"
            )
        )
    # ... and an unknown token never reaches the database at all.
    with pytest.raises(ValueError, match="unknown granularity"):
        Subscription(id="", source_id="gran-source", channel="bars", granularity="7 minutes")

    # No granularity remains the honest default: "whatever the source is set to".
    plain = catalog.save_subscription(
        Subscription(id="", source_id="gran-source", channel="quotes")
    )
    assert plain.granularity == ""


def test_describe_publishes_the_granularity_options(catalog: SourceCatalog) -> None:
    """The chips the Source Catalog renders come from the source itself."""
    catalog.save(
        make_config(
            id="chip-source",
            data_domain=DataDomain.MARKET,
            advanced=AdvancedConfig(extra={"dataset": "bars", "interval": "1d"}),
        )
    )
    payload = catalog.describe("chip-source")
    assert payload["granularity_options"] == list(granularity_options(DataDomain.MARKET))
    assert payload["granularity_default"] == "1d"


async def test_subscription_granularity_reaches_the_connector_fetch(
    settings: Settings, bus: EventBus, tmp_path: Path
) -> None:
    """A bar granularity is handed to the fetch; a disagreement is not."""
    seen: list[Any] = []

    class RecordingConnector(StubConnector):
        """A stub that remembers the parameters the scheduler passed it."""

        async def fetch(self, **params: Any) -> FetchResult:
            """Record the parameters, then behave like the plain stub."""
            seen.append(params.get("interval"))
            return await super().fetch(**params)

    config = make_config(
        id="lib-source",
        protocol=Protocol.LIBRARY,
        provider="yfinance",
        endpoint="yfinance",
        format=PayloadFormat.ROWS,
        scope=["AAPL"],
        advanced=AdvancedConfig(extra={"dataset": "bars", "interval": "1d"}),
    )
    stub = RecordingConnector(config, records=bar_records())
    catalog = SourceCatalog(bus, connector_factory=lambda cfg: stub)
    catalog.save(config)
    scheduler = IngestScheduler(
        catalog, bus, store=TimeSeriesStore(tmp_path / "ts-gran"), tick_seconds=0.01
    )
    await scheduler.reload()

    # No subscription: the source's own configured interval is left alone.
    await scheduler.run_now("lib-source")
    assert seen == [None]

    catalog.save_subscription(
        Subscription(id="", source_id="lib-source", channel="bars", granularity="5m")
    )
    assert scheduler.requested_interval(catalog.get("lib-source")) == "5m"
    await scheduler.run_now("lib-source")
    assert seen[-1] == "5m", "the stored granularity must reach the fetch"

    # Two active subscriptions asking for different bars: neither is imposed.
    catalog.save_subscription(
        Subscription(id="", source_id="lib-source", channel="bars2", granularity="1h")
    )
    assert scheduler.requested_interval(catalog.get("lib-source")) is None
    await scheduler.run_now("lib-source")
    assert seen[-1] is None


async def test_library_connector_honours_an_interval_override() -> None:
    """The override, not the configured interval, is what the provider is asked for."""
    asked: list[BarInterval] = []

    class RecordingProvider:
        """The narrowest provider surface the bars dataset touches."""

        name = "recording"

        async def get_bars(
            self, symbol: str, *, start: Any = None, end: Any = None,
            interval: BarInterval = BarInterval.DAY_1, limit: Any = None,
        ) -> list[Any]:
            """Record the interval that was requested."""
            asked.append(interval)
            return []

        async def close(self) -> None:
            """Nothing to release."""
            return None

    config = make_config(
        id="interval-source",
        protocol=Protocol.LIBRARY,
        provider="recording",
        endpoint="recording",
        format=PayloadFormat.ROWS,
        scope=["AAPL"],
        advanced=AdvancedConfig(extra={"dataset": "bars", "interval": "1d"}),
    )
    connector = LibraryConnector(config, provider=RecordingProvider())
    await connector.fetch()
    await connector.fetch(interval="5m")
    assert asked == [BarInterval.DAY_1, BarInterval.MIN_5]

    with pytest.raises(ValueError):
        await connector.fetch(interval="1 fortnight")


@pytest.mark.parametrize(
    ("token", "seconds", "label"),
    [
        ("45s", 45.0, "Every 45s"),
        ("2m", 120.0, "Every 2m"),
        ("4h", 14_400.0, "Every 4h"),
        ("1d", 86_400.0, "Daily"),
        ("90 s", 90.0, "Every 90s"),
        ("3 hours", 10_800.0, "Every 3h"),
    ],
)
def test_custom_poll_intervals_parse(token: str, seconds: float, label: str) -> None:
    """A cadence outside the preset list is a real, runnable, readable schedule."""
    schedule = parse_schedule(token)
    assert schedule.kind is ScheduleKind.INTERVAL
    assert schedule.interval_seconds == pytest.approx(seconds)
    assert schedule.periodic is True
    # A custom cadence is labelled in its own units, not as a raw second count.
    assert schedule.label == label
    assert schedule.next_run_after(None) is not None


@pytest.mark.parametrize("token", ["45x", "soon", "0s", "-2m", "5 minute-ish"])
def test_custom_poll_intervals_are_validated(token: str) -> None:
    """An unparseable cadence is refused before it can be stored on a source."""
    with pytest.raises(ValueError):
        parse_schedule(token)
    with pytest.raises(ValueError):
        make_config(id="bad-schedule", schedule=token)


async def test_changing_a_cadence_retimes_the_pending_run(
    catalog: SourceCatalog, bus: EventBus, tmp_path: Path
) -> None:
    """Re-registering a source at a new cadence moves its pending fire.

    This is the whole promise of the custom poll interval: switching a source
    from ``1d`` to ``45s`` has to make the next run 45 seconds after the last
    one, not leave the fire a day out - and lengthening has to push it back
    just as honestly.
    """
    catalog.save(make_config(id="retime", schedule="1d"))
    scheduler = IngestScheduler(catalog, bus, store=TimeSeriesStore(tmp_path / "ts-retime"))
    job = await scheduler.add_source("retime")
    last = datetime.now(UTC) - timedelta(minutes=5)
    job.last_run_at = last
    job.next_run_at = last + timedelta(days=1)

    # Shortened: 45s after the last run is already past, so the job is due now.
    catalog.save(make_config(id="retime", schedule="45s"))
    job = await scheduler.add_source("retime")
    assert job.schedule == "45s"
    assert job.next_run_at == last + timedelta(seconds=45)
    assert [j.source_id for j in scheduler.due_jobs()] == ["retime"]

    # Lengthened: the fire moves out to one day after the last run, and the
    # job that was due a moment ago is no longer due.
    catalog.save(make_config(id="retime", schedule="1d"))
    job = await scheduler.add_source("retime")
    assert job.next_run_at == last + timedelta(days=1)
    assert scheduler.due_jobs() == []

    # Written differently, meaning the same cadence: nothing is disturbed.
    job.next_run_at = last + timedelta(hours=6)
    catalog.save(make_config(id="retime", schedule="24h"))
    job = await scheduler.add_source("retime")
    assert job.next_run_at == last + timedelta(hours=6)

    # A source that has never run is due immediately at whatever cadence it is
    # given, exactly as it is when the job is first registered.
    job.last_run_at = None
    catalog.save(make_config(id="retime", schedule="5m"))
    job = await scheduler.add_source("retime")
    assert job.next_run_at is not None
    assert job.next_run_at <= datetime.now(UTC)


def test_granularity_only_reaches_a_fetch_that_takes_a_bar_size() -> None:
    """The capability follows the connector that will really run the source.

    A source naming a provider is served by the library connector whatever
    protocol it advertises (``build_connector``), and only its ``bars`` dataset
    has an ``interval`` argument - including when the dataset is implied rather
    than written down.
    """
    # Implied dataset: yfinance defaults to bars, EDGAR to filings.
    assert granularity_reaches_fetch(
        make_config(id="yf", protocol=Protocol.LIBRARY, provider="yfinance", endpoint="yfinance")
    ) is True
    assert granularity_reaches_fetch(
        make_config(
            id="edgar-like",
            protocol=Protocol.REST,
            provider="edgar",
            endpoint="https://data.sec.gov",
        )
    ) is False
    # Written down, and not a bar dataset.
    assert granularity_reaches_fetch(
        make_config(
            id="quotes",
            protocol=Protocol.LIBRARY,
            provider="alpaca",
            endpoint="alpaca",
            advanced=AdvancedConfig(extra={"dataset": "quotes"}),
        )
    ) is False
    # No provider: the scheduler passes no interval at all, and the HTTP
    # connector has nothing to apply one to.
    assert granularity_reaches_fetch(make_config(id="plain-rest")) is False
