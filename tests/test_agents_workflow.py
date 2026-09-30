"""Findings, knowledge, approvals, audit and AutoPipeline.

Every test here is offline. The LLM is :class:`tradinglib.agents.providers.MockProvider`;
everything else is the real subsystem: real SQLite persistence, a real SQLite
FTS5 index, the real pipeline runner (so an approval really blocks a run and
approving it really resumes one), and a real APScheduler loop firing a real run.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from tradinglib.agents.approvals import ApprovalGate, ApprovalRequest, ApprovalState
from tradinglib.agents.audit import ActorKind, AuditLog, classify_actor
from tradinglib.agents.autopipeline import (
    AutoPipeline,
    GuardrailBlocked,
    build_trigger,
    next_fire_time,
)
from tradinglib.agents.findings import (
    LINEAGE_TYPES,
    Confidence,
    EvidenceGraph,
    FindingsStore,
    FindingStatus,
    band_for_percent,
    coerce_confidence,
)
from tradinglib.agents.knowledge import KnowledgeBase, fts_query
from tradinglib.agents.pipeline import IOContract, PipelineRegistry, Stage, StageKind
from tradinglib.agents.providers import MockProvider, MockRequest, MockTurn, ProviderRegistry, ToolCall
from tradinglib.agents.registry import AgentKind, AgentRegistry
from tradinglib.agents.runner import LibraryToolset, PipelineRunner, RunStatus, StageStatus
from tradinglib.config import Settings
from tradinglib.db import get_session_factory, init_db
from tradinglib.events import EventBus

REQUIRED_KEYS_RE = re.compile(r"containing exactly these keys: ([^.]+)\.")


# --------------------------------------------------------------------------
# fixtures and helpers
# --------------------------------------------------------------------------
@pytest.fixture()
def database(settings: Settings) -> Iterator[Settings]:
    """A fresh SQLite database in the temporary data directory."""
    init_db()
    yield settings


@pytest.fixture()
def findings(database: Settings, bus: EventBus) -> FindingsStore:
    """A findings store backed by the temporary database."""
    return FindingsStore(bus=bus)


@pytest.fixture()
def knowledge(database: Settings, bus: EventBus) -> KnowledgeBase:
    """A knowledge base backed by the temporary database."""
    return KnowledgeBase(bus=bus)


@pytest.fixture()
def audit(database: Settings) -> AuditLog:
    """An audit log backed by the temporary database."""
    return AuditLog()


def scripted_responder() -> Callable[[MockRequest], MockTurn]:
    """A responder that answers with exactly the keys the stage's contract asks for."""

    def responder(request: MockRequest) -> MockTurn:
        """Answer one mock completion request."""
        system = request.system or ""
        match = re.search(r"running the '([^']+)' stage", system)
        stage_name = match.group(1) if match else "stage"
        keys_match = REQUIRED_KEYS_RE.search(system)
        payload: dict[str, object] = {}
        if keys_match:
            for key in [part.strip() for part in keys_match.group(1).split(",")]:
                payload[key] = [f"{stage_name}:{key}"]
        else:
            payload["summary"] = stage_name
        return MockTurn(text=f"{stage_name} done.\n```json\n{json.dumps(payload)}\n```")

    return responder


def build_runner(
    *,
    bus: EventBus,
    agents: AgentRegistry,
    pipelines: PipelineRegistry,
    provider: MockProvider,
    toolset: LibraryToolset | None = None,
    audit: AuditLog | None = None,
) -> PipelineRunner:
    """Create a runner wired to the mock provider and the given registries."""
    return PipelineRunner(
        bus,
        pipelines=pipelines,
        agents=agents,
        providers=ProviderRegistry(providers={"mock": provider}),
        toolset=toolset or LibraryToolset(),
        audit=audit,
    )


def seed_pipeline(agents: AgentRegistry, pipelines: PipelineRegistry) -> str:
    """Create a two-stage pipeline: one agent stage, then a human-approval gate."""
    agents.create(
        "Analyst",
        "Evidence synthesis",
        "Summarise the brief and hand over a verdict.",
        "mock",
        "mock-small",
        agent_id="analyst",
        tools=["save_finding"],
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
    pipeline = pipelines.create(
        "Gated review",
        pipeline_id="gated-review",
        objective="Check one candidate and stop for a person.",
        stages=[
            Stage(
                id="analyse",
                name="Analyse",
                agent_id="analyst",
                objective="Check the candidate.",
                inputs=IOContract(text="Run brief", required=[]),
                outputs=IOContract(text="Verdict card", required=["verdict"]),
                handoff="Send the verdict to Human Approval",
                handoff_to=["approval"],
                pass_condition="A verdict was produced",
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


async def wait_for(predicate: Callable[[], bool], *, timeout: float = 10.0) -> None:
    """Poll ``predicate`` until it is true or the timeout expires."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition was not reached in time")


# --------------------------------------------------------------------------
# findings
# --------------------------------------------------------------------------
def test_confidence_accepts_bands_percentages_and_fractions() -> None:
    """An agent may state a band, a percentage or a fraction - not nonsense."""
    assert coerce_confidence("high") == (Confidence.HIGH, None)
    assert coerce_confidence(82) == (Confidence.HIGH, 82.0)
    assert coerce_confidence(0.61) == (Confidence.MEDIUM, 61.0)
    assert coerce_confidence("45%") == (Confidence.LOW, 45.0)
    assert coerce_confidence(None) == (Confidence.MEDIUM, None)
    assert band_for_percent(75.0) is Confidence.HIGH
    with pytest.raises(ValueError, match="unknown confidence"):
        coerce_confidence("very sure")
    with pytest.raises(ValueError, match="outside 0-100"):
        coerce_confidence(140)


def test_findings_crud_and_validation(findings: FindingsStore) -> None:
    """Create, read, update and delete a finding; refuse invalid input."""
    finding = findings.create(
        "Operating leverage accelerates after incentive normalisation",
        "Gross margin expanded in each of the last four quarters while incentives fell.",
        confidence=82,
        symbols=["grab", "GRAB", "SE"],
        tags=["margins", "unit economics"],
        run_id="RUN-1234",
        stage_id="analyse",
        agent_id="research-agent",
        evidence=[{"kind": "filing", "ref": "10-Q"}, "manual note"],
    )
    assert finding.id.startswith("FND-")
    assert finding.confidence is Confidence.HIGH
    assert finding.confidence_pct == 82.0
    assert finding.status is FindingStatus.NEW
    assert finding.symbols == ["GRAB", "SE"]  # normalised and de-duplicated
    assert finding.evidence[1] == {"note": "manual note"}
    assert finding.symbol_label == "GRAB +1"

    assert findings.get(finding.id).title == finding.title
    updated = findings.update(finding.id, summary="Revised claim.", confidence="low")
    assert updated.summary == "Revised claim."
    assert updated.confidence is Confidence.LOW
    assert updated.updated_at is not None and updated.created_at is not None

    with pytest.raises(ValueError, match="unknown field"):
        findings.create("t", "s", nonsense=1)
    with pytest.raises(ValueError, match="needs a summary"):
        findings.create("title only", "   ")
    with pytest.raises(KeyError):
        findings.get("FND-NOPE")

    findings.delete(finding.id)
    assert findings.exists(finding.id) is False
    with pytest.raises(KeyError):
        findings.delete(finding.id)


def test_findings_search_ranks_titles_over_bodies(findings: FindingsStore) -> None:
    """Search hits ids, titles, symbols and bodies, ranked in that order."""
    body_hit = findings.create(
        "Unrelated headline", "Buyback pace is the real driver of the margin story."
    )
    title_hit = findings.create("Margin story is structural", "Nothing else to say.")
    symbol_hit = findings.create("Third claim", "Third body.", symbols=["MARGIN"])

    ranked = findings.search("margin")
    assert [item.id for item in ranked] == [title_hit.id, symbol_hit.id, body_hit.id]
    assert findings.search("   ") == []
    assert findings.search("nothingmatchesthis") == []
    assert [item.id for item in findings.search(body_hit.id)] == [body_hit.id]


def test_findings_filters_inbox_and_triage(findings: FindingsStore) -> None:
    """Filters, the inbox feed and the triage lifecycle all work off real rows."""
    first = findings.create(
        "NVDA search momentum leads revisions",
        "Trends data leads estimate revisions by three weeks.",
        symbols=["NVDA"],
        tags=["altdata"],
        confidence="high",
        run_id="RUN-A",
    )
    second = findings.create(
        "Universe-wide profitability momentum",
        "Profitability momentum survives regime changes.",
        tags=["factor"],
        confidence="low",
        run_id="RUN-B",
    )

    assert [item.id for item in findings.list(symbol="nvda")] == [first.id]
    assert [item.id for item in findings.list(tag="FACTOR")] == [second.id]
    assert [item.id for item in findings.list(run_id="RUN-A")] == [first.id]
    assert findings.tags() == ["altdata", "factor"]
    assert findings.symbols() == ["NVDA"]

    inbox = findings.inbox()
    assert {item["id"] for item in inbox} == {first.id, second.id}
    top = next(item for item in inbox if item["id"] == first.id)
    assert top["type"] == "Finding"
    assert top["urgency"] == "High"
    assert top["symbol"] == "NVDA"
    assert top["route"] == "findings"
    assert top["context"] == {"findingId": first.id, "runId": "RUN-A"}
    assert top["age_seconds"] >= 0.0

    findings.acknowledge(first.id, by="tester")
    acknowledged = findings.get(first.id)
    assert acknowledged.acknowledged is True
    assert acknowledged.acknowledged_by == "tester"
    assert acknowledged.status is FindingStatus.TRIAGED
    assert [item["id"] for item in findings.inbox()] == [second.id]

    promoted = findings.promote(first.id, hypothesis_id="HYP-2026-018")
    assert promoted.status is FindingStatus.PROMOTED
    assert promoted.hypothesis_id == "HYP-2026-018"
    dismissed = findings.dismiss(second.id)
    assert dismissed.status is FindingStatus.DISMISSED

    counts = findings.counts()
    assert counts["total"] == 2
    assert counts["promoted"] == 1
    assert counts["dismissed"] == 1
    assert counts["unacknowledged"] == 0


async def test_save_finding_tool_writes_a_real_finding(
    database: Settings, findings: FindingsStore
) -> None:
    """The runner's ``save_finding`` tool lands a row in this store."""
    toolset = LibraryToolset(findings=findings, settings=database)
    result = await toolset.execute(
        "save_finding",
        {
            "title": "Incentive normalisation is durable",
            "summary": "Four quarters of falling incentives with stable volume.",
            "confidence": "high",
            "symbols": ["GRAB"],
        },
        scope={"run_id": "RUN-TOOL", "stage_id": "analyse"},
    )
    assert result.is_error is False
    assert result.data is not None and result.data["saved"] is True

    stored = findings.list()
    assert len(stored) == 1
    assert stored[0].id == result.data["finding_id"]
    assert stored[0].run_id == "RUN-TOOL"
    assert stored[0].stage_id == "analyse"
    assert stored[0].confidence is Confidence.HIGH


# --------------------------------------------------------------------------
# knowledge base
# --------------------------------------------------------------------------
def test_fts_query_is_built_safely() -> None:
    """Query tokens are quoted and prefixed; punctuation cannot become syntax."""
    assert fts_query("margin story") == '"margin"* AND "story"*'
    assert fts_query("margin story", operator="OR") == '"margin"* OR "story"*'
    assert fts_query('NOT "quoted') == '"not"* AND "quoted"*'
    assert fts_query("   ") == ""


def test_knowledge_full_text_search_indexes_bodies(knowledge: KnowledgeBase) -> None:
    """FTS5 finds a word that only appears in the body, and ranks the best hit first."""
    assert knowledge.fts is True
    incentives = knowledge.create(
        "GRAB incentive accounting definitions",
        "Incentives are netted against revenue. The take rate is reported gross of "
        "consumer incentives, which distorts the margin comparison against peers.",
        kind="definition",
        tags=["regional platforms"],
        symbols=["GRAB"],
        source_run_id="RUN-04A8",
        finding_ids=["FND-000001"],
        sources=[{"label": "Q2 filing", "ref": "10-Q"}, "analyst call"],
    )
    peers = knowledge.create(
        "Peer GMV normalisation mapping",
        "GMV definitions differ by reporting standard; normalise before comparing.",
        tags=["regional platforms", "methodology"],
    )
    knowledge.create("Unrelated note", "Nothing about platforms at all.", tags=["misc"])

    assert knowledge.index_size() == 3
    hits = knowledge.search("incentives")
    assert [entry.id for entry in hits] == [incentives.id]

    ranked = knowledge.search_ranked("gmv normalisation")
    assert ranked and ranked[0]["entry"].id == peers.id
    assert ranked[0]["matched"] == "all"
    assert ranked[0]["score"] != 0.0

    # Both terms together match nothing, so the OR retry answers honestly.
    partial = knowledge.search_ranked("incentives helicopter")
    assert [hit["entry"].id for hit in partial] == [incentives.id]
    assert partial[0]["matched"] == "any"

    assert knowledge.search("helicopter turbine inventory") == []
    assert knowledge.search("") == []

    # Provenance survives the round trip.
    stored = knowledge.get(incentives.id)
    assert stored.source_run_id == "RUN-04A8"
    assert stored.finding_ids == ["FND-000001"]
    assert stored.sources[1] == {"ref": "analyst call"}
    assert stored.symbols == ["GRAB"]


def test_knowledge_index_follows_updates_and_deletes(knowledge: KnowledgeBase) -> None:
    """Editing an entry re-indexes it; deleting one removes it from the index."""
    entry = knowledge.create("Working title", "The original body mentions kangaroos.")
    assert [item.id for item in knowledge.search("kangaroos")] == [entry.id]

    knowledge.update(entry.id, body="The revised body mentions wombats instead.")
    assert knowledge.search("kangaroos") == []
    assert [item.id for item in knowledge.search("wombats")] == [entry.id]

    assert knowledge.reindex() == 1
    assert [item.id for item in knowledge.search("wombats")] == [entry.id]

    knowledge.delete(entry.id)
    assert knowledge.search("wombats") == []
    assert knowledge.index_size() == 0
    with pytest.raises(KeyError):
        knowledge.get(entry.id)


def test_knowledge_collections_and_counts_are_counted(knowledge: KnowledgeBase) -> None:
    """The collections panel is grouped from the rows, not decorated."""
    knowledge.create("A", "body a", tags=["platforms"], symbols=["GRAB"], source_run_id="RUN-1")
    knowledge.create("B", "body b", tags=["platforms"], symbols=["SE"], source_run_id="RUN-2")
    knowledge.create("C", "body c", tags=["infra"], kind="brief")

    collections = knowledge.collections()
    assert collections[0]["tag"] == "platforms"
    assert collections[0]["entries"] == 2
    assert collections[0]["symbol_count"] == 2
    assert collections[0]["run_count"] == 2
    assert collections[0]["updated_at"] is not None

    counts = knowledge.counts()
    assert counts["total"] == 3
    assert counts["by_kind"] == {"note": 2, "brief": 1}
    assert counts["fts"] is True
    assert counts["indexed"] == 3
    assert len(knowledge.recent(limit=2)) == 2

    with pytest.raises(ValueError, match="needs a body"):
        knowledge.create("no body", "")
    with pytest.raises(ValueError, match="unknown field"):
        knowledge.create("t", "b", nonsense=True)


async def test_search_knowledge_tool_reads_the_knowledge_base(
    database: Settings, knowledge: KnowledgeBase
) -> None:
    """The runner's ``search_knowledge`` tool retrieves through FTS5."""
    knowledge.create(
        "Regime changes and profitability momentum",
        "Profitability momentum degrades in rate-shock regimes but recovers within two quarters.",
        tags=["factor"],
    )
    toolset = LibraryToolset(knowledge=knowledge, settings=database)
    result = await toolset.execute("search_knowledge", {"query": "rate-shock regimes"})
    assert result.is_error is False
    assert result.data is not None
    assert result.data["count"] == 1
    assert result.data["entries"][0]["title"].startswith("Regime changes")


# --------------------------------------------------------------------------
# audit log
# --------------------------------------------------------------------------
def test_audit_records_query_and_classify(audit: AuditLog) -> None:
    """Records are appended, filtered and classified by actor."""
    audit.record("Research Agent", "created hypothesis", target="HYP-2026-018", result="success")
    audit.record("Quant Agent", "ran experiment", target="EXP-018-A", run_id="RUN-1", result="pass")
    audit.record(
        "Nicklas",
        "approved for paper research",
        target="APV-017",
        run_id="RUN-1",
        actor_kind=ActorKind.HUMAN,
        comment="evidence is complete",
    )

    assert audit.count() == 3
    newest = audit.list(limit=1)[0]
    assert newest.actor == "Nicklas"
    assert newest.actor_kind is ActorKind.HUMAN
    assert newest.detail["comment"] == "evidence is complete"
    assert newest.id.startswith("AUD-")

    assert [item.action for item in audit.list(run_id="RUN-1")] == [
        "approved for paper research",
        "ran experiment",
    ]
    assert [item.actor for item in audit.for_run("RUN-1")] == ["Quant Agent", "Nicklas"]
    assert audit.count(actor_kind=ActorKind.AGENT) == 2
    assert [item.actor for item in audit.list(query="quant")] == ["Quant Agent"]
    assert audit.actors() == ["Nicklas", "Quant Agent", "Research Agent"]
    assert "ran experiment" in audit.actions()
    assert audit.get(newest.id).target == "APV-017"
    with pytest.raises(KeyError):
        audit.get("AUD-NOPE")

    summary = audit.summary()
    assert summary["total"] == 3
    assert summary["by_actor_kind"]["agent"] == 2
    assert summary["by_actor_kind"]["human"] == 1

    since = datetime.now(timezone.utc) + timedelta(seconds=5)
    assert audit.list(since=since) == []

    with pytest.raises(ValueError, match="actor and an action"):
        audit.record("", "nothing")

    assert classify_actor("runner") is ActorKind.SYSTEM
    assert classify_actor("Validation Agent") is ActorKind.AGENT
    assert classify_actor("Nicklas") is ActorKind.HUMAN


def test_audit_log_is_append_only_in_the_database(audit: AuditLog) -> None:
    """The append-only promise is enforced by triggers, not by convention."""
    record = audit.record("Research Agent", "created hypothesis", target="HYP-1")
    assert not hasattr(audit, "update")
    assert not hasattr(audit, "delete")

    factory = get_session_factory()
    session = factory()
    try:
        with pytest.raises(Exception, match="append-only"):
            session.execute(
                text("UPDATE agent_audit SET action = 'rewritten' WHERE id = :id"),
                {"id": record.id},
            )
        session.rollback()
        with pytest.raises(Exception, match="append-only"):
            session.execute(
                text("DELETE FROM agent_audit WHERE id = :id"), {"id": record.id}
            )
        session.rollback()
    finally:
        session.close()

    assert audit.get(record.id).action == "created hypothesis"


async def test_runner_audit_hook_writes_to_the_audit_log(
    database: Settings, bus: EventBus, audit: AuditLog
) -> None:
    """The runner's audit sink is this module: a real run leaves a real trail."""
    agents = AgentRegistry(bus=bus)
    pipelines = PipelineRegistry(bus=bus)
    pipeline_id = seed_pipeline(agents, pipelines)
    runner = build_runner(
        bus=bus,
        agents=agents,
        pipelines=pipelines,
        provider=MockProvider(responder=scripted_responder()),
        audit=audit,
    )

    run = await runner.start(pipeline_id, inputs={"symbol": "GRAB"})
    await wait_for(lambda: runner.get(run.id).status is RunStatus.AWAITING_APPROVAL)
    approval = runner.approvals(state=ApprovalState.PENDING, run_id=run.id)[0]
    await runner.approve(approval.id, by="Nicklas", comment="evidence is complete")
    await wait_for(lambda: runner.get(run.id).status is RunStatus.SUCCEEDED, timeout=15.0)

    trail = audit.for_run(run.id)
    actions = [item.action for item in trail]
    assert "run queued" in actions
    assert any(action.startswith("approval ") for action in actions)
    assert any(item.actor == "Nicklas" for item in trail)
    assert any(item.actor_kind is ActorKind.SYSTEM for item in trail)
    assert all(item.run_id == run.id for item in trail)


async def test_runner_default_audit_sink_is_the_audit_log(
    database: Settings, bus: EventBus
) -> None:
    """A runner built with no audit argument still audits: the default sink is this module."""
    from tradinglib.agents.runner import default_audit_sink

    assert isinstance(default_audit_sink(), AuditLog)

    agents = AgentRegistry(bus=bus)
    pipelines = PipelineRegistry(bus=bus)
    pipeline_id = seed_pipeline(agents, pipelines)
    runner = PipelineRunner(
        bus,
        pipelines=pipelines,
        agents=agents,
        providers=ProviderRegistry(providers={"mock": MockProvider(responder=scripted_responder())}),
        toolset=LibraryToolset(),
    )
    run = await runner.start(pipeline_id)
    await wait_for(lambda: runner.get(run.id).status is RunStatus.AWAITING_APPROVAL)

    trail = AuditLog().for_run(run.id)
    assert [item.action for item in trail][0] == "run queued"
    assert any(item.action == "stopped automated execution" for item in trail)
    await runner.cancel(run.id, reason="test teardown")


# --------------------------------------------------------------------------
# approval queue
# --------------------------------------------------------------------------
async def test_approval_queue_resolves_the_runners_pause(
    database: Settings, bus: EventBus
) -> None:
    """Approving through the gate releases the stage the runner is blocked on."""
    agents = AgentRegistry(bus=bus)
    pipelines = PipelineRegistry(bus=bus)
    pipeline_id = seed_pipeline(agents, pipelines)
    runner = build_runner(
        bus=bus,
        agents=agents,
        pipelines=pipelines,
        provider=MockProvider(responder=scripted_responder()),
    )
    gate = ApprovalGate(bus, runner=runner)
    frames: list[dict[str, object]] = []
    bus.subscribe("approvals", lambda event: frames.append(event.payload))

    run = await runner.start(pipeline_id, inputs={"symbol": "GRAB"}, name="GRAB review")
    await wait_for(lambda: runner.get(run.id).status is RunStatus.AWAITING_APPROVAL)

    queue = gate.queue()
    assert len(queue) == 1
    request = queue[0]
    assert isinstance(request, ApprovalRequest)
    assert request.pending is True
    assert request.run_id == run.id
    assert request.stage_id == "approval"
    assert request.stage_name == "Human Approval"
    assert request.pipeline_id == pipeline_id
    assert request.pipeline_version == 1
    assert request.agent_id == "human-review"
    assert "paper research" in request.requested_action
    assert request.contract["locked"] is True
    assert request.context["run"]["name"] == "GRAB review"
    assert request.context["run"]["cost_usd"] > 0
    assert request.context["upstream_outputs"]["analyse"]["verdict"] == ["Analyse:verdict"]
    assert [stage["stage_id"] for stage in request.context["stages"]] == ["analyse", "approval"]
    assert gate.pending_count() == 1
    assert gate.counts()["pending"] == 1
    assert request.age_seconds() is not None

    payload = request.as_dict()
    assert payload["state"] == "pending"
    assert payload["contract"]["outputs"]["declared"] is False

    # The run really is still blocked.
    await asyncio.sleep(0.1)
    assert runner.get(run.id).status is RunStatus.AWAITING_APPROVAL

    decided = await gate.approve(request.id, by="Nicklas", comment="looks right")
    assert decided.state is ApprovalState.APPROVED
    assert decided.decided_by == "Nicklas"
    assert decided.comment == "looks right"

    await wait_for(lambda: runner.get(run.id).status is RunStatus.SUCCEEDED, timeout=15.0)
    finished = runner.get(run.id)
    assert [stage.status for stage in finished.stages] == [StageStatus.SUCCEEDED] * 2
    assert gate.pending_count() == 0
    assert gate.queue() == []
    assert gate.counts()["approved"] == 1

    phases = [frame.get("phase") for frame in frames]
    assert "requested" in phases  # published by the runner when the stage blocked
    assert "approved" in phases
    assert frames[-1]["pending"] == 0

    with pytest.raises(ValueError, match="already"):
        await gate.approve(request.id)
    with pytest.raises(KeyError):
        gate.get("APV-NOPE")


async def test_rejecting_through_the_gate_kills_the_run(
    database: Settings, bus: EventBus
) -> None:
    """Rejection terminates the run rather than quietly finishing it."""
    agents = AgentRegistry(bus=bus)
    pipelines = PipelineRegistry(bus=bus)
    pipeline_id = seed_pipeline(agents, pipelines)
    runner = build_runner(
        bus=bus,
        agents=agents,
        pipelines=pipelines,
        provider=MockProvider(responder=scripted_responder()),
    )
    gate = ApprovalGate(bus, runner=runner)

    run = await runner.start(pipeline_id)
    await wait_for(lambda: runner.get(run.id).status is RunStatus.AWAITING_APPROVAL)
    request = gate.queue()[0]

    decided = await gate.reject(request.id, by="Nicklas", comment="not convinced")
    assert decided.state is ApprovalState.REJECTED
    await wait_for(lambda: runner.get(run.id).status is RunStatus.KILLED)
    assert runner.get(run.id).error == "rejected at the human-approval gate"
    assert gate.pending_count() == 0
    assert gate.list(state=ApprovalState.REJECTED)[0].comment == "not convinced"


async def test_gate_wait_blocks_until_a_decision_and_times_out(
    database: Settings, bus: EventBus
) -> None:
    """``wait`` reads the persisted decision, so it works across processes."""
    agents = AgentRegistry(bus=bus)
    pipelines = PipelineRegistry(bus=bus)
    pipeline_id = seed_pipeline(agents, pipelines)
    runner = build_runner(
        bus=bus,
        agents=agents,
        pipelines=pipelines,
        provider=MockProvider(responder=scripted_responder()),
    )
    gate = ApprovalGate(bus, runner=runner)

    run = await runner.start(pipeline_id)
    await wait_for(lambda: runner.get(run.id).status is RunStatus.AWAITING_APPROVAL)
    request = gate.queue()[0]

    with pytest.raises(TimeoutError, match="still pending"):
        await gate.wait(request.id, timeout=0.15)
    assert gate.get(request.id).pending is True

    waiter = asyncio.create_task(gate.wait(request.id, timeout=5.0))
    await asyncio.sleep(0.05)
    await gate.approve(request.id, by="Nicklas")
    decided = await waiter
    assert decided.state is ApprovalState.APPROVED


async def test_gate_records_a_decision_without_a_runner(
    database: Settings, bus: EventBus
) -> None:
    """A gate with no runner still records the decision the run will read back."""
    agents = AgentRegistry(bus=bus)
    pipelines = PipelineRegistry(bus=bus)
    pipeline_id = seed_pipeline(agents, pipelines)
    runner = build_runner(
        bus=bus,
        agents=agents,
        pipelines=pipelines,
        provider=MockProvider(responder=scripted_responder()),
    )
    run = await runner.start(pipeline_id)
    await wait_for(lambda: runner.get(run.id).status is RunStatus.AWAITING_APPROVAL)
    approval_id = runner.approvals(state=ApprovalState.PENDING)[0].id

    detached = ApprovalGate(bus, runner=None)
    detached._runner_resolved = True  # noqa: SLF001 - simulating a process with no runner
    decided = await detached.approve(approval_id, by="operator", comment="out of band")

    assert decided.state is ApprovalState.APPROVED
    assert runner.approval(approval_id).state is ApprovalState.APPROVED
    assert detached.pending_count() == 0
    # The blocked run is untouched by the out-of-band write until a runner reads it.
    assert runner.get(run.id).status is RunStatus.AWAITING_APPROVAL
    await runner.cancel(run.id, reason="test teardown")


async def test_gate_can_request_an_approval_of_its_own(
    database: Settings, bus: EventBus
) -> None:
    """A request made through the gate is the same row the runner counts."""
    agents = AgentRegistry(bus=bus)
    pipelines = PipelineRegistry(bus=bus)
    seed_pipeline(agents, pipelines)
    runner = build_runner(
        bus=bus,
        agents=agents,
        pipelines=pipelines,
        provider=MockProvider(responder=scripted_responder()),
    )
    gate = ApprovalGate(bus, runner=runner)
    frames: list[dict[str, object]] = []
    bus.subscribe("approvals", lambda event: frames.append(event.payload))

    request = await gate.request(
        "RUN-EXTERNAL",
        "promotion",
        "Promote STRAT-044 to paper research",
        "Two validation gates were waived by hand.",
        resume_index=3,
        waived_gates=["stability", "turnover"],
    )
    assert request.id.startswith("APV-")
    assert request.payload["waived_gates"] == ["stability", "turnover"]
    assert request.run_name is None  # no such run: the gate does not invent one
    assert runner.pending_approvals() == 1
    assert frames[-1]["phase"] == "requested"
    assert frames[-1]["pending"] == 1

    with pytest.raises(ValueError, match="run id and a stage id"):
        await gate.request("", "stage", "title", "summary")

    assert await gate.publish_pending() == 1

    # Deciding it records the decision without asking the runner to resume a run
    # that never existed.
    decided = await gate.approve(request.id, by="Nicklas", comment="gates waived knowingly")
    assert decided.state is ApprovalState.APPROVED
    assert decided.decided_by == "Nicklas"
    assert gate.pending_count() == 0
    assert runner.pending_approvals() == 0


# --------------------------------------------------------------------------
# AutoPipeline
# --------------------------------------------------------------------------
def test_cadence_expressions_are_parsed_or_refused() -> None:
    """Crontabs, aliases and intervals parse; anything else is refused."""
    assert next_fire_time(build_trigger("*/5 * * * *")) is not None
    assert next_fire_time(build_trigger("@daily", timezone_name="America/New_York")) is not None
    interval = build_trigger("@every 30s")
    assert next_fire_time(interval) is not None
    with pytest.raises(ValueError, match="unknown cadence alias"):
        build_trigger("@fortnightly")
    with pytest.raises(ValueError, match="invalid cadence"):
        build_trigger("not a cron")
    with pytest.raises(ValueError, match="needs a cadence"):
        build_trigger("")
    with pytest.raises(ValueError, match="unknown timezone"):
        build_trigger("@daily", timezone_name="Mars/Olympus")


async def test_autopipeline_crud_validates_and_computes_next_run(
    database: Settings, bus: EventBus
) -> None:
    """Schedules validate against the real pipeline registry and report next fire."""
    agents = AgentRegistry(bus=bus)
    pipelines = PipelineRegistry(bus=bus)
    pipeline_id = seed_pipeline(agents, pipelines)
    runner = build_runner(
        bus=bus,
        agents=agents,
        pipelines=pipelines,
        provider=MockProvider(responder=scripted_responder()),
    )
    auto = AutoPipeline(runner, bus)

    schedule = auto.create(
        pipeline_id,
        "30 6 * * 1-5",
        name="Weekday scan",
        timezone="America/New_York",
        objective="Morning sweep",
        inputs={"query": "AI infrastructure momentum", "top_n": 5},
        max_runs_per_day=2,
        max_spend_usd_per_day=1.5,
    )
    assert schedule.id.startswith("SCH-")
    assert schedule.trigger_name == f"schedule:{schedule.id}"
    assert schedule.next_run_at is not None and schedule.next_run_at > datetime.now(timezone.utc)
    assert schedule.as_dict()["guardrails"]["max_runs_per_day"] == 2

    with pytest.raises(KeyError):
        auto.create("no-such-pipeline", "@daily")
    with pytest.raises(ValueError, match="invalid cadence"):
        auto.create(pipeline_id, "every tuesday")
    with pytest.raises(ValueError, match="unknown field"):
        auto.create(pipeline_id, "@daily", nonsense=1)
    with pytest.raises(ValueError, match="cannot be negative"):
        auto.create(pipeline_id, "@daily", max_runs_per_day=-1)

    updated = auto.update(schedule.id, enabled=False, cron="@hourly")
    assert updated.enabled is False
    assert updated.next_run_at is None  # a disabled schedule has no next fire
    assert auto.get(schedule.id).cron == "@hourly"

    status = auto.status()
    assert status["total"] == 1 and status["enabled"] == 0 and status["running"] is False

    auto.delete(schedule.id)
    assert auto.list() == []
    with pytest.raises(KeyError):
        auto.get(schedule.id)


async def test_autopipeline_fires_a_real_run_through_apscheduler(
    database: Settings, bus: EventBus
) -> None:
    """A one-second schedule really fires, and the run it starts is a real run."""
    agents = AgentRegistry(bus=bus)
    pipelines = PipelineRegistry(bus=bus)
    pipeline_id = seed_pipeline(agents, pipelines)
    runner = build_runner(
        bus=bus,
        agents=agents,
        pipelines=pipelines,
        provider=MockProvider(responder=scripted_responder()),
    )
    auto = AutoPipeline(runner, bus)
    notes: list[dict[str, object]] = []
    bus.subscribe("notifications", lambda event: notes.append(event.payload))

    schedule = auto.create(
        pipeline_id,
        "@every 1s",
        name="Tiny schedule",
        inputs={"query": "AI infrastructure momentum"},
        max_runs_per_day=1,
    )
    await auto.start()
    try:
        assert auto.running is True
        assert auto.scheduler is not None
        assert auto.scheduler.get_job(f"tradinglib-autopipeline-{schedule.id}") is not None
        assert auto.get(schedule.id).next_run_at is not None

        await wait_for(lambda: bool(auto.runs(schedule.id)), timeout=15.0)
        await wait_for(
            lambda: runner.counts()["awaiting_approval"] == 1 or runner.counts()["succeeded"] == 1,
            timeout=15.0,
        )
    finally:
        await auto.stop()
    assert auto.running is False

    started = auto.runs(schedule.id)
    assert len(started) == 1
    run = runner.get(started[0]["run_id"])
    assert run.trigger == schedule.trigger_name
    assert run.inputs == {"query": "AI infrastructure momentum"}
    assert run.stages[0].outputs["verdict"] == ["Analyse:verdict"]
    assert run.status is RunStatus.AWAITING_APPROVAL

    stored = auto.get(schedule.id)
    assert stored.run_count == 1
    assert stored.last_run_id == run.id

    usage = auto.usage(schedule.id)
    assert usage["runs"] == 1
    assert usage["active_runs"] == 1
    assert usage["spend_usd"] > 0

    # The guardrail is now spent, so the next fire is refused rather than run.
    with pytest.raises(GuardrailBlocked, match="daily run guardrail"):
        await auto.trigger_now(schedule.id)
    assert len(auto.runs(schedule.id)) == 1
    assert auto.get(schedule.id).last_status == "blocked"
    assert any("held" in str(note.get("message", "")) for note in notes)
    assert any("started" in str(note.get("message", "")) for note in notes)

    await runner.cancel(run.id, reason="test teardown")


async def test_autopipeline_guardrails_cover_spend_and_concurrency(
    database: Settings, bus: EventBus
) -> None:
    """Spend, concurrency and the disabled flag each stop a firing."""
    agents = AgentRegistry(bus=bus)
    pipelines = PipelineRegistry(bus=bus)
    pipeline_id = seed_pipeline(agents, pipelines)
    runner = build_runner(
        bus=bus,
        agents=agents,
        pipelines=pipelines,
        provider=MockProvider(responder=scripted_responder()),
    )
    auto = AutoPipeline(runner, bus)
    schedule = auto.create(pipeline_id, "@daily", name="Spend fenced", max_concurrent_runs=1)

    first = await auto.trigger_now(schedule.id)
    await wait_for(lambda: runner.get(first).status is RunStatus.AWAITING_APPROVAL)

    # The first run is still active, which is the concurrency guardrail.
    with pytest.raises(GuardrailBlocked, match="still active"):
        await auto.trigger_now(schedule.id)

    approval = runner.approvals(state=ApprovalState.PENDING, run_id=first)[0]
    await runner.approve(approval.id, by="tester")
    await wait_for(lambda: runner.get(first).status is RunStatus.SUCCEEDED, timeout=15.0)

    # The run is finished, so concurrency is free again - but a spend guardrail
    # set below what it already cost refuses the next firing.
    auto.update(schedule.id, max_spend_usd_per_day=0.0000001)
    assert auto.usage(schedule.id)["spend_usd"] > 0.0000001
    with pytest.raises(GuardrailBlocked, match="spend guardrail"):
        await auto.trigger_now(schedule.id)

    # An operator may override the guardrail deliberately.
    forced = await auto.trigger_now(schedule.id, force=True)
    await wait_for(lambda: runner.get(forced).status is RunStatus.AWAITING_APPROVAL)
    assert len(auto.runs(schedule.id)) == 2
    await runner.cancel(forced, reason="test teardown")

    auto.update(schedule.id, enabled=False)
    with pytest.raises(GuardrailBlocked, match="disabled"):
        await auto.trigger_now(schedule.id)


# --------------------------------------------------------------------------
# evidence lineage
# --------------------------------------------------------------------------
def test_evidence_graph_traces_sources_claims_and_findings(findings: FindingsStore) -> None:
    """The Explorer's graph is built from stored references, never invented."""
    finding = findings.create(
        "Momentum decay is crowding-conditioned",
        "Half the OOS Sharpe disappears once crowding is above the 60th percentile.",
        confidence=72,
        hypothesis_id="H-EV1",
        agent_id="research-agent",
        run_id="run-ev1",
        evidence=[
            {"note": "OOS Sharpe 1.34 on the uncrowded half", "source": "yfinance"},
            {"note": "Excess return weakens in the crowded half", "source": "yfinance"},
            {"note": "Rate regime shift coincides with the bad fold", "source": "fred"},
        ],
    )

    # Only the findings store is available here, so the research columns are
    # honestly reported as unfillable rather than fabricated.
    graph = EvidenceGraph(
        findings,
        hypotheses=False,
        experiments=False,
        backtests=False,
        validation=False,
        approvals=False,
    ).build(finding_id=finding.id)

    assert graph["types"] == list(LINEAGE_TYPES)
    assert graph["counts"]["Sources"] == 2  # yfinance and fred, deduplicated
    assert graph["counts"]["Claims"] == 3
    assert graph["counts"]["Findings"] == 1
    # The hypothesis the finding was filed against is a real reference, so the
    # column is filled even though the registry itself is not mounted here.
    assert graph["counts"]["Hypotheses"] == 1
    assert graph["counts"]["Experiments"] == 0
    assert graph["selected"] == f"FIND-{finding.id}"

    by_id = {node["id"]: node for node in graph["nodes"]}
    node = by_id[f"FIND-{finding.id}"]
    assert len(node["upstream"]) == 3
    assert node["route"] == {"page": "agents", "sub": "findings", "id": finding.id}
    # A claim that argues against the finding is labelled as such.
    relations = {by_id[claim]["relation"] for claim in node["upstream"]}
    assert relations == {"supports", "contradicts"}
    # Every claim points back at its source, and the edge is navigable both ways.
    for claim_id in node["upstream"]:
        claim = by_id[claim_id]
        assert claim["downstream"] == [node["id"]]
        for source_id in claim["upstream"]:
            assert claim_id in by_id[source_id]["downstream"]

    upstream = EvidenceGraph.related(graph, node["id"], "upstream")
    assert upstream == {node["id"], *node["upstream"], "SRC-yfinance", "SRC-fred"}
    assert EvidenceGraph.related(graph, node["id"], "downstream") == {
        node["id"], "HYP-H-EV1",
    }
    assert EvidenceGraph.related(graph, "SRC-fred", "downstream") >= {"SRC-fred", node["id"]}
    assert EvidenceGraph.related(graph, "NOPE", "all") == set()


def test_evidence_graph_is_empty_without_findings(findings: FindingsStore) -> None:
    """No findings means no graph and a stated reason, not a blank canvas."""
    graph = EvidenceGraph(
        findings,
        hypotheses=False,
        experiments=False,
        backtests=False,
        validation=False,
        approvals=False,
    ).build()
    assert graph["nodes"] == []
    assert graph["selected"] is None
    assert any("hypothesis" in note for note in graph["notes"])
