"""Agent subsystem: providers, registry, pipelines and the pipeline runner.

Every test here is offline. The LLM is :class:`tradinglib.agents.providers.MockProvider`
- a deterministic stub whose token counts come from the real prompt text, so the
cost arithmetic and the budget enforcement are exercised for real - while the
tools the agents call are the genuine library: a real universe registry, a real
Parquet + DuckDB store, real SQLite persistence.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tradinglib.agents.pipeline import (
    IOContract,
    PipelineRegistry,
    PipelineStatus,
    Stage,
    StageKind,
)
from tradinglib.agents.providers import (
    ANTHROPIC_PRICES,
    AnthropicProvider,
    BudgetExceeded,
    MockProvider,
    MockRequest,
    MockTurn,
    ModelPrice,
    OpenAIProvider,
    ProviderRegistry,
    SpendLedger,
    ToolCall,
    Usage,
    load_price_overrides,
    lookup_price,
    price_usage,
)
from tradinglib.agents.registry import (
    AgentKind,
    AgentRegistry,
    AgentStatus,
)
from tradinglib.agents.runner import (
    TERMINAL_RUN_STATES,
    ApprovalState,
    LibraryToolset,
    PipelineRunner,
    RunStatus,
    StageStatus,
    extract_structured_output,
)
from tradinglib.config import Budgets, Settings
from tradinglib.data.base import Bar
from tradinglib.data.store import TimeSeriesStore, reset_store
from tradinglib.data.universe import UniverseRegistry, reset_registries
from tradinglib.db import init_db
from tradinglib.events import EventBus

REQUIRED_KEYS_RE = re.compile(r"containing exactly these keys: ([^.]+)\.")


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------
@pytest.fixture()
def database(settings: Settings) -> Iterator[Settings]:
    """A fresh SQLite database in the temporary data directory."""
    init_db()
    yield settings
    reset_store()
    reset_registries()


@pytest.fixture()
def agents(database: Settings, bus: EventBus) -> AgentRegistry:
    """An agent registry backed by the temporary database."""
    return AgentRegistry(bus=bus)


@pytest.fixture()
def pipelines(database: Settings, bus: EventBus) -> PipelineRegistry:
    """A pipeline registry backed by the temporary database."""
    return PipelineRegistry(bus=bus)


def scripted_responder(
    *, tool_plan: dict[str, list[ToolCall]] | None = None
) -> Callable[[MockRequest], MockTurn]:
    """Build a deterministic responder for :class:`MockProvider`.

    On the first turn of a stage it issues whatever tool calls ``tool_plan``
    lists for that stage (keyed by the stage name in the system prompt); on the
    next turn it answers with a JSON block carrying exactly the keys the stage's
    output contract asked for.
    """
    plan = tool_plan or {}

    def responder(request: MockRequest) -> MockTurn:
        """Answer one mock completion request."""
        system = request.system or ""
        stage_name = ""
        match = re.search(r"running the '([^']+)' stage", system)
        if match:
            stage_name = match.group(1)
        calls = plan.get(stage_name, [])
        if calls and len(request.messages) == 1:
            return MockTurn(text=f"calling {len(calls)} tool(s)", tool_calls=list(calls))
        keys_match = REQUIRED_KEYS_RE.search(system)
        payload: dict[str, object] = {}
        if keys_match:
            for key in [part.strip() for part in keys_match.group(1).split(",")]:
                payload[key] = [f"{stage_name}:{key}"]
        else:
            payload["summary"] = stage_name
        body = json.dumps(payload)
        return MockTurn(text=f"{stage_name} finished.\n```json\n{body}\n```")

    return responder


def build_runner(
    *,
    bus: EventBus,
    agents: AgentRegistry,
    pipelines: PipelineRegistry,
    provider: MockProvider,
    toolset: LibraryToolset | None = None,
) -> PipelineRunner:
    """Create a runner wired to the mock provider and the given registries."""
    return PipelineRunner(
        bus,
        pipelines=pipelines,
        agents=agents,
        providers=ProviderRegistry(providers={"mock": provider}),
        toolset=toolset or LibraryToolset(),
        audit=None,
    )


async def wait_for(predicate: Callable[[], bool], *, timeout: float = 10.0) -> None:
    """Poll ``predicate`` until it is true or the timeout expires."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition was not reached in time")


# --------------------------------------------------------------------------
# prices and cost accounting
# --------------------------------------------------------------------------
def test_price_table_prices_known_models_and_admits_unknown_ones(database: Settings) -> None:
    """A model in the table prices; one outside it is unknown, not free."""
    price = lookup_price("anthropic", "claude-sonnet-4-6")
    assert price is not None
    assert price.input_per_mtok == ANTHROPIC_PRICES["claude-sonnet-4-6"].input_per_mtok
    # A dated vendor snapshot prices as its base model.
    assert lookup_price("anthropic", "claude-sonnet-4-6-20260101") is price
    assert lookup_price("openai", "gpt-imaginary-9") is None

    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    priced = price_usage("anthropic", "claude-sonnet-4-6", usage)
    assert priced.cost_usd == pytest.approx(3.0 + 15.0)
    assert priced.cost_known is True

    unknown = price_usage("openai", "gpt-imaginary-9", usage)
    assert unknown.cost_usd == 0.0
    assert unknown.cost_known is False


def test_price_overrides_are_read_from_the_data_directory(database: Settings) -> None:
    """A price the operator corrects on disk wins over the built-in table."""
    path = Path(database.data_dir) / "model_prices.json"
    path.write_text(
        json.dumps({"openai/gpt-imaginary-9": {"input_per_mtok": 1.0, "output_per_mtok": 2.0}}),
        encoding="utf-8",
    )
    overrides = load_price_overrides(database)
    assert "openai/gpt-imaginary-9" in overrides
    price = lookup_price("openai", "gpt-imaginary-9", settings=database)
    assert price is not None and price.source == "override"
    assert price.cost(input_tokens=1_000_000, output_tokens=0) == pytest.approx(1.0)


def test_model_price_falls_back_to_the_input_rate_for_cache_tokens() -> None:
    """Missing cache rates over-state rather than under-state the cost."""
    price = ModelPrice(2.0, 8.0)
    assert price.cost(input_tokens=0, output_tokens=0, cache_read_tokens=1_000_000) == pytest.approx(2.0)


def test_spend_ledger_enforces_every_budget_scope() -> None:
    """Daily, monthly, per-run and per-stage ceilings all raise."""
    ledger = SpendLedger(Budgets(per_run_usd=1.0, daily_usd=2.0, monthly_usd=5.0))
    ledger.record(provider="mock", model="mock-small", usage=Usage(cost_usd=0.75), run_id="R1")
    assert ledger.run_total("R1") == pytest.approx(0.75)
    assert ledger.window_total("day") == pytest.approx(0.75)

    ledger.check(run_id="R1", projected_usd=0.1)
    with pytest.raises(BudgetExceeded) as excinfo:
        ledger.check(run_id="R1", projected_usd=0.5)
    assert excinfo.value.scope == "per_run"

    with pytest.raises(BudgetExceeded) as stage_error:
        ledger.check(run_id="R2", projected_usd=0.2, stage_budget_usd=0.1)
    assert stage_error.value.scope == "stage"

    ledger.record(provider="mock", model="mock-small", usage=Usage(cost_usd=1.5), run_id="R2")
    with pytest.raises(BudgetExceeded) as daily_error:
        ledger.check(run_id="R3", projected_usd=0.01)
    assert daily_error.value.scope == "daily"

    snapshot = ledger.snapshot(run_id="R1")
    assert snapshot["used"] == pytest.approx(2.25)
    assert snapshot["budget"] == 2.0
    assert snapshot["run"] == {"id": "R1", "used": pytest.approx(0.75), "budget": 1.0}


def test_unpriced_usage_is_flagged_rather_than_counted_as_free() -> None:
    """An unpriced charge is visible in the snapshot as such."""
    ledger = SpendLedger(Budgets())
    ledger.record(
        provider="openai",
        model="gpt-imaginary-9",
        usage=Usage(input_tokens=100, output_tokens=100, cost_known=False),
    )
    assert ledger.unpriced_count() == 1
    assert ledger.snapshot()["unpriced_entries"] == 1


# --------------------------------------------------------------------------
# providers
# --------------------------------------------------------------------------
async def test_mock_provider_is_deterministic_and_priced() -> None:
    """The stub returns the same usage for the same prompt, and it costs money."""
    provider = MockProvider(turns=[MockTurn(text="hello"), MockTurn(text="hello")])
    first = await provider.complete(
        model="mock-small", system="s", messages=[{"role": "user", "content": "abcd" * 10}]
    )
    second = await provider.complete(
        model="mock-small", system="s", messages=[{"role": "user", "content": "abcd" * 10}]
    )
    assert first.usage == second.usage
    assert first.usage.cost_usd > 0
    assert first.usage.cost_known is True
    assert provider.executes_tools is False


async def test_openai_provider_reports_missing_key_honestly() -> None:
    """Without a key the OpenAI provider says so instead of pretending."""
    provider = OpenAIProvider(api_key=None)
    capability = await provider.capability()
    assert capability.available is False
    assert "OPENAI_API_KEY" in capability.detail
    assert capability.models == []


async def test_anthropic_capability_reports_a_real_auth_mode() -> None:
    """The Anthropic report names the auth mode it actually found."""
    provider = AnthropicProvider(api_key=None)
    capability = await provider.capability()
    assert capability.provider == "anthropic"
    assert capability.auth_mode in {"api_key", "claude_cli", "none"}
    assert capability.detail
    # The model catalogue is priced whether or not the provider is usable.
    assert all(model.priced for model in capability.models)
    if capability.available:
        assert capability.auth_mode == "claude_cli"
        assert AnthropicProvider.cli_path()


async def test_provider_registry_payload_lists_only_usable_providers(database: Settings) -> None:
    """``/api/agents/models`` shows every provider's state and usable models."""
    registry = ProviderRegistry(providers={"mock": MockProvider(), "openai": OpenAIProvider(api_key=None)})
    payload = await registry.models_payload()
    providers = {entry["provider"]: entry for entry in payload["providers"]}
    assert providers["mock"]["available"] is True
    assert providers["openai"]["available"] is False
    assert {model["id"] for model in payload["models"]} == {"mock-small", "mock-large"}
    assert payload["default"] == {"provider": "mock", "model": "mock-small"}
    assert payload["prices"]["anthropic_as_of"]


# --------------------------------------------------------------------------
# agent registry
# --------------------------------------------------------------------------
def test_agent_registry_crud_and_validation(agents: AgentRegistry) -> None:
    """Definitions persist, validate their tools and refuse illegal edits."""
    definition = agents.create(
        "Scanner",
        "Opportunity discovery",
        "Find candidates.",
        "mock",
        "mock-small",
        tools=["resolve_universe", "run_screen"],
        budget_usd=0.5,
    )
    assert definition.id == "scanner"
    assert agents.get("scanner").tools == ["resolve_universe", "run_screen"]

    with pytest.raises(ValueError, match="unknown tool"):
        agents.create("Bad", "role", "prompt", "mock", "mock-small", tools=["delete_everything"])
    with pytest.raises(ValueError, match="system prompt"):
        agents.create("Empty", "role", "   ", "mock", "mock-small")

    updated = agents.update("scanner", model="mock-large", tools=["get_series"])
    assert updated.model == "mock-large"
    assert updated.version == 2
    with pytest.raises(ValueError, match="unknown field"):
        agents.update("scanner", nonsense=1)

    copy = agents.duplicate("scanner")
    assert copy.id != "scanner"
    assert copy.tools == ["get_series"]

    agents.delete(copy.id)
    with pytest.raises(KeyError):
        agents.get(copy.id)


def test_human_agent_is_locked_and_model_free(agents: AgentRegistry) -> None:
    """The approval participant has no provider, no tools, and cannot be edited."""
    created = agents.ensure_defaults()
    assert {item.id for item in created} >= {"human-review", "quant-agent"}
    assert agents.ensure_defaults() == []  # idempotent

    human = agents.get("human-review")
    assert human.kind is AgentKind.HUMAN
    assert human.calls_a_model is False
    assert human.locked is True
    with pytest.raises(PermissionError):
        agents.update("human-review", model="mock-small")
    with pytest.raises(PermissionError):
        agents.delete("human-review")

    counts = agents.counts()
    assert counts["total"] == len(agents.list())
    assert counts["active"] == 0
    assert counts["idle"] == counts["enabled"]
    assert counts["human"] == 1


def test_agent_counts_use_the_bound_activity_source(agents: AgentRegistry) -> None:
    """Active counts come from a real activity source, not a guess."""
    agents.create("Worker", "role", "prompt", "mock", "mock-small", kind=AgentKind.WORKER)
    agents.bind_activity(lambda: {"worker"})
    counts = agents.counts()
    assert counts["active"] == 1
    assert counts["idle"] == counts["enabled"] - 1
    assert counts["workers"] == 1


def test_paused_agent_reports_its_status(agents: AgentRegistry) -> None:
    """Enabling and status are separate, persisted facts."""
    agents.create("Sweeper", "role", "prompt", "mock", "mock-small")
    agents.update("sweeper", enabled=False, status=AgentStatus.PAUSED)
    definition = agents.get("sweeper")
    assert definition.enabled is False
    assert definition.status is AgentStatus.PAUSED
    assert agents.counts()["disabled"] == 1


# --------------------------------------------------------------------------
# pipelines
# --------------------------------------------------------------------------
def test_pipeline_versions_duplicate_and_lock(pipelines: PipelineRegistry) -> None:
    """Editing marks the document dirty; saving freezes a readable version."""
    pipeline = pipelines.create(
        "Research cycle",
        objective="find something",
        stages=[
            Stage(id="scan", name="Scan", agent_id="scanner", outputs=IOContract(required=["candidates"])),
            Stage(id="test", name="Test", agent_id="quant", inputs=IOContract(required=["candidates"])),
        ],
    )
    assert pipeline.version == 1
    saved = pipelines.save_version(pipeline.id, note="first")
    assert saved.version == 2
    assert saved.dirty is False

    pipelines.update_stage(pipeline.id, "test", objective="run the backtest")
    assert pipelines.get(pipeline.id).dirty is True
    frozen = pipelines.get(pipeline.id, version=1)
    assert frozen.stage("test").objective is None
    assert pipelines.get(pipeline.id).stage("test").objective == "run the backtest"

    versions = pipelines.versions(pipeline.id)
    assert [entry["version"] for entry in versions] == [1]
    assert versions[0]["note"] == "first"

    copy = pipelines.duplicate(pipeline.id, name="Research cycle B")
    assert copy.id != pipeline.id
    assert [stage.id for stage in copy.stages] == ["scan", "test"]

    pipelines.update(pipeline.id, locked=True)
    assert pipelines.get(pipeline.id).status is PipelineStatus.LOCKED
    with pytest.raises(PermissionError):
        pipelines.update_stage(pipeline.id, "test", objective="nope")
    pipelines.update(pipeline.id, locked=False)
    pipelines.update_stage(pipeline.id, "test", objective="allowed again")


def test_pipeline_stage_editing_and_approval_immutability(pipelines: PipelineRegistry) -> None:
    """Stages add, duplicate, reorder and remove - except the approval gate."""
    pipeline = pipelines.create(
        "Gated",
        stages=[
            Stage(id="scan", name="Scan", agent_id="scanner"),
            Stage(
                id="approval",
                name="Human Approval",
                kind=StageKind.HUMAN_APPROVAL,
                agent_id="human-review",
                locked=True,
            ),
        ],
    )
    pipeline = pipelines.add_stage(
        pipeline.id, Stage(id="research", name="Research", agent_id="researcher"), index=1
    )
    assert [stage.id for stage in pipeline.stages] == ["scan", "research", "approval"]

    pipeline = pipelines.duplicate_stage(pipeline.id, "research")
    assert "research-copy" in [stage.id for stage in pipeline.stages]

    with pytest.raises(PermissionError):
        pipelines.remove_stage(pipeline.id, "approval")
    with pytest.raises(PermissionError):
        pipelines.update_stage(pipeline.id, "approval", model="mock-small")
    # The gate still accepts documentation changes.
    pipelines.update_stage(pipeline.id, "approval", objective="review everything")

    pipeline = pipelines.remove_stage(pipeline.id, "research-copy")
    pipeline = pipelines.reorder_stages(pipeline.id, ["research", "scan", "approval"])
    assert [stage.id for stage in pipeline.stages] == ["research", "scan", "approval"]


def test_pipeline_validation_reports_real_problems(pipelines: PipelineRegistry) -> None:
    """Validation catches unknown agents, dangling handoffs, cycles and contracts."""
    pipeline = pipelines.create(
        "Broken",
        stages=[
            Stage(id="a", name="A", agent_id="ghost", handoff_to=["b"]),
            Stage(id="b", name="B", agent_id="ghost", handoff_to=["a"],
                  inputs=IOContract(required=["candidates"])),
        ],
    )
    problems = pipelines.validate(pipeline, agents={"scanner"})
    assert any("unknown agent 'ghost'" in problem for problem in problems)
    assert any("handoff cycle" in problem for problem in problems)
    assert any("requires candidates" in problem for problem in problems)

    healthy = pipelines.create(
        "Fine",
        stages=[
            Stage(id="a", name="A", agent_id="scanner", outputs=IOContract(required=["candidates"])),
            Stage(id="b", name="B", agent_id="scanner", inputs=IOContract(required=["candidates"])),
        ],
    )
    assert pipelines.validate(healthy, agents={"scanner"}) == []


def test_shipped_pipeline_matches_the_shipped_agents(
    pipelines: PipelineRegistry, agents: AgentRegistry
) -> None:
    """The default pipeline validates against the default agent roster."""
    agents.ensure_defaults()
    pipeline = pipelines.ensure_defaults()
    assert pipeline is not None
    document = pipelines.get(pipeline.id)
    assert pipelines.validate(document, agents={item.id for item in agents.list()}) == []
    assert document.stages[-1].kind is StageKind.HUMAN_APPROVAL


# --------------------------------------------------------------------------
# tools
# --------------------------------------------------------------------------
def _write_bars(store: TimeSeriesStore, symbol: str, days: int = 40) -> None:
    """Write a deterministic price path into the real Parquet store."""
    start = datetime(2026, 1, 2, tzinfo=timezone.utc)
    bars = []
    for index in range(days):
        price = 100.0 + index
        bars.append(
            Bar(
                symbol=symbol,
                ts=start + timedelta(days=index),
                open=price,
                high=price * 1.01,
                low=price * 0.99,
                close=price,
                volume=1_000_000 + index,
                source="test",
            )
        )
    store.write_bars(bars)


async def test_toolset_calls_the_real_library(database: Settings) -> None:
    """resolve_universe and get_series read genuine registry and store data."""
    store = TimeSeriesStore()
    reset_store(store)
    _write_bars(store, "AAA")
    universes = UniverseRegistry()
    universes.create("Test book", ["AAA", "BBB"], universe_id="test-book")

    toolset = LibraryToolset(store=store, universes=universes, settings=database)
    resolved = await toolset.execute("resolve_universe", {"ref": "test-book"})
    assert resolved.is_error is False
    assert resolved.data["symbols"] == ["AAA", "BBB"]

    series = await toolset.execute("get_series", {"symbol": "AAA", "points": 5})
    assert series.data["count"] == 40
    assert len(series.data["tail"]) == 5
    assert series.data["last_close"] == pytest.approx(139.0)

    empty = await toolset.execute("get_series", {"symbol": "ZZZ"})
    assert empty.data["count"] == 0
    assert "no bars" in empty.data["detail"]

    unknown = await toolset.execute("not_a_tool", {})
    assert unknown.is_error is True

    blocked = await toolset.execute(
        "get_series", {"symbol": "BBB"}, scope={"symbols": ["AAA"]}
    )
    assert blocked.is_error is True
    assert "outside this stage's scope" in blocked.content


async def test_toolset_reports_a_missing_subsystem_instead_of_faking_it(
    database: Settings,
) -> None:
    """A tool whose store is not implemented answers with an honest error."""

    class _Unbuilt:
        """Stands in for a store whose persistence is not implemented yet."""

        def create(self, *args: object, **kwargs: object) -> None:
            """Raise the way an unimplemented store does."""
            raise NotImplementedError("findings store")

    toolset = LibraryToolset(findings=_Unbuilt(), settings=database)
    result = await toolset.execute("save_finding", {"title": "t", "summary": "s"})
    assert result.is_error is True
    assert "not available in this build" in result.content


def test_structured_output_extraction() -> None:
    """The last fenced JSON block is the stage's structured answer."""
    assert extract_structured_output("no json here") is None
    text = 'prose\n```json\n{"a": 1}\n```\nmore\n```json\n{"b": 2}\n```'
    assert extract_structured_output(text) == {"b": 2}
    assert extract_structured_output('{"bare": true}') == {"bare": True}


# --------------------------------------------------------------------------
# running a pipeline
# --------------------------------------------------------------------------
def _seed_run_fixtures(
    agents: AgentRegistry, pipelines: PipelineRegistry, universes: UniverseRegistry
) -> str:
    """Create the agents, universe and pipeline the run tests share."""
    agents.create(
        "Scanner",
        "Opportunity discovery",
        "Scan the universe and list candidates.",
        "mock",
        "mock-small",
        agent_id="scanner",
        tools=["resolve_universe", "get_series"],
        budget_usd=1.0,
    )
    agents.create(
        "Analyst",
        "Evidence synthesis",
        "Summarise the candidates.",
        "mock",
        "mock-small",
        agent_id="analyst",
        tools=["get_series"],
    )
    agents.create(
        "Human Review",
        "Mandatory decision authority",
        "A person decides.",
        "none",
        "",
        agent_id="human-review",
        kind=AgentKind.HUMAN,
        locked=True,
    )
    universes.create("Test book", ["AAA", "BBB"], universe_id="test-book")
    pipeline = pipelines.create(
        "Gated cycle",
        pipeline_id="gated-cycle",
        objective="Find and check one candidate.",
        stages=[
            Stage(
                id="scan",
                name="Scan",
                agent_id="scanner",
                objective="Resolve the universe and pick candidates.",
                scope={"universe": "test-book"},
                outputs=IOContract(text="candidate list", required=["candidates"]),
                handoff="Send candidates to Analyse",
                handoff_to=["analyse"],
            ),
            Stage(
                id="analyse",
                name="Analyse",
                agent_id="analyst",
                objective="Check the candidates.",
                inputs=IOContract(required=["candidates"]),
                outputs=IOContract(required=["verdict"]),
                handoff_to=["approval"],
            ),
            Stage(
                id="approval",
                name="Human Approval",
                kind=StageKind.HUMAN_APPROVAL,
                agent_id="human-review",
                objective="Approve before anything is promoted.",
                locked=True,
            ),
        ],
    )
    return pipeline.id


async def test_full_pipeline_run_pauses_for_approval_and_resumes(
    database: Settings, bus: EventBus, agents: AgentRegistry, pipelines: PipelineRegistry
) -> None:
    """A real run: tools called, contracts met, approval blocks, approval resumes."""
    store = TimeSeriesStore()
    reset_store(store)
    _write_bars(store, "AAA")
    universes = UniverseRegistry()
    pipeline_id = _seed_run_fixtures(agents, pipelines, universes)

    frames: list[dict[str, object]] = []
    bus.subscribe("runs", lambda event: frames.append(event.payload))
    approvals_seen: list[dict[str, object]] = []
    bus.subscribe("approvals", lambda event: approvals_seen.append(event.payload))

    provider = MockProvider(
        responder=scripted_responder(
            tool_plan={
                "Scan": [
                    ToolCall(id="c1", name="resolve_universe", arguments={"ref": "test-book"}),
                ],
                "Analyse": [
                    ToolCall(id="c2", name="get_series", arguments={"symbol": "AAA", "points": 3}),
                ],
            }
        )
    )
    runner = build_runner(
        bus=bus,
        agents=agents,
        pipelines=pipelines,
        provider=provider,
        toolset=LibraryToolset(store=store, universes=universes, settings=database),
    )

    run = await runner.start(pipeline_id, inputs={"benchmark": "SPY"}, objective="test run")
    assert run.status in {RunStatus.QUEUED, RunStatus.RUNNING}

    await wait_for(lambda: runner.get(run.id).status is RunStatus.AWAITING_APPROVAL)
    blocked = runner.get(run.id)
    assert [stage.status for stage in blocked.stages] == [
        StageStatus.SUCCEEDED,
        StageStatus.SUCCEEDED,
        StageStatus.AWAITING_APPROVAL,
    ]

    # The two agent stages really called the library.
    scan = blocked.stages[0]
    assert scan.tool_calls[0]["name"] == "resolve_universe"
    assert scan.outputs["candidates"] == ["Scan:candidates"]
    assert scan.usage.total_tokens > 0
    assert scan.usage.cost_usd > 0
    analyse = blocked.stages[1]
    assert analyse.tool_calls[0]["name"] == "get_series"

    tool_artifacts = [
        artifact
        for artifact in runner.artifacts(run.id)
        if artifact.kind == "tool_result"
    ]
    assert {artifact.stage_id for artifact in tool_artifacts} == {"scan", "analyse"}
    resolve_artifact = next(a for a in tool_artifacts if a.name.startswith("resolve_universe"))
    assert resolve_artifact.data["symbols"] == ["AAA", "BBB"]

    # The approval record exists and is pending.
    pending = runner.approvals(state=ApprovalState.PENDING)
    assert len(pending) == 1
    assert pending[0].run_id == run.id
    assert runner.pending_approvals() == 1
    assert any(frame.get("phase") == "awaiting_approval" for frame in frames)
    assert any(frame.get("phase") == "requested" for frame in approvals_seen)

    # ... and the run genuinely stops there.
    await asyncio.sleep(0.1)
    assert runner.get(run.id).status is RunStatus.AWAITING_APPROVAL

    await runner.approve(pending[0].id, by="tester", comment="looks right")
    await wait_for(lambda: runner.get(run.id).status is RunStatus.SUCCEEDED, timeout=15.0)

    finished = runner.get(run.id)
    assert finished.status is RunStatus.SUCCEEDED
    assert [stage.status for stage in finished.stages] == [StageStatus.SUCCEEDED] * 3
    assert finished.stages[2].approval_id == pending[0].id
    assert finished.outputs["scan"]["candidates"] == ["Scan:candidates"]
    assert finished.outputs["analyse"]["verdict"] == ["Analyse:verdict"]

    # Cost accounting: the run total is the sum of its stages, and it is real.
    stage_cost = sum(stage.usage.cost_usd for stage in finished.stages)
    assert finished.cost_usd == pytest.approx(stage_cost)
    assert finished.cost_usd > 0
    assert runner.spend(run_id=run.id)["run"]["used"] == pytest.approx(finished.cost_usd, rel=1e-6)

    # Logs and live frames tell the same story.
    logs = runner.logs(run.id)
    assert any("Run complete" in entry["message"] for entry in logs)
    assert any(entry["message"].startswith("tool resolve_universe") for entry in logs)
    phases = [frame.get("phase") for frame in frames if frame.get("kind") == "run"]
    assert phases[0] == "queued"
    assert "complete" in phases
    assert all(frame["source"] == "agents" for frame in frames)

    stats = runner.agent_stats("scanner")
    assert stats["stage_runs"] == 1
    assert stats["failed"] == 0
    assert stats["failure_rate"] == 0.0
    assert stats["cost_usd"] > 0

    counts = runner.counts()
    assert counts["succeeded"] == 1
    assert counts["active"] == 0
    assert counts["pending_approvals"] == 0


async def test_rejecting_the_gate_kills_the_run(
    database: Settings, bus: EventBus, agents: AgentRegistry, pipelines: PipelineRegistry
) -> None:
    """Rejection terminates the run instead of quietly finishing it."""
    store = TimeSeriesStore()
    reset_store(store)
    _write_bars(store, "AAA")
    universes = UniverseRegistry()
    pipeline_id = _seed_run_fixtures(agents, pipelines, universes)
    runner = build_runner(
        bus=bus,
        agents=agents,
        pipelines=pipelines,
        provider=MockProvider(responder=scripted_responder()),
        toolset=LibraryToolset(store=store, universes=universes, settings=database),
    )

    run = await runner.start(pipeline_id)
    await wait_for(lambda: runner.get(run.id).status is RunStatus.AWAITING_APPROVAL)
    approval = runner.approvals(state=ApprovalState.PENDING, run_id=run.id)[0]

    rejected = await runner.reject(approval.id, by="tester", comment="not convinced")
    assert rejected.state is ApprovalState.REJECTED
    await wait_for(lambda: runner.get(run.id).status is RunStatus.KILLED)

    killed = runner.get(run.id)
    assert killed.error == "rejected at the human-approval gate"
    assert killed.stages[2].status is StageStatus.KILLED
    assert runner.pending_approvals() == 0
    with pytest.raises(ValueError, match="already"):
        await runner.reject(approval.id)


async def test_approval_survives_a_lost_waiter(
    database: Settings, bus: EventBus, agents: AgentRegistry, pipelines: PipelineRegistry
) -> None:
    """A run blocked when the process died is resumed by approving it later."""
    store = TimeSeriesStore()
    reset_store(store)
    _write_bars(store, "AAA")
    universes = UniverseRegistry()
    pipeline_id = _seed_run_fixtures(agents, pipelines, universes)
    runner = build_runner(
        bus=bus,
        agents=agents,
        pipelines=pipelines,
        provider=MockProvider(responder=scripted_responder()),
        toolset=LibraryToolset(store=store, universes=universes, settings=database),
    )
    run = await runner.start(pipeline_id)
    await wait_for(lambda: runner.get(run.id).status is RunStatus.AWAITING_APPROVAL)

    # Simulate a restart: the process that held the waiter is gone, but the
    # approval record and the run rows are still in the database.
    orphaned = list(runner._tasks.values())  # noqa: SLF001 - simulating a restart
    runner._waiters.clear()  # noqa: SLF001
    runner._tasks.clear()  # noqa: SLF001

    resumed = await runner.resume(run.id)
    assert resumed.id == run.id
    await wait_for(lambda: runner.get(run.id).status is RunStatus.SUCCEEDED, timeout=15.0)
    assert runner.get(run.id).stages[2].outputs["decision"] == "approved"

    # The abandoned task cannot rewrite the run it no longer owns.
    for task in orphaned:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert runner.get(run.id).status is RunStatus.SUCCEEDED


async def test_budget_stops_a_stage_before_it_spends(
    database: Settings, bus: EventBus, agents: AgentRegistry, pipelines: PipelineRegistry
) -> None:
    """A run that would breach the per-run budget fails instead of spending."""
    store = TimeSeriesStore()
    reset_store(store)
    universes = UniverseRegistry()
    pipeline_id = _seed_run_fixtures(agents, pipelines, universes)
    runner = build_runner(
        bus=bus,
        agents=agents,
        pipelines=pipelines,
        provider=MockProvider(responder=scripted_responder()),
        toolset=LibraryToolset(store=store, universes=universes, settings=database),
    )
    runner.ledger.set_budgets(Budgets(per_run_usd=0.0001, daily_usd=10.0, monthly_usd=100.0))

    run = await runner.start(pipeline_id, wait=True)
    assert run.status is RunStatus.FAILED
    assert "per_run budget exceeded" in (run.error or "")
    assert run.stages[0].status is StageStatus.PENDING
    assert run.cost_usd == 0.0
    assert runner.ledger.run_total(run.id) == 0.0


async def test_daily_budget_counts_what_earlier_runs_spent(
    database: Settings, bus: EventBus, agents: AgentRegistry, pipelines: PipelineRegistry
) -> None:
    """Spend already recorded today blocks the next run."""
    store = TimeSeriesStore()
    reset_store(store)
    universes = UniverseRegistry()
    pipeline_id = _seed_run_fixtures(agents, pipelines, universes)
    runner = build_runner(
        bus=bus,
        agents=agents,
        pipelines=pipelines,
        provider=MockProvider(responder=scripted_responder()),
        toolset=LibraryToolset(store=store, universes=universes, settings=database),
    )
    runner.ledger.set_budgets(Budgets(per_run_usd=5.0, daily_usd=1.0, monthly_usd=100.0))
    runner.ledger.record(
        provider="mock", model="mock-small", usage=Usage(cost_usd=0.99), run_id="EARLIER"
    )
    run = await runner.start(pipeline_id, wait=True)
    assert run.status is RunStatus.FAILED
    assert "daily budget exceeded" in (run.error or "")


async def test_contract_violation_fails_the_stage(
    database: Settings, bus: EventBus, agents: AgentRegistry, pipelines: PipelineRegistry
) -> None:
    """A stage that does not honour its output contract fails the run."""
    store = TimeSeriesStore()
    reset_store(store)
    universes = UniverseRegistry()
    pipeline_id = _seed_run_fixtures(agents, pipelines, universes)
    provider = MockProvider(
        turns=[MockTurn(text='I forgot the contract.\n```json\n{"unrelated": 1}\n```')]
    )
    runner = build_runner(
        bus=bus,
        agents=agents,
        pipelines=pipelines,
        provider=provider,
        toolset=LibraryToolset(store=store, universes=universes, settings=database),
    )
    run = await runner.start(pipeline_id, wait=True)
    assert run.status is RunStatus.FAILED
    assert "output contract" in (run.error or "")
    assert run.stages[0].status is StageStatus.FAILED
    assert "candidates" in (run.stages[0].error or "")
    # The tokens it did burn are still accounted for.
    assert run.stages[0].usage.cost_usd > 0


async def test_cancelling_a_running_pipeline_kills_it(
    database: Settings, bus: EventBus, agents: AgentRegistry, pipelines: PipelineRegistry
) -> None:
    """Cancel stops a run that is waiting on a person and marks it killed."""
    store = TimeSeriesStore()
    reset_store(store)
    universes = UniverseRegistry()
    pipeline_id = _seed_run_fixtures(agents, pipelines, universes)
    runner = build_runner(
        bus=bus,
        agents=agents,
        pipelines=pipelines,
        provider=MockProvider(responder=scripted_responder()),
        toolset=LibraryToolset(store=store, universes=universes, settings=database),
    )
    run = await runner.start(pipeline_id)
    await wait_for(lambda: runner.get(run.id).status is RunStatus.AWAITING_APPROVAL)
    cancelled = await runner.kill(run.id, reason="operator cancelled")
    assert cancelled.status is RunStatus.KILLED
    assert runner.counts()["killed"] == 1


async def test_excluded_stage_is_skipped_not_run(
    database: Settings, bus: EventBus, agents: AgentRegistry, pipelines: PipelineRegistry
) -> None:
    """A stage excluded from the run is recorded as skipped, never executed."""
    store = TimeSeriesStore()
    reset_store(store)
    universes = UniverseRegistry()
    pipeline_id = _seed_run_fixtures(agents, pipelines, universes)
    pipelines.update_stage(pipeline_id, "analyse", include_in_run=False)
    provider = MockProvider(responder=scripted_responder())
    runner = build_runner(
        bus=bus,
        agents=agents,
        pipelines=pipelines,
        provider=provider,
        toolset=LibraryToolset(store=store, universes=universes, settings=database),
    )
    run = await runner.start(pipeline_id)
    await wait_for(lambda: runner.get(run.id).status is RunStatus.AWAITING_APPROVAL)
    stages = runner.get(run.id).stages
    assert stages[1].status is StageStatus.SKIPPED
    assert stages[1].usage.total_tokens == 0
    assert len(provider.requests) == 1

    await runner.kill(run.id)


async def test_unknown_pipeline_and_invalid_document_are_refused(
    database: Settings, bus: EventBus, agents: AgentRegistry, pipelines: PipelineRegistry
) -> None:
    """Starting a run validates first and refuses to execute a broken pipeline."""
    runner = build_runner(
        bus=bus, agents=agents, pipelines=pipelines, provider=MockProvider()
    )
    with pytest.raises(KeyError):
        await runner.start("nope")

    pipelines.create("Broken", pipeline_id="broken", stages=[Stage(id="a", name="A")])
    with pytest.raises(ValueError, match="does not validate"):
        await runner.start("broken")


async def test_stage_tools_narrow_the_agent_grant(
    database: Settings, bus: EventBus, agents: AgentRegistry, pipelines: PipelineRegistry
) -> None:
    """A stage may take tools away from its agent but never add one."""
    store = TimeSeriesStore()
    reset_store(store)
    universes = UniverseRegistry()
    pipeline_id = _seed_run_fixtures(agents, pipelines, universes)
    pipelines.update_stage(pipeline_id, "scan", tools=["get_series", "run_backtest"])
    provider = MockProvider(responder=scripted_responder())
    runner = build_runner(
        bus=bus,
        agents=agents,
        pipelines=pipelines,
        provider=provider,
        toolset=LibraryToolset(store=store, universes=universes, settings=database),
    )
    run = await runner.start(pipeline_id)
    await wait_for(lambda: runner.get(run.id).status is RunStatus.AWAITING_APPROVAL)
    granted = runner.get(run.id).stages[0].inputs["tools"]
    # run_backtest was never granted to the scanner agent, so the stage cannot add it.
    assert granted == ["get_series"]
    await runner.kill(run.id)


# --------------------------------------------------------------------------
# pause, resume and retry
# --------------------------------------------------------------------------
async def test_pause_stops_at_a_stage_boundary_and_resume_finishes_the_run(
    database: Settings, bus: EventBus, agents: AgentRegistry, pipelines: PipelineRegistry
) -> None:
    """Pausing parks a run between stages; resuming continues from that stage."""
    agents.create("Scanner", "scan", "prompt", "mock", "mock-small", agent_id="scanner")
    agents.create("Analyst", "analyse", "prompt", "mock", "mock-small", agent_id="analyst")
    pipelines.create(
        "Two step",
        pipeline_id="two-step",
        stages=[
            Stage(id="one", name="One", agent_id="scanner", outputs=IOContract(required=["a"])),
            Stage(id="two", name="Two", agent_id="analyst", outputs=IOContract(required=["b"])),
        ],
    )
    provider = MockProvider(responder=scripted_responder())
    runner = build_runner(bus=bus, agents=agents, pipelines=pipelines, provider=provider)

    frames: list[dict[str, object]] = []
    bus.subscribe("runs", lambda event: frames.append(event.payload))

    # Pausing a queued run parks it in front of its first stage, so nothing runs.
    run = runner._create_run(  # noqa: SLF001 - exercising the boundary directly
        pipelines.get("two-step"), trigger="test", inputs={}, objective=None, name="Two step"
    )
    paused = await runner.pause(run.id, reason="tester pressed pause")
    assert paused.status is RunStatus.PAUSED
    assert paused.resume_index == 0
    assert paused.paused_at is not None
    assert paused.as_dict()["can_resume"] is True
    assert paused.as_dict()["can_pause"] is False
    assert [stage.status for stage in paused.stages] == [StageStatus.PENDING] * 2
    assert any(frame.get("phase") == "paused" for frame in frames)

    # A paused run does not occupy a worker.
    assert runner.active_count() == 0

    resumed = await runner.unpause(run.id)
    assert resumed.status is RunStatus.RUNNING
    await wait_for(lambda: runner.get(run.id).status in TERMINAL_RUN_STATES, timeout=20.0)
    finished = runner.get(run.id)
    assert finished.status is RunStatus.SUCCEEDED
    assert [stage.status for stage in finished.stages] == [StageStatus.SUCCEEDED] * 2

    # Pausing something already finished is refused rather than silently ignored.
    with pytest.raises(ValueError, match="already finished"):
        await runner.pause(run.id)


async def test_retry_reruns_only_from_the_failed_stage(
    database: Settings, bus: EventBus, agents: AgentRegistry, pipelines: PipelineRegistry
) -> None:
    """A retry keeps what succeeded and re-executes from the first failure."""
    agents.create("Scanner", "scan", "prompt", "mock", "mock-small", agent_id="scanner")
    agents.create("Analyst", "analyse", "prompt", "mock", "mock-small", agent_id="analyst")
    pipelines.create(
        "Fragile",
        pipeline_id="fragile",
        stages=[
            Stage(id="one", name="One", agent_id="scanner", outputs=IOContract(required=["a"])),
            Stage(id="two", name="Two", agent_id="analyst", outputs=IOContract(required=["b"])),
        ],
    )

    calls: list[str] = []
    base = scripted_responder()

    def flaky(request: MockRequest) -> MockTurn:
        """Fail the second stage exactly once, then behave."""
        stage = "Two" if "Two" in request.system else "One"
        calls.append(stage)
        if stage == "Two" and calls.count("Two") == 1:
            raise RuntimeError("vendor hiccup")
        return base(request)

    provider = MockProvider(responder=flaky)
    runner = build_runner(bus=bus, agents=agents, pipelines=pipelines, provider=provider)

    run = await runner.start("fragile", wait=True)
    assert run.status is RunStatus.FAILED
    statuses = {stage.stage_id: stage.status for stage in run.stages}
    assert statuses["one"] is StageStatus.SUCCEEDED
    assert statuses["two"] is StageStatus.FAILED
    payload = run.as_dict()
    assert payload["can_retry"] is True
    assert payload["failed_stage_id"] == "two"
    succeeded_once = calls.count("One")

    await runner.retry(run.id)
    await wait_for(lambda: runner.get(run.id).status in TERMINAL_RUN_STATES, timeout=20.0)
    retried = runner.get(run.id)
    assert retried.status is RunStatus.SUCCEEDED
    assert retried.error is None
    assert all(stage.status is StageStatus.SUCCEEDED for stage in retried.stages)
    # The stage that already succeeded was not paid for a second time.
    assert calls.count("One") == succeeded_once

    # Nothing left to retry once every stage has succeeded.
    with pytest.raises(ValueError, match="no failed or pending stage"):
        await runner.retry(run.id)
    with pytest.raises(KeyError):
        await runner.retry(run.id, stage_id="nope")
