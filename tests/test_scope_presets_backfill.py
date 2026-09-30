"""Tests for the fix-round-2 engines.

Four features whose absence the coverage matrix used to record, each exercised
against the real engine rather than a mock of it:

* the *active scope* — the topbar selector may point at a universe **or** a
  watchlist, and exactly one of them is active at a time;
* the universe **change log** — add/drop diffs between snapshots, attributed to
  the construction rule that was in force, plus corporate-action pairing
  (EDGAR is stubbed here so the test is offline and deterministic);
* agent-definition **presets** — named effort dials that may only move the
  cost/latency knobs;
* **backfill on first connection** — saving an enabled source with the flag set
  starts exactly one backfill.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pytest

from tradinglib.agents.registry import (
    PRESET_FIELDS,
    AgentRegistry,
)
from tradinglib.config import Settings
from tradinglib.data.universe import (
    UniverseRegistry,
    WatchlistRegistry,
    detect_corporate_actions,
)

pytestmark = pytest.mark.usefixtures("settings")


# ---------------------------------------------------------------- active scope
def test_active_scope_starts_on_a_universe(settings: Settings) -> None:
    """A fresh install has a universe selected, and says which kind it is."""
    registry = UniverseRegistry()
    registry.create("Alpha", ["AAPL", "MSFT"], universe_id="alpha")
    registry.set_active("alpha")

    scope = registry.active_scope()
    assert scope["kind"] == "universe"
    assert scope["id"] == "alpha"
    assert scope["ref"] == "universe:alpha"
    assert scope["symbols"] == ["AAPL", "MSFT"]
    assert registry.active_universe_id() == "alpha"
    assert registry.active_watchlist_id() is None


def test_selecting_a_watchlist_deselects_the_universe(settings: Settings) -> None:
    """Exactly one collection is active across both tables."""
    registry = UniverseRegistry()
    registry.create("Alpha", ["AAPL", "MSFT"], universe_id="alpha")
    registry.set_active("alpha")
    watchlists = WatchlistRegistry()
    watchlists.create("Semis", ["NVDA", "AVGO"], watchlist_id="semis")

    scope = registry.set_active_scope("watchlist:semis")
    assert scope["kind"] == "watchlist"
    assert scope["id"] == "semis"
    assert scope["symbols"] == ["NVDA", "AVGO"]
    # A caller that specifically needs a *universe* is told there is none,
    # rather than being handed the stale previous one.
    assert registry.active_universe_id() is None
    assert registry.active_watchlist_id() == "semis"

    # ...and selecting a universe again clears the watchlist.
    registry.set_active_scope("alpha")
    assert registry.active_watchlist_id() is None
    assert registry.active_scope()["kind"] == "universe"


def test_active_scope_is_empty_when_nothing_is_selected(settings: Settings) -> None:
    """No selection reports `kind: None`, never a default."""
    registry = UniverseRegistry()
    registry.create("Alpha", ["AAPL"], universe_id="alpha")
    scope = registry.active_scope()
    assert scope["kind"] is None
    assert scope["symbols"] == []


def test_set_active_scope_rejects_an_unknown_reference(settings: Settings) -> None:
    """A reference that names nothing selectable raises rather than guessing."""
    registry = UniverseRegistry()
    with pytest.raises(KeyError):
        registry.set_active_scope("watchlist:nope")


# ------------------------------------------------------------------ change log
def _seed_change_log(registry: UniverseRegistry) -> str:
    """Build a universe with three dated snapshots and one rule edit."""
    universe = registry.create(
        "Probe", ["AAPL", "MSFT", "GOOG", "PYPL"], universe_id="probe",
        rule="ROIC pctl > 0.8",
    )
    registry.snapshot(universe.id, as_of=date(2025, 1, 31), source="rebalance")
    registry.update(universe.id, symbols=["AAPL", "MSFT", "GOOGL", "AVGO"])
    registry.snapshot(universe.id, as_of=date(2025, 4, 30), source="rebalance")
    registry.update(
        universe.id, rule="ROIC pctl > 0.85",
        symbols=["AAPL", "MSFT", "GOOGL", "AVGO", "PANW"],
    )
    registry.snapshot(universe.id, as_of=date(2025, 7, 31), source="rebalance")
    return universe.id


def test_change_log_diffs_consecutive_snapshots(settings: Settings) -> None:
    """Each transition names what entered and left, newest first."""
    registry = UniverseRegistry()
    universe_id = _seed_change_log(registry)

    entries = registry.change_log(universe_id)
    assert [e["as_of"] for e in entries] == ["2025-07-31", "2025-04-30", "2025-01-31"]

    latest, middle, baseline = entries
    assert latest["added"] == ["PANW"]
    assert latest["dropped"] == []
    assert latest["net"] == 1
    assert middle["added"] == ["AVGO", "GOOGL"]
    assert middle["dropped"] == ["GOOG", "PYPL"]
    assert middle["net"] == 0
    # The oldest snapshot has no predecessor, so it is a baseline rather than
    # an addition of every name.
    assert baseline["baseline"] is True
    assert baseline["added"] == [] and baseline["dropped"] == []


def test_change_log_attributes_the_rule_in_force_at_the_snapshot(
    settings: Settings,
) -> None:
    """The rule is read off the snapshot, not off the (since edited) universe."""
    registry = UniverseRegistry()
    universe_id = _seed_change_log(registry)
    latest, middle, _baseline = registry.change_log(universe_id)

    assert latest["rule"] == "ROIC pctl > 0.85"
    assert latest["previous_rule"] == "ROIC pctl > 0.8"
    assert latest["rule_changed"] is True
    # The April transition happened under the *old* rule and still says so,
    # even though the universe now carries a newer one.
    assert middle["rule"] == "ROIC pctl > 0.8"
    assert middle["rule_changed"] is False

    revisions = registry.rule_revisions(universe_id)
    assert [r["version"] for r in revisions] == [1, 2]
    assert [r["rule"] for r in revisions] == ["ROIC pctl > 0.8", "ROIC pctl > 0.85"]


def test_change_log_of_a_universe_without_snapshots_is_empty(
    settings: Settings,
) -> None:
    """No snapshots means no history — not a fabricated first entry."""
    registry = UniverseRegistry()
    registry.create("Bare", ["AAPL"], universe_id="bare")
    assert registry.change_log("bare") == []
    assert registry.rule_revisions("bare") == []


# ---------------------------------------------------------- corporate actions
class _StubEdgar:
    """EDGAR stand-in resolving tickers to CIKs from a fixed table."""

    def __init__(self, ciks: dict[str, str]) -> None:
        self._ciks = ciks
        self.calls: list[str] = []

    async def resolve_cik(self, symbol: str) -> str:
        """Return the CIK, raising for a ticker the index does not carry."""
        self.calls.append(symbol)
        try:
            return self._ciks[symbol]
        except KeyError:
            raise KeyError(f"unknown ticker {symbol!r}") from None


@pytest.mark.asyncio
async def test_corporate_actions_pair_one_registrant_two_tickers() -> None:
    """A drop and an add sharing a CIK is a share-class change, not a decision."""
    edgar = _StubEdgar({
        "GOOG": "0001652044",
        "GOOGL": "0001652044",
        "AVGO": "0001730168",
        "PYPL": "0001633917",
    })
    actions = await detect_corporate_actions(
        ["AVGO", "GOOGL"], ["GOOG", "PYPL"], edgar=edgar
    )
    assert len(actions) == 1
    assert actions[0]["from"] == "GOOG"
    assert actions[0]["to"] == "GOOGL"
    assert actions[0]["cik"] == "0001652044"
    # AVGO/PYPL are different registrants and are left as real membership changes.
    assert {a["to"] for a in actions} == {"GOOGL"}


@pytest.mark.asyncio
async def test_corporate_actions_report_nothing_when_edgar_cannot_resolve() -> None:
    """An unverified pairing is never asserted."""
    edgar = _StubEdgar({})
    assert await detect_corporate_actions(["GOOGL"], ["GOOG"], edgar=edgar) == []


@pytest.mark.asyncio
async def test_corporate_actions_skip_the_lookup_when_one_side_is_empty() -> None:
    """Nothing to pair means EDGAR is not called at all."""
    edgar = _StubEdgar({"GOOG": "1"})
    assert await detect_corporate_actions(["GOOGL"], [], edgar=edgar) == []
    assert edgar.calls == []


# --------------------------------------------------------------- agent presets
def test_shipped_agents_carry_effort_presets(settings: Settings) -> None:
    """The model-calling roster ships Fast / Quick / Full; the human one does not."""
    registry = AgentRegistry()
    registry.ensure_defaults()
    by_id = {d.id: d for d in registry.list()}

    assert list(by_id["scanner-agent"].presets) == ["Fast", "Quick", "Full"]
    assert by_id["human-review"].presets == {}
    # The dials really differ, and in the direction their names claim.
    fast = by_id["scanner-agent"].presets["Fast"]
    full = by_id["scanner-agent"].presets["Full"]
    assert fast["max_tokens"] < full["max_tokens"]
    assert fast["timeout_s"] < full["timeout_s"]


def test_presets_are_backfilled_without_bumping_the_version(
    settings: Settings,
) -> None:
    """A row written before presets existed gains them, but is not "edited"."""
    registry = AgentRegistry()
    registry.ensure_defaults()
    registry.update("scanner-agent", presets={})
    before = registry.get("scanner-agent")
    assert before.presets == {}

    registry.ensure_defaults()
    after = registry.get("scanner-agent")
    assert list(after.presets) == ["Fast", "Quick", "Full"]
    assert after.version == before.version


def test_ensure_defaults_leaves_edited_presets_alone(settings: Settings) -> None:
    """A deliberate single-entry preset set is configuration, not a gap."""
    registry = AgentRegistry()
    registry.ensure_defaults()
    registry.update("scanner-agent", presets={"Mine": {"max_tokens": 111}})
    registry.ensure_defaults()
    assert list(registry.get("scanner-agent").presets) == ["Mine"]


def test_a_preset_may_not_rewrite_the_prompt_model_or_tools(
    settings: Settings,
) -> None:
    """A preset is an effort dial; changing what the agent *is* is refused."""
    registry = AgentRegistry()
    registry.ensure_defaults()
    for forbidden in ("system_prompt", "model", "provider", "tools", "name"):
        assert forbidden not in PRESET_FIELDS
        with pytest.raises(ValueError, match="may not change"):
            registry.update("scanner-agent", presets={"Bad": {forbidden: "x"}})


def test_preset_values_are_validated_and_coerced(settings: Settings) -> None:
    """Types are normalised on the way in, and nonsense is refused."""
    registry = AgentRegistry()
    registry.ensure_defaults()
    updated = registry.update(
        "scanner-agent",
        presets={"Tuned": {"max_tokens": "2048", "retries": "1", "timeout_s": "30",
                           "scope": {"max_candidates": 5}}},
    )
    tuned = updated.presets["Tuned"]
    assert tuned == {
        "max_tokens": 2048, "retries": 1, "timeout_s": 30.0,
        "scope": {"max_candidates": 5},
    }
    with pytest.raises(ValueError, match="max_tokens must be positive"):
        registry.update("scanner-agent", presets={"Bad": {"max_tokens": 0}})
    with pytest.raises(ValueError, match="retries cannot be negative"):
        registry.update("scanner-agent", presets={"Bad": {"retries": -1}})


def test_presets_survive_duplication_and_the_version_archive(
    settings: Settings,
) -> None:
    """A copy carries the dials, and a frozen version can be read back with them."""
    registry = AgentRegistry()
    registry.ensure_defaults()
    copy = registry.duplicate("scanner-agent", name="Scanner copy")
    assert list(copy.presets) == ["Fast", "Quick", "Full"]

    original = registry.get("scanner-agent")
    registry.update("scanner-agent", presets={"Only": {"max_tokens": 999}})
    frozen = registry.version("scanner-agent", original.version)
    assert list(frozen.presets) == ["Fast", "Quick", "Full"]


# ------------------------------------------------------- first-connection backfill
@pytest.mark.asyncio
async def test_saving_an_enabled_source_starts_one_backfill(settings: Settings) -> None:
    """The checkbox's promise is kept: the save really starts a backfill, once."""
    from tradinglib.api.routes import ingest as ingest_routes

    started: list[str] = []

    class _Scheduler:
        """Records the backfills the route asks for."""

        async def backfill(
            self,
            source_id: str,
            *,
            range: str | None = None,
            end: datetime | None = None,
        ) -> dict[str, Any]:
            started.append(source_id)
            return {"source_id": source_id, "ok": True}

    class _Request:
        """Minimal stand-in exposing only `app.state.backfill_tasks`."""

        class app:  # noqa: N801 - mirrors Starlette's attribute shape
            class state:  # noqa: N801
                backfill_tasks: dict[str, Any] = {}

    from tradinglib.data.ingest.catalog import SourceCatalog
    from tradinglib.data.ingest.source import SourceConfig

    catalog = SourceCatalog()
    config = SourceConfig.from_form({
        "sourceName": "Probe", "dataDomain": "prices", "protocol": "library",
        "endpoint": "yfinance", "authType": "none", "format": "json",
        "schedule": "manual", "scope": "AAPL", "sourceTz": "UTC",
        "backfillEnabled": True, "backfillRange": "1mo", "enabled": True,
    })
    saved = catalog.save(config)

    outcome = ingest_routes._start_first_connection_backfill(
        _Request, catalog, _Scheduler(), saved  # type: ignore[arg-type]
    )
    assert outcome is not None and outcome["started"] is True
    # The route dispatches the backfill as a background task; let it start.
    await _Request.app.state.backfill_tasks[saved.id]
    assert started == [saved.id]

    # Once the scheduler stamps the state, a second save declines.
    catalog.record_backfill(saved.id, rows=120)
    again = ingest_routes._start_first_connection_backfill(
        _Request, catalog, _Scheduler(), saved  # type: ignore[arg-type]
    )
    assert again is not None and again["started"] is False
    assert again["reason"] == "already backfilled"
    assert started == [saved.id]
    # The declined save queued nothing new.
    assert set(_Request.app.state.backfill_tasks) == {saved.id}


def test_backfill_is_not_started_when_it_cannot_be(settings: Settings) -> None:
    """Every refusal names its reason rather than silently doing nothing."""
    from tradinglib.api.routes import ingest as ingest_routes
    from tradinglib.data.ingest.catalog import SourceCatalog
    from tradinglib.data.ingest.source import SourceConfig

    class _Request:
        class app:  # noqa: N801
            class state:  # noqa: N801
                backfill_tasks: dict[str, Any] = {}

    catalog = SourceCatalog()
    form = {
        "sourceName": "Paused probe", "dataDomain": "prices", "protocol": "library",
        "endpoint": "yfinance", "authType": "none", "format": "json",
        "schedule": "manual", "scope": "AAPL", "sourceTz": "UTC",
        "backfillEnabled": True, "backfillRange": "1mo", "enabled": False,
    }
    paused = catalog.save(SourceConfig.from_form(form))
    outcome = ingest_routes._start_first_connection_backfill(
        _Request, catalog, object(), paused  # type: ignore[arg-type]
    )
    assert outcome == {"started": False, "reason": "the source is paused"}

    # The flag off means the route does not act at all.
    off = catalog.save(SourceConfig.from_form({**form, "sourceName": "Off probe",
                                               "backfillEnabled": False, "enabled": True}))
    assert ingest_routes._start_first_connection_backfill(
        _Request, catalog, object(), off  # type: ignore[arg-type]
    ) is None

    # No scheduler running is reported, not swallowed.
    live = catalog.save(SourceConfig.from_form({**form, "sourceName": "Live probe",
                                                "enabled": True}))
    assert ingest_routes._start_first_connection_backfill(
        _Request, catalog, None, live
    ) == {"started": False, "reason": "the ingest scheduler is not running"}


def test_record_backfill_stamps_only_on_success(settings: Settings) -> None:
    """A failed backfill leaves the stamp unset so it will be retried."""
    from tradinglib.data.ingest.catalog import SourceCatalog
    from tradinglib.data.ingest.source import SourceConfig

    catalog = SourceCatalog()
    saved = catalog.save(SourceConfig.from_form({
        "sourceName": "Stamp probe", "dataDomain": "prices", "protocol": "library",
        "endpoint": "yfinance", "authType": "none", "format": "json",
        "schedule": "manual", "scope": "AAPL", "sourceTz": "UTC",
    }))
    assert catalog.state(saved.id).backfilled_at is None

    catalog.record_backfill(saved.id, rows=0, error="vendor refused")
    assert catalog.state(saved.id).backfilled_at is None

    # A backfill that completed but wrote nothing still counts as done: the
    # window genuinely held no data, and re-running it would find none either.
    catalog.record_backfill(saved.id, rows=0)
    assert catalog.state(saved.id).backfilled_at is not None


# ----------------------------------------------------- fundamentals coverage
class _StubMarket:
    """A market provider answering fundamentals for a known subset of symbols."""

    name = "stub"

    def __init__(self, complete: set[str], partial: set[str]) -> None:
        self._complete = complete
        self._partial = partial

    async def get_fundamentals(self, symbol: str) -> Any:
        """Return a snapshot, an empty one, or raise for an unknown symbol."""
        from tradinglib.data.base import Fundamentals, SymbolNotFound

        if symbol in self._complete:
            return Fundamentals(symbol=symbol, market_cap=1e11, revenue_ttm=1e10)
        if symbol in self._partial:
            # A snapshot the vendor returned but did not populate is a gap, and
            # must be counted as one rather than as coverage.
            return Fundamentals(symbol=symbol)
        raise SymbolNotFound(self.name, symbol)


@pytest.mark.asyncio
async def test_fundamentals_coverage_counts_only_usable_snapshots(
    settings: Settings,
) -> None:
    """Coverage is strict: an empty snapshot does not count as covered."""
    from tradinglib.research.company import CompanyResearch

    registry = UniverseRegistry()
    registry.create("Cov", ["AAA", "BBB", "CCC", "DDD"], universe_id="cov")
    registry.set_active("cov")

    engine = CompanyResearch(
        market=_StubMarket(complete={"AAA", "BBB"}, partial={"CCC"}),
        universes=registry,
    )
    coverage = await engine.measure_fundamentals_coverage("cov")

    assert coverage.total == 4
    assert coverage.covered == 2
    assert coverage.missing == ["CCC", "DDD"]
    assert coverage.pct == pytest.approx(50.0)
    assert coverage.universe_name == "Cov"


@pytest.mark.asyncio
async def test_fundamentals_coverage_follows_a_watchlist_scope(
    settings: Settings,
) -> None:
    """The active scope may be a watchlist; coverage measures that instead."""
    from tradinglib.research.company import CompanyResearch

    registry = UniverseRegistry()
    registry.create("Cov", ["AAA", "BBB"], universe_id="cov")
    WatchlistRegistry().create("Focus", ["AAA"], watchlist_id="focus")
    registry.set_active_scope("watchlist:focus")

    engine = CompanyResearch(market=_StubMarket({"AAA"}, set()), universes=registry)
    coverage = await engine.measure_fundamentals_coverage()

    assert coverage.universe_id == "focus"
    assert coverage.universe_name == "Focus"
    assert (coverage.covered, coverage.total) == (1, 1)


@pytest.mark.asyncio
async def test_fundamentals_coverage_reader_never_blocks(settings: Settings) -> None:
    """The datastrip reader returns None until a measurement has landed."""
    from tradinglib.research.company import CompanyResearch

    registry = UniverseRegistry()
    registry.create("Cov", ["AAA"], universe_id="cov")
    registry.set_active("cov")
    engine = CompanyResearch(market=_StubMarket({"AAA"}, set()), universes=registry)

    # Nothing measured yet: "unknown", not a zero somebody could mistake for
    # a real count.
    assert engine.fundamentals_coverage("cov") is None

    await engine.measure_fundamentals_coverage("cov")
    landed = engine.fundamentals_coverage("cov")
    assert landed is not None and landed.covered == 1
    # A measurement of a *different* collection is a wrong answer, not an old
    # one, so it is withheld.
    assert engine.fundamentals_coverage("some-other-id") is None


@pytest.mark.asyncio
async def test_fundamentals_coverage_of_an_unknown_collection_is_zero(
    settings: Settings,
) -> None:
    """"Nothing to cover" is a real answer, not an error."""
    from tradinglib.research.company import CompanyResearch

    engine = CompanyResearch(market=_StubMarket(set(), set()), universes=UniverseRegistry())
    coverage = await engine.measure_fundamentals_coverage("no-such-universe")
    assert (coverage.covered, coverage.total) == (0, 0)
    assert coverage.pct is None


# ------------------------------------------------------- API surface (end to end)
@pytest.mark.asyncio
async def test_active_scope_endpoint_accepts_a_watchlist(client: Any) -> None:
    """`POST /api/universes/active` takes either kind and reports which it set."""
    created = await client.post("/api/watchlists", json={"name": "Semis", "symbols": ["NVDA"]})
    watchlist_id = created.json()["id"]

    response = await client.post("/api/universes/active", json={"id": f"watchlist:{watchlist_id}"})
    assert response.status_code == 200
    scope = response.json()["scope"]
    assert scope["kind"] == "watchlist"
    assert scope["id"] == watchlist_id
    assert response.json()["symbols"] == ["NVDA"]
    # `universe` is null because the selection is not one.
    assert response.json()["universe"] is None

    status = (await client.get("/api/status")).json()
    assert status["universe"]["kind"] == "watchlist"
    assert status["universe"]["id"] == watchlist_id


@pytest.mark.asyncio
async def test_changelog_endpoint_returns_diffs_and_rule_versions(client: Any) -> None:
    """The endpoint the CHANGE LOG panel reads is real end to end."""
    registry = UniverseRegistry()
    universe_id = _seed_change_log(registry)

    body = (await client.get(
        f"/api/universes/{universe_id}/changelog",
        params={"corporate_actions": "false"},
    )).json()
    assert body["rule_version"] == 2
    assert body["entries"][0]["added"] == ["PANW"]
    assert body["entries"][1]["dropped"] == ["GOOG", "PYPL"]
    # With EDGAR reconciliation off, every change is attributed to the rule.
    assert body["entries"][1]["dropped_by_rule"] == ["GOOG", "PYPL"]
    assert body["entries"][1]["corporate_actions"] == []


@pytest.mark.asyncio
async def test_pipeline_stages_report_the_model_they_would_run_on(
    client: Any,
) -> None:
    """A stage card must never read "no model" for a stage that would run."""
    pipelines = (await client.get("/api/agents/pipelines")).json()["pipelines"]
    pipeline_id = pipelines[0]["id"]
    stages = (await client.get(f"/api/agents/pipelines/{pipeline_id}")).json()["stages"]

    assert stages, "the default pipeline should ship with stages"
    for stage in stages:
        assert "effective_model" in stage
        assert "effective_source" in stage
        if stage["kind"] == "human_approval":
            assert stage["effective_model"] is None
            assert stage["effective_source"] == "human"
        else:
            # Either a model was resolved, or the reason none could be is
            # recorded — never an unexplained blank.
            assert stage["effective_source"] in {
                "stage", "agent", "provider_default", "unresolved",
            }
            if stage["effective_source"] != "unresolved":
                assert stage["effective_model"]
