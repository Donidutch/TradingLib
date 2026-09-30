"""API tests for the data domain: universes, watchlists, ingestion and quality.

Everything here runs offline. The only ingestion source exercised end to end is
a local CSV drop read by the real :class:`FileConnector`, so the catalog,
scheduler, Parquet store and quality engine are driven for real without
touching a vendor. Where a live connector would be needed the test injects one
explicitly instead of stubbing the layer under test.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.testclient import TestClient

from tradinglib.config import Settings
from tradinglib.data.ingest.connectors.base import ConnectionTestResult, Connector, FetchResult
from tradinglib.data.ingest.source import SourceConfig, SourceStatus
from tradinglib.data.ingest.catalog import builtin_sources
from tradinglib.events import EventBus

#: Ids of the pre-registered provider sources, read from the catalog so that
#: adding a built-in vendor does not mean editing a literal set here.
BUILTIN_SOURCES = {source.id for source in builtin_sources()}


def csv_form(path: Path, *, source_id: str = "csv-bars", name: str = "CSV bars") -> dict[str, Any]:
    """Return the ingestion form payload for a local CSV bar drop."""
    return {
        "id": source_id,
        "sourceName": name,
        "sourceDomain": "Prices & quotes",
        "sourceProtocol": "Database file / flat file",
        "sourceEndpoint": str(path),
        "sourceFormat": "CSV",
        "sourceFrequency": "Every minute",
        "sourceAuth": "None",
        "sourceTimezone": "UTC",
        "sourceRetries": "Exponential backoff",
        "backfillEnabled": False,
    }


@pytest.fixture()
def bar_csv(tmp_path: Path) -> Path:
    """A CSV drop directory holding two real trading sessions of bars."""
    drop = tmp_path / "drop"
    drop.mkdir()
    (drop / "bars.csv").write_text(
        "symbol,ts,open,high,low,close,volume\n"
        "AAPL,2026-01-02T00:00:00Z,10,11,9,10.5,1000\n"
        "AAPL,2026-01-05T00:00:00Z,11,12,10,11.5,1200\n"
    )
    return drop / "*.csv"


# ---------------------------------------------------------------------------
# universes
# ---------------------------------------------------------------------------
async def test_universes_list_includes_builtins(client: AsyncClient) -> None:
    """The built-in universes exist and US Quality 50 carries its real 50 names."""
    body = (await client.get("/api/universes")).json()
    ids = {u["id"] for u in body["universes"]}
    assert {"us-quality-50", "sp500", "nasdaq100"} <= ids
    assert body["active"] is not None

    quality = (await client.get("/api/universes/us-quality-50")).json()
    assert quality["member_count"] == 50
    assert quality["builtin"] is True
    assert all(m["sector"] for m in quality["members"])

    # sp500 has never been fetched in this temporary data directory, so it is
    # honestly empty rather than filled with a plausible list.
    sp500 = (await client.get("/api/universes/sp500")).json()
    assert sp500["member_count"] == 0
    assert sp500["members_fetched_at"] is None


async def test_universe_crud_snapshot_and_active(client: AsyncClient) -> None:
    """A user universe can be created, updated, snapshotted, selected and deleted."""
    created = await client.post(
        "/api/universes",
        json={
            "collectionName": "Semis research",
            "collectionSymbols": "NVDA, AMD, brk.b",
            "collectionDescription": "Semiconductor names",
            "constructionMethod": "Manual symbols",
            "collectionRefresh": "Daily",
        },
    )
    assert created.status_code == 201
    universe = created.json()
    uid = universe["id"]
    assert [m["symbol"] for m in universe["members"]] == ["NVDA", "AMD", "BRK-B"]
    assert universe["rule"] == "Manual symbols / Daily"

    updated = (
        await client.put(f"/api/universes/{uid}", json={"symbols": ["NVDA", "TSM"]})
    ).json()
    assert [m["symbol"] for m in updated["members"]] == ["NVDA", "TSM"]

    snapshot = await client.post(f"/api/universes/{uid}/snapshot", json={"as_of": "2026-06-30"})
    assert snapshot.status_code == 201
    assert snapshot.json()["symbol_count"] == 2

    members = (await client.get(f"/api/universes/{uid}/members?as_of=2026-07-01")).json()
    assert members["basis"] == "snapshot"
    assert members["count"] == 2

    listed = (await client.get(f"/api/universes/{uid}/snapshots")).json()
    assert listed["count"] == 1

    active = (await client.post("/api/universes/active", json={"universe_id": uid})).json()
    assert active["active"] == uid
    assert (await client.get("/api/universes/active")).json()["symbols"] == ["NVDA", "TSM"]

    assert (await client.delete(f"/api/universes/{uid}")).json() == {"deleted": uid}
    assert (await client.get(f"/api/universes/{uid}")).status_code == 404


async def test_builtin_universe_is_protected(client: AsyncClient) -> None:
    """Built-ins cannot be edited or deleted through the API."""
    response = await client.delete("/api/universes/us-quality-50")
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "invalid_request"

    response = await client.put("/api/universes/us-quality-50", json={"name": "mine"})
    assert response.status_code == 400


async def test_universe_refresh_of_static_builtin_needs_no_network(client: AsyncClient) -> None:
    """``us-quality-50`` re-materialises from its static definition, offline."""
    body = (await client.post("/api/universes/us-quality-50/refresh")).json()
    assert body["member_count"] == 50
    assert body["members_fetched_at"] is not None


# ---------------------------------------------------------------------------
# watchlists
# ---------------------------------------------------------------------------
async def test_watchlist_crud(client: AsyncClient) -> None:
    """Watchlists are created, listed, amended and deleted through the API."""
    created = await client.post(
        "/api/watchlists",
        json={"collectionName": "Desk list", "collectionSymbols": "AAPL, msft"},
    )
    assert created.status_code == 201
    watchlist = created.json()
    wid = watchlist["id"]
    assert watchlist["symbols"] == ["AAPL", "MSFT"]
    assert watchlist["is_default"] is True  # the first list becomes the default

    added = (await client.post(f"/api/watchlists/{wid}/symbols", json={"symbol": "brk.b"})).json()
    assert added["symbols"] == ["AAPL", "MSFT", "BRK-B"]

    removed = (await client.delete(f"/api/watchlists/{wid}/symbols/MSFT")).json()
    assert removed["symbols"] == ["AAPL", "BRK-B"]

    renamed = (await client.put(f"/api/watchlists/{wid}", json={"name": "Renamed"})).json()
    assert renamed["name"] == "Renamed"

    listed = (await client.get("/api/watchlists")).json()
    assert listed["count"] == 1
    assert listed["default"] == wid

    assert (await client.delete(f"/api/watchlists/{wid}")).status_code == 200
    assert (await client.get(f"/api/watchlists/{wid}")).status_code == 404


async def test_watchlist_reports_fields_it_cannot_store(client: AsyncClient) -> None:
    """The dialog's description/method/refresh are reported, not silently dropped."""
    body = (
        await client.post(
            "/api/watchlists",
            json={
                "collectionName": "With extras",
                "collectionSymbols": "AAPL",
                "collectionDescription": "some notes",
                "constructionMethod": "Screener result",
            },
        )
    ).json()
    assert body["ignored_fields"] == ["description", "construction_method"]


# ---------------------------------------------------------------------------
# ingestion
# ---------------------------------------------------------------------------
async def test_ingest_sources_lists_builtins_with_unknown_status(client: AsyncClient) -> None:
    """The five built-ins are catalogued; nothing claims to be connected yet."""
    body = (await client.get("/api/ingest/sources")).json()
    assert {s["id"] for s in body["sources"]} == BUILTIN_SOURCES
    assert all(s["state"]["status"] == "unknown" for s in body["sources"])
    assert body["summary"]["connected"] == 0
    assert body["summary"]["builtin"] == len(BUILTIN_SOURCES)


async def test_ingest_source_save_test_run_and_backfill(
    client: AsyncClient, bar_csv: Path
) -> None:
    """A CSV source is saved, probed, run and backfilled through the API."""
    saved = await client.post("/api/ingest/sources", json=csv_form(bar_csv))
    assert saved.status_code == 201
    body = saved.json()
    assert body["source"]["protocol"] == "file"
    assert body["job"]["source_id"] == "csv-bars"  # a 1-minute schedule is scheduled

    tested = (await client.post("/api/ingest/sources/csv-bars/test")).json()
    assert tested["state"]["status"] == "connected"
    assert tested["state"]["latency_ms"] is not None

    outcome = (await client.post("/api/ingest/sources/csv-bars/run")).json()
    assert outcome["ok"] is True
    assert outcome["rows_written"] == 2
    assert outcome["dataset"] == "bars"

    # The file ledger means a second run has nothing new to ingest.
    again = (await client.post("/api/ingest/sources/csv-bars/run")).json()
    assert again["ok"] is True
    assert again["rows_written"] == 0

    backfill = (
        await client.post("/api/ingest/sources/csv-bars/backfill?wait=true&range=1y")
    ).json()
    assert backfill["ok"] is True
    assert backfill["rows_written"] == 2  # backfill re-reads every matching file

    detail = (await client.get("/api/ingest/sources/csv-bars")).json()
    assert detail["state"]["rows_ingested"] == 4
    assert detail["state"]["last_ingest_at"] is not None


async def test_ingest_test_payload_reports_a_real_failure(
    client: AsyncClient, tmp_path: Path
) -> None:
    """Testing an unsaved source that matches nothing fails honestly."""
    payload = csv_form(tmp_path / "nowhere" / "*.csv", source_id="missing", name="Missing drop")
    body = (await client.post("/api/ingest/sources/test", json=payload)).json()
    assert body["ok"] is False
    assert body["error"]
    assert body["sample"] == []
    assert "state" not in body  # nothing was saved, so nothing was recorded

    # An unsaved source is not silently created by testing it.
    assert (await client.get("/api/ingest/sources/missing")).status_code == 404


async def test_ingest_form_validation_is_rejected(client: AsyncClient) -> None:
    """A protocol/endpoint mismatch is a 400 with the reason, not a 500."""
    response = await client.post(
        "/api/ingest/sources",
        json={
            "sourceName": "Bad websocket",
            "sourceProtocol": "WebSocket",
            "sourceEndpoint": "https://example.com/stream",
            "sourceFrequency": "Streaming",
        },
    )
    assert response.status_code == 400
    assert "ws://" in response.json()["detail"]["detail"]


async def test_builtin_source_cannot_be_deleted(client: AsyncClient) -> None:
    """Built-in sources are peers of user sources but are not deletable."""
    response = await client.delete("/api/ingest/sources/yfinance")
    assert response.status_code == 400


async def test_subscription_crud(client: AsyncClient) -> None:
    """Subscriptions are stored against a real source and can be removed."""
    created = await client.post(
        "/api/ingest/subscriptions",
        json={"source_id": "fred", "channel": "series", "scope": ["DGS10"]},
    )
    assert created.status_code == 201
    sub_id = created.json()["id"]

    listed = (await client.get("/api/ingest/subscriptions?source_id=fred")).json()
    assert listed["count"] == 1
    assert listed["subscriptions"][0]["scope"] == ["DGS10"]

    assert (await client.delete(f"/api/ingest/subscriptions/{sub_id}")).status_code == 200
    assert (await client.get("/api/ingest/subscriptions")).json()["count"] == 0

    unknown = await client.post(
        "/api/ingest/subscriptions", json={"source_id": "nope", "channel": "series"}
    )
    assert unknown.status_code == 404


async def test_subscription_granularity_round_trips_through_the_api(
    client: AsyncClient,
) -> None:
    """The frequency chip's value is stored, returned, and honestly labelled."""
    # yfinance is a library-driven bars source, so a bar size really is applied.
    applied = await client.post(
        "/api/ingest/subscriptions",
        json={"source_id": "yfinance", "channel": "bars", "granularity": "5m"},
    )
    assert applied.status_code == 201
    payload = applied.json()
    assert payload["granularity"] == "5m"
    assert payload["granularity_applied"] is True
    assert "5m bars" in payload["granularity_detail"]
    assert "1m" in payload["granularity_options"]

    listed = (await client.get("/api/ingest/subscriptions?source_id=yfinance")).json()
    assert listed["subscriptions"][0]["granularity"] == "5m"
    assert listed["subscriptions"][0]["granularity_applied"] is True

    # EDGAR is a filings feed: quarterly is recorded, and the API says it is advisory.
    advisory = await client.post(
        "/api/ingest/subscriptions",
        json={"source_id": "edgar", "channel": "filings", "granularity": "quarterly"},
    )
    assert advisory.status_code == 201
    assert advisory.json()["granularity"] == "quarterly"
    assert advisory.json()["granularity_applied"] is False
    assert "advisory" in advisory.json()["granularity_detail"]

    # A granularity the source's domain does not offer is refused, not stored.
    wrong = await client.post(
        "/api/ingest/subscriptions",
        json={"source_id": "yfinance", "channel": "bars", "granularity": "quarterly"},
    )
    assert wrong.status_code == 400
    unknown = await client.post(
        "/api/ingest/subscriptions",
        json={"source_id": "yfinance", "channel": "bars", "granularity": "1 fortnight"},
    )
    assert unknown.status_code == 400


async def test_granularity_applied_mirrors_what_the_scheduler_would_do(
    client: AsyncClient, bus: EventBus
) -> None:
    """``granularity_applied`` is true only when the fetch really gets the interval.

    The scheduler imposes a subscription's bar size on a source only when the
    subscription is active and every active subscription of that source agrees
    on one interval (``IngestScheduler.requested_interval()``). The API answers
    from that same rule, so the two are asserted against each other here rather
    than the API being trusted on its own word.
    """
    from tradinglib.data.ingest.catalog import SourceCatalog
    from tradinglib.data.ingest.scheduler import IngestScheduler

    catalog = SourceCatalog(bus)
    scheduler = IngestScheduler(catalog, bus)
    yfinance = catalog.get("yfinance")

    # 1. A paused subscription is never read: nothing of it reaches the fetch.
    paused = await client.post(
        "/api/ingest/subscriptions",
        json={
            "source_id": "yfinance",
            "channel": "bars-paused",
            "granularity": "5m",
            "active": False,
        },
    )
    assert paused.status_code == 201
    assert paused.json()["granularity_applied"] is False
    assert "paused" in paused.json()["granularity_detail"]
    assert scheduler.requested_interval(yfinance) is None

    # 2. One active subscription: the interval genuinely is imposed.
    active = await client.post(
        "/api/ingest/subscriptions",
        json={"source_id": "yfinance", "channel": "bars-a", "granularity": "5m"},
    )
    assert active.json()["granularity_applied"] is True
    assert scheduler.requested_interval(yfinance) == "5m"

    # 3. A second active subscription asking for a different bar size: the
    #    scheduler imposes neither, so neither may claim to be applied.
    clashing = await client.post(
        "/api/ingest/subscriptions",
        json={"source_id": "yfinance", "channel": "bars-b", "granularity": "1h"},
    )
    assert clashing.json()["granularity_applied"] is False
    assert "different bars" in clashing.json()["granularity_detail"]
    assert scheduler.requested_interval(yfinance) is None

    # The listing agrees with the creation responses, including for the
    # subscription that *was* applied until the disagreement appeared.
    listed = (await client.get("/api/ingest/subscriptions?source_id=yfinance")).json()
    rows = {row["channel"]: row for row in listed["subscriptions"]}
    assert {row["granularity_applied"] for row in rows.values()} == {False}
    assert "different bars" in rows["bars-a"]["granularity_detail"]
    assert "paused" in rows["bars-paused"]["granularity_detail"]

    # Removing the disagreement restores the applied claim - for the active
    # subscriptions only.
    await client.delete("/api/ingest/subscriptions/" + rows["bars-b"]["id"])
    again = (await client.get("/api/ingest/subscriptions?source_id=yfinance")).json()
    rows = {row["channel"]: row for row in again["subscriptions"]}
    assert rows["bars-a"]["granularity_applied"] is True
    assert rows["bars-paused"]["granularity_applied"] is False
    assert scheduler.requested_interval(yfinance) == "5m"


async def test_custom_poll_interval_is_validated_server_side(
    client: AsyncClient, bar_csv: Path
) -> None:
    """An interval the presets do not offer is accepted; nonsense is refused."""
    form = csv_form(bar_csv)
    form["schedule"] = "45s"
    saved = await client.post("/api/ingest/sources", json=form)
    assert saved.status_code == 201
    body = saved.json()
    assert body["source"]["schedule"] == "45s"
    assert body["source"]["schedule_detail"]["interval_seconds"] == 45.0
    assert body["source"]["schedule_detail"]["periodic"] is True
    assert body["job"] is not None, "a custom cadence must reach the scheduler"

    fetched = (await client.get("/api/ingest/sources/csv-bars")).json()
    assert fetched["schedule"] == "45s"

    form["schedule"] = "every other tuesday"
    rejected = await client.post("/api/ingest/sources", json=form)
    assert rejected.status_code == 400
    assert "schedule" in str(rejected.json()["detail"]).lower()


async def test_source_can_be_paused_and_resumed(client: AsyncClient, bar_csv: Path) -> None:
    """Pausing a source unschedules it; enabling it schedules it again."""
    await client.post("/api/ingest/sources", json=csv_form(bar_csv))
    paused = (
        await client.post("/api/ingest/sources/csv-bars/enabled", json={"enabled": False})
    ).json()
    assert paused["source"]["enabled"] is False
    assert paused["job"] is None
    assert "csv-bars" not in {
        job["source_id"] for job in (await client.get("/api/ingest/scheduler")).json()["jobs"]
    }

    resumed = (
        await client.post("/api/ingest/sources/csv-bars/enabled", json={"enabled": True})
    ).json()
    assert resumed["job"]["source_id"] == "csv-bars"


# ---------------------------------------------------------------------------
# gateway + status
# ---------------------------------------------------------------------------
async def test_gateway_and_status_reflect_real_probes(
    client: AsyncClient, bar_csv: Path
) -> None:
    """``/api/status`` carries the gateway's observed numbers, not guesses."""
    before = (await client.get("/api/status")).json()
    assert before["sources_connected"] == 0
    assert before["gateway_ms"] is None
    assert before["data_live"] is False
    assert before["universe"]["id"] == "us-quality-50"
    assert before["subsystems"]["ingest_gateway"] == "up"
    assert before["subsystems"]["alpaca_stream"] in {"not_configured", "up", "error"}

    await client.post("/api/ingest/sources", json=csv_form(bar_csv))
    await client.post("/api/ingest/sources/csv-bars/test")

    gateway = (await client.get("/api/ingest/gateway")).json()
    assert gateway["sources_connected"] == 1
    assert gateway["sources_total"] == len(BUILTIN_SOURCES) + 1
    assert gateway["latency_ms"] is not None
    assert {row["id"] for row in gateway["sources"]} == BUILTIN_SOURCES | {"csv-bars"}

    after = (await client.get("/api/status")).json()
    assert after["sources_connected"] == 1
    assert after["sources_total"] == len(BUILTIN_SOURCES) + 1
    assert after["gateway_ms"] == gateway["latency_ms"]
    assert after["delayed_series"] == 0


async def test_status_reports_subsystems_that_are_not_running(client: AsyncClient) -> None:
    """The scheduler is not autostarted in tests, and the payload says so."""
    body = (await client.get("/api/status")).json()
    assert body["subsystems"]["ingest_scheduler"] == "not_started"
    assert body["engine_health"] == "degraded"
    assert "ingest_scheduler" in body["degraded_subsystems"]
    assert body["trials_logged"] == 0
    assert body["spend"]["used"] == 0.0


# ---------------------------------------------------------------------------
# quality
# ---------------------------------------------------------------------------
async def test_quality_summary_is_empty_before_any_run(client: AsyncClient) -> None:
    """Nothing pretends to be 97.4% before a check has ever run."""
    body = (await client.get("/api/quality/summary")).json()
    assert body["overall_quality_pct"] is None
    assert body["datasets"] == 0
    assert body["last_run"] is None
    assert body["datasets_in_store"] == 0


async def test_quality_run_over_ingested_bars(client: AsyncClient, bar_csv: Path) -> None:
    """Real ingested bars are checked, scored and recorded as an incident."""
    await client.post("/api/ingest/sources", json=csv_form(bar_csv))
    assert (await client.post("/api/ingest/sources/csv-bars/run")).json()["rows_written"] == 2

    datasets = (await client.get("/api/quality/datasets")).json()
    assert [ref["key"] for ref in datasets["datasets"]] == ["bars:AAPL:1d"]

    run = (await client.post("/api/quality/run", json={})).json()
    assert run["count"] == 1
    result = run["results"][0]
    assert result["dataset"] == "bars:AAPL:1d"
    assert {check["check"] for check in result["checks"]} == {
        "completeness",
        "staleness",
        "outliers",
        "duplicates",
        "schema_drift",
        "lookahead",
    }
    completeness = next(c for c in result["checks"] if c["check"] == "completeness")
    assert completeness["metrics"]["observed"] == 2

    summary = (await client.get("/api/quality/summary")).json()
    assert summary["datasets"] == 1
    assert summary["last_run"] is not None

    series = (await client.get("/api/quality/series?dataset=bars:AAPL:1d")).json()
    assert series["points"] == 1
    assert series["score"] == result["score"]

    incidents = (await client.get("/api/quality/incidents")).json()
    # The fixture's newest bar is months old, so staleness legitimately fails.
    assert incidents["count"] >= 1
    incident = incidents["incidents"][0]
    assert incident["status"] == "open"

    resolved = (
        await client.post(f"/api/quality/incidents/{incident['id']}/resolve")
    ).json()
    assert resolved["status"] == "resolved"
    assert (await client.get("/api/quality/incidents")).json()["count"] == incidents["count"] - 1


async def test_outlier_can_be_escalated_over_the_api(
    client: AsyncClient, settings: Settings
) -> None:
    """POST .../decide accepts 'escalate' and the point stays in the queue."""
    from datetime import date, datetime, timezone

    from tradinglib.data.base import Bar, BarInterval
    from tradinglib.data.quality import business_days
    from tradinglib.data.store import TimeSeriesStore

    days = business_days(date(2024, 1, 2), date(2026, 8, 28))
    closes = [100.0 + 0.05 * index for index in range(len(days))]
    closes[150] = closes[150] * 11.0  # a spike no reviewer can classify at a glance
    store = TimeSeriesStore(settings.ts_dir)
    store.write_bars(
        [
            Bar(
                symbol="ESCAPI",
                ts=datetime(day.year, day.month, day.day, tzinfo=timezone.utc),
                open=price,
                high=price * 1.01,
                low=price * 0.99,
                close=price,
                volume=1_000_000.0,
                interval=BarInterval.DAY_1,
                source="fixture",
            )
            for day, price in zip(days, closes, strict=True)
        ]
    )

    run = await client.post("/api/quality/run", json={"datasets": ["bars:ESCAPI:1d"]})
    assert run.status_code == 200

    queue = (await client.get("/api/quality/outliers")).json()
    assert queue["count"] >= 1, "the spike must reach the review queue"
    flag = queue["outliers"][0]
    stored_before = len(store.read_bars("ESCAPI"))

    decided = await client.post(
        f"/api/quality/outliers/{flag['id']}/decide",
        json={"decision": "escalate", "by": "reviewer"},
    )
    assert decided.status_code == 200
    body = decided.json()
    assert body["outlier"]["state"] == "escalated"
    assert body["outlier"]["needs_review"] is True
    assert body["counts"]["escalated"] == 1
    assert body["counts"]["quarantined"] == 0

    # Escalation quarantines nothing: the value is still readable.
    assert len(store.read_bars("ESCAPI")) == stored_before

    # And the point is still queued for review.
    open_only = (await client.get("/api/quality/outliers?include_decided=false")).json()
    assert flag["id"] in {row["id"] for row in open_only["outliers"]}

    bad = await client.post(
        f"/api/quality/outliers/{flag['id']}/decide", json={"decision": "shrug"}
    )
    assert bad.status_code == 422


# ---------------------------------------------------------------------------
# WebSocket channels
# ---------------------------------------------------------------------------
def test_status_channel_delivers_the_datastrip(
    settings: Settings, bus: EventBus, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A client subscribed to ``status`` receives real datastrip frames."""
    monkeypatch.setenv("TRADINGLIB_STATUS_INTERVAL", "0.05")
    from tradinglib.api.app import create_app

    app = create_app(settings, bus=bus)
    with TestClient(app) as test_client:
        with test_client.websocket_connect("/ws") as socket:
            assert socket.receive_json()["type"] == "welcome"
            socket.send_text(json.dumps({"action": "subscribe", "channel": "status"}))
            assert socket.receive_json()["type"] == "subscribed"

            for _ in range(20):
                frame = socket.receive_json()
                if frame.get("type") == "event" and frame["payload"].get("event") == "datastrip":
                    break
            else:  # pragma: no cover - only on a broken broadcaster
                pytest.fail("no datastrip frame arrived on the status channel")

            payload = frame["payload"]
            assert frame["channel"] == "status"
            assert payload["engine_health"] in {"healthy", "degraded"}
            assert payload["sources_total"] == len(BUILTIN_SOURCES)
            assert isinstance(payload["changed"], list)


def test_ingest_and_quality_events_reach_a_subscriber(settings: Settings, bus: EventBus) -> None:
    """Catalog work publishes on ``ingest``; the hub fans it out to browsers."""
    from tradinglib.api.app import create_app

    app = create_app(settings, bus=bus)
    with TestClient(app) as test_client:
        with test_client.websocket_connect("/ws") as socket:
            socket.receive_json()
            socket.send_text(json.dumps({"action": "subscribe", "channel": "ingest"}))
            assert socket.receive_json()["type"] == "subscribed"

            response = test_client.post(
                "/api/ingest/subscriptions",
                json={"source_id": "fred", "channel": "series", "scope": ["DGS10"]},
            )
            assert response.status_code == 201

            frame = socket.receive_json()
            assert frame["channel"] == "ingest"
            assert frame["payload"]["event"] == "subscription_saved"


# ---------------------------------------------------------------------------
# lifespan wiring
# ---------------------------------------------------------------------------
class _RecordingConnector(Connector):
    """A connector that answers without a network, for lifespan tests."""

    protocol = "recording"
    calls: list[str] = []

    async def connect(self) -> None:
        """No resources to open."""

    async def close(self) -> None:
        """No resources to release."""

    async def test(self) -> ConnectionTestResult:
        """Report a successful probe with one sample record."""
        return ConnectionTestResult.success(
            "injected connector", latency_ms=1.0, records=[{"ok": True}]
        )

    async def fetch(self, **params: Any) -> FetchResult:
        """Record the call and return an honest empty batch."""
        type(self).calls.append(self.config.id)
        return FetchResult.empty(dataset="none")

    async def status(self) -> SourceStatus:
        """Report the connector as connected."""
        return SourceStatus.CONNECTED


async def test_lifespan_starts_and_stops_the_scheduler(
    settings: Settings, bus: EventBus
) -> None:
    """With autostart on, the lifespan schedules the built-ins and stops cleanly."""
    from tradinglib.api.app import create_app

    _RecordingConnector.calls = []
    app: FastAPI = create_app(
        settings,
        bus=bus,
        autostart_ingest=True,
        connector_factory=lambda config: _RecordingConnector(config),
    )
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as http:
            scheduler = app.state.ingest_scheduler
            assert scheduler.running is True
            body = (await http.get("/api/status")).json()
            assert body["subsystems"]["ingest_scheduler"] == "up"
            assert body["subsystems"]["source_catalog"] == "up"

            jobs = (await http.get("/api/ingest/scheduler")).json()
            # The polling sources run on a period; alpaca streams instead, so
            # it is the one built-in without a scheduled job.
            assert {job["source_id"] for job in jobs["jobs"]} == BUILTIN_SOURCES - {"alpaca"}
            states = (await http.post("/api/ingest/refresh")).json()
            assert states["connected"] == len(BUILTIN_SOURCES)
            assert (await http.get("/api/status")).json()["data_live"] is True

    assert app.state.ingest_scheduler.running is False
    # The built-in with a scope (yfinance -> the benchmark) was actually fetched.
    assert "yfinance" in _RecordingConnector.calls


async def test_routes_report_503_before_the_lifespan_runs(app: FastAPI) -> None:
    """Without a lifespan nothing is running, and the API says so."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http:
        response = await http.get("/api/universes")
        assert response.status_code == 503
        assert response.json()["detail"]["subsystem"] == "universe_registry"
        assert (await http.get("/api/status")).json()["subsystems"]["source_catalog"] == (
            "not_started"
        )


# ---------------------------------------------------------------------------
# scope resolution
# ---------------------------------------------------------------------------
async def test_scope_label_resolves_through_the_universe_registry(
    app: FastAPI, client: AsyncClient
) -> None:
    """A source scoped to "US Quality 50" ingests that universe's real members."""
    from tradinglib.api.scope import resolve_scope_label

    registry = app.state.universe_registry
    assert len(resolve_scope_label(registry, "US Quality 50")) == 50
    assert "AAPL" in resolve_scope_label(registry, "us-quality-50")
    assert resolve_scope_label(registry, "Custom symbols…") == []
    assert resolve_scope_label(registry, "All available symbols") == []
    assert resolve_scope_label(registry, "Nothing named this") == []


async def test_scheduler_uses_the_scope_resolver(app: FastAPI, client: AsyncClient) -> None:
    """The scheduler resolves a universe label instead of reporting no scope."""
    scheduler = app.state.ingest_scheduler
    config = SourceConfig.from_form(
        {
            "sourceName": "Scoped provider",
            "id": "scoped",
            "sourceProtocol": "Python library",
            "provider": "yfinance",
            "sourceEndpoint": "yfinance",
            "sourceFrequency": "Daily",
            "sourceScope": "US Quality 50",
        }
    )
    assert config.scope == []
    assert len(scheduler.resolve_scope(config)) == 50


# ---------------------------------------------------------------------------
# error translation
# ---------------------------------------------------------------------------
def test_translate_errors_maps_domain_exceptions() -> None:
    """Domain exceptions become the right HTTP status instead of a 500."""
    from fastapi import HTTPException

    from tradinglib.api.deps import translate_errors
    from tradinglib.data.base import (
        ProviderNotConfigured,
        ProviderRateLimited,
        ProviderUnavailable,
        SymbolNotFound,
    )

    cases: list[tuple[Exception, int]] = [
        (KeyError("unknown universe 'nope'"), 404),
        (SymbolNotFound("yfinance", "NOSUCH"), 404),
        (ValueError("endpoint is required"), 400),
        (ProviderNotConfigured("alpaca", "no credentials"), 503),
        (ProviderUnavailable("fred", "HTTP 500"), 503),
        (ProviderRateLimited("trends", "quota", retry_after=30), 429),
    ]
    for exc, expected in cases:
        with pytest.raises(HTTPException) as caught:
            with translate_errors("thing"):
                raise exc
        assert caught.value.status_code == expected, exc
        assert caught.value.detail["entity"] == "thing"

    with pytest.raises(HTTPException) as caught:
        with translate_errors("thing"):
            raise ProviderRateLimited("trends", "quota", retry_after=30)
    assert caught.value.headers == {"Retry-After": "30"}

    # A KeyError's message is unwrapped rather than shown as a quoted repr.
    with pytest.raises(HTTPException) as caught:
        with translate_errors("universe"):
            raise KeyError("unknown universe 'nope'")
    assert caught.value.detail["detail"] == "unknown universe 'nope'"
