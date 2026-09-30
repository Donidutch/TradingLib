"""Regression tests for the round-2 verification findings.

Each test here pins one behaviour that was reported broken or missing:

* the job-postings vendor behind the Alternative Data tile (C24);
* the FROM/TO backfill window that reaches the connectors (F42);
* pinned-screen membership alerting (F30);
* the measured Research thesis catalysts and risks (C10);
* the workspace inventory that keeps an agent stage from inventing ids;
* the ``Protocol.can_backfill`` capability the Source Catalog reads.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from tradinglib.agents.pipeline import Stage
from tradinglib.agents.runner import build_stage_system_prompt
from tradinglib.data.ingest.connectors.base import FetchResult
from tradinglib.data.ingest.source import BackfillConfig, Protocol
from tradinglib.data.providers.jobs_provider import (
    JobPostings,
    JobPostingsProvider,
    slug_candidates,
)
from tradinglib.research.company import Metric, thesis_items
from tradinglib.research.screen.alerts import ALERT_TAG, ScreenAlert, ScreenAlertMonitor

UTC = timezone.utc


# ---------------------------------------------------------------------------
# C24 - job postings
# ---------------------------------------------------------------------------
def test_slug_candidates_strip_corporate_suffixes() -> None:
    """Board slugs are derived from the company name, most likely first."""
    assert slug_candidates("Datadog, Inc.", "DDOG")[0] == "datadog"
    assert slug_candidates("Grab Holdings Limited", "GRAB")[0] == "grab"
    # With no name at all the ticker is the only thing left to try.
    assert slug_candidates(None, "NVDA") == ["nvda"]


def _greenhouse_payload(*, first_published: list[str], company: str = "Datadog") -> dict[str, Any]:
    """Build a Greenhouse ``content=true`` body with the given publish dates."""
    return {
        "jobs": [
            {"id": index, "title": f"Role {index}", "company_name": company,
             "first_published": stamp}
            for index, stamp in enumerate(first_published)
        ]
    }


def _transport(routes: dict[str, httpx.Response]) -> httpx.MockTransport:
    """Serve canned responses by URL prefix; anything else is a 404."""

    def handler(request: httpx.Request) -> httpx.Response:
        for prefix, response in routes.items():
            if str(request.url).startswith(prefix):
                return response
        return httpx.Response(404, json={"error": "no such board"})

    return httpx.MockTransport(handler)


async def _provider_with(routes: dict[str, httpx.Response]) -> JobPostingsProvider:
    """A provider whose HTTP client is bound to canned responses."""
    provider = JobPostingsProvider()
    provider._client = httpx.AsyncClient(transport=_transport(routes))  # noqa: SLF001
    return provider


async def test_job_postings_counts_the_trailing_window() -> None:
    """The reading counts roles first published inside the trailing 90 days."""
    now = datetime(2026, 8, 29, tzinfo=UTC)
    recent = [(now - timedelta(days=day)).isoformat() for day in (5, 20, 80)]
    older = [(now - timedelta(days=day)).isoformat() for day in (120, 150)]
    provider = await _provider_with(
        {
            "https://boards-api.greenhouse.io/v1/boards/datadog/jobs": httpx.Response(
                200, json=_greenhouse_payload(first_published=recent + older)
            )
        }
    )
    reading = await provider.get_postings("DDOG", company_name="Datadog, Inc.", now=now)
    await provider.close()

    assert reading.board == "greenhouse"
    assert reading.slug == "datadog"
    assert reading.open_roles == 5
    assert reading.opened_recent == 3
    assert reading.opened_prior == 2
    assert reading.change_pct == pytest.approx(50.0)


async def test_job_postings_reports_a_missing_board_honestly() -> None:
    """A company with no public board gets no number and an explicit reason."""
    provider = await _provider_with({})
    reading = await provider.get_postings("NVDA", company_name="NVIDIA Corporation")
    await provider.close()

    assert reading.board is None
    assert reading.open_roles is None
    assert "no public Greenhouse, Ashby or Lever job board" in (reading.reason or "")
    # Every slug it tried is reported, so the miss is auditable.
    assert any(entry.startswith("greenhouse:nvidia") for entry in reading.tried)


async def test_job_postings_rejects_a_board_belonging_to_someone_else() -> None:
    """A slug collision must not report another company's hiring as ours."""
    now = datetime(2026, 8, 29, tzinfo=UTC)
    provider = await _provider_with(
        {
            "https://boards-api.greenhouse.io/v1/boards/acme/jobs": httpx.Response(
                200,
                json=_greenhouse_payload(
                    first_published=[now.isoformat()], company="Totally Different Corp"
                ),
            )
        }
    )
    reading = await provider.get_postings("ACME", company_name="Acme Industries")
    await provider.close()
    assert reading.board is None


# ---------------------------------------------------------------------------
# F42 - backfill FROM / TO
# ---------------------------------------------------------------------------
def test_backfill_range_accepts_calendar_boundaries() -> None:
    """FROM takes a duration, "all", an ISO date or a calendar quarter."""
    now = datetime(2026, 8, 29, tzinfo=UTC)
    assert BackfillConfig(enabled=True, range="all").range_start(now=now) is None
    assert BackfillConfig(enabled=True, range="2016-01-04").range_start(now=now) == datetime(
        2016, 1, 4, tzinfo=UTC
    )
    assert BackfillConfig(enabled=True, range="2009-Q2").range_start(now=now) == datetime(
        2009, 4, 1, tzinfo=UTC
    )
    # A duration is measured back from `now`, which is the TO boundary.
    assert BackfillConfig(enabled=True, range="5d").range_start(now=now) == now - timedelta(days=5)
    with pytest.raises(ValueError, match="unknown backfill range"):
        BackfillConfig(enabled=True, range="whenever").range_start(now=now)


def test_protocol_can_backfill_matches_the_connector_package() -> None:
    """Only WebSocket lacks a backfill; the Source Catalog reads this flag."""
    assert Protocol.WEBSOCKET.can_backfill is False
    for protocol in (
        Protocol.REST,
        Protocol.POLLING,
        Protocol.DATABASE,
        Protocol.FILE,
        Protocol.LIBRARY,
    ):
        assert protocol.can_backfill is True, protocol


async def test_scheduler_backfill_passes_the_window_to_the_connector(
    tmp_path: Path,
) -> None:
    """`end` reaches the connector, so the TO control is not decorative."""
    from tradinglib.data.ingest.catalog import SourceCatalog
    from tradinglib.data.ingest.connectors.base import Connector
    from tradinglib.data.ingest.scheduler import IngestScheduler
    from tradinglib.data.ingest.source import DataDomain, SourceConfig, SourceStatus
    from tradinglib.data.store import TimeSeriesStore
    from tradinglib.events import EventBus

    seen: dict[str, Any] = {}

    class WindowConnector(Connector):
        """Records the window it was asked for."""

        async def connect(self) -> None:
            """No transport to open."""

        async def close(self) -> None:
            """No transport to close."""

        async def test(self) -> Any:
            """Not exercised here."""
            raise NotImplementedError

        async def fetch(self, **params: Any) -> FetchResult:
            """Not exercised here."""
            return FetchResult.empty()

        async def status(self) -> SourceStatus:
            """Always connected."""
            return SourceStatus.CONNECTED

        async def backfill(
            self, *, range: str = "1y", batch_size: int = 500, end: datetime | None = None
        ) -> AsyncIterator[FetchResult]:
            """Record the window and yield nothing."""
            seen["range"] = range
            seen["end"] = end
            yield FetchResult.empty()

    bus = EventBus()
    catalog = SourceCatalog(bus, connector_factory=lambda config: WindowConnector(config))
    catalog.save(
        SourceConfig(
            id="windowed",
            name="Windowed",
            data_domain=DataDomain.MARKET,
            protocol=Protocol.REST,
            endpoint="https://vendor.test/v1",
            schedule="1d",
        )
    )
    scheduler = IngestScheduler(catalog, bus, store=TimeSeriesStore(tmp_path / "ts"))
    end = datetime(2024, 6, 30, tzinfo=UTC)
    summary = await scheduler.backfill("windowed", range="2024-01-01", end=end)

    assert seen == {"range": "2024-01-01", "end": end}
    assert summary["ok"] is True
    assert summary["end"] == end.isoformat()


# ---------------------------------------------------------------------------
# F30 - screen membership alerting
# ---------------------------------------------------------------------------
class _FakeScreen:
    """A saved screen with just the fields the monitor reads."""

    def __init__(self, screen_id: str, name: str, tags: list[str]) -> None:
        self.id = screen_id
        self.name = name
        self.tags = tags


class _FakeRun:
    """A screen run with just the fields the monitor reads."""

    def __init__(self, run_id: str, symbols: list[str]) -> None:
        self.id = run_id
        self.symbols = symbols


class _FakeRegistry:
    """In-memory stand-in for the screen registry."""

    def __init__(self, screens: list[_FakeScreen]) -> None:
        self._screens = screens
        self.latest: dict[str, dict[str, Any]] = {}
        self.session_factory = None

    def list_screens(self) -> list[_FakeScreen]:
        """Return every saved screen."""
        return list(self._screens)

    def latest_run(self, screen_id: str) -> dict[str, Any] | None:
        """Return the previous stored run, if there is one."""
        return self.latest.get(screen_id)


class _FakeEngine:
    """Screen engine stand-in returning a scripted sequence of runs."""

    def __init__(self, registry: _FakeRegistry, sequences: dict[str, list[list[str]]]) -> None:
        self.registry = registry
        self._sequences = sequences
        self._counter: dict[str, int] = {}

    async def run(self, screen: _FakeScreen, *, persist: bool = True) -> _FakeRun:
        """Return the next scripted run and record it as the newest one."""
        index = self._counter.get(screen.id, 0)
        self._counter[screen.id] = index + 1
        symbols = self._sequences[screen.id][index]
        run = _FakeRun(f"{screen.id}-run-{index}", symbols)
        if persist:
            self.registry.latest[screen.id] = {"id": run.id, "symbols": list(symbols)}
        return run


async def test_screen_alert_baseline_then_change(settings: Any) -> None:
    """The first cycle records a baseline; the second reports what moved.

    Bound to the `settings` fixture so the alerts land in the throwaway
    database rather than the developer's real one.
    """
    from tradinglib.events import EventBus

    screen = _FakeScreen("scr-1", "Quality momentum", [ALERT_TAG, "pinned"])
    registry = _FakeRegistry([screen])
    engine = _FakeEngine(registry, {"scr-1": [["AAPL", "MSFT"], ["AAPL", "NVDA"]]})
    bus = EventBus()
    received: list[dict[str, Any]] = []
    bus.subscribe("notifications", lambda event: received.append(event.payload))

    monitor = ScreenAlertMonitor(engine, bus)
    assert [s.id for s in monitor.alerting_screens()] == ["scr-1"]

    assert await monitor.check(screen) is None  # baseline, nothing to compare
    alert = await monitor.check(screen)
    assert alert is not None
    assert alert.entered == ["NVDA"]
    assert alert.exited == ["MSFT"]
    assert alert.previous_count == 2
    assert alert.pass_count == 2
    assert "NVDA" in alert.headline

    stored = monitor.list_alerts(screen_id="scr-1")
    assert [a.id for a in stored] == [alert.id]
    assert stored[0].detected_at.tzinfo is not None  # UTC survives the round trip

    acknowledged = monitor.acknowledge(alert.id)
    assert acknowledged.acknowledged_at is not None
    assert monitor.list_alerts(screen_id="scr-1", unacknowledged_only=True) == []

    assert received and received[0]["event"] == "screen_alert"
    assert received[0]["alert"]["entered"] == ["NVDA"]


def test_screen_alert_headline_states_what_moved() -> None:
    """A headline names the symbols rather than saying "something changed"."""
    alert = ScreenAlert(
        screen_id="scr-1", screen_name="Quality", run_id="r1", entered=["A"], exited=["B"]
    )
    assert alert.headline == "Quality: +A · −B"
    assert ScreenAlert(screen_id="s", screen_name="Q", run_id="r").headline.endswith(
        "membership changed"
    )


# ---------------------------------------------------------------------------
# C10 - measured catalysts and risks
# ---------------------------------------------------------------------------
def _metric(key: str, value: float | None) -> Metric:
    """A derived metric carrying just a value."""
    return Metric(key=key, label=key, value=value, unit="x", basis="test")


def test_thesis_items_are_measured_not_written() -> None:
    """Each catalyst and risk fires from a measurement and quotes it."""
    metrics = {
        key: _metric(key, value)
        for key, value in {
            "revenue_ttm": 4000.0,
            "revenue_prior_ttm": 3000.0,
            "ebitda_ttm": 800.0,
            "ebitda_margin": 0.20,
            "fcf_ttm": -200.0,
            "fcf_margin": -0.05,
            "net_cash": 1200.0,
            "market_cap": 6000.0,
            "ev_to_sales": 12.5,
        }.items()
    }
    items = thesis_items(
        metrics,
        consensus={"revision_30d": 0.04, "fy_revenue_growth": 0.30, "analysts": 42, "dispersion": 0.2},
        next_earnings={"days_away": 30, "date": "2026-11-05", "eps_estimate": 0.64,
                       "source": "yfinance"},
        sentiment={"recency_weighted": -0.4, "sample": 12},
        warnings=["estimates are unavailable for this ticker"],
    )
    catalysts = {item.label for item in items if item.kind == "catalyst"}
    risks = {item.label for item in items if item.kind == "risk"}

    assert "Results in 30d" in catalysts
    assert "Estimates rising" in catalysts
    assert "Consensus growth" in catalysts
    assert "Revenue compounding" in catalysts
    assert "Net cash balance sheet" in catalysts
    assert {"Cash burn", "Valuation", "Estimate dispersion", "Negative news flow"} <= risks
    assert "Incomplete data" in risks
    # Nothing is stated without the measurement behind it.
    for item in items:
        assert item.basis, item.label
        assert item.metric


def test_thesis_items_are_empty_when_nothing_is_measurable() -> None:
    """No data means no claims - not a default set of plausible bullets."""
    assert thesis_items({}, consensus=None, next_earnings=None, sentiment=None) == []


# ---------------------------------------------------------------------------
# agent stage prompt - the workspace inventory
# ---------------------------------------------------------------------------
def test_stage_prompt_states_the_real_identifiers() -> None:
    """The prompt names the ids that exist, so a stage cannot invent one."""
    stage = Stage(id="scanner", name="Scanner", objective="Scan the universe.")
    prompt = build_stage_system_prompt(
        None,
        stage,
        tool_names=["resolve_universe", "run_screen"],
        workspace={
            "universes": ["universe:us-quality-50", "watchlist:primary-research"],
            "screens": ["quality-momentum"],
            "active_universe": "universe:us-quality-50",
        },
    )
    assert "universe:us-quality-50" in prompt
    assert "watchlist:primary-research" in prompt
    assert "quality-momentum" in prompt
    assert "never invent an id" in prompt


def test_stage_prompt_says_when_no_screen_is_saved() -> None:
    """An empty workspace is stated, not left for the model to guess about."""
    stage = Stage(id="scanner", name="Scanner")
    prompt = build_stage_system_prompt(
        None,
        stage,
        tool_names=["run_screen"],
        workspace={"universes": [], "screens": [], "active_universe": None},
    )
    assert "No screen is saved in this workspace" in prompt


def test_job_postings_payload_round_trips() -> None:
    """The reading serialises everything the UI tooltip needs."""
    reading = JobPostings(
        symbol="DDOG",
        board="greenhouse",
        board_label="Greenhouse",
        slug="datadog",
        open_roles=100,
        opened_recent=40,
        opened_prior=25,
        change_pct=60.0,
        as_of=datetime(2026, 8, 29, tzinfo=UTC),
    )
    payload = json.loads(json.dumps(reading.as_dict()))
    assert payload["window_days"] == 90
    assert payload["as_of"] == "2026-08-29T00:00:00+00:00"
    assert payload["open_roles"] == 100
