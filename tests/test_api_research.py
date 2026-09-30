"""API tests for the research domain: Stock Research, screener and Strategy Lab.

Everything here runs offline and against the real engines:

* the Stock Research routes are served by a :class:`CompanyResearch` wired to
  the same deterministic stub providers the engine's own tests use, so the
  routes are exercised without a vendor;
* the screener routes run over a stub metric panel but the real rule engine,
  the real SQLite persistence and the real backtest hand-off - "Backtest this
  screen" therefore creates a genuine Strategy Lab record here, and the test
  waits for that backtest to finish and reads its equity curve back;
* the Strategy Lab routes run over synthetic-but-real bars written into the
  application's own Parquet store, so backtests, gates and the evaluation views
  all execute their production code paths.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from typing import Any

import numpy as np
import pandas as pd
import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from starlette.testclient import TestClient

from tradinglib.config import Settings
from tradinglib.data.base import Bar, BarInterval
from tradinglib.data.cache import DataCache
from tradinglib.events import EventBus
from tradinglib.research.company import CompanyResearch
from tradinglib.research.screen.funnel import FunnelEvaluator, ScreenRegistry
from tests.test_research_company import (
    StubEdgarProvider,
    StubMarketProvider,
    StubTrendsProvider,
)
from tests.test_research_screen import StubBuilder, synthetic_frame

#: Symbols the screener's rules let through, plus the benchmark. Bars for these
#: are written into the store so the queued backtest has real prices to run on.
BACKTEST_SYMBOLS = ("AAA", "BBB", "SPY")

#: Business days of synthetic history seeded into the store (~2.4 years, which
#: is long enough for the minimum-history gate to be measurable).
SEEDED_BARS = 600


def _seed_store(store: Any, symbols: tuple[str, ...] = BACKTEST_SYMBOLS) -> dict[str, pd.Series]:
    """Write deterministic daily bars for ``symbols`` into the store.

    The paths are synthetic but the bars are real rows in the real Parquet
    store: the backtester reads them through DuckDB exactly as it reads
    ingested vendor data.
    """
    index = pd.bdate_range(end=pd.Timestamp(date.today(), tz="UTC"), periods=SEEDED_BARS)
    rng = np.random.default_rng(20260828)
    drift = {"AAA": 0.0009, "BBB": 0.0007, "SPY": 0.0003, "LOSE": -0.0011}
    paths: dict[str, pd.Series] = {}
    for symbol in symbols:
        shocks = rng.normal(drift.get(symbol, 0.0004), 0.008, size=SEEDED_BARS)
        series = pd.Series(100.0 * np.exp(np.cumsum(shocks)), index=index, name=symbol)
        paths[symbol] = series
        store.write_bars(
            [
                Bar(
                    symbol=symbol,
                    ts=ts.to_pydatetime(),
                    open=float(value),
                    high=float(value) * 1.003,
                    low=float(value) * 0.997,
                    close=float(value),
                    volume=1_000_000.0,
                    interval=BarInterval.DAY_1,
                    source="synthetic-test",
                )
                for ts, value in series.items()
            ]
        )
    return paths


@pytest.fixture()
def rclient(app: FastAPI, client: AsyncClient, tmp_path_factory: pytest.TempPathFactory) -> AsyncClient:
    """A client whose application has its vendor edges replaced by stubs.

    Only the vendor edges change: the routes, the research engines, the rule
    engine, the SQLite persistence, the Parquet store and the backtester are
    all the real ones. The fixture runs after the lifespan has built the real
    services, so it also proves those services are replaceable in place.
    """
    cache_dir = tmp_path_factory.mktemp("research-cache")
    app.state.company_research = CompanyResearch(
        market=StubMarketProvider(),
        edgar=StubEdgarProvider(),
        trends=StubTrendsProvider(),
        cache=DataCache(cache_dir, default_ttl=60.0),
        store=app.state.store,
        universes=app.state.universe_registry,
    )
    registry = app.state.universe_registry
    registry.create(
        "Test Universe",
        [
            {"symbol": symbol, "sector": sector}
            for symbol, sector in synthetic_frame()["sector"].items()
        ],
        universe_id="test-universe",
    )
    app.state.funnel_evaluator = FunnelEvaluator(
        registry=ScreenRegistry(),
        universes=registry,
        builder=StubBuilder(synthetic_frame()),  # type: ignore[arg-type]
        bus=app.state.bus,
    )
    _seed_store(app.state.store)
    return client


# ---------------------------------------------------------------------------
# Stock Research
# ---------------------------------------------------------------------------
async def test_company_payload_loads_any_ticker(rclient: AsyncClient) -> None:
    """A symbol in no watchlist still loads a complete overview payload."""
    response = await rclient.get("/api/research/company/GRAB")
    assert response.status_code == 200
    body = response.json()
    assert body["symbol"] == "GRAB"
    assert body["profile"]["symbol"] == "GRAB"
    assert body["quote"]["price"] == pytest.approx(39.5)
    # Derived, never zero-filled: every metric carries a value or a reason.
    metrics = body["metrics"]
    assert metrics["revenue_ttm"]["value"] == pytest.approx(4_600.0)
    assert metrics["gross_margin"]["value"] is not None
    assert body["freshness"]["quote"]["source"] == "stub-market"


async def test_unknown_symbol_is_a_404(rclient: AsyncClient) -> None:
    """An unknown ticker is the one thing that 404s; an outage is not."""
    response = await rclient.get("/api/research/company/NOPE")
    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "not_found"


async def test_price_range_is_served_and_validated(rclient: AsyncClient) -> None:
    """``/price`` returns real bars for a valid range and rejects an invalid one."""
    ok = await rclient.get("/api/research/company/TEST/price", params={"range": "1m"})
    assert ok.status_code == 200
    payload = ok.json()
    assert payload["count"] > 0
    assert payload["points"][0]["close"] is not None

    bad = await rclient.get("/api/research/company/TEST/price", params={"range": "17y"})
    assert bad.status_code == 400
    assert "17y" in bad.json()["detail"]["detail"]


async def test_every_company_subresource_answers(rclient: AsyncClient) -> None:
    """Financials, estimates, valuation, news, alt data, filings and freshness."""
    for suffix in (
        "financials",
        "estimates",
        "valuation",
        "news",
        "altdata",
        "filings",
        "freshness",
    ):
        response = await rclient.get(f"/api/research/company/TEST/{suffix}")
        assert response.status_code == 200, suffix
        assert response.json(), suffix

    news = (await rclient.get("/api/research/company/TEST/news")).json()
    assert news["count"] == 2
    assert news["aggregate"]["mean_compound"] is not None

    alt = (await rclient.get("/api/research/company/TEST/altdata")).json()
    # The two interest-derived tiles are sourced from Google Trends and must
    # state the proxy they use; a value without a stated basis would be exactly
    # the kind of unattributed number the spec forbids.
    for key in ("app_rank_composite", "web_traffic_yoy"):
        signal = alt["signals"][key]
        assert signal["basis"], key
        if signal["value"] is None:
            assert signal["reason"], key
        else:
            assert signal["source"] == "trends", key
    # Job postings genuinely have no vendor behind them and must say so.
    assert alt["signals"]["job_postings_90d"]["value"] is None
    assert alt["signals"]["job_postings_90d"]["reason"]

    filings = (await rclient.get("/api/research/company/TEST/filings")).json()
    assert filings["count"] == 2
    assert filings["form_counts"] == {"10-K": 1, "10-Q": 1}


async def test_resolve_finds_symbols(rclient: AsyncClient) -> None:
    """The symbol form's lookup returns the vendor's matches."""
    response = await rclient.get("/api/research/resolve", params={"q": "test"})
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["results"][0]["symbol"] == "TEST"


async def test_ranges_are_published(rclient: AsyncClient) -> None:
    """The chart range keys come from the engine, not from the UI's imagination."""
    body = (await rclient.get("/api/research/ranges")).json()
    keys = {row["key"] for row in body["ranges"]}
    assert {"1d", "1m", "1y", "max"} <= keys


# ---------------------------------------------------------------------------
# screener
# ---------------------------------------------------------------------------
async def _create_screen(client: AsyncClient) -> str:
    """Create the screen used by the screener tests and return its id."""
    response = await client.post(
        "/api/screens",
        json={
            "name": "Quality growth",
            "universe_id": "test-universe",
            "rules": [
                {"field": "mkt_cap", "operator": ">", "value": 2e9, "label": "Cap floor"},
                {"field": "rev_growth", "operator": ">", "value": 20.0},
            ],
            "rank_by": "rev_growth",
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def test_metric_catalogue_is_served(rclient: AsyncClient) -> None:
    """The rule builder's metric list is the engine's own catalogue."""
    body = (await rclient.get("/api/screens/metrics")).json()
    assert body["count"] == len(body["metrics"]) > 20
    keys = {row["key"] for row in body["metrics"]}
    assert {"mkt_cap", "rev_growth", "mom_12_1", "composite"} <= keys
    assert ">" in body["operators"] and "top_pctl" in body["operators"]


async def test_screen_crud_and_query_rendering(rclient: AsyncClient) -> None:
    """A screen round-trips through the API and renders its query text."""
    screen_id = await _create_screen(rclient)
    listed = (await rclient.get("/api/screens")).json()
    assert screen_id in {row["id"] for row in listed["screens"]}

    fetched = (await rclient.get(f"/api/screens/{screen_id}")).json()
    assert 'screen(universe="test-universe")' in fetched["query"]
    assert ".filter(rev_growth > 20.0)" in fetched["query"]
    assert fetched["active_rule_count"] == 2

    updated = await rclient.put(
        f"/api/screens/{screen_id}", json={"description": "cap and growth floor"}
    )
    assert updated.json()["description"] == "cap and growth floor"

    assert (await rclient.delete(f"/api/screens/{screen_id}")).status_code == 200
    assert (await rclient.get(f"/api/screens/{screen_id}")).status_code == 404


async def test_screen_run_returns_a_real_funnel(rclient: AsyncClient) -> None:
    """Running a screen returns per-rule counts, survivors and statistics."""
    screen_id = await _create_screen(rclient)
    response = await rclient.post(f"/api/screens/{screen_id}/run", json={})
    assert response.status_code == 200, response.text
    run = response.json()
    assert run["base_count"] == 8
    assert [stage["output_count"] for stage in run["stages"]] == [7, 2]
    assert run["symbols"] == ["AAA", "BBB"]
    assert run["stats"]["count"] == 2
    assert run["stats"]["turnover"] is None  # first run: nothing to compare with

    stored = (await rclient.get(f"/api/screens/{screen_id}/runs")).json()
    assert stored["count"] == 1
    assert stored["runs"][0]["id"] == run["id"]


async def test_preview_counts_an_unsaved_rule_set(rclient: AsyncClient) -> None:
    """The live count while editing rules comes from the same engine."""
    response = await rclient.post(
        "/api/screens/preview",
        json={
            "universe_id": "test-universe",
            "rules": [{"field": "mkt_cap", "operator": ">", "value": 2e9}],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["base_count"] == 8
    assert body["count"] == 7


async def test_queue_backtest_creates_a_real_lab_record(rclient: AsyncClient) -> None:
    """"Backtest this screen" creates - and runs - a genuine lab backtest."""
    screen_id = await _create_screen(rclient)
    assert (await rclient.post(f"/api/screens/{screen_id}/run", json={})).status_code == 200

    queued = await rclient.post(f"/api/screens/{screen_id}/queue-backtest", json={})
    assert queued.status_code == 201, queued.text
    record = queued.json()
    assert record["status"] in {"queued", "running", "complete"}
    backtest_id = record["backtest_id"]
    assert backtest_id, record

    listed = (await rclient.get("/api/lab/backtests")).json()
    assert backtest_id in {row["id"] for row in listed["backtests"]}
    assert listed["backtests"][0]["screen_id"] == screen_id

    finished = await _await_backtest(rclient, backtest_id)
    assert finished["status"] == "complete", finished.get("error")
    assert finished["symbols"] == ["AAA", "BBB"]

    results = (await rclient.get(f"/api/lab/backtests/{backtest_id}/results")).json()
    curve = results["equity_curve"]
    assert len(curve["equity"]) > 100
    assert len(curve["dates"]) == len(curve["equity"])
    assert curve["equity"][0] == pytest.approx(curve["initial_capital"])
    assert results["stats"]["sharpe"] is not None


async def test_queue_backtest_without_a_run_is_refused(rclient: AsyncClient) -> None:
    """A screen that has never run has no symbol list, and the API says so."""
    screen_id = await _create_screen(rclient)
    response = await rclient.post(f"/api/screens/{screen_id}/queue-backtest", json={})
    assert response.status_code == 400
    assert "run it before queueing" in response.json()["detail"]["detail"]


async def _await_backtest(
    client: AsyncClient, backtest_id: str, *, timeout: float = 60.0
) -> dict[str, Any]:
    """Poll a backtest record until it leaves the queued/running states."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        record = (await client.get(f"/api/lab/backtests/{backtest_id}")).json()
        if record["status"] not in {"queued", "running"}:
            return record
        if asyncio.get_running_loop().time() > deadline:  # pragma: no cover - timing only
            pytest.fail(f"backtest {backtest_id} never finished: {record['status']}")
        await asyncio.sleep(0.1)


# ---------------------------------------------------------------------------
# Strategy Lab
# ---------------------------------------------------------------------------
def _static_config(name: str, symbols: list[str]) -> dict[str, Any]:
    """A static equal-weighted basket over seeded symbols."""
    return {
        "name": name,
        "symbols": symbols,
        "signal": {"kind": "static", "symbols": symbols, "weighting": "equal"},
        "benchmark": "SPY",
        "rebalance": "monthly",
        "oos_fraction": 0.3,
    }


async def test_hypothesis_lifecycle(rclient: AsyncClient) -> None:
    """Create, read, transition and delete a hypothesis through the API."""
    missing = await rclient.post(
        "/api/lab/hypotheses",
        json={"title": "Momentum works", "statement": "Winners win", "falsifier": "  "},
    )
    assert missing.status_code == 400
    assert "falsifier" in missing.json()["detail"]["detail"]

    created = await rclient.post(
        "/api/lab/hypotheses",
        json={
            "title": "Quality growth outperforms",
            "statement": "High-growth, high-margin names beat the benchmark.",
            "falsifier": "OOS Sharpe below 0.8 after costs",
            "owner": "ana",
            "tags": ["quality"],
        },
    )
    assert created.status_code == 201, created.text
    hypothesis_id = created.json()["id"]

    evidence = await rclient.post(
        f"/api/lab/hypotheses/{hypothesis_id}/evidence",
        json={"kind": "paper", "ref": "Novy-Marx (2013)", "label": "quality"},
    )
    assert evidence.status_code == 201

    listed = (await rclient.get("/api/lab/hypotheses")).json()
    assert listed["count"] == 1
    assert listed["summary"]["by_status"]["draft"] == 1
    assert listed["summary"]["total"] == 1

    moved = await rclient.post(
        f"/api/lab/hypotheses/{hypothesis_id}/status",
        json={"status": "testing", "note": "backtest queued"},
    )
    assert moved.status_code == 200
    assert moved.json()["status"] == "testing"

    illegal = await rclient.post(
        f"/api/lab/hypotheses/{hypothesis_id}/status", json={"status": "draft"}
    )
    assert illegal.status_code == 400

    detail = (await rclient.get(f"/api/lab/hypotheses/{hypothesis_id}")).json()
    assert detail["stats"]["experiments"] == 0
    assert len(detail["evidence"]) == 1

    assert (await rclient.delete(f"/api/lab/hypotheses/{hypothesis_id}")).status_code == 200
    assert (await rclient.get(f"/api/lab/hypotheses/{hypothesis_id}")).status_code == 404


async def test_backtest_run_and_results_over_the_api(rclient: AsyncClient) -> None:
    """``POST /api/lab/backtests/run`` executes a real backtest end to end."""
    response = await rclient.post(
        "/api/lab/backtests/run",
        json={"config": _static_config("api-static", ["AAA", "BBB"])},
    )
    assert response.status_code == 201, response.text
    record = response.json()
    assert record["status"] == "complete"
    assert record["stats"]["periods"] > 100
    assert record["out_of_sample"]["periods"] > 0

    results = (await rclient.get(f"/api/lab/backtests/{record['id']}/results")).json()
    curve = results["equity_curve"]
    assert len(curve["equity"]) == record["stats"]["periods"] + 1
    assert len(curve["dates"]) == len(curve["equity"])
    assert curve["benchmark_symbol"] == "SPY"

    # A configuration naming a symbol the store has never seen fails honestly.
    broken = await rclient.post(
        "/api/lab/backtests/run",
        json={"config": _static_config("api-missing", ["NOSUCHSYMBOL"])},
    )
    assert broken.status_code == 400


async def test_experiment_definition_and_run(rclient: AsyncClient) -> None:
    """An experiment runs a real backtest plus a real IC study, and logs it."""
    hypothesis = (
        await rclient.post(
            "/api/lab/hypotheses",
            json={
                "title": "Static basket beats the tape",
                "statement": "An equal-weighted basket of the survivors beats SPY.",
                "falsifier": "It trails SPY out of sample",
            },
        )
    ).json()

    created = await rclient.post(
        "/api/lab/experiments",
        json={
            "name": "static-basket",
            "hypothesis_id": hypothesis["id"],
            "config": _static_config("static-basket", ["AAA", "BBB"]),
            "criteria": {"min_oos_sharpe": 0.5, "max_drawdown": 0.4, "min_trades": 1},
            "horizon": 21,
        },
    )
    assert created.status_code == 201, created.text
    experiment_id = created.json()["id"]

    run = await rclient.post(f"/api/lab/experiments/{experiment_id}/run")
    assert run.status_code == 200, run.text
    body = run.json()
    assert body["status"] == "complete"
    assert body["backtest_id"]
    assert body["log"], "the run log must be recorded, not just streamed"
    assert body["results"]["ic"]["horizon"] == 21
    assert "passed_gates" in body["results"]

    runs = (await rclient.get(f"/api/lab/experiments/{experiment_id}/runs")).json()
    assert runs["count"] == 1
    single = (await rclient.get(f"/api/lab/experiments/runs/{body['id']}")).json()
    assert single["id"] == body["id"]
    assert len(single["log"]) == body["log_lines"]


async def test_validation_gates_and_blocked_promotion(rclient: AsyncClient) -> None:
    """The gate suite is served, evaluated for real, and blocks a bad promotion."""
    suite = (await rclient.get("/api/lab/validation/gates")).json()
    assert suite["count"] >= 8
    assert "min_history" in {gate["id"] for gate in suite["gates"]}
    assert suite["hard_gates"], "at least one gate must be non-overridable"

    started = await rclient.post(
        "/api/lab/backtests/run",
        json={"config": _static_config("api-gates", ["AAA", "BBB"])},
    )
    backtest_id = started.json()["id"]

    pending = (await rclient.get(f"/api/lab/validation/{backtest_id}/gates")).json()
    assert pending["evaluated"] is False
    assert pending["report"] is None

    report = await rclient.post(f"/api/lab/validation/{backtest_id}")
    assert report.status_code == 201, report.text
    payload = report.json()
    assert payload["verdict"] in {"pass", "conditional", "fail"}
    assert len(payload["gates"]) == suite["count"]
    observed = {gate["id"]: gate for gate in payload["gates"]}
    assert observed["min_trades"]["observed"] is not None

    after = (await rclient.get(f"/api/lab/validation/{backtest_id}/gates")).json()
    assert after["evaluated"] is True
    assert after["report"]["id"] == payload["id"]

    if payload["verdict"] != "pass":
        blocked = await rclient.post(f"/api/lab/validation/{backtest_id}/promote", json={})
        assert blocked.status_code == 409
        assert "blocked" in blocked.json()["detail"]["detail"]

    reports = (await rclient.get("/api/lab/validation")).json()
    assert reports["count"] == 1


async def test_hard_gate_cannot_be_overridden(rclient: AsyncClient) -> None:
    """Overriding a hard gate is refused with 409, whatever the note says."""
    started = await rclient.post(
        "/api/lab/backtests/run",
        json={"config": _static_config("api-hard-gate", ["AAA"])},
    )
    backtest_id = started.json()["id"]
    suite = (await rclient.get("/api/lab/validation/gates")).json()
    hard = suite["hard_gates"][0]

    response = await rclient.post(
        f"/api/lab/validation/{backtest_id}/promote",
        json={"overrides": [hard], "comment": "shipping anyway"},
    )
    assert response.status_code == 409
    assert hard in response.json()["detail"]["detail"] or "blocked by" in (
        response.json()["detail"]["detail"]
    )


async def test_evaluation_views_are_honest_when_empty(rclient: AsyncClient) -> None:
    """The evaluation payload counts real rows and shows zeros before any work."""
    body = (await rclient.get("/api/lab/evaluation")).json()
    assert body["summary"]["promoted"]["total"] == 0
    assert body["summary"]["runs"]["backtest_runs"] == 0
    assert body["candidates"] == []
    assert body["tracking"] == []
    assert body["funnel"]["total"] == 0
    assert {row["stage"] for row in body["funnel"]["stages"]} >= {"research", "paper"}

    lab = (await rclient.get("/api/lab")).json()
    assert lab["experiments"] == 0
    assert lab["backtests"] == 0


async def test_lab_routes_report_503_without_a_lifespan(app: FastAPI) -> None:
    """Before the lifespan runs nothing is wired, and the API says so."""
    from httpx import ASGITransport

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http:
        for path in ("/api/research/company/AAPL", "/api/screens", "/api/lab/hypotheses"):
            response = await http.get(path)
            assert response.status_code == 503, path
            assert response.json()["detail"]["error"] == "subsystem_unavailable"


# ---------------------------------------------------------------------------
# WebSocket: lab progress
# ---------------------------------------------------------------------------
def test_lab_progress_reaches_both_channels(settings: Settings, bus: EventBus) -> None:
    """Lab events are subscribable on ``lab`` and mirrored onto ``runs``."""
    from tradinglib.api.app import create_app

    app = create_app(settings, bus=bus)
    with TestClient(app) as test_client:
        with test_client.websocket_connect("/ws") as socket:
            welcome = socket.receive_json()
            assert "lab" in welcome["channels"]
            socket.send_text(json.dumps({"action": "subscribe", "channel": "lab"}))
            assert socket.receive_json()["type"] == "subscribed"
            socket.send_text(json.dumps({"action": "subscribe", "channel": "runs"}))
            assert socket.receive_json()["type"] == "subscribed"

            store = app.state.store
            _seed_store(store, ("AAA", "SPY"))
            response = test_client.post(
                "/api/lab/backtests/run",
                json={"config": _static_config("ws-run", ["AAA"])},
            )
            assert response.status_code == 201, response.text

            frames: list[dict[str, Any]] = []
            for _ in range(8):
                frame = socket.receive_json()
                if frame.get("type") == "event":
                    frames.append(frame)
                if len(frames) >= 4:
                    break

    channels = {frame["channel"] for frame in frames}
    assert {"lab", "runs"} <= channels
    started = [f for f in frames if f["payload"].get("phase") == "started"]
    assert started and started[0]["payload"]["kind"] == "backtest"
    mirrored = [f for f in frames if f["channel"] == "runs"]
    assert mirrored and mirrored[0]["payload"]["source"] == "lab"
