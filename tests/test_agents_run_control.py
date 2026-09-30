"""Run control: per-run overrides, single-stage runs, replay and estimation.

These are the entry points the Pipeline Builder's *Run this stage*, the run
inspector's *Replay from stage...*, the pre-run cost line and AutoPipeline's
per-run model overrides call. Everything runs offline against
:class:`tradinglib.agents.providers.MockProvider`, whose token counts come from the
real prompt text, so the estimator's arithmetic is exercised for real.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator

import pytest

from tradinglib.agents.approvals import VALIDATION_TARGETS
from tradinglib.agents.autopipeline import AutoPipeline, Schedule
from tradinglib.agents.pipeline import IOContract, PipelineRegistry, Stage, StageKind
from tradinglib.agents.providers import MockProvider, MockRequest, MockTurn, ProviderRegistry
from tradinglib.agents.registry import AgentRegistry
from tradinglib.agents.runner import (
    ESTIMATE_FALLBACK_TOKENS,
    LibraryToolset,
    PipelineRunner,
    RunStatus,
    StageStatus,
    apply_stage_overrides,
)
from tradinglib.config import Settings
from tradinglib.data.store import reset_store
from tradinglib.data.universe import reset_registries
from tradinglib.db import init_db
from tradinglib.events import EventBus

REQUIRED_KEYS_RE = re.compile(r"containing exactly these keys: ([^.]+)\.")


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


def responder(request: MockRequest) -> MockTurn:
    """Answer every stage with exactly the keys its output contract asks for."""
    system = request.system or ""
    match = re.search(r"running the '([^']+)' stage", system)
    stage_name = match.group(1) if match else "stage"
    keys = REQUIRED_KEYS_RE.search(system)
    payload: dict[str, object] = {}
    if keys:
        for key in [part.strip() for part in keys.group(1).split(",")]:
            payload[key] = [f"{stage_name}:{key}"]
    else:
        payload["summary"] = stage_name
    return MockTurn(text=f"{stage_name} done.\n```json\n{json.dumps(payload)}\n```")


def build_runner(
    bus: EventBus, agents: AgentRegistry, pipelines: PipelineRegistry
) -> PipelineRunner:
    """Create a runner wired to the deterministic mock provider."""
    return PipelineRunner(
        bus,
        pipelines=pipelines,
        agents=agents,
        providers=ProviderRegistry(providers={"mock": MockProvider(responder=responder)}),
        toolset=LibraryToolset(),
        audit=None,
    )


def three_stage(pipelines: PipelineRegistry, agents: AgentRegistry) -> str:
    """Create a three-stage pipeline whose agents all resolve to the mock."""
    for agent_id in ("scanner", "quant", "writer"):
        agents.create(
            name=agent_id.title(),
            role=f"{agent_id} stage",
            system_prompt=f"You are the {agent_id}.",
            provider="mock",
            model="mock-small",
            agent_id=agent_id,
        )
    pipeline = pipelines.create(
        "Research cycle",
        objective="find something",
        stages=[
            Stage(
                id="scan",
                name="Scan",
                agent_id="scanner",
                outputs=IOContract(required=["candidates"]),
            ),
            Stage(
                id="test",
                name="Test",
                agent_id="quant",
                inputs=IOContract(required=["candidates"]),
                outputs=IOContract(required=["verdict"]),
            ),
            Stage(id="write", name="Write", agent_id="writer"),
        ],
    )
    return pipeline.id


# --------------------------------------------------------------------------
# per-run overrides
# --------------------------------------------------------------------------
def test_stage_overrides_apply_to_the_copy_and_report_what_changed(
    pipelines: PipelineRegistry, agents: AgentRegistry
) -> None:
    """An override edits the run's copy, never the saved pipeline."""
    pipeline_id = three_stage(pipelines, agents)
    copy = pipelines.get(pipeline_id)
    applied = apply_stage_overrides(
        copy, {"scan": {"model": "mock-large"}, "write": {"include_in_run": False}}
    )
    assert applied == {
        "scan": {"model": "mock-large"},
        "write": {"include_in_run": False},
    }
    assert copy.stage("scan").model == "mock-large"
    assert copy.stage("write").include_in_run is False
    # The saved document is untouched.
    saved = pipelines.get(pipeline_id)
    assert saved.stage("scan").model is None
    assert saved.stage("write").include_in_run is True


def test_stage_overrides_refuse_fields_that_would_break_comparability(
    pipelines: PipelineRegistry, agents: AgentRegistry
) -> None:
    """Only model, provider and inclusion may be overridden per run."""
    copy = pipelines.get(three_stage(pipelines, agents))
    with pytest.raises(ValueError, match="cannot be overridden per run"):
        apply_stage_overrides(copy, {"scan": {"objective": "something else"}})
    with pytest.raises(KeyError):
        apply_stage_overrides(copy, {"nope": {"model": "mock-large"}})


def test_the_approval_boundary_cannot_be_switched_off_for_a_run(
    pipelines: PipelineRegistry, agents: AgentRegistry
) -> None:
    """The mandatory human gate is not something a run may opt out of."""
    agents.create(
        name="Human review",
        role="approval",
        system_prompt="A person decides.",
        provider="none",
        model="",
        kind="human",
        agent_id="human-review",
    )
    pipeline = pipelines.create(
        "Gated",
        stages=[
            Stage(
                id="approval",
                name="Human Approval",
                kind=StageKind.HUMAN_APPROVAL,
                agent_id="human-review",
                locked=True,
            )
        ],
    )
    copy = pipelines.get(pipeline.id)
    with pytest.raises(ValueError, match="cannot be switched off"):
        apply_stage_overrides(copy, {"approval": {"include_in_run": False}})
    with pytest.raises(ValueError, match="never calls a model"):
        apply_stage_overrides(copy, {"approval": {"model": "mock-small"}})


# --------------------------------------------------------------------------
# single-stage runs
# --------------------------------------------------------------------------
async def test_run_stage_executes_only_that_stage(
    bus: EventBus, agents: AgentRegistry, pipelines: PipelineRegistry
) -> None:
    """A single-stage run is a real run with every other stage switched off."""
    pipeline_id = three_stage(pipelines, agents)
    runner = build_runner(bus, agents, pipelines)
    run = await runner.run_stage(pipeline_id, "test", inputs={"candidates": ["NVDA"]})

    assert run.status is RunStatus.SUCCEEDED
    states = {stage.stage_id: stage.status for stage in run.stages}
    assert states["test"] is StageStatus.SUCCEEDED
    assert states["scan"] is StageStatus.SKIPPED
    assert states["write"] is StageStatus.SKIPPED
    assert run.overrides["scan"] == {"include_in_run": False}
    assert run.inputs == {"candidates": ["NVDA"]}
    # The saved pipeline still has all three stages enabled.
    assert all(stage.include_in_run for stage in pipelines.get(pipeline_id).stages)


async def test_run_stage_refuses_what_cannot_run_alone(
    bus: EventBus, agents: AgentRegistry, pipelines: PipelineRegistry
) -> None:
    """The approval boundary and an unassigned stage are refused, with a reason."""
    pipeline_id = three_stage(pipelines, agents)
    pipelines.add_stage(pipeline_id, {"id": "orphan", "name": "Orphan"})
    runner = build_runner(bus, agents, pipelines)
    with pytest.raises(ValueError, match="no agent assigned"):
        await runner.run_stage(pipeline_id, "orphan")
    with pytest.raises(KeyError):
        await runner.run_stage(pipeline_id, "missing-stage")


# --------------------------------------------------------------------------
# replay
# --------------------------------------------------------------------------
async def test_replay_creates_a_new_run_and_inherits_earlier_stages(
    bus: EventBus, agents: AgentRegistry, pipelines: PipelineRegistry
) -> None:
    """Replaying never mutates the original run - the ledger stays append-only."""
    pipeline_id = three_stage(pipelines, agents)
    runner = build_runner(bus, agents, pipelines)
    original = await runner.start(pipeline_id, wait=True)
    assert original.status is RunStatus.SUCCEEDED

    replay = await runner.replay(
        original.id, stage_id="test", stage_overrides={"test": {"model": "mock-large"}}, wait=True
    )
    assert replay.id != original.id
    assert replay.replay_of == original.id
    assert replay.replay_from == "test"
    assert replay.status is RunStatus.SUCCEEDED

    states = {stage.stage_id: stage for stage in replay.stages}
    # "scan" ran in the original; the replay inherits its output instead of
    # paying for it again, and records that honestly as skipped.
    assert states["scan"].status is StageStatus.SKIPPED
    assert states["scan"].outputs == original.outputs["scan"]
    assert states["scan"].usage.cost_usd == 0.0
    assert states["test"].status is StageStatus.SUCCEEDED
    assert states["test"].model == "mock-large"

    # The original is exactly as it was.
    reread = runner.get(original.id, include_logs=False)
    assert reread.status is RunStatus.SUCCEEDED
    assert reread.replay_of is None
    assert {s.stage_id: s.status for s in reread.stages}["scan"] is StageStatus.SUCCEEDED


async def test_replay_refuses_a_run_that_is_still_executing(
    bus: EventBus, agents: AgentRegistry, pipelines: PipelineRegistry
) -> None:
    """A run has to be finished before it can be replayed."""
    pipeline_id = three_stage(pipelines, agents)
    runner = build_runner(bus, agents, pipelines)
    run = await runner.start(pipeline_id, wait=True)
    runner._update_run(run.id, status=RunStatus.RUNNING)  # noqa: SLF001 - forcing the state
    with pytest.raises(ValueError, match="still"):
        await runner.replay(run.id)


# --------------------------------------------------------------------------
# estimation
# --------------------------------------------------------------------------
async def test_estimate_falls_back_to_a_stated_budget_then_learns_from_history(
    bus: EventBus, agents: AgentRegistry, pipelines: PipelineRegistry
) -> None:
    """With no history the estimate says so; after a run it uses the real usage."""
    pipeline_id = three_stage(pipelines, agents)
    runner = build_runner(bus, agents, pipelines)

    cold = await runner.estimate(pipeline_id)
    assert cold["stage_count"] == 3
    assert all("no history yet" in row["basis"] for row in cold["stages"])
    assert cold["stages"][0]["input_tokens"] == ESTIMATE_FALLBACK_TOKENS[0]

    run = await runner.start(pipeline_id, wait=True)
    assert run.status is RunStatus.SUCCEEDED

    warm = await runner.estimate(pipeline_id)
    assert all("previous run(s) of this stage" in row["basis"] for row in warm["stages"])
    # The mock prompts are far shorter than the cold-start budget, so a learned
    # estimate must come out materially below it rather than repeating it.
    assert warm["stages"][0]["input_tokens"] < ESTIMATE_FALLBACK_TOKENS[0]
    assert warm["seconds"] >= 0.0


async def test_estimate_excludes_an_unpriced_model_from_the_total(
    bus: EventBus, agents: AgentRegistry, pipelines: PipelineRegistry
) -> None:
    """A model with no published price is reported, never counted as free."""
    pipeline_id = three_stage(pipelines, agents)
    runner = build_runner(bus, agents, pipelines)

    priced = await runner.estimate(pipeline_id)
    assert priced["priced"] is True
    assert priced["total_usd"] > 0.0
    assert all(row["cost_usd"] is not None for row in priced["stages"])

    # One stage now points at a model no provider offers. Its cost is unknown,
    # so the run is reported as not fully priced and that stage contributes
    # nothing to the total instead of being treated as free.
    unknown = await runner.estimate(
        pipeline_id, stage_overrides={"scan": {"model": "no-such-model-9"}}
    )
    assert unknown["priced"] is False
    scan_row = next(row for row in unknown["stages"] if row["stage_id"] == "scan")
    assert scan_row["cost_usd"] is None
    assert "no published price" in scan_row["basis"]
    assert unknown["total_usd"] == pytest.approx(
        priced["total_usd"]
        - next(row for row in priced["stages"] if row["stage_id"] == "scan")["cost_usd"]
    )


# --------------------------------------------------------------------------
# agent version archive
# --------------------------------------------------------------------------
def test_editing_an_agent_freezes_the_configuration_it_replaced(
    agents: AgentRegistry,
) -> None:
    """The archive holds the config each version actually ran with."""
    definition = agents.create(
        name="Scanner",
        role="scan",
        system_prompt="Original prompt.",
        provider="mock",
        model="mock-small",
        agent_id="scanner",
    )
    assert definition.version == 1

    agents.update("scanner", system_prompt="Second prompt.")
    agents.update("scanner", temperature=0.4)

    versions = agents.versions("scanner")
    numbers = [entry["version"] for entry in versions]
    assert numbers == [3, 2, 1]
    assert versions[0]["current"] is True and versions[0]["frozen"] is False
    assert versions[-1]["frozen"] is True

    frozen = agents.version("scanner", 1)
    assert frozen.system_prompt == "Original prompt."
    assert frozen.temperature == 0.0

    restored = agents.restore("scanner", 1)
    assert restored.system_prompt == "Original prompt."
    # The restore moves history forward rather than rewinding it.
    assert restored.version > 3
    assert agents.version("scanner", 3).temperature == 0.4


def test_deleting_an_agent_takes_its_archive_with_it(agents: AgentRegistry) -> None:
    """A deleted agent leaves no orphaned versions behind."""
    agents.create(
        name="Temp",
        role="temp",
        system_prompt="Prompt.",
        provider="mock",
        model="mock-small",
        agent_id="temp",
    )
    agents.update("temp", notes="edited")
    assert agents.versions("temp")
    agents.delete("temp")
    with pytest.raises(KeyError):
        agents.versions("temp")


# --------------------------------------------------------------------------
# AutoPipeline per-run configuration
# --------------------------------------------------------------------------
def test_schedule_overrides_become_per_run_stage_overrides() -> None:
    """Model picks and stage toggles are translated for ``runner.start``."""
    schedule = Schedule(
        id="SCH-1",
        pipeline_id="p",
        cron="@daily",
        model_overrides={"scan": "mock-large"},
        stage_toggles={"write": False},
    )
    assert AutoPipeline.stage_overrides(schedule) == {
        "scan": {"model": "mock-large"},
        "write": {"include_in_run": False},
    }


def test_a_schedule_stores_its_brief_toggles_and_overrides(
    database: Settings, bus: EventBus, agents: AgentRegistry, pipelines: PipelineRegistry
) -> None:
    """The per-run form round-trips through create/update."""
    pipeline_id = three_stage(pipelines, agents)
    auto = AutoPipeline(build_runner(bus, agents, pipelines), bus, audit=None)
    schedule = auto.create(
        pipeline_id,
        "@daily",
        name="Nightly",
        inputs={"query": "AI infrastructure", "tickers": ["NVDA"], "top_n": 5},
        auto_advance=True,
        model_overrides={"scan": "mock-large", "test": ""},
        stage_toggles={"write": False},
    )
    assert schedule.auto_advance is True
    assert schedule.inputs["query"] == "AI infrastructure"
    # An empty selection means "use the stage's own model" and is not stored.
    assert schedule.model_overrides == {"scan": "mock-large"}
    assert schedule.stage_toggles == {"write": False}

    updated = auto.update(schedule.id, auto_advance=False, model_overrides={})
    assert updated.auto_advance is False
    assert updated.model_overrides == {}


async def test_auto_advance_says_why_it_did_not_advance(
    database: Settings, bus: EventBus, agents: AgentRegistry, pipelines: PipelineRegistry
) -> None:
    """A run with nothing to promote reports the reason, never a silent no-op."""
    pipeline_id = three_stage(pipelines, agents)
    runner = build_runner(bus, agents, pipelines)
    auto = AutoPipeline(runner, bus, audit=None)
    schedule = auto.create(pipeline_id, "@daily", auto_advance=True)
    run = await runner.start(pipeline_id, trigger=schedule.trigger_name, wait=True)

    outcome = await auto.advance(schedule.id, run.id)
    assert outcome["advanced"] is False
    assert "no experiment" in outcome["reason"]
    assert auto.get(schedule.id).last_advance_note == outcome["reason"]

    off = auto.update(schedule.id, auto_advance=False)
    assert (await auto.advance(off.id, run.id))["reason"] == (
        "auto-advance is off for this schedule"
    )


# --------------------------------------------------------------------------
# approval: request more validation
# --------------------------------------------------------------------------
def test_validation_targets_are_a_closed_set() -> None:
    """The select is served from the backend, so it cannot offer the impossible."""
    assert "walk_forward" in VALIDATION_TARGETS
    assert "cost_sensitivity" in VALIDATION_TARGETS
    assert all(isinstance(label, str) and label for label in VALIDATION_TARGETS.values())
