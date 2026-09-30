"""Agent Center endpoints (``docs/SPEC.md`` section 5).

Everything the Agent Center shows is served from here, and every figure is
counted from the agents tables rather than assumed:

* ``/models`` and ``/config`` - what the LLM providers actually offer. A
  provider without credentials reports ``available: false`` with the reason,
  and its models never reach the picker;
* ``/definitions`` - Agent Studio's registry CRUD, each definition joined to
  its real execution record (runs, failures, spend) from the run tables;
* ``/pipelines`` - the Pipeline Builder: pipeline CRUD, per-stage editing
  (add / update / duplicate / remove / reorder), frozen versions, duplication
  and ``POST {id}/run``, which starts a real run;
* ``/runs`` - the run ledger with stage records, logs and artifacts. Progress
  streams over the ``runs`` WebSocket channel while a run executes, published
  by :class:`tradinglib.agents.runner.PipelineRunner` onto the application's event
  bus;
* ``/inbox`` and ``/approvals`` - the attention queue and the human gate. A
  decision here really releases (or kills) the paused run;
* ``/findings``, ``/knowledge`` (with search), ``/audit`` and
  ``/autopipeline`` - the research registries and the schedule table.

Budgets written through ``PUT /config`` are applied to the running spend ledger
and persisted to ``<data_dir>/agent_budgets.json``, which the lifespan reads
back on boot. API keys are *not* writable here - they belong to
``/api/settings`` - so this endpoint reports their status and nothing else.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, TypeVar
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from tradinglib.agents.approvals import (
    AUTONOMY_CHOICES,
    NOTIFICATION_CHOICES,
    RETENTION_CHOICES,
    VALIDATION_TARGETS,
    AgentPolicy,
    ApprovalGate,
    get_policy,
    set_policy,
)
from tradinglib.agents.audit import AuditLog
from tradinglib.agents.autopipeline import AutoPipeline, GuardrailBlocked
from tradinglib.agents.findings import EvidenceGraph, FindingsStore, FindingStatus
from tradinglib.agents.inbox import InboxComposer, InboxSources
from tradinglib.agents.knowledge import KnowledgeBase
from tradinglib.agents.pipeline import PipelineRegistry, PipelineStatus, StageKind
from tradinglib.agents.providers import ProviderRegistry
from tradinglib.agents.registry import AgentKind, AgentRegistry, AgentStatus
from tradinglib.agents.runner import ApprovalState, PipelineRunner, RunStatus
from tradinglib.api.deps import (
    get_agents,
    get_approvals,
    get_audit,
    get_autopipeline,
    get_findings,
    get_knowledge,
    get_pipelines,
    get_providers,
    get_runner,
    translate_errors,
)
from tradinglib.config import Budgets, get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agents", tags=["agents"])

Agents = Annotated[AgentRegistry, Depends(get_agents)]
Pipelines = Annotated[PipelineRegistry, Depends(get_pipelines)]
Runner = Annotated[PipelineRunner, Depends(get_runner)]
Providers = Annotated[ProviderRegistry, Depends(get_providers)]
Findings = Annotated[FindingsStore, Depends(get_findings)]
Knowledge = Annotated[KnowledgeBase, Depends(get_knowledge)]
Approvals = Annotated[ApprovalGate, Depends(get_approvals)]
Audit = Annotated[AuditLog, Depends(get_audit)]
Schedules = Annotated[AutoPipeline, Depends(get_autopipeline)]

T = TypeVar("T")

#: File the budgets edited in the Configuration tab are persisted to.
BUDGET_FILENAME = "agent_budgets.json"


async def _thread(fn: Callable[[], T]) -> T:
    """Run a blocking (SQLite) domain call on a worker thread."""
    return await asyncio.to_thread(fn)


def budget_file(data_dir: Path | None = None) -> Path:
    """Return the path budgets edited in the UI are stored in."""
    root = data_dir if data_dir is not None else get_settings().data_dir
    return root / BUDGET_FILENAME


def load_saved_budgets(data_dir: Path | None = None) -> Budgets | None:
    """Read budgets previously written through ``PUT /api/agents/config``.

    Returns ``None`` when nothing has been saved, or when the file is unreadable
    - in which case the configured (TOML/environment) budgets stay in force and
    the fault is logged rather than silently replaced with defaults.
    """
    path = budget_file(data_dir)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return Budgets(
            per_run_usd=float(payload["per_run_usd"]),
            daily_usd=float(payload["daily_usd"]),
            monthly_usd=float(payload["monthly_usd"]),
        )
    except (OSError, ValueError, KeyError, TypeError):
        logger.warning("saved agent budgets at %s are unreadable", path, exc_info=True)
        return None


def _save_budgets(budgets: Budgets, data_dir: Path | None = None) -> Path:
    """Persist budgets so a restart keeps what the Configuration tab set."""
    path = budget_file(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(budgets.as_dict(), indent=2) + "\n", encoding="utf-8")
    return path


def _session_day_start() -> datetime:
    """Return midnight of the current New York session day, in UTC.

    "Today" in a trading workspace is the exchange's day, not the server's, and
    the UI clock is America/New_York - so the "blocked today" figure has to be
    cut on the same boundary the person reading it sees.
    """
    zone = ZoneInfo("America/New_York")
    local = datetime.now(zone)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(UTC)


def _parse_ts(value: str | None) -> datetime | None:
    """Parse an ISO-8601 query parameter, or ``None``.

    Raises:
        ValueError: when the value is present but not a timestamp - a filter
            silently ignored would show the wrong rows.
    """
    if value is None or not value.strip():
        return None
    return datetime.fromisoformat(value)


# ---------------------------------------------------------------------------
# request bodies
# ---------------------------------------------------------------------------
class BudgetUpdate(BaseModel):
    """Body of ``PUT /api/agents/config``. Keys are handled by ``/api/settings``.

    Budgets and the workspace autonomy/notification policy are both writable
    here; unset fields keep their current value.
    """

    model_config = ConfigDict(extra="ignore")

    per_run_usd: float | None = Field(default=None, ge=0)
    daily_usd: float | None = Field(default=None, ge=0)
    monthly_usd: float | None = Field(default=None, ge=0)
    research_generation: str | None = None
    sandbox_experiments: str | None = None
    max_concurrent_runs: int | None = Field(default=None, ge=1, le=64)
    artifact_retention: str | None = None
    compute_approval_usd: float | None = Field(default=None, ge=0)
    feature_publish_review: bool | None = None
    notify_approval_requests: str | None = None
    notify_failed_validation: str | None = None
    notify_stale_evidence: str | None = None
    min_evidence_score: float | None = Field(default=None, ge=0, le=1)

    def policy_fields(self) -> dict[str, Any]:
        """Return only the policy fields that were actually supplied."""
        names = set(AgentPolicy().as_dict()) - {"candidate_disposition"}
        return {
            key: value
            for key, value in self.model_dump(exclude_none=True).items()
            if key in names
        }


class AgentCreate(BaseModel):
    """Body of ``POST /api/agents/definitions``."""

    model_config = ConfigDict(extra="ignore")

    name: str
    role: str
    system_prompt: str
    provider: str
    model: str
    agent_id: str | None = Field(default=None, validation_alias="id")
    kind: AgentKind = AgentKind.STAGE
    status: AgentStatus | None = None
    fallback_model: str | None = None
    tools: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    temperature: float = 0.0
    max_tokens: int = 4096
    budget_usd: float | None = None
    timeout_s: float | None = None
    retries: int = 0
    scope: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    notes: str | None = None
    enabled: bool = True
    presets: dict[str, dict[str, Any]] = Field(default_factory=dict)


class AgentUpdate(BaseModel):
    """Body of ``PUT /api/agents/definitions/{id}``; unset fields are untouched."""

    model_config = ConfigDict(extra="ignore")

    name: str | None = None
    role: str | None = None
    system_prompt: str | None = None
    provider: str | None = None
    model: str | None = None
    fallback_model: str | None = None
    kind: AgentKind | None = None
    status: AgentStatus | None = None
    tools: list[str] | None = None
    permissions: list[str] | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    budget_usd: float | None = None
    timeout_s: float | None = None
    retries: int | None = None
    scope: dict[str, Any] | None = None
    tags: list[str] | None = None
    notes: str | None = None
    enabled: bool | None = None
    locked: bool | None = None
    presets: dict[str, dict[str, Any]] | None = None


class NameBody(BaseModel):
    """Body of the duplicate endpoints: an optional name for the copy."""

    model_config = ConfigDict(extra="ignore")

    name: str | None = None


class PipelineCreate(BaseModel):
    """Body of ``POST /api/agents/pipelines``."""

    model_config = ConfigDict(extra="ignore")

    name: str
    pipeline_id: str | None = Field(default=None, validation_alias="id")
    objective: str | None = None
    description: str | None = None
    stages: list[dict[str, Any]] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    status: PipelineStatus = PipelineStatus.DRAFT


class PipelineUpdate(BaseModel):
    """Body of ``PUT /api/agents/pipelines/{id}`` (the working document)."""

    model_config = ConfigDict(extra="ignore")

    name: str | None = None
    objective: str | None = None
    description: str | None = None
    tags: list[str] | None = None
    status: PipelineStatus | None = None
    locked: bool | None = None
    stages: list[dict[str, Any]] | None = None


class VersionBody(BaseModel):
    """Body of ``POST /api/agents/pipelines/{id}/versions``."""

    model_config = ConfigDict(extra="ignore")

    note: str | None = None


class StageBody(BaseModel):
    """A stage as the Pipeline Builder edits it.

    Every field is optional so the same model serves ``POST`` (add) and ``PUT``
    (update): on update only the fields actually sent are applied.
    """

    model_config = ConfigDict(extra="ignore")

    stage_id: str | None = Field(default=None, validation_alias="id")
    name: str | None = None
    kind: StageKind | None = None
    agent_id: str | None = None
    provider: str | None = None
    model: str | None = None
    objective: str | None = None
    scope: dict[str, Any] | None = None
    include_in_run: bool | None = None
    inputs: dict[str, Any] | str | None = None
    outputs: dict[str, Any] | str | None = None
    handoff: str | None = None
    handoff_to: list[str] | None = None
    pass_condition: str | None = None
    fail_condition: str | None = None
    stop_condition: str | None = None
    tools: list[str] | None = None
    budget_usd: float | None = None
    timeout_s: float | None = None
    retries: int | None = None
    config: dict[str, Any] | None = None
    index: int | None = None


class StageOrder(BaseModel):
    """Body of ``POST /api/agents/pipelines/{id}/stages/order``."""

    model_config = ConfigDict(extra="ignore")

    order: list[str]


class RunStart(BaseModel):
    """Body of ``POST /api/agents/pipelines/{id}/run``."""

    model_config = ConfigDict(extra="ignore")

    trigger: str = "manual"
    inputs: dict[str, Any] = Field(default_factory=dict)
    version: int | None = None
    objective: str | None = None
    name: str | None = None
    #: Per-run stage overrides, keyed by stage id: ``model``, ``provider`` and
    #: ``include_in_run``. They apply to this run's frozen document only, which
    #: is exactly what the reference's per-run model selects and stage toggles
    #: promise - the saved pipeline is not edited.
    stage_overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)


class StageRunBody(BaseModel):
    """Body of ``POST /api/agents/pipelines/{id}/stages/{stage_id}/run``."""

    model_config = ConfigDict(extra="ignore")

    inputs: dict[str, Any] = Field(default_factory=dict)
    version: int | None = None
    model: str | None = None
    provider: str | None = None
    wait: bool = True


class ReplayBody(BaseModel):
    """Body of ``POST /api/agents/runs/{id}/replay``.

    ``stage_id`` omitted replays from the first stage that did not succeed.
    """

    model_config = ConfigDict(extra="ignore")

    stage_id: str | None = None
    inputs: dict[str, Any] | None = None
    stage_overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)
    wait: bool = False


class CancelBody(BaseModel):
    """Body of ``POST /api/agents/runs/{id}/cancel``."""

    model_config = ConfigDict(extra="ignore")

    reason: str = "cancelled by the user"


class PauseBody(BaseModel):
    """Body of ``POST /api/agents/runs/{id}/pause``."""

    model_config = ConfigDict(extra="ignore")

    reason: str = "paused by the user"


class RetryBody(BaseModel):
    """Body of ``POST /api/agents/runs/{id}/retry``.

    ``stage_id`` is optional: omitted, the runner rewinds to the first stage
    that did not succeed, which is what the inspector's "Retry failed stage"
    button means.
    """

    model_config = ConfigDict(extra="ignore")

    stage_id: str | None = None


class ApprovalDecision(BaseModel):
    """Body of ``POST /api/agents/approvals/{id}``."""

    model_config = ConfigDict(extra="ignore")

    decision: str
    comment: str | None = None
    by: str = "user"
    #: Required when ``decision`` is ``request_validation``: which further
    #: evidence the reviewer wants before they will decide.
    target: str | None = None


class FindingCreate(BaseModel):
    """Body of ``POST /api/agents/findings``."""

    model_config = ConfigDict(extra="ignore")

    title: str
    summary: str
    confidence: str | None = None
    confidence_pct: float | None = None
    status: FindingStatus | None = None
    symbols: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    run_id: str | None = None
    stage_id: str | None = None
    agent_id: str | None = None
    hypothesis_id: str | None = None
    evidence: list[dict[str, Any]] = Field(default_factory=list)


class FindingUpdate(BaseModel):
    """Body of ``PUT /api/agents/findings/{id}``."""

    model_config = ConfigDict(extra="ignore")

    title: str | None = None
    summary: str | None = None
    confidence: str | None = None
    confidence_pct: float | None = None
    status: FindingStatus | None = None
    symbols: list[str] | None = None
    tags: list[str] | None = None
    hypothesis_id: str | None = None
    evidence: list[dict[str, Any]] | None = None
    acknowledged: bool | None = None


class KnowledgeCreate(BaseModel):
    """Body of ``POST /api/agents/knowledge``."""

    model_config = ConfigDict(extra="ignore")

    title: str
    body: str
    kind: str = "note"
    tags: list[str] = Field(default_factory=list)
    symbols: list[str] = Field(default_factory=list)
    source_run_id: str | None = None
    source_stage_id: str | None = None
    finding_ids: list[str] = Field(default_factory=list)
    sources: list[dict[str, Any]] = Field(default_factory=list)
    author: str | None = None


class KnowledgeUpdate(BaseModel):
    """Body of ``PUT /api/agents/knowledge/{id}``."""

    model_config = ConfigDict(extra="ignore")

    title: str | None = None
    body: str | None = None
    kind: str | None = None
    tags: list[str] | None = None
    symbols: list[str] | None = None
    finding_ids: list[str] | None = None
    sources: list[dict[str, Any]] | None = None
    author: str | None = None


class ScheduleCreate(BaseModel):
    """Body of ``POST /api/agents/autopipeline``."""

    model_config = ConfigDict(extra="ignore")

    pipeline_id: str
    cron: str
    name: str = ""
    timezone: str = "UTC"
    enabled: bool = True
    objective: str | None = None
    pipeline_version: int | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    max_runs_per_day: int | None = None
    max_spend_usd_per_day: float | None = None
    max_concurrent_runs: int = 1
    #: Per-run overrides of the saved pipeline, applied to the runs this
    #: schedule starts: stage id -> model, and stage id -> included.
    model_overrides: dict[str, str] = Field(default_factory=dict)
    stage_toggles: dict[str, bool] = Field(default_factory=dict)
    #: Set an experiment the run produced to ``validated`` when its backtest
    #: cleared every gate. Anything beyond that still needs human approvals.
    auto_advance: bool = False


class ScheduleUpdate(BaseModel):
    """Body of ``PUT /api/agents/autopipeline/{id}``."""

    model_config = ConfigDict(extra="ignore")

    name: str | None = None
    pipeline_id: str | None = None
    cron: str | None = None
    timezone: str | None = None
    enabled: bool | None = None
    objective: str | None = None
    pipeline_version: int | None = None
    inputs: dict[str, Any] | None = None
    max_runs_per_day: int | None = None
    max_spend_usd_per_day: float | None = None
    max_concurrent_runs: int | None = None
    model_overrides: dict[str, str] | None = None
    stage_toggles: dict[str, bool] | None = None
    auto_advance: bool | None = None


# ---------------------------------------------------------------------------
# models and configuration
# ---------------------------------------------------------------------------
@router.get("/models", summary="Models the configured providers really offer")
async def list_models(
    providers: Providers,
    refresh: Annotated[bool, Query(description="re-probe instead of using the cache")] = False,
) -> dict[str, Any]:
    """Return the model picker's contents: only providers that verified are listed."""
    with translate_errors("agent models"):
        return await providers.models_payload(refresh=refresh)


@router.get("/config", summary="Budgets, provider status and spend")
async def get_config(
    providers: Providers,
    runner: Runner,
    agents: Agents,
    schedules: Schedules,
    refresh: Annotated[bool, Query()] = False,
) -> dict[str, Any]:
    """Return the Configuration tab: real budgets, real spend, honest provider status."""
    settings = get_settings()
    with translate_errors("agent configuration"):
        capabilities = await providers.capabilities(refresh=refresh)
        spend = await _thread(lambda: runner.spend(window="day"))
        run_counts = await _thread(runner.counts)
        agent_counts = await _thread(
            lambda: agents.counts(active_agent_ids=runner.active_agent_ids())
        )
        schedule_status = await _thread(schedules.status)
    saved = load_saved_budgets(settings.data_dir)
    policy = get_policy(refresh=refresh)
    return {
        "budgets": runner.ledger.budgets.as_dict(),
        "budget_source": "saved" if saved is not None else "configured",
        "budget_file": str(budget_file(settings.data_dir)),
        "spend": spend,
        "providers": [report.as_dict(include_models=False) for report in capabilities],
        "keys": settings.provider_status(),
        "runs": run_counts,
        "agents": agent_counts,
        "autopipeline": {
            key: value for key, value in schedule_status.items() if key != "schedules"
        },
        "unpriced_charges": runner.ledger.unpriced_count(),
        "policy": policy.as_dict(),
        "policy_choices": {
            "autonomy": [{"value": v, "label": label} for v, label in AUTONOMY_CHOICES],
            "retention": [{"value": v, "label": label} for v, label in RETENTION_CHOICES],
            "notifications": {
                key: [{"value": v, "label": label} for v, label in choices]
                for key, choices in NOTIFICATION_CHOICES.items()
            },
        },
        "guardrails": policy.guardrails(),
    }


@router.put("/config", summary="Update the agent budgets and autonomy policy")
async def update_config(runner: Runner, body: BudgetUpdate, providers: Providers,
                        agents: Agents, schedules: Schedules) -> dict[str, Any]:
    """Apply new budgets and autonomy policy, and persist both.

    Budgets go to the running spend ledger; the policy takes effect on the next
    tool call, run start and stage cost check. API keys are write-only fields of
    ``/api/settings``; this endpoint never accepts a secret, and
    ``candidate_disposition`` is not writable at all - a candidate can only leave
    the pipeline through a human-approval stage.
    """
    current = runner.ledger.budgets
    updated = Budgets(
        per_run_usd=current.per_run_usd if body.per_run_usd is None else body.per_run_usd,
        daily_usd=current.daily_usd if body.daily_usd is None else body.daily_usd,
        monthly_usd=current.monthly_usd if body.monthly_usd is None else body.monthly_usd,
    )
    changes = body.policy_fields()
    with translate_errors("agent configuration"):
        runner.ledger.set_budgets(updated)
        await _thread(lambda: _save_budgets(updated, get_settings().data_dir))
        if changes:
            merged = AgentPolicy.from_dict({**get_policy().as_dict(), **changes})
            await _thread(lambda: set_policy(merged, get_settings().data_dir))
            logger.info("agent policy updated: %s", ", ".join(sorted(changes)))
    logger.info("agent budgets updated to %s", updated.as_dict())
    return await get_config(providers, runner, agents, schedules, refresh=False)


# ---------------------------------------------------------------------------
# agent definitions (Agent Studio)
# ---------------------------------------------------------------------------
@router.get("/definitions", summary="List agent definitions")
async def list_definitions(
    agents: Agents,
    runner: Runner,
    kind: Annotated[AgentKind | None, Query()] = None,
    status: Annotated[AgentStatus | None, Query()] = None,
    enabled: Annotated[bool | None, Query()] = None,
    q: Annotated[str | None, Query(description="substring of the name or role")] = None,
    include_stats: Annotated[bool, Query()] = True,
) -> dict[str, Any]:
    """Return the Agent Studio inventory with each agent's real execution record."""
    with translate_errors("agent definition"):
        items = await _thread(lambda: agents.list(kind=kind, enabled=enabled, query=q))
        # The registry filters on kind/enabled in SQL; ``status`` is a small
        # enum best filtered here rather than adding a second query path.
        if status is not None:
            items = [item for item in items if item.status is status]
        active = await _thread(runner.active_agent_ids)
        counts = await _thread(lambda: agents.counts(active_agent_ids=active))
        payload = []
        for item in items:
            entry = item.as_dict()
            entry["active"] = item.id in active
            if include_stats:
                entry["stats"] = await _thread(lambda i=item.id: runner.agent_stats(i))
            payload.append(entry)
    return {"agents": payload, "count": len(payload), "counts": counts}


@router.post("/definitions", status_code=201, summary="Create an agent definition")
async def create_definition(agents: Agents, body: AgentCreate) -> dict[str, Any]:
    """Create an agent. An unknown tool or an empty prompt is refused with 400."""
    with translate_errors("agent definition"):
        item = await _thread(
            lambda: agents.create(
                body.name,
                body.role,
                body.system_prompt,
                body.provider,
                body.model,
                agent_id=body.agent_id,
                kind=body.kind,
                status=body.status,
                fallback_model=body.fallback_model,
                tools=body.tools,
                permissions=body.permissions,
                temperature=body.temperature,
                max_tokens=body.max_tokens,
                budget_usd=body.budget_usd,
                timeout_s=body.timeout_s,
                retries=body.retries,
                scope=body.scope,
                tags=body.tags,
                notes=body.notes,
                enabled=body.enabled,
                presets=body.presets,
            )
        )
    return item.as_dict()


@router.get("/definitions/{agent_id}", summary="Return one agent definition")
async def get_definition(agents: Agents, runner: Runner, agent_id: str) -> dict[str, Any]:
    """Return an agent definition joined to its real run statistics."""
    with translate_errors("agent definition"):
        item = await _thread(lambda: agents.get(agent_id))
        stats = await _thread(lambda: runner.agent_stats(agent_id))
    payload = item.as_dict()
    payload["stats"] = stats
    payload["active"] = bool(stats.get("active"))
    return payload


@router.put("/definitions/{agent_id}", summary="Update an agent definition")
async def update_definition(agents: Agents, agent_id: str, body: AgentUpdate) -> dict[str, Any]:
    """Apply the fields that were sent; the definition's version is bumped."""
    changes = body.model_dump(exclude_none=True)
    with translate_errors("agent definition"):
        if not changes:
            item = await _thread(lambda: agents.get(agent_id))
        else:
            item = await _thread(lambda: agents.update(agent_id, **changes))
    return item.as_dict()


@router.delete("/definitions/{agent_id}", summary="Delete an agent definition")
async def delete_definition(agents: Agents, agent_id: str) -> dict[str, Any]:
    """Delete an agent definition. A locked definition is refused with 409."""
    with translate_errors("agent definition"):
        await _thread(lambda: agents.delete(agent_id))
    return {"deleted": agent_id}


@router.post("/definitions/{agent_id}/duplicate", status_code=201, summary="Duplicate an agent")
async def duplicate_definition(agents: Agents, agent_id: str, body: NameBody) -> dict[str, Any]:
    """Copy an agent definition under a new id."""
    with translate_errors("agent definition"):
        item = await _thread(lambda: agents.duplicate(agent_id, name=body.name))
    return item.as_dict()


@router.get("/definitions/{agent_id}/versions", summary="Frozen versions of an agent")
async def list_agent_versions(agents: Agents, agent_id: str) -> dict[str, Any]:
    """Return the agent's version archive, newest first.

    Every edit freezes the configuration it replaced, so the history is complete
    without anyone having to remember to save one. The working version is listed
    too, marked ``frozen: false``.
    """
    with translate_errors("agent definition"):
        versions = await _thread(lambda: agents.versions(agent_id))
    return {"agent_id": agent_id, "versions": versions, "count": len(versions)}


@router.get(
    "/definitions/{agent_id}/versions/{version}", summary="Return one frozen agent version"
)
async def get_agent_version(agents: Agents, agent_id: str, version: int) -> dict[str, Any]:
    """Return one archived version of an agent definition, in full."""
    with translate_errors("agent definition"):
        item = await _thread(lambda: agents.version(agent_id, version))
    return item.as_dict()


@router.post("/definitions/{agent_id}/versions", status_code=201, summary="Freeze a version")
async def save_agent_version(agents: Agents, agent_id: str, body: VersionBody) -> dict[str, Any]:
    """Freeze the agent's current configuration under its version number."""
    with translate_errors("agent definition"):
        return await _thread(lambda: agents.save_version(agent_id, note=body.note))


@router.post(
    "/definitions/{agent_id}/versions/{version}/restore", summary="Restore a frozen version"
)
async def restore_agent_version(agents: Agents, agent_id: str, version: int) -> dict[str, Any]:
    """Make an archived version current again.

    The restore is itself recorded as an edit - the state it replaced is frozen
    first and the version counter moves forward - so the history is a record of
    what happened rather than being rewound.
    """
    with translate_errors("agent definition"):
        item = await _thread(lambda: agents.restore(agent_id, version))
    return item.as_dict()


# ---------------------------------------------------------------------------
# pipelines (Pipeline Builder)
# ---------------------------------------------------------------------------
@router.get("/pipelines", summary="List saved pipelines")
async def list_pipelines(
    pipelines: Pipelines,
    include_stages: Annotated[bool, Query()] = True,
) -> dict[str, Any]:
    """Return every saved pipeline for the builder's "Saved pipeline" selector."""
    with translate_errors("pipeline"):
        items = await _thread(pipelines.list)
    return {
        "pipelines": [item.as_dict(include_stages=include_stages) for item in items],
        "count": len(items),
    }


@router.post("/pipelines", status_code=201, summary="Create a pipeline")
async def create_pipeline(pipelines: Pipelines, body: PipelineCreate) -> dict[str, Any]:
    """Create a pipeline, optionally with its stages in one call."""
    with translate_errors("pipeline"):
        item = await _thread(
            lambda: pipelines.create(
                body.name,
                pipeline_id=body.pipeline_id,
                objective=body.objective,
                description=body.description,
                stages=body.stages,
                tags=body.tags,
                status=body.status,
            )
        )
    return item.as_dict()


async def _annotate_effective_models(
    stages: list[dict[str, Any]],
    definitions: dict[str, Any],
    providers: ProviderRegistry,
) -> None:
    """Fill in the model each stage would actually run on, in place.

    A stage card must not say "no model" for a stage the runner would happily
    execute: the effective model is the stage override, else the assigned
    agent's model, else the configured provider's default resolved exactly the
    way :meth:`PipelineRunner._run_agent_stage` resolves it. ``effective_source``
    says which of the three it came from so the UI can mark an inherited value
    as inherited rather than as a choice somebody made.
    """
    # One resolution per distinct provider, not one per stage: `resolve` probes
    # the provider, and six stages of the same provider must not probe six times.
    defaults: dict[str | None, tuple[str | None, str | None]] = {}

    async def default_for(provider_name: str | None) -> tuple[str | None, str | None]:
        if provider_name not in defaults:
            try:
                instance, model_id = await providers.resolve(provider_name, None)
                defaults[provider_name] = (instance.name, model_id)
            except Exception as exc:  # noqa: BLE001 - reported as "unresolved"
                logger.debug("no default model for provider %r: %s", provider_name, exc)
                defaults[provider_name] = (provider_name, None)
        return defaults[provider_name]

    for stage in stages:
        if stage.get("kind") == "human_approval":
            stage["effective_model"] = None
            stage["effective_provider"] = None
            stage["effective_source"] = "human"
            continue
        agent = definitions.get(stage.get("agent_id"))
        agent_provider = getattr(agent, "provider", None) if agent else None
        agent_model = getattr(agent, "model", None) if agent else None
        provider_name = stage.get("provider") or agent_provider or None
        if stage.get("model"):
            stage["effective_model"] = stage["model"]
            stage["effective_provider"] = provider_name
            stage["effective_source"] = "stage"
            continue
        if agent_model:
            stage["effective_model"] = agent_model
            stage["effective_provider"] = provider_name
            stage["effective_source"] = "agent"
            continue
        resolved_provider, resolved_model = await default_for(provider_name)
        stage["effective_model"] = resolved_model
        stage["effective_provider"] = resolved_provider
        stage["effective_source"] = "provider_default" if resolved_model else "unresolved"


@router.get("/pipelines/{pipeline_id}", summary="Return one pipeline")
async def get_pipeline(
    pipelines: Pipelines,
    agents: Agents,
    providers: Providers,
    pipeline_id: str,
    version: Annotated[int | None, Query(description="a frozen version instead of the draft")] = None,
) -> dict[str, Any]:
    """Return a pipeline with its stages and the problems that would block a run.

    Each stage also carries the model it would really execute on
    (``effective_model``), resolved the same way the runner resolves it.
    """
    with translate_errors("pipeline"):
        item = await _thread(lambda: pipelines.get(pipeline_id, version=version))
        definitions = await _thread(lambda: {d.id: d for d in agents.list()})
        problems = await _thread(lambda: pipelines.validate(item, agents=set(definitions)))
        versions = await _thread(lambda: pipelines.versions(pipeline_id))
    payload = item.as_dict()
    await _annotate_effective_models(payload.get("stages") or [], definitions, providers)
    payload["problems"] = problems
    payload["valid"] = not problems
    payload["versions"] = versions
    return payload


@router.put("/pipelines/{pipeline_id}", summary="Update a pipeline")
async def update_pipeline(
    pipelines: Pipelines, pipeline_id: str, body: PipelineUpdate
) -> dict[str, Any]:
    """Update the working document. Saving a version is a separate call."""
    changes = body.model_dump(exclude_none=True)
    with translate_errors("pipeline"):
        if not changes:
            item = await _thread(lambda: pipelines.get(pipeline_id))
        else:
            item = await _thread(lambda: pipelines.update(pipeline_id, **changes))
    return item.as_dict()


@router.delete("/pipelines/{pipeline_id}", summary="Delete a pipeline")
async def delete_pipeline(pipelines: Pipelines, pipeline_id: str) -> dict[str, Any]:
    """Delete a pipeline and its frozen versions. Runs keep their snapshot."""
    with translate_errors("pipeline"):
        await _thread(lambda: pipelines.delete(pipeline_id))
    return {"deleted": pipeline_id}


@router.post("/pipelines/{pipeline_id}/duplicate", status_code=201, summary="Duplicate a pipeline")
async def duplicate_pipeline(
    pipelines: Pipelines, pipeline_id: str, body: NameBody
) -> dict[str, Any]:
    """Copy a pipeline, stages included, as an unlocked draft."""
    with translate_errors("pipeline"):
        item = await _thread(lambda: pipelines.duplicate(pipeline_id, name=body.name))
    return item.as_dict()


@router.get("/pipelines/{pipeline_id}/versions", summary="List frozen versions")
async def list_versions(pipelines: Pipelines, pipeline_id: str) -> dict[str, Any]:
    """Return the frozen versions of a pipeline, newest first."""
    with translate_errors("pipeline"):
        versions = await _thread(lambda: pipelines.versions(pipeline_id))
        item = await _thread(lambda: pipelines.get(pipeline_id))
    return {
        "pipeline_id": pipeline_id,
        "current_version": item.version,
        "dirty": item.dirty,
        "versions": versions,
        "count": len(versions),
    }


@router.post("/pipelines/{pipeline_id}/versions", status_code=201, summary="Save a new version")
async def save_version(
    pipelines: Pipelines, pipeline_id: str, body: VersionBody
) -> dict[str, Any]:
    """Freeze the working document and bump the version number."""
    with translate_errors("pipeline"):
        item = await _thread(lambda: pipelines.save_version(pipeline_id, note=body.note))
    return item.as_dict()


@router.post("/pipelines/{pipeline_id}/stages", status_code=201, summary="Add a stage")
async def add_stage(pipelines: Pipelines, pipeline_id: str, body: StageBody) -> dict[str, Any]:
    """Insert a stage into the working document."""
    payload = body.model_dump(exclude_none=True, exclude={"index", "stage_id"})
    if body.stage_id:
        payload["id"] = body.stage_id
    payload.setdefault("name", body.stage_id or "New stage")
    with translate_errors("pipeline stage"):
        item = await _thread(lambda: pipelines.add_stage(pipeline_id, payload, index=body.index))
    return item.as_dict()


@router.put("/pipelines/{pipeline_id}/stages/{stage_id}", summary="Update a stage")
async def update_stage(
    pipelines: Pipelines, pipeline_id: str, stage_id: str, body: StageBody
) -> dict[str, Any]:
    """Update one stage's configuration; only the fields sent are applied.

    ``exclude_unset`` - not ``exclude_none`` - is what makes a field *clearable*:
    the Pipeline Builder sends ``{"model": null, "provider": null}`` to mean "no
    model", and dropping explicit nulls would silently keep the old value while
    answering 200.
    """
    changes = body.model_dump(exclude_unset=True, exclude={"index", "stage_id"})
    with translate_errors("pipeline stage"):
        if not changes:
            item = await _thread(lambda: pipelines.get(pipeline_id))
        else:
            item = await _thread(lambda: pipelines.update_stage(pipeline_id, stage_id, **changes))
    return item.as_dict()


@router.delete("/pipelines/{pipeline_id}/stages/{stage_id}", summary="Remove a stage")
async def remove_stage(pipelines: Pipelines, pipeline_id: str, stage_id: str) -> dict[str, Any]:
    """Remove a stage. The locked human-approval boundary is refused with 409."""
    with translate_errors("pipeline stage"):
        item = await _thread(lambda: pipelines.remove_stage(pipeline_id, stage_id))
    return item.as_dict()


@router.post(
    "/pipelines/{pipeline_id}/stages/{stage_id}/duplicate",
    status_code=201,
    summary="Duplicate a stage",
)
async def duplicate_stage(pipelines: Pipelines, pipeline_id: str, stage_id: str) -> dict[str, Any]:
    """Duplicate a stage in place, right after the original."""
    with translate_errors("pipeline stage"):
        item = await _thread(lambda: pipelines.duplicate_stage(pipeline_id, stage_id))
    return item.as_dict()


@router.post("/pipelines/{pipeline_id}/stages/order", summary="Reorder stages")
async def reorder_stages(
    pipelines: Pipelines, pipeline_id: str, body: StageOrder
) -> dict[str, Any]:
    """Reorder the stages. The list must be a permutation of the current ids."""
    with translate_errors("pipeline stage"):
        item = await _thread(lambda: pipelines.reorder_stages(pipeline_id, body.order))
    return item.as_dict()


@router.get("/pipelines/{pipeline_id}/validate", summary="Validate a pipeline")
async def validate_pipeline(
    pipelines: Pipelines, agents: Agents, pipeline_id: str
) -> dict[str, Any]:
    """Return the contract/DAG problems the builder's graph preview shows."""
    with translate_errors("pipeline"):
        item = await _thread(lambda: pipelines.get(pipeline_id))
        known = await _thread(lambda: {definition.id for definition in agents.list()})
        problems = await _thread(lambda: pipelines.validate(item, agents=known))
    return {
        "pipeline_id": pipeline_id,
        "version": item.version,
        "valid": not problems,
        "problems": problems,
        "stages": [stage.as_dict() for stage in item.stages],
    }


@router.post("/pipelines/{pipeline_id}/run", status_code=201, summary="Start a pipeline run")
async def run_pipeline(runner: Runner, pipeline_id: str, body: RunStart) -> dict[str, Any]:
    """Start a real run. Progress and logs stream on the ``runs`` WS channel."""
    with translate_errors("pipeline run"):
        run = await runner.start(
            pipeline_id,
            trigger=body.trigger,
            inputs=body.inputs,
            version=body.version,
            objective=body.objective,
            name=body.name,
            stage_overrides=body.stage_overrides,
        )
    return run.as_dict()


@router.get("/pipelines/{pipeline_id}/estimate", summary="Estimated cost and time of a run")
async def estimate_pipeline(
    runner: Runner,
    pipeline_id: str,
    version: Annotated[int | None, Query(description="price a frozen version")] = None,
) -> dict[str, Any]:
    """Return the pre-run cost/time estimate the run panels show.

    Built from this workspace's own recorded token usage per stage, priced at
    the model each stage would use. Every row names the basis it used, and a
    stage whose model has no published price is excluded from the total rather
    than silently counted as free.
    """
    with translate_errors("pipeline"):
        return await runner.estimate(pipeline_id, version=version)


@router.post(
    "/pipelines/{pipeline_id}/stages/{stage_id}/run",
    status_code=201,
    summary="Run a single stage",
)
async def run_single_stage(
    runner: Runner, pipeline_id: str, stage_id: str, body: StageRunBody
) -> dict[str, Any]:
    """Execute one stage on its own (the Pipeline Builder's *Run this stage*).

    The result is a real run limited to that stage: every other stage is
    switched off for this run only, so the saved pipeline is untouched. The
    stage sees the supplied ``inputs`` in place of upstream output, because the
    stages before it did not execute.
    """
    with translate_errors("pipeline stage run"):
        run = await runner.run_stage(
            pipeline_id,
            stage_id,
            inputs=body.inputs,
            version=body.version,
            model=body.model,
            provider=body.provider,
            wait=body.wait,
        )
    return run.as_dict()


# ---------------------------------------------------------------------------
# runs
# ---------------------------------------------------------------------------
@router.get("/runs", summary="List pipeline runs")
async def list_runs(
    runner: Runner,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    status: Annotated[RunStatus | None, Query()] = None,
    pipeline_id: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """Return runs newest first, with the real per-status counts."""
    with translate_errors("pipeline run"):
        runs = await _thread(
            lambda: runner.list(limit=limit, status=status, pipeline_id=pipeline_id)
        )
        counts = await _thread(runner.counts)
    return {
        "runs": [run.as_dict(include_payloads=False) for run in runs],
        "count": len(runs),
        "counts": counts,
    }


@router.get("/runs/compare", summary="Compare two runs side by side")
async def compare_runs(
    runner: Runner,
    a: Annotated[str, Query(description="run id on the left")],
    b: Annotated[str, Query(description="run id on the right")],
) -> dict[str, Any]:
    """Return two runs and the per-stage differences between them.

    This backs the Pipeline tab's *Run comparison*: the same pipeline run twice
    is the only way to see what a prompt or model change actually did, so the
    payload pairs the stages by id and reports the deltas in status, model,
    tokens, cost and duration rather than leaving the reader to eyeball them.
    """
    with translate_errors("pipeline run"):
        left = await _thread(lambda: runner.get(a, include_logs=False))
        right = await _thread(lambda: runner.get(b, include_logs=False))
    left_stages = {stage.stage_id: stage for stage in left.stages}
    right_stages = {stage.stage_id: stage for stage in right.stages}
    order: list[str] = [stage.stage_id for stage in sorted(left.stages, key=lambda s: s.index)]
    order += [stage_id for stage_id in right_stages if stage_id not in left_stages]
    rows: list[dict[str, Any]] = []
    for stage_id in order:
        one = left_stages.get(stage_id)
        two = right_stages.get(stage_id)
        rows.append(
            {
                "stage_id": stage_id,
                "name": (one or two).name if (one or two) else stage_id,
                "a": _compare_side(one),
                "b": _compare_side(two),
                "only_in": None if one and two else ("a" if one else "b"),
                "model_changed": bool(one and two and one.model != two.model),
                "status_changed": bool(one and two and one.status is not two.status),
                "cost_delta": (
                    round(two.usage.cost_usd - one.usage.cost_usd, 6) if one and two else None
                ),
                "duration_delta_ms": (
                    round((two.duration_ms or 0.0) - (one.duration_ms or 0.0), 1)
                    if one and two and one.duration_ms is not None and two.duration_ms is not None
                    else None
                ),
            }
        )
    return {
        "a": left.as_dict(include_payloads=False),
        "b": right.as_dict(include_payloads=False),
        "same_pipeline": left.pipeline_id == right.pipeline_id,
        "stages": rows,
        "totals": {
            "cost_delta": round(right.cost_usd - left.cost_usd, 6),
            "token_delta": (
                right.usage.input_tokens
                + right.usage.output_tokens
                - left.usage.input_tokens
                - left.usage.output_tokens
            ),
            "duration_delta_ms": (
                round((right.duration_ms or 0.0) - (left.duration_ms or 0.0), 1)
                if left.duration_ms is not None and right.duration_ms is not None
                else None
            ),
        },
    }


def _compare_side(stage: Any | None) -> dict[str, Any] | None:
    """Render one side of a run comparison row."""
    if stage is None:
        return None
    usage = stage.usage
    return {
        "status": str(stage.status),
        "model": stage.model,
        "provider": stage.provider,
        "agent_id": stage.agent_id,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cost_usd": round(usage.cost_usd, 6),
        "duration_ms": stage.duration_ms,
        "tool_calls": len(stage.tool_calls or []),
        "error": stage.error,
    }


@router.get("/runs/{run_id}", summary="Return one run")
async def get_run(
    runner: Runner,
    run_id: str,
    include_logs: Annotated[bool, Query()] = True,
) -> dict[str, Any]:
    """Return a run with its stage records, inputs, outputs and logs."""
    with translate_errors("pipeline run"):
        run = await _thread(lambda: runner.get(run_id, include_logs=include_logs))
        spend = await _thread(lambda: runner.spend(window="day", run_id=run_id))
    payload = run.as_dict()
    payload["spend"] = spend
    return payload


@router.get("/runs/{run_id}/logs", summary="Run log lines")
async def get_run_logs(
    runner: Runner,
    run_id: str,
    stage_id: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """Return the persisted log lines; the live ones arrive over WebSocket."""
    with translate_errors("pipeline run"):
        logs = await _thread(lambda: runner.logs(run_id, stage_id=stage_id))
    return {"run_id": run_id, "stage_id": stage_id, "logs": logs, "count": len(logs)}


@router.get("/runs/{run_id}/artifacts", summary="Run artifacts")
async def get_run_artifacts(
    runner: Runner,
    run_id: str,
    stage_id: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """Return artifact metadata; the payload itself is fetched per artifact."""
    with translate_errors("pipeline run"):
        artifacts = await _thread(lambda: runner.artifacts(run_id, stage_id=stage_id))
    return {
        "run_id": run_id,
        "stage_id": stage_id,
        "artifacts": [item.as_dict(include_data=False) for item in artifacts],
        "count": len(artifacts),
    }


@router.get("/artifacts/{artifact_id}", summary="Return one artifact with its data")
async def get_artifact(runner: Runner, artifact_id: str) -> dict[str, Any]:
    """Return one artifact, reading it back from disk when it was spilled."""
    with translate_errors("artifact"):
        artifact = await _thread(lambda: runner.artifact(artifact_id))
    return artifact.as_dict()


@router.post("/runs/{run_id}/cancel", summary="Cancel a run")
async def cancel_run(runner: Runner, run_id: str, body: CancelBody) -> dict[str, Any]:
    """Kill a run that is queued, executing or waiting on an approval."""
    with translate_errors("pipeline run"):
        run = await runner.cancel(run_id, reason=body.reason)
    return run.as_dict(include_payloads=False)


@router.post("/runs/{run_id}/resume", summary="Resume an approved or paused run")
async def resume_run(runner: Runner, run_id: str) -> dict[str, Any]:
    """Continue a run that is paused, or whose approval was granted out of band.

    A paused run resumes from the stage it stopped in front of; a run blocked on
    an approval gate is released by approving the outstanding request.
    """
    with translate_errors("pipeline run"):
        current = await _thread(lambda: runner.get(run_id, include_logs=False))
        if current.status is RunStatus.PAUSED:
            run = await runner.unpause(run_id)
        else:
            run = await runner.resume(run_id)
    return run.as_dict(include_payloads=False)


@router.post("/runs/{run_id}/pause", summary="Pause a run")
async def pause_run(runner: Runner, run_id: str, body: PauseBody) -> dict[str, Any]:
    """Stop a run at its next stage boundary without killing it.

    The stage in flight is allowed to finish (its tokens are already spent);
    nothing after it starts until the run is resumed.
    """
    with translate_errors("pipeline run"):
        run = await runner.pause(run_id, reason=body.reason)
    return run.as_dict(include_payloads=False)


@router.post("/runs/{run_id}/replay", status_code=201, summary="Replay a run from a stage")
async def replay_run(runner: Runner, run_id: str, body: ReplayBody) -> dict[str, Any]:
    """Create a *new* run that repeats a finished one from the chosen stage.

    Unlike a retry, the original run is never modified - the ledger stays
    append-only. Stages before the chosen one are inherited from the original
    (recorded as ``skipped`` with their payload attached, so the replay never
    claims to have paid for them) and execution restarts from there.
    """
    with translate_errors("pipeline run"):
        run = await runner.replay(
            run_id,
            stage_id=body.stage_id,
            inputs=body.inputs,
            stage_overrides=body.stage_overrides,
            wait=body.wait,
        )
    return run.as_dict()


@router.post("/runs/{run_id}/retry", summary="Retry a run's failed stage")
async def retry_run(runner: Runner, run_id: str, body: RetryBody) -> dict[str, Any]:
    """Re-execute the run from its first unsuccessful stage.

    Stages that already succeeded are kept; the retried stage and everything
    after it are reset to ``pending`` before execution restarts.
    """
    with translate_errors("pipeline run"):
        run = await runner.retry(run_id, stage_id=body.stage_id)
    return run.as_dict(include_payloads=False)


# ---------------------------------------------------------------------------
# inbox and approvals
# ---------------------------------------------------------------------------
@router.get("/inbox", summary="The attention queue")
async def get_inbox(
    request: Request,
    findings: Findings,
    approvals: Approvals,
    limit: Annotated[int, Query(ge=1, le=200)] = 25,
    type: Annotated[
        str | None, Query(description="keep only one kind: Approval/Validation/Experiment/Data/Agent")
    ] = None,
) -> dict[str, Any]:
    """Merge everything that needs a person, urgent first.

    :class:`tradinglib.agents.inbox.InboxComposer` builds the five reference kinds
    from their real sources - approval gates, failed validation gates, failed
    experiment runs, open quality incidents, and agent findings plus failed
    pipeline runs. A subsystem that is not running in this process is reported
    as unavailable in ``sources_available`` instead of being faked.
    """
    state = request.app.state
    sources = InboxSources(
        approvals=approvals,
        findings=findings,
        validation=getattr(state, "validation_engine", None),
        experiments=getattr(state, "experiment_engine", None),
        quality=getattr(state, "quality_engine", None),
        runner=getattr(state, "pipeline_runner", None),
    )
    with translate_errors("agent inbox"):
        payload = await _thread(lambda: InboxComposer(sources).build(limit=limit))
    if type and type != "All types":
        wanted = type.strip().lower()
        payload["items"] = [
            item for item in payload["items"] if str(item["type"]).lower() == wanted
        ]
        payload["count"] = len(payload["items"])
        payload["filtered_by"] = type
    payload["unacknowledged_findings"] = payload["counts"].get("Finding", 0)
    return payload


@router.get("/approvals/validation-targets", summary="What more validation may be asked for")
async def validation_targets() -> dict[str, Any]:
    """Return the choices the Approval Review's validation-request select offers.

    Served from the backend so the select can never offer evidence this
    workspace has no engine to produce.
    """
    return {
        "targets": [
            {"value": value, "label": label} for value, label in sorted(VALIDATION_TARGETS.items())
        ],
        "count": len(VALIDATION_TARGETS),
    }


@router.get("/approvals", summary="List approval requests")
async def list_approvals(
    approvals: Approvals,
    state: Annotated[ApprovalState | None, Query()] = None,
    run_id: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    """Return approval requests, newest first, with the counts by state."""
    with translate_errors("approval"):
        items = await _thread(
            lambda: approvals.list(state=state, run_id=run_id, limit=limit)
        )
        counts = await _thread(approvals.counts)
        pending = await _thread(approvals.pending_count)
    return {
        "approvals": [item.as_dict() for item in items],
        "count": len(items),
        "counts": counts,
        "pending": pending,
    }


@router.get("/approvals/{approval_id}", summary="Return one approval request")
async def get_approval(approvals: Approvals, approval_id: str) -> dict[str, Any]:
    """Return one request joined to the run and stage it is blocking."""
    with translate_errors("approval"):
        item = await _thread(lambda: approvals.get(approval_id))
    return item.as_dict()


@router.post("/approvals/{approval_id}", summary="Approve or reject")
async def decide_approval(
    approvals: Approvals, approval_id: str, body: ApprovalDecision
) -> dict[str, Any]:
    """Decide a pending request, or ask for more evidence before deciding.

    ``approve`` resumes the run and ``reject`` kills it. ``request_validation``
    is not a third outcome: it leaves the request pending (and its run blocked)
    and records which further validation the reviewer wants, so the ask becomes
    an actionable work item instead of a comment.
    """
    decision = body.decision.strip().lower().replace("-", "_").replace(" ", "_")
    with translate_errors("approval"):
        if decision in {"approve", "approved", "accept"}:
            item = await approvals.approve(approval_id, by=body.by, comment=body.comment)
        elif decision in {"reject", "rejected", "deny"}:
            item = await approvals.reject(approval_id, by=body.by, comment=body.comment)
        elif decision in {"request_validation", "more_validation", "validate"}:
            if not body.target:
                raise ValueError(
                    "requesting more validation needs a target; expected one of: "
                    + ", ".join(sorted(VALIDATION_TARGETS))
                )
            item = await approvals.request_validation(
                approval_id, body.target, by=body.by, comment=body.comment
            )
        else:
            raise ValueError(
                f"unknown decision {body.decision!r}; expected 'approve', 'reject' "
                "or 'request_validation'"
            )
        pending = await _thread(approvals.pending_count)
    payload = item.as_dict()
    payload["pending"] = pending
    return payload


# ---------------------------------------------------------------------------
# findings
# ---------------------------------------------------------------------------
@router.get("/findings", summary="List findings")
async def list_findings(
    findings: Findings,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    tag: Annotated[str | None, Query()] = None,
    symbol: Annotated[str | None, Query()] = None,
    status: Annotated[FindingStatus | None, Query()] = None,
    run_id: Annotated[str | None, Query()] = None,
    agent_id: Annotated[str | None, Query()] = None,
    acknowledged: Annotated[bool | None, Query()] = None,
    q: Annotated[str | None, Query(description="free-text search across the registry")] = None,
) -> dict[str, Any]:
    """Return the findings registry, newest first, with counts and filter values."""
    with translate_errors("finding"):
        if q:
            items = await _thread(lambda: findings.search(q, limit=limit))
        else:
            items = await _thread(
                lambda: findings.list(
                    limit=limit,
                    offset=offset,
                    tag=tag,
                    symbol=symbol,
                    status=status,
                    run_id=run_id,
                    agent_id=agent_id,
                    acknowledged=acknowledged,
                )
            )
        counts = await _thread(findings.counts)
        tags = await _thread(findings.tags)
        symbols = await _thread(findings.symbols)
    return {
        "findings": [item.as_dict() for item in items],
        "count": len(items),
        "counts": counts,
        "tags": tags,
        "symbols": symbols,
        "query": q,
    }


@router.post("/findings", status_code=201, summary="Record a finding")
async def create_finding(findings: Findings, body: FindingCreate) -> dict[str, Any]:
    """Record a finding. The same store the agents' ``save_finding`` tool writes to."""
    fields = body.model_dump(exclude_none=True, exclude={"title", "summary"})
    with translate_errors("finding"):
        item = await _thread(lambda: findings.create(body.title, body.summary, **fields))
    return item.as_dict()


@router.get("/findings/{finding_id}", summary="Return one finding")
async def get_finding(findings: Findings, finding_id: str) -> dict[str, Any]:
    """Return one finding with its evidence list."""
    with translate_errors("finding"):
        item = await _thread(lambda: findings.get(finding_id))
    return item.as_dict()


@router.put("/findings/{finding_id}", summary="Update a finding")
async def update_finding(
    findings: Findings, finding_id: str, body: FindingUpdate
) -> dict[str, Any]:
    """Update a finding's claim, status, confidence or evidence."""
    changes = body.model_dump(exclude_none=True)
    with translate_errors("finding"):
        if not changes:
            item = await _thread(lambda: findings.get(finding_id))
        else:
            item = await _thread(lambda: findings.update(finding_id, **changes))
    return item.as_dict()


@router.delete("/findings/{finding_id}", summary="Delete a finding")
async def delete_finding(findings: Findings, finding_id: str) -> dict[str, Any]:
    """Delete a finding."""
    with translate_errors("finding"):
        await _thread(lambda: findings.delete(finding_id))
    return {"deleted": finding_id}


@router.post("/findings/{finding_id}/acknowledge", summary="Acknowledge a finding")
async def acknowledge_finding(
    findings: Findings, finding_id: str, by: Annotated[str, Query()] = "user"
) -> dict[str, Any]:
    """Mark a finding reviewed, which removes it from the inbox."""
    with translate_errors("finding"):
        item = await _thread(lambda: findings.acknowledge(finding_id, by=by))
    return item.as_dict()


# ---------------------------------------------------------------------------
# knowledge
# ---------------------------------------------------------------------------
@router.get("/knowledge", summary="Knowledge base")
async def list_knowledge(
    knowledge: Knowledge,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    tag: Annotated[str | None, Query()] = None,
    kind: Annotated[str | None, Query()] = None,
    symbol: Annotated[str | None, Query()] = None,
    run_id: Annotated[str | None, Query()] = None,
    q: Annotated[str | None, Query(description="full-text search")] = None,
) -> dict[str, Any]:
    """Return knowledge entries, collections and the "recently learned" feed.

    With ``q`` the list is the ranked search result and each row carries its
    score and snippet; ``matched`` says whether every term hit or only some.
    """
    with translate_errors("knowledge entry"):
        if q:
            hits = await _thread(lambda: knowledge.search_ranked(q, limit=limit))
            entries = [
                {
                    **hit["entry"].as_dict(include_body=False),
                    "score": hit["score"],
                    "snippet": hit["snippet"],
                    "matched": hit["matched"],
                }
                for hit in hits
            ]
        else:
            items = await _thread(
                lambda: knowledge.list(
                    limit=limit, offset=offset, tag=tag, kind=kind, symbol=symbol, run_id=run_id
                )
            )
            entries = [item.as_dict(include_body=False) for item in items]
        collections = await _thread(knowledge.collections)
        recent = await _thread(lambda: knowledge.recent(limit=10))
        counts = await _thread(knowledge.counts)
    return {
        "entries": entries,
        "count": len(entries),
        "collections": collections,
        "recent": recent,
        "counts": counts,
        "query": q,
    }


@router.post("/knowledge", status_code=201, summary="Create a knowledge entry")
async def create_knowledge(knowledge: Knowledge, body: KnowledgeCreate) -> dict[str, Any]:
    """Create and index a knowledge entry."""
    fields = body.model_dump(exclude_none=True, exclude={"title", "body"})
    with translate_errors("knowledge entry"):
        item = await _thread(lambda: knowledge.create(body.title, body.body, **fields))
    return item.as_dict()


@router.get("/knowledge/{entry_id}", summary="Return one knowledge entry")
async def get_knowledge_entry(knowledge: Knowledge, entry_id: str) -> dict[str, Any]:
    """Return one entry, body included."""
    with translate_errors("knowledge entry"):
        item = await _thread(lambda: knowledge.get(entry_id))
    return item.as_dict()


@router.put("/knowledge/{entry_id}", summary="Update a knowledge entry")
async def update_knowledge(
    knowledge: Knowledge, entry_id: str, body: KnowledgeUpdate
) -> dict[str, Any]:
    """Update an entry and re-index it."""
    changes = body.model_dump(exclude_none=True)
    with translate_errors("knowledge entry"):
        if not changes:
            item = await _thread(lambda: knowledge.get(entry_id))
        else:
            item = await _thread(lambda: knowledge.update(entry_id, **changes))
    return item.as_dict()


@router.delete("/knowledge/{entry_id}", summary="Delete a knowledge entry")
async def delete_knowledge(knowledge: Knowledge, entry_id: str) -> dict[str, Any]:
    """Delete an entry and drop it from the index."""
    with translate_errors("knowledge entry"):
        await _thread(lambda: knowledge.delete(entry_id))
    return {"deleted": entry_id}


# ---------------------------------------------------------------------------
# audit log
# ---------------------------------------------------------------------------
@router.get("/audit", summary="Immutable audit log")
async def list_audit(
    audit: Audit,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
    actor: Annotated[str | None, Query()] = None,
    action: Annotated[str | None, Query()] = None,
    target: Annotated[str | None, Query()] = None,
    run_id: Annotated[str | None, Query()] = None,
    since: Annotated[str | None, Query(description="ISO-8601 lower bound")] = None,
    until: Annotated[str | None, Query(description="ISO-8601 upper bound")] = None,
    q: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """Return audit records newest first, with the filter values the tab offers."""
    with translate_errors("audit record"):
        start = _parse_ts(since)
        end = _parse_ts(until)
        records = await _thread(
            lambda: audit.list(
                limit=limit,
                offset=offset,
                actor=actor,
                action=action,
                target=target,
                run_id=run_id,
                since=start,
                until=end,
                query=q,
            )
        )
        total = await _thread(
            lambda: audit.count(
                actor=actor,
                action=action,
                target=target,
                run_id=run_id,
                since=start,
                until=end,
                query=q,
            )
        )
        actors = await _thread(audit.actors)
        actions = await _thread(audit.actions)
        summary = await _thread(audit.summary)
        today = _session_day_start()
        blocked_today = await _thread(lambda: audit.guardrail_blocks(since=today))
    policy = get_policy()
    return {
        "records": [record.as_dict() for record in records],
        "count": len(records),
        "total": total,
        "actors": actors,
        "actions": actions,
        "summary": summary,
        "blocked_today": blocked_today,
        "day_start": today.isoformat(),
        "guardrails": policy.guardrails(blocked_today=blocked_today),
    }


@router.get("/audit/export", summary="Export the audit log")
async def export_audit(
    audit: Audit,
    fmt: Annotated[str, Query(alias="format", description="csv or json")] = "csv",
    limit: Annotated[int, Query(ge=1, le=50_000)] = 5000,
    actor: Annotated[str | None, Query()] = None,
    action: Annotated[str | None, Query()] = None,
    target: Annotated[str | None, Query()] = None,
    run_id: Annotated[str | None, Query()] = None,
    since: Annotated[str | None, Query(description="ISO-8601 lower bound")] = None,
    until: Annotated[str | None, Query(description="ISO-8601 upper bound")] = None,
    q: Annotated[str | None, Query(description="substring match")] = None,
) -> Response:
    """Download the filtered audit log as CSV or JSON.

    The same filters the tab is showing are applied, so the file matches the
    table the person is looking at rather than the whole store.
    """
    with translate_errors("audit record"):
        start = _parse_ts(since)
        end = _parse_ts(until)
        body, media_type, filename = await _thread(
            lambda: audit.export(
                fmt=fmt,
                limit=limit,
                actor=actor,
                action=action,
                target=target,
                run_id=run_id,
                since=start,
                until=end,
                query=q,
            )
        )
    return Response(
        content=body,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# autopipeline schedules
# ---------------------------------------------------------------------------
@router.get("/autopipeline", summary="AutoPipeline schedules")
async def list_schedules(
    schedules: Schedules,
    enabled: Annotated[bool | None, Query()] = None,
) -> dict[str, Any]:
    """Return the schedule table with each schedule's usage and guardrail state."""
    with translate_errors("schedule"):
        status = await _thread(schedules.status)
        items = await _thread(lambda: schedules.list(enabled=enabled))
        rows = []
        for schedule in items:
            entry = schedule.as_dict()
            entry["usage"] = await _thread(lambda i=schedule.id: schedules.usage(i))
            entry["blocked"] = await _thread(lambda i=schedule.id: schedules.guardrail_block(i))
            entry["runs"] = await _thread(lambda i=schedule.id: schedules.runs(i, limit=10))
            rows.append(entry)
    status["schedules"] = rows
    status["count"] = len(rows)
    return status


@router.post("/autopipeline", status_code=201, summary="Create a schedule")
async def create_schedule(schedules: Schedules, body: ScheduleCreate) -> dict[str, Any]:
    """Create a recurring trigger for a pipeline. An unparsable cron is a 400."""
    fields = body.model_dump(exclude_none=True, exclude={"pipeline_id", "cron"})
    with translate_errors("schedule"):
        item = await _thread(lambda: schedules.create(body.pipeline_id, body.cron, **fields))
    return item.as_dict()


@router.get("/autopipeline/{schedule_id}", summary="Return one schedule")
async def get_schedule(schedules: Schedules, schedule_id: str) -> dict[str, Any]:
    """Return one schedule with its usage, guardrail state and real runs."""
    with translate_errors("schedule"):
        item = await _thread(lambda: schedules.get(schedule_id))
        usage = await _thread(lambda: schedules.usage(schedule_id))
        blocked = await _thread(lambda: schedules.guardrail_block(schedule_id))
        runs = await _thread(lambda: schedules.runs(schedule_id, limit=25))
    payload = item.as_dict()
    payload["usage"] = usage
    payload["blocked"] = blocked
    payload["runs"] = runs
    return payload


@router.put("/autopipeline/{schedule_id}", summary="Update a schedule")
async def update_schedule(
    schedules: Schedules, schedule_id: str, body: ScheduleUpdate
) -> dict[str, Any]:
    """Update a schedule; the timer is re-installed with the new cadence."""
    changes = body.model_dump(exclude_none=True)
    with translate_errors("schedule"):
        if not changes:
            item = await _thread(lambda: schedules.get(schedule_id))
        else:
            item = await _thread(lambda: schedules.update(schedule_id, **changes))
    return item.as_dict()


@router.delete("/autopipeline/{schedule_id}", summary="Delete a schedule")
async def delete_schedule(schedules: Schedules, schedule_id: str) -> dict[str, Any]:
    """Delete a schedule and remove its timer."""
    with translate_errors("schedule"):
        await _thread(lambda: schedules.delete(schedule_id))
    return {"deleted": schedule_id}


@router.post("/autopipeline/{schedule_id}/run", status_code=201, summary="Fire a schedule now")
async def fire_schedule(
    schedules: Schedules,
    schedule_id: str,
    force: Annotated[bool, Query(description="ignore the guardrails")] = False,
) -> dict[str, Any]:
    """Start this schedule's pipeline immediately; a guardrail block is a 409."""
    with translate_errors("schedule"):
        try:
            run_id = await schedules.trigger_now(schedule_id, force=force)
        except GuardrailBlocked as exc:
            # A guardrail refusal is a valid request the state forbids, which is
            # the same shape ``translate_errors`` gives a PermissionError.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "guardrail_blocked",
                    "entity": "schedule",
                    "schedule_id": exc.schedule_id,
                    "detail": exc.reason,
                },
            ) from exc
    return {"schedule_id": schedule_id, "run_id": run_id}


# ---------------------------------------------------------------------------
# evidence explorer
# ---------------------------------------------------------------------------
@router.get("/evidence", summary="The evidence lineage graph")
async def evidence_graph(
    findings: Findings,
    audit: Audit,
    finding_id: Annotated[str | None, Query(description="centre the graph on one finding")] = None,
    run_id: Annotated[str | None, Query(description="centre it on one run's findings")] = None,
    node: Annotated[str | None, Query(description="node to select and focus on")] = None,
    focus: Annotated[
        str, Query(description="all, upstream or downstream")
    ] = "all",
    limit: Annotated[int, Query(ge=1, le=200)] = 40,
) -> dict[str, Any]:
    """Return the Sources -> Claims -> Findings -> Hypotheses -> Experiments ->
    Validation -> Decision graph.

    Every edge is a stored reference (a finding's hypothesis, an experiment's
    hypothesis, a backtest's experiment, a report's backtest, an approval's
    run), so the graph traces the objects that actually exist rather than a
    drawn illustration of how research is supposed to flow. ``notes`` reports
    any column that could not be filled and why.
    """
    with translate_errors("evidence graph"):
        graph = await _thread(
            lambda: EvidenceGraph(findings).build(
                finding_id=finding_id, run_id=run_id, limit=limit
            )
        )
    selected = node or graph["selected"]
    related = (
        sorted(EvidenceGraph.related(graph, selected, focus)) if selected else []
    )
    graph["selected"] = selected
    graph["focus"] = focus
    graph["related"] = related
    graph["total"] = len(graph["nodes"])
    await _attach_audit_counts(audit, graph["nodes"])
    return graph


async def _attach_audit_counts(audit: AuditLog, nodes: list[dict[str, Any]]) -> None:
    """Add ``audit_events`` to every evidence node.

    The inspector shows how many immutable audit records name the object a node
    stands for. The count is done here rather than in the graph builder so the
    builder stays a pure join over the research registries.

    A node id is ``<COLUMN>-<object id>``; the object id is what the audit log
    stores in ``target``/``run_id``, so the prefix is stripped before matching.
    """
    try:
        records = await _thread(lambda: audit.list(limit=2000))
    except Exception:  # noqa: BLE001 - an unreadable log must not break the graph
        logger.warning("audit counts for the evidence graph are unavailable", exc_info=True)
        for item in nodes:
            item["audit_events"] = None
        return
    targets = [
        {str(record.target or ""), str(record.run_id or "")} for record in records
    ]
    for item in nodes:
        identifier = str(item.get("id") or "")
        _, _, object_id = identifier.partition("-")
        route_id = str((item.get("route") or {}).get("id") or "")
        wanted = {value for value in (object_id, route_id) if value}
        item["audit_events"] = sum(1 for names in targets if names & wanted)
