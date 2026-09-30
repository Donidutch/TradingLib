"""Pipeline execution: stage running, artifacts, logs, tokens and budgets.

A run walks a pipeline document stage by stage. Each agent stage builds a tool
set that calls **the real library** - resolve a universe, run a saved screen,
read a company, read stored bars, run a backtest, save a finding, search the
knowledge base - hands the stage's contracts to the configured model, and
records what actually happened: the inputs it was given, the structured output
it produced, every log line, every artifact, the tokens it burned and the
dollars that cost.

Three behaviours are load-bearing:

* **Live progress.** Every state change is published on the ``runs`` channel
  with ``source: "agents"``, so the Run inspector updates while the run is in
  flight (``tradinglib.api.live.LabRunBridge`` puts Strategy Lab progress on the
  same channel with ``source: "lab"``).
* **Human approval really blocks.** A ``human_approval`` stage creates an
  approval record, moves the run to ``awaiting_approval`` and waits on an
  :class:`asyncio.Event`. :meth:`PipelineRunner.approve` resumes the run;
  :meth:`PipelineRunner.reject` kills it. Because the record is persisted, a run
  that was waiting when the process stopped can be approved afterwards and
  continues from the stage it stopped at.
* **Budgets are enforced, not decorated.** Before a stage starts, its projected
  cost is checked against the per-run, daily and monthly budgets and any
  per-stage cap; a breach fails the run with ``budget_exceeded`` instead of
  spending the money.

Audit records go through :mod:`tradinglib.agents.audit`. That module's store may not
be implemented yet in a partially built tree, so the call is made through a
tolerant sink: when the audit log is available every action is recorded, and
when it is not the run's own log still carries the same lines.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Protocol

from sqlalchemy import JSON, DateTime, Float, Integer, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from tradinglib.agents.pipeline import (
    IOContract,
    Pipeline,
    PipelineRegistry,
    Stage,
    StageKind,
    get_pipeline_registry,
)
from tradinglib.agents.providers import (
    BudgetExceeded,
    Completion,
    LLMProvider,
    ProviderRegistry,
    SpendEntry,
    SpendLedger,
    ToolCall,
    ToolResult,
    ToolSpec,
    Usage,
    get_provider_registry,
    lookup_price,
    new_tool_call_id,
)
from tradinglib.agents.registry import AgentDefinition, AgentRegistry, get_agent_registry
from tradinglib.config import Settings, get_settings
from tradinglib.db import Base, ensure_columns, get_engine, get_session_factory
from tradinglib.events import EventBus, get_event_bus

logger = logging.getLogger(__name__)

#: WebSocket channel run progress is published on (``docs/SPEC.md`` section 5).
RUNS_CHANNEL: Final[str] = "runs"

#: Channel the pending-approval count is published on.
APPROVALS_CHANNEL: Final[str] = "approvals"

#: Value of the ``source`` field on every frame this module publishes.
RUN_SOURCE: Final[str] = "agents"

#: Largest tool result handed back to a model, in characters. Anything longer is
#: truncated with an explicit marker - the full payload is kept as an artifact.
MAX_TOOL_RESULT_CHARS: Final[int] = 8_000

#: Largest artifact stored inline in SQLite; bigger ones go to a file under
#: ``<data_dir>/artifacts/<run_id>/``.
MAX_INLINE_ARTIFACT_CHARS: Final[int] = 64_000

#: Safety valve on one stage's tool loop.
MAX_TOOL_ITERATIONS: Final[int] = 8


def utcnow() -> datetime:
    """Return the current UTC time as an aware datetime."""
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    """Attach UTC to a naive datetime read back from SQLite."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _iso(value: datetime | None) -> str | None:
    """Render a datetime as ISO-8601, or ``None``."""
    return value.isoformat() if value else None


def new_run_id() -> str:
    """Return a fresh run identifier (``RUN-XXXXXXXX``)."""
    return f"RUN-{uuid.uuid4().hex[:8].upper()}"


def new_approval_id() -> str:
    """Return a fresh approval identifier (``APV-XXXXXX``)."""
    return f"APV-{uuid.uuid4().hex[:6].upper()}"


# --------------------------------------------------------------------------
# states
# --------------------------------------------------------------------------
class RunStatus(StrEnum):
    """Lifecycle states of a pipeline run."""

    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    KILLED = "killed"


#: Run states that mean the run is over.
TERMINAL_RUN_STATES: Final[frozenset[RunStatus]] = frozenset(
    {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.KILLED}
)

#: Run states that mean the run occupies a worker right now. A paused run is
#: deliberately excluded: it holds no worker, which is the whole point of
#: pausing it, and it does not count against the concurrency limit.
ACTIVE_RUN_STATES: Final[frozenset[RunStatus]] = frozenset(
    {RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.AWAITING_APPROVAL}
)


class StageStatus(StrEnum):
    """Lifecycle states of one stage inside a run."""

    PENDING = "pending"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    KILLED = "killed"


#: Stage states a "retry failed stage" may restart from. A succeeded stage is
#: never silently re-executed - retrying rewinds the run to the first stage that
#: did not succeed, and everything after it is reset with it.
RETRYABLE_STAGE_STATES: Final[frozenset[StageStatus]] = frozenset(
    {StageStatus.FAILED, StageStatus.KILLED, StageStatus.PENDING}
)

#: (input, output) tokens assumed for a stage the workspace has never run.
#: Deliberately a stated *budget*, not a guess dressed up as a measurement: a
#: tool-using stage with a system prompt, a few tool round trips and a
#: structured answer. Every estimate row says which basis produced it.
ESTIMATE_FALLBACK_TOKENS: Final[tuple[int, int]] = (12_000, 1_500)

#: Wall-clock seconds assumed for a stage with no recorded history.
ESTIMATE_FALLBACK_SECONDS: Final[float] = 45.0


class ApprovalState(StrEnum):
    """States of an approval record created by a ``human_approval`` stage."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


# --------------------------------------------------------------------------
# records
# --------------------------------------------------------------------------
@dataclass(slots=True)
class Artifact:
    """A file or structured object produced by a stage."""

    id: str
    run_id: str
    stage_id: str
    name: str
    kind: str
    path: str | None = None
    data: Any = None
    size_bytes: int = 0
    created_at: datetime | None = None

    def as_dict(self, *, include_data: bool = True) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of the artifact."""
        payload: dict[str, Any] = {
            "id": self.id,
            "run_id": self.run_id,
            "stage_id": self.stage_id,
            "name": self.name,
            "kind": self.kind,
            "path": self.path,
            "size_bytes": self.size_bytes,
            "created_at": _iso(self.created_at),
            "inline": self.data is not None,
        }
        if include_data:
            payload["data"] = self.data
        return payload


@dataclass(slots=True)
class StageRun:
    """Execution record for one stage of one run."""

    stage_id: str
    name: str
    index: int = 0
    kind: StageKind = StageKind.AGENT
    agent_id: str | None = None
    provider: str | None = None
    model: str | None = None
    status: StageStatus = StageStatus.PENDING
    started_at: datetime | None = None
    finished_at: datetime | None = None
    usage: Usage = field(default_factory=Usage)
    inputs: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    logs: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[Artifact] = field(default_factory=list)
    approval_id: str | None = None
    error: str | None = None

    @property
    def duration_ms(self) -> float | None:
        """Wall-clock duration of the stage, when it has finished."""
        if self.started_at is None or self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds() * 1000.0

    def as_dict(self, *, include_payloads: bool = True) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of the stage record."""
        payload: dict[str, Any] = {
            "stage_id": self.stage_id,
            "name": self.name,
            "index": self.index,
            "kind": str(self.kind),
            "agent_id": self.agent_id,
            "provider": self.provider,
            "model": self.model,
            "status": str(self.status),
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
            "duration_ms": self.duration_ms,
            "usage": self.usage.as_dict(),
            "cost_usd": round(self.usage.cost_usd, 6),
            "tool_call_count": len(self.tool_calls),
            "artifact_count": len(self.artifacts),
            "approval_id": self.approval_id,
            "error": self.error,
        }
        if include_payloads:
            payload["inputs"] = self.inputs
            payload["outputs"] = self.outputs
            payload["tool_calls"] = self.tool_calls
            payload["logs"] = self.logs
            payload["artifacts"] = [a.as_dict(include_data=False) for a in self.artifacts]
        return payload


@dataclass(slots=True)
class Run:
    """A pipeline run with all of its stage records."""

    id: str
    pipeline_id: str
    pipeline_version: int
    name: str = ""
    objective: str | None = None
    status: RunStatus = RunStatus.QUEUED
    trigger: str = "manual"
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    stages: list[StageRun] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    inputs: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    current_stage_id: str | None = None
    resume_index: int | None = None
    paused_at: datetime | None = None
    replay_of: str | None = None
    replay_from: str | None = None
    overrides: dict[str, Any] = field(default_factory=dict)

    @property
    def cost_usd(self) -> float:
        """Total dollar cost actually incurred by this run."""
        return self.usage.cost_usd

    @property
    def duration_ms(self) -> float | None:
        """Wall-clock duration of the run, when it has finished."""
        if self.started_at is None or self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds() * 1000.0

    def progress(self) -> dict[str, Any]:
        """Return the stage-progress summary the run table renders."""
        done = sum(
            1
            for stage in self.stages
            if stage.status in {StageStatus.SUCCEEDED, StageStatus.SKIPPED}
        )
        runnable = [stage for stage in self.stages if stage.status is not StageStatus.SKIPPED]
        return {
            "completed": done,
            "total": len(self.stages),
            "runnable": len(runnable),
            "percent": round(100.0 * done / len(self.stages), 1) if self.stages else 0.0,
            "stages": [str(stage.status) for stage in self.stages],
        }

    def as_dict(self, *, include_stages: bool = True, include_payloads: bool = True) -> dict[str, Any]:
        """Return a JSON-serialisable mapping for the API layer."""
        payload: dict[str, Any] = {
            "id": self.id,
            "pipeline_id": self.pipeline_id,
            "pipeline_version": self.pipeline_version,
            "name": self.name,
            "objective": self.objective,
            "status": str(self.status),
            "trigger": self.trigger,
            "created_at": _iso(self.created_at),
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
            "duration_ms": self.duration_ms,
            "usage": self.usage.as_dict(),
            "cost_usd": round(self.usage.cost_usd, 6),
            "error": self.error,
            "current_stage_id": self.current_stage_id,
            "resume_index": self.resume_index,
            "paused_at": _iso(self.paused_at),
            "replay_of": self.replay_of,
            "replay_from": self.replay_from,
            "overrides": dict(self.overrides),
            "can_replay": self.status in TERMINAL_RUN_STATES,
            "can_pause": self.status is RunStatus.RUNNING,
            "can_resume": self.status is RunStatus.PAUSED,
            "can_retry": self.status in {RunStatus.FAILED, RunStatus.KILLED}
            or any(stage.status is StageStatus.FAILED for stage in self.stages),
            "failed_stage_id": next(
                (stage.stage_id for stage in self.stages if stage.status is StageStatus.FAILED),
                None,
            ),
            "progress": self.progress(),
        }
        if include_stages:
            payload["stages"] = [
                stage.as_dict(include_payloads=include_payloads) for stage in self.stages
            ]
        if include_payloads:
            payload["inputs"] = self.inputs
            payload["outputs"] = self.outputs
        return payload


@dataclass(slots=True)
class ApprovalRecord:
    """A blocked stage waiting on a human decision."""

    id: str
    run_id: str
    stage_id: str
    title: str
    summary: str
    payload: dict[str, Any] = field(default_factory=dict)
    state: ApprovalState = ApprovalState.PENDING
    requested_at: datetime | None = None
    decided_at: datetime | None = None
    decided_by: str | None = None
    comment: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of the approval."""
        return {
            "id": self.id,
            "run_id": self.run_id,
            "stage_id": self.stage_id,
            "title": self.title,
            "summary": self.summary,
            "payload": self.payload,
            "state": str(self.state),
            "requested_at": _iso(self.requested_at),
            "decided_at": _iso(self.decided_at),
            "decided_by": self.decided_by,
            "comment": self.comment,
        }


# --------------------------------------------------------------------------
# ORM rows
# --------------------------------------------------------------------------
class AgentRunRow(Base):
    """SQLite row for one pipeline run."""

    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    pipeline_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    pipeline_version: Mapped[int] = mapped_column(Integer(), nullable=False, default=1)
    name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    objective: Mapped[str | None] = mapped_column(Text(), default=None)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default=RunStatus.QUEUED)
    trigger: Mapped[str] = mapped_column(String(32), nullable=False, default="manual")
    inputs: Mapped[dict[str, Any]] = mapped_column(JSON(), nullable=False, default=dict)
    outputs: Mapped[dict[str, Any]] = mapped_column(JSON(), nullable=False, default=dict)
    usage: Mapped[dict[str, Any]] = mapped_column(JSON(), nullable=False, default=dict)
    #: Denormalised copy of ``usage["cost_usd"]``. ``/api/status`` sums this
    #: column directly when the runner subsystem is not mounted in the process,
    #: so the datastrip's SPEND figure stays real either way.
    cost_usd: Mapped[float] = mapped_column(Float(), nullable=False, default=0.0)
    document: Mapped[dict[str, Any]] = mapped_column(JSON(), nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(Text(), default=None)
    current_stage_id: Mapped[str | None] = mapped_column(String(64), default=None)
    #: Stage index a paused (or retried) run resumes from. ``None`` on a run
    #: that was never interrupted.
    resume_index: Mapped[int | None] = mapped_column(Integer(), default=None)
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(), default=None)
    #: The run this one replays, when it was created by "Replay from stage...".
    #: A replay never mutates the run it copies - the ledger stays append-only.
    replay_of: Mapped[str | None] = mapped_column(String(32), default=None, index=True)
    replay_from: Mapped[str | None] = mapped_column(String(64), default=None)
    #: Per-run stage overrides applied to the frozen document at creation time
    #: (AutoPipeline model overrides, stage toggles, single-stage runs).
    overrides: Mapped[dict[str, Any]] = mapped_column(JSON(), nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(), default=None)


class AgentStageRunRow(Base):
    """SQLite row for one stage of one run."""

    __tablename__ = "agent_stage_runs"

    id: Mapped[int] = mapped_column(Integer(), primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    stage_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    idx: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    kind: Mapped[str] = mapped_column(String(24), nullable=False, default=StageKind.AGENT)
    agent_id: Mapped[str | None] = mapped_column(String(64), default=None)
    provider: Mapped[str | None] = mapped_column(String(32), default=None)
    model: Mapped[str | None] = mapped_column(String(120), default=None)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default=StageStatus.PENDING)
    inputs: Mapped[dict[str, Any]] = mapped_column(JSON(), nullable=False, default=dict)
    outputs: Mapped[dict[str, Any]] = mapped_column(JSON(), nullable=False, default=dict)
    tool_calls: Mapped[list[dict[str, Any]]] = mapped_column(JSON(), nullable=False, default=list)
    usage: Mapped[dict[str, Any]] = mapped_column(JSON(), nullable=False, default=dict)
    cost_usd: Mapped[float] = mapped_column(Float(), nullable=False, default=0.0)
    approval_id: Mapped[str | None] = mapped_column(String(32), default=None)
    error: Mapped[str | None] = mapped_column(Text(), default=None)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(), default=None)


class AgentRunLogRow(Base):
    """SQLite row for one log line of a run."""

    __tablename__ = "agent_run_logs"

    id: Mapped[int] = mapped_column(Integer(), primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    stage_id: Mapped[str | None] = mapped_column(String(64), default=None)
    ts: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    level: Mapped[str] = mapped_column(String(16), nullable=False, default="info")
    message: Mapped[str] = mapped_column(Text(), nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON(), nullable=False, default=dict)


class AgentArtifactRow(Base):
    """SQLite row for one artifact produced by a stage."""

    __tablename__ = "agent_artifacts"

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    stage_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="json")
    path: Mapped[str | None] = mapped_column(Text(), default=None)
    data: Mapped[Any] = mapped_column(JSON(), default=None)
    size_bytes: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)


class AgentApprovalRow(Base):
    """SQLite row for one human-approval record.

    Named ``agent_run_approvals`` rather than ``agent_approvals`` so that
    :mod:`tradinglib.agents.approvals`, which owns the standalone approval inbox,
    keeps its own table name free.
    """

    __tablename__ = "agent_run_approvals"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    stage_id: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    summary: Mapped[str] = mapped_column(Text(), nullable=False, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON(), nullable=False, default=dict)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default=ApprovalState.PENDING)
    resume_index: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    decided_by: Mapped[str | None] = mapped_column(String(64), default=None)
    comment: Mapped[str | None] = mapped_column(Text(), default=None)
    requested_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(), default=None)


_OWNED_TABLES = (
    AgentRunRow.__table__,
    AgentStageRunRow.__table__,
    AgentRunLogRow.__table__,
    AgentArtifactRow.__table__,
    AgentApprovalRow.__table__,
)


def ensure_schema() -> None:
    """Create the run tables if they do not exist yet, and migrate new columns."""
    Base.metadata.create_all(get_engine(), tables=list(_OWNED_TABLES))
    for table in _OWNED_TABLES:
        ensure_columns(table)


# --------------------------------------------------------------------------
# audit sink
# --------------------------------------------------------------------------
class AuditSink(Protocol):
    """The slice of :class:`tradinglib.agents.audit.AuditLog` the runner uses."""

    def record(
        self,
        actor: str,
        action: str,
        *,
        target: str | None = ...,
        run_id: str | None = ...,
        stage_id: str | None = ...,
        **detail: Any,
    ) -> Any:
        """Append one audit record."""
        ...


def default_audit_sink() -> AuditSink | None:
    """Return the process audit log, or ``None`` when it is not usable yet.

    The audit store is owned by :mod:`tradinglib.agents.audit`. Until that module's
    persistence exists, this returns an instance anyway - the runner calls it
    defensively - and falls back to run-local logging when the call raises
    ``NotImplementedError``.
    """
    try:
        from tradinglib.agents.audit import AuditLog
    except Exception:  # noqa: BLE001 - a missing audit module is not fatal
        logger.debug("audit log unavailable", exc_info=True)
        return None
    try:
        return AuditLog()
    except Exception:  # noqa: BLE001
        logger.debug("audit log could not be constructed", exc_info=True)
        return None


# --------------------------------------------------------------------------
# tools
# --------------------------------------------------------------------------
#: Every tool an agent can be granted. The agent registry validates configured
#: tool names against this list.
TOOL_NAMES: Final[tuple[str, ...]] = (
    "resolve_universe",
    "run_screen",
    "get_company",
    "get_series",
    "run_backtest",
    "save_finding",
    "search_knowledge",
)

_COMPANY_SECTIONS: Final[tuple[str, ...]] = (
    "overview",
    "financials",
    "estimates",
    "valuation",
    "news",
    "altdata",
    "filings",
    "price",
    "freshness",
)

TOOL_SPECS: Final[dict[str, ToolSpec]] = {
    "resolve_universe": ToolSpec(
        name="resolve_universe",
        description=(
            "Resolve a universe or watchlist reference to its concrete symbol list. "
            "Accepts 'universe:<id>', 'watchlist:<id>', a bare id, or a comma separated "
            "symbol list. Optionally point-in-time as of a date."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "universe/watchlist reference"},
                "as_of": {"type": "string", "description": "ISO date for point-in-time membership"},
            },
            "required": [],
        },
    ),
    "run_screen": ToolSpec(
        name="run_screen",
        description=(
            "Run a saved screen over its universe and return the funnel: how many names "
            "each rule removed, which names survived, and the coverage of the data used."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "screen_id": {"type": "string", "description": "id of the saved screen"},
                "as_of": {"type": "string", "description": "ISO date for point-in-time run"},
                "limit": {"type": "integer", "description": "max survivors to return"},
            },
            "required": ["screen_id"],
        },
    ),
    "get_company": ToolSpec(
        name="get_company",
        description=(
            "Read one section of a company's research page: overview (derived metrics), "
            "financials, estimates, valuation, news, altdata, filings, price or freshness."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "section": {"type": "string", "enum": list(_COMPANY_SECTIONS)},
                "range": {"type": "string", "description": "price range key, e.g. 1y"},
                "limit": {"type": "integer"},
            },
            "required": ["symbol"],
        },
    ),
    "get_series": ToolSpec(
        name="get_series",
        description=(
            "Read a stored time series: daily bars for a symbol, or a named series from a "
            "namespace (for example fred/DGS10). Returns summary statistics and the tail "
            "of the series."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "namespace": {"type": "string"},
                "name": {"type": "string"},
                "start": {"type": "string", "description": "ISO date"},
                "end": {"type": "string", "description": "ISO date"},
                "points": {"type": "integer", "description": "how many trailing points to return"},
            },
            "required": [],
        },
    ),
    "run_backtest": ToolSpec(
        name="run_backtest",
        description=(
            "Run a backtest and return its real statistics. The config takes a name, "
            "either symbols or universe_id, a signal specification, a benchmark, a "
            "rebalance frequency and an optional date window."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "config": {
                    "type": "object",
                    "description": "BacktestConfig payload (name, universe_id/symbols, signal, ...)",
                }
            },
            "required": ["config"],
        },
    ),
    "save_finding": ToolSpec(
        name="save_finding",
        description=(
            "Record a research conclusion with its evidence so it appears in the Findings "
            "tab and the research inbox."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "summary": {"type": "string"},
                "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                "symbols": {"type": "array", "items": {"type": "string"}},
                "tags": {"type": "array", "items": {"type": "string"}},
                "evidence": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["title", "summary"],
        },
    ),
    "search_knowledge": ToolSpec(
        name="search_knowledge",
        description="Search the knowledge base for notes and documents relevant to a query.",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
        },
    ),
}


def _parse_date(value: Any) -> date | None:
    """Coerce an ISO string to a date, returning ``None`` when absent."""
    if value in (None, ""):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    return date.fromisoformat(str(value)[:10])


class LibraryToolset:
    """Binds the agent tool names to the real TradingLib engines.

    Every engine is optional at construction and resolved lazily, so a runner
    built in a bare process still works for the tools it can serve and reports
    honestly on the ones it cannot. Nothing here fabricates a result: when an
    engine is missing or a lookup finds nothing, the tool says so.
    """

    def __init__(
        self,
        *,
        universes: Any | None = None,
        funnel: Any | None = None,
        screens: Any | None = None,
        research: Any | None = None,
        store: Any | None = None,
        backtests: Any | None = None,
        findings: Any | None = None,
        knowledge: Any | None = None,
        settings: Settings | None = None,
    ) -> None:
        """Create the toolset, binding whichever engines were supplied."""
        self._universes = universes
        self._funnel = funnel
        self._screens = screens
        self._research = research
        self._store = store
        self._backtests = backtests
        self._findings = findings
        self._knowledge = knowledge
        self._settings = settings

    # --- lazily resolved engines -----------------------------------------
    @property
    def store(self) -> Any:
        """The Parquet + DuckDB time-series store."""
        if self._store is None:
            from tradinglib.data.store import get_store

            self._store = get_store()
        return self._store

    @property
    def universes(self) -> Any:
        """The universe registry."""
        if self._universes is None:
            from tradinglib.data.universe import get_universe_registry

            self._universes = get_universe_registry()
        return self._universes

    @property
    def screens(self) -> Any:
        """The saved-screen registry."""
        if self._screens is None:
            from tradinglib.research.screen.funnel import ScreenRegistry

            self._screens = ScreenRegistry()
        return self._screens

    @property
    def funnel(self) -> Any:
        """The screener's funnel evaluator."""
        if self._funnel is None:
            from tradinglib.research.screen.funnel import FunnelEvaluator
            from tradinglib.research.screen.rules import MetricBuilder

            self._funnel = FunnelEvaluator(
                registry=self.screens,
                universes=self.universes,
                builder=MetricBuilder(store=self.store),
            )
        return self._funnel

    @property
    def research(self) -> Any:
        """The Stock Research engine."""
        if self._research is None:
            from tradinglib.research.company import CompanyResearch

            self._research = CompanyResearch(store=self.store, universes=self.universes)
        return self._research

    @property
    def backtests(self) -> Any:
        """The Strategy Lab backtest engine."""
        if self._backtests is None:
            from tradinglib.research.backtest import get_backtest_engine

            self._backtests = get_backtest_engine()
        return self._backtests

    @property
    def findings(self) -> Any:
        """The findings store."""
        if self._findings is None:
            from tradinglib.agents.findings import FindingsStore

            self._findings = FindingsStore()
        return self._findings

    @property
    def knowledge(self) -> Any:
        """The knowledge base."""
        if self._knowledge is None:
            from tradinglib.agents.knowledge import KnowledgeBase

            self._knowledge = KnowledgeBase()
        return self._knowledge

    # --- specs ------------------------------------------------------------
    @staticmethod
    def specs(names: Sequence[str]) -> list[ToolSpec]:
        """Return the tool specs for ``names``, ignoring unknown entries."""
        return [TOOL_SPECS[name] for name in names if name in TOOL_SPECS]

    # --- workspace inventory ---------------------------------------------
    def universe_refs(self) -> list[str]:
        """Return every universe and watchlist reference that resolves today."""
        refs: list[str] = []
        try:
            refs.extend(
                f"universe:{u.id}"
                for u in self.universes.list_universes(include_members=False)
            )
        except Exception:  # noqa: BLE001 - an unreachable registry is not fatal here
            logger.warning("listing universes for the stage prompt failed", exc_info=True)
        try:
            refs.extend(f"watchlist:{w.id}" for w in self.universes.watchlists.list_watchlists())
        except Exception:  # noqa: BLE001 - same
            logger.warning("listing watchlists for the stage prompt failed", exc_info=True)
        return refs

    def screen_ids(self) -> list[str]:
        """Return the id of every saved screen."""
        try:
            return [screen.id for screen in self.screens.list_screens()]
        except Exception:  # noqa: BLE001 - an unreachable registry is not fatal here
            logger.warning("listing screens for the stage prompt failed", exc_info=True)
            return []

    def workspace(self) -> dict[str, Any]:
        """Return the identifiers an agent is allowed to name.

        The single biggest way a stage used to fail was inventing plausible
        identifiers - ``universe:quality-equities``, a screen called after the
        pipeline - and burning its whole turn budget on tool errors. The stage
        prompt therefore states what actually exists, and the tools quote the
        same list back when a reference misses.
        """
        active = None
        try:
            active = self.universes.active_universe_id()
        except Exception:  # noqa: BLE001 - the prompt survives without it
            logger.debug("reading the active universe failed", exc_info=True)
        return {
            "universes": self.universe_refs(),
            "screens": self.screen_ids(),
            "active_universe": f"universe:{active}" if active else None,
        }

    # --- execution --------------------------------------------------------
    async def execute(
        self, name: str, arguments: Mapping[str, Any], *, scope: Mapping[str, Any] | None = None
    ) -> ToolResult:
        """Run one tool and return its result.

        A tool that raises is reported back to the model as an error result
        rather than crashing the stage: the model can then say what it could not
        do, which is more useful than a dead run.
        """
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return ToolResult(f"unknown tool {name!r}", is_error=True)
        try:
            data = await handler(dict(arguments or {}), dict(scope or {}))
        except NotImplementedError as exc:
            return ToolResult(
                f"tool {name!r} is not available in this build: {exc or 'not implemented'}",
                is_error=True,
            )
        except Exception as exc:  # noqa: BLE001 - a tool failure is data for the model
            logger.info("tool %s failed", name, exc_info=True)
            return ToolResult(f"{type(exc).__name__}: {exc}", is_error=True, data={"error": str(exc)})
        text = json.dumps(data, default=str, sort_keys=False)
        if len(text) > MAX_TOOL_RESULT_CHARS:
            text = text[:MAX_TOOL_RESULT_CHARS] + f"... [truncated at {MAX_TOOL_RESULT_CHARS} chars]"
        return ToolResult(text, data=data)

    def _require_autonomy(self, capability: str, tool: str, scope: Mapping[str, Any]) -> None:
        """Refuse a tool the workspace autonomy policy says to ask about first.

        The refusal is written to the audit log before it is raised, which is
        what makes "blocked attempts are always logged and surfaced" true.

        Raises:
            GuardrailBlocked: when the policy sets this capability to ``ask_first``.
        """
        from tradinglib.agents.approvals import GuardrailBlocked, get_policy

        policy = get_policy()
        if policy.allows(capability):
            return
        detail = (
            f"{tool} is blocked: the workspace autonomy policy sets "
            f"{capability.replace('_', ' ')} to 'ask first'. A person must run this."
        )
        sink = default_audit_sink()
        if sink is not None:
            with contextlib.suppress(Exception):
                sink.record(
                    "policy engine",
                    "guardrail block",
                    run_id=scope.get("run_id"),
                    stage_id=scope.get("stage_id"),
                    capability=capability,
                    tool=tool,
                    result="blocked",
                    detail=detail,
                )
        raise GuardrailBlocked(capability, detail)

    async def _tool_resolve_universe(
        self, args: dict[str, Any], scope: dict[str, Any]
    ) -> dict[str, Any]:
        """Resolve a universe reference to symbols."""
        ref = str(args.get("ref") or scope.get("universe") or scope.get("universe_id") or "").strip()
        if not ref:
            # No reference and no scope: fall back to the workspace's active
            # universe rather than failing, which is what a human would mean.
            ref = str(self.workspace().get("active_universe") or "")
            if not ref:
                raise ValueError(
                    "no universe reference was given, the stage scope names none, and no "
                    "universe is active. Available references: "
                    + (", ".join(self.universe_refs()) or "none")
                )
        as_of = _parse_date(args.get("as_of"))
        try:
            symbols = await asyncio.to_thread(self.universes.resolve, ref, as_of=as_of)
        except KeyError as exc:
            # Quote the real list so the model's next turn can be right rather
            # than another guess.
            raise ValueError(
                f"{ref!r} is not a universe or watchlist in this workspace. "
                "Use one of: " + (", ".join(self.universe_refs()) or "none configured")
            ) from exc
        allowed = {str(s).upper() for s in scope.get("symbols") or []}
        if allowed:
            symbols = [symbol for symbol in symbols if symbol.upper() in allowed]
        return {
            "ref": ref,
            "as_of": as_of.isoformat() if as_of else None,
            "count": len(symbols),
            "symbols": symbols,
            "scope_restricted": bool(allowed),
        }

    async def _tool_run_screen(self, args: dict[str, Any], scope: dict[str, Any]) -> dict[str, Any]:
        """Run a saved screen and return its funnel."""
        screen_id = str(args.get("screen_id") or scope.get("screen_id") or "").strip()
        available = self.screen_ids()
        if not screen_id:
            raise ValueError(
                "run_screen needs a screen_id. Saved screens: "
                + (", ".join(available) or "none are saved in this workspace")
            )
        if available and screen_id not in available:
            raise ValueError(
                f"{screen_id!r} is not a saved screen. Use one of: " + ", ".join(available)
            )
        as_of = _parse_date(args.get("as_of"))
        run = await self.funnel.run_screen_id(screen_id, as_of=as_of)
        payload = run.as_dict(include_survivors=True)
        limit = int(args.get("limit") or 25)
        survivors = payload.get("survivors") or []
        payload["survivors"] = survivors[:limit]
        payload["survivors_returned"] = len(payload["survivors"])
        return payload

    async def _tool_get_company(self, args: dict[str, Any], scope: dict[str, Any]) -> dict[str, Any]:
        """Read one section of a company's research payload."""
        symbol = str(args.get("symbol") or "").strip().upper()
        if not symbol:
            raise ValueError("get_company needs a symbol")
        allowed = {str(s).upper() for s in scope.get("symbols") or []}
        if allowed and symbol not in allowed:
            raise PermissionError(f"{symbol} is outside this stage's scope ({sorted(allowed)})")
        section = str(args.get("section") or "overview").strip().lower()
        if section not in _COMPANY_SECTIONS:
            raise ValueError(f"unknown section {section!r}; use one of {', '.join(_COMPANY_SECTIONS)}")
        research = self.research
        if section == "overview":
            return await research.company(symbol)
        if section == "price":
            return await research.price(symbol, range=str(args.get("range") or "1y"))
        if section == "news":
            return await research.news(symbol, limit=int(args.get("limit") or 15))
        if section == "filings":
            return await research.filings(symbol, limit=int(args.get("limit") or 20))
        if section == "valuation":
            return await research.valuation(symbol)
        return await getattr(research, section)(symbol)

    async def _tool_get_series(self, args: dict[str, Any], scope: dict[str, Any]) -> dict[str, Any]:
        """Read stored bars or a stored named series."""
        points = max(1, min(int(args.get("points") or 30), 500))
        namespace = str(args.get("namespace") or "").strip()
        name = str(args.get("name") or "").strip()
        start = _parse_date(args.get("start"))
        end = _parse_date(args.get("end"))
        if namespace and name:
            series = await asyncio.to_thread(
                self.store.read_series, namespace, name, start=start, end=end
            )
            values = [(ts.isoformat(), value) for ts, value in series.points]
            return {
                "kind": "series",
                "namespace": namespace,
                "name": name,
                "count": len(values),
                "unit": series.unit,
                "source": series.source,
                "tail": values[-points:],
            }
        symbol = str(args.get("symbol") or "").strip().upper()
        if not symbol:
            raise ValueError("get_series needs either a symbol or a namespace and name")
        allowed = {str(s).upper() for s in scope.get("symbols") or []}
        if allowed and symbol not in allowed:
            raise PermissionError(f"{symbol} is outside this stage's scope ({sorted(allowed)})")
        start_dt = datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc) if start else None
        end_dt = datetime.combine(end, datetime.max.time(), tzinfo=timezone.utc) if end else None
        frame = await asyncio.to_thread(
            self.store.read_bars, symbol, start=start_dt, end=end_dt
        )
        if frame.empty:
            return {
                "kind": "bars",
                "symbol": symbol,
                "count": 0,
                "detail": "no bars are stored for this symbol yet",
                "tail": [],
            }
        tail = frame.tail(points)
        return {
            "kind": "bars",
            "symbol": symbol,
            "count": int(len(frame)),
            "first": frame.index[0].isoformat(),
            "last": frame.index[-1].isoformat(),
            "last_close": float(frame["close"].iloc[-1]),
            "tail": [
                {
                    "ts": index.isoformat(),
                    "open": float(row.open),
                    "high": float(row.high),
                    "low": float(row.low),
                    "close": float(row.close),
                    "volume": float(row.volume),
                }
                for index, row in tail.iterrows()
            ],
        }

    async def _tool_run_backtest(self, args: dict[str, Any], scope: dict[str, Any]) -> dict[str, Any]:
        """Run a backtest through the real engine and return its statistics."""
        from tradinglib.research.backtest import BacktestConfig

        self._require_autonomy("sandbox_experiments", "run_backtest", scope)
        payload = dict(args.get("config") or {})
        if not payload:
            raise ValueError("run_backtest needs a config object")
        payload.setdefault("name", "agent backtest")
        if not payload.get("symbols") and not payload.get("universe_id"):
            universe = scope.get("universe") or scope.get("universe_id")
            if universe:
                payload["universe_id"] = universe
        allowed = [str(s).upper() for s in scope.get("symbols") or []]
        if allowed and payload.get("symbols"):
            payload["symbols"] = [
                symbol for symbol in payload["symbols"] if str(symbol).upper() in allowed
            ]
        config = BacktestConfig.from_dict(payload)
        record, result = await self.backtests.run_with_result(config, source="agent")
        return {
            "backtest_id": record.id,
            "name": record.name,
            "status": str(record.status),
            "symbols": record.symbols,
            "stats": record.stats,
            "out_of_sample": record.out_of_sample,
            "warnings": record.warnings,
            "curve_points": int(len(result.equity_curve)),
            "periods_per_year": result.periods_per_year,
        }

    async def _tool_save_finding(self, args: dict[str, Any], scope: dict[str, Any]) -> dict[str, Any]:
        """Record a finding through the findings store."""
        self._require_autonomy("research_generation", "save_finding", scope)
        title = str(args.get("title") or "").strip()
        summary = str(args.get("summary") or "").strip()
        if not title or not summary:
            raise ValueError("save_finding needs a title and a summary")
        extra: dict[str, Any] = {}
        for key in ("confidence", "symbols", "tags", "evidence"):
            if args.get(key) is not None:
                extra[key] = args[key]
        extra.setdefault("run_id", scope.get("run_id"))
        extra.setdefault("stage_id", scope.get("stage_id"))
        # "Feature publish -> human review": an agent's claim always lands in
        # the triage queue. Nothing an agent writes is promoted by an agent.
        extra["status"] = "new"
        finding = await asyncio.to_thread(
            lambda: self.findings.create(title, summary, **extra)
        )
        identifier = getattr(finding, "id", None)
        return {
            "saved": True,
            "finding_id": identifier,
            "title": title,
            "status": "new",
            "note": "recorded for human triage; agents cannot promote their own findings",
        }

    async def _tool_search_knowledge(
        self, args: dict[str, Any], scope: dict[str, Any]
    ) -> dict[str, Any]:
        """Search the knowledge base."""
        query = str(args.get("query") or "").strip()
        if not query:
            raise ValueError("search_knowledge needs a query")
        limit = max(1, min(int(args.get("limit") or 10), 50))
        entries = await asyncio.to_thread(lambda: self.knowledge.search(query, limit=limit))
        return {
            "query": query,
            "count": len(entries),
            "entries": [
                {
                    "id": getattr(entry, "id", None),
                    "title": getattr(entry, "title", None),
                    "kind": getattr(entry, "kind", None),
                    "body": (getattr(entry, "body", "") or "")[:1000],
                }
                for entry in entries
            ],
        }


# --------------------------------------------------------------------------
# prompt construction
# --------------------------------------------------------------------------
_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def extract_structured_output(text: str) -> dict[str, Any] | None:
    """Return the JSON object a stage answered with, if it produced one.

    Stages are asked to end with a fenced ``json`` block; the last such block
    wins. A bare JSON document is accepted too. Anything else returns ``None``,
    which the contract check then reports honestly.
    """
    if not text:
        return None
    matches = _JSON_BLOCK_RE.findall(text)
    for candidate in reversed(matches):
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            parsed = json.loads(stripped)
        except ValueError:
            return None
        if isinstance(parsed, dict):
            return parsed
    return None


def build_stage_system_prompt(
    agent: AgentDefinition | None,
    stage: Stage,
    *,
    tool_names: Sequence[str],
    workspace: Mapping[str, Any] | None = None,
) -> str:
    """Compose the system prompt for one stage from the agent and its contracts.

    ``workspace`` is the inventory from :meth:`LibraryToolset.workspace` - the
    universe, watchlist and screen identifiers that actually exist. Stating
    them is what stops a stage inventing ``universe:quality-equities`` and
    spending its whole turn budget being told the reference does not resolve.
    """
    parts: list[str] = []
    if agent is not None:
        parts.append(agent.system_prompt.strip())
        if agent.role:
            parts.append(f"Your role: {agent.role}.")
    parts.append(f"You are running the '{stage.name}' stage of an TradingLib research pipeline.")
    if stage.objective:
        parts.append(f"Stage objective: {stage.objective.strip()}")
    if stage.inputs.declared:
        parts.append(f"Input contract: {_contract_text(stage.inputs)}")
    if stage.outputs.declared:
        parts.append(f"Output contract: {_contract_text(stage.outputs)}")
    if stage.handoff:
        parts.append(f"Handoff condition: {stage.handoff}")
    if stage.pass_condition:
        parts.append(f"Pass condition: {stage.pass_condition}")
    if stage.fail_condition:
        parts.append(f"Fail condition: {stage.fail_condition}")
    if stage.stop_condition:
        parts.append(f"Stop condition: {stage.stop_condition}")
    if stage.scope:
        parts.append(f"Scope you must stay inside: {json.dumps(stage.scope, sort_keys=True)}")
    if tool_names:
        parts.append(
            "Tools available to you: "
            + ", ".join(tool_names)
            + ". Use them for every fact you report - never state a number a tool did not return."
        )
        inventory = _workspace_text(workspace)
        if inventory:
            parts.append(inventory)
    else:
        parts.append("You have no tools in this stage; reason only over the input you were given.")
    required = stage.outputs.required
    if required:
        parts.append(
            "Finish your answer with a fenced ```json block containing exactly these keys: "
            + ", ".join(required)
            + "."
        )
    else:
        parts.append(
            "Finish your answer with a fenced ```json block summarising your result as an object."
        )
    return "\n\n".join(part for part in parts if part)


def _workspace_text(workspace: Mapping[str, Any] | None) -> str:
    """Render the workspace inventory as prompt text, or ``""`` when empty."""
    if not workspace:
        return ""
    lines: list[str] = []
    universes = list(workspace.get("universes") or [])
    screens = list(workspace.get("screens") or [])
    active = workspace.get("active_universe")
    if universes:
        lines.append("resolve_universe accepts exactly these refs: " + ", ".join(universes))
    if active:
        lines.append(f"The active scope is {active}; use it unless the run says otherwise.")
    if screens:
        lines.append("run_screen accepts exactly these screen ids: " + ", ".join(screens))
    else:
        lines.append("No screen is saved in this workspace, so run_screen has nothing to run.")
    if not lines:
        return ""
    return (
        "This workspace contains the following identifiers. They are the only valid ones - "
        "never invent an id, and never guess one from a name.\n" + "\n".join(lines)
    )


def _contract_text(contract: IOContract) -> str:
    """Render a contract as one line of prompt text."""
    bits: list[str] = []
    if contract.text:
        bits.append(contract.text)
    if contract.required:
        bits.append("required keys: " + ", ".join(contract.required))
    if contract.schema:
        bits.append("schema: " + json.dumps(contract.schema, sort_keys=True))
    return "; ".join(bits)


def build_stage_user_prompt(
    run: Run, stage: Stage, upstream: Mapping[str, Any], run_inputs: Mapping[str, Any]
) -> str:
    """Compose the user message for one stage: the run brief and what came before."""
    parts = [f"Run {run.id} of pipeline {run.pipeline_id} (version {run.pipeline_version})."]
    if run.objective:
        parts.append(f"Run objective: {run.objective}")
    if run_inputs:
        parts.append("Run inputs:\n" + json.dumps(run_inputs, default=str, indent=2, sort_keys=True))
    if upstream:
        parts.append(
            "Output of the previous stages:\n"
            + json.dumps(upstream, default=str, indent=2, sort_keys=True)[:20_000]
        )
    else:
        parts.append("No upstream stage produced output; you are the first stage.")
    parts.append(f"Do the '{stage.name}' work now.")
    return "\n\n".join(parts)


#: Stage fields a caller may override for one run without editing the pipeline.
#: Deliberately narrow: a per-run override may change *who answers* and *whether
#: a stage participates*, never the stage's contract, tools or objective - those
#: are what make two runs of the same pipeline comparable.
RUN_STAGE_OVERRIDES: Final[frozenset[str]] = frozenset(
    {"model", "provider", "include_in_run"}
)


def apply_stage_overrides(
    pipeline: Pipeline, overrides: Mapping[str, Mapping[str, Any]] | None
) -> dict[str, Any]:
    """Apply per-run stage overrides to ``pipeline`` in place.

    The pipeline passed in is the *frozen copy* a run is created from, so the
    saved document is never touched.

    Args:
        pipeline: the copy this run will execute.
        overrides: stage id -> field mapping, restricted to
            :data:`RUN_STAGE_OVERRIDES`.

    Returns:
        What was actually applied, stage by stage, so the run can record it and
        the inspector can show "this run did not use the saved configuration".

    Raises:
        KeyError: when a stage id is not in the pipeline.
        ValueError: when a field may not be overridden per run, or the override
            would switch off the mandatory human-approval boundary.
    """
    if not overrides:
        return {}
    applied: dict[str, Any] = {}
    for stage_id, changes in overrides.items():
        stage = pipeline.stage(str(stage_id))
        unknown = set(changes) - RUN_STAGE_OVERRIDES
        if unknown:
            raise ValueError(
                f"stage {stage_id!r}: {', '.join(sorted(unknown))} cannot be overridden per run; "
                f"allowed: {', '.join(sorted(RUN_STAGE_OVERRIDES))}"
            )
        record: dict[str, Any] = {}
        for field_name, value in changes.items():
            if field_name == "include_in_run":
                wanted = bool(value)
                if stage.locked and not wanted:
                    raise ValueError(
                        f"stage {stage_id!r} is the human-approval boundary and cannot be "
                        "switched off for a run"
                    )
                if stage.include_in_run != wanted:
                    record["include_in_run"] = wanted
                    stage.include_in_run = wanted
                continue
            if stage.locked and value:
                raise ValueError(
                    f"stage {stage_id!r} is the human-approval boundary; it never calls a model"
                )
            cleaned = None if value in (None, "") else str(value)
            if getattr(stage, field_name) != cleaned:
                record[field_name] = cleaned
                setattr(stage, field_name, cleaned)
        if record:
            applied[str(stage_id)] = record
    return applied


# --------------------------------------------------------------------------
# runner
# --------------------------------------------------------------------------
class PipelineRunner:
    """Executes pipelines stage by stage against real LLM providers."""

    def __init__(
        self,
        bus: EventBus | None = None,
        *,
        pipelines: PipelineRegistry | None = None,
        agents: AgentRegistry | None = None,
        providers: ProviderRegistry | None = None,
        toolset: LibraryToolset | None = None,
        session_factory: sessionmaker[Session] | None = None,
        settings: Settings | None = None,
        audit: AuditSink | None = None,
        ledger: SpendLedger | None = None,
    ) -> None:
        """Create the runner bound to an event bus and the registries it drives."""
        self._bus = bus
        self._pipelines = pipelines
        self._agents = agents
        self._providers = providers
        self._toolset = toolset or LibraryToolset(settings=settings)
        self._session_factory = session_factory
        self._settings = settings
        self._audit: AuditSink | None = audit if audit is not None else default_audit_sink()
        ensure_schema()
        self.ledger = ledger or SpendLedger(loader=self._load_spend_entries)
        #: run_id -> the task executing it.
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        #: approval_id -> event the waiting stage is blocked on.
        self._waiters: dict[str, asyncio.Event] = {}
        #: approval_id -> the decision that woke the waiter.
        self._decisions: dict[str, ApprovalState] = {}
        #: run_id -> agent id currently executing, for the live active count.
        self._active: dict[str, str | None] = {}
        #: run ids asked to stop at the next stage boundary.
        self._pause_requests: set[str] = set()
        #: run_id -> stage index a paused run must resume from.
        self._resume_points: dict[str, int] = {}
        if self._agents is not None:
            self._agents.bind_activity(self.active_agent_ids)

    # --- plumbing ---------------------------------------------------------
    @property
    def bus(self) -> EventBus:
        """The event bus run progress is published on."""
        if self._bus is None:
            self._bus = get_event_bus()
        return self._bus

    @property
    def pipelines(self) -> PipelineRegistry:
        """The pipeline registry runs are read from."""
        if self._pipelines is None:
            self._pipelines = get_pipeline_registry()
        return self._pipelines

    @property
    def agents(self) -> AgentRegistry:
        """The agent registry stage assignments are resolved through."""
        if self._agents is None:
            self._agents = get_agent_registry()
            self._agents.bind_activity(self.active_agent_ids)
        return self._agents

    @property
    def providers(self) -> ProviderRegistry:
        """The provider registry models are resolved through."""
        if self._providers is None:
            self._providers = get_provider_registry()
        return self._providers

    @property
    def toolset(self) -> LibraryToolset:
        """The library toolset agents call."""
        return self._toolset

    @property
    def settings(self) -> Settings:
        """Resolved settings (data directory, budgets)."""
        return self._settings or get_settings()

    @contextmanager
    def _session(self) -> Iterator[Session]:
        """Transactional session scope bound to this runner."""
        factory = self._session_factory or get_session_factory()
        session = factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _load_spend_entries(self) -> list[SpendEntry]:
        """Seed the spend ledger from persisted stage runs."""
        statement = select(AgentStageRunRow).where(AgentStageRunRow.cost_usd > 0)
        with self._session() as session:
            rows = list(session.scalars(statement))
        return [
            SpendEntry(
                ts=_aware(row.finished_at or row.started_at) or utcnow(),
                run_id=row.run_id,
                stage_id=row.stage_id,
                provider=row.provider or "unknown",
                model=row.model or "unknown",
                usage=Usage.from_dict(row.usage),
            )
            for row in rows
        ]

    # --- publishing -------------------------------------------------------
    async def _publish(self, payload: dict[str, Any], *, channel: str = RUNS_CHANNEL) -> None:
        """Publish one live frame, never letting a subscriber break the run."""
        frame = {"source": RUN_SOURCE, "ts": utcnow().isoformat(), **payload}
        try:
            await self.bus.publish(channel, frame)
        except Exception:  # noqa: BLE001 - progress reporting is not the run
            logger.warning("publishing run progress failed", exc_info=True)

    async def _log(
        self,
        run_id: str,
        stage_id: str | None,
        message: str,
        *,
        level: str = "info",
        **detail: Any,
    ) -> dict[str, Any]:
        """Persist one log line and stream it on the ``runs`` channel."""
        entry = {
            "ts": utcnow().isoformat(),
            "level": level,
            "message": message,
            "stage_id": stage_id,
            "detail": detail,
        }
        with self._session() as session:
            session.add(
                AgentRunLogRow(
                    run_id=run_id,
                    stage_id=stage_id,
                    ts=utcnow(),
                    level=level,
                    message=message,
                    detail=detail,
                )
            )
        await self._publish({"kind": "log", "run_id": run_id, **entry})
        return entry

    def _record_audit(
        self,
        actor: str,
        action: str,
        *,
        target: str | None = None,
        run_id: str | None = None,
        stage_id: str | None = None,
        **detail: Any,
    ) -> None:
        """Append an audit record, tolerating an audit store that is not built."""
        if self._audit is None:
            return
        try:
            self._audit.record(
                actor, action, target=target, run_id=run_id, stage_id=stage_id, **detail
            )
        except NotImplementedError:
            logger.debug("audit log is not implemented yet; %s/%s only in the run log", actor, action)
        except Exception:  # noqa: BLE001 - auditing must never break a run
            logger.warning("audit record failed for %s/%s", actor, action, exc_info=True)

    # --- run construction -------------------------------------------------
    async def start(
        self,
        pipeline_id: str,
        *,
        trigger: str = "manual",
        inputs: Mapping[str, Any] | None = None,
        version: int | None = None,
        objective: str | None = None,
        name: str | None = None,
        stage_overrides: Mapping[str, Mapping[str, Any]] | None = None,
        wait: bool = False,
    ) -> Run:
        """Queue and begin a run; returns as soon as the run record exists.

        Args:
            pipeline_id: pipeline to execute.
            trigger: what started it (``manual``, ``schedule``, ``api``...).
            inputs: the run brief handed to the first stage.
            version: run a frozen version instead of the working document.
            objective: overrides the pipeline objective for this run.
            name: human label for the run.
            stage_overrides: per-run changes to individual stages, keyed by
                stage id - ``model``, ``provider`` and ``include_in_run``. They
                are applied to this run's frozen document only, so the saved
                pipeline is untouched and the run records what it actually ran.
            wait: execute inline and return the finished run (tests, CLI).

        Raises:
            KeyError: when the pipeline is unknown.
            ValueError: when the pipeline does not validate.
            GuardrailBlocked: when the workspace concurrency ceiling is reached.
        """
        self.check_concurrency()
        pipeline = self.pipelines.get(pipeline_id, version=version)
        applied = apply_stage_overrides(pipeline, stage_overrides)
        known_agents = {definition.id for definition in self.agents.list()}
        problems = self.pipelines.validate(pipeline, agents=known_agents)
        if problems:
            raise ValueError("pipeline does not validate: " + "; ".join(problems))
        run = self._create_run(
            pipeline,
            trigger=trigger,
            inputs=dict(inputs or {}),
            objective=objective or pipeline.objective,
            name=name or pipeline.name,
            overrides=applied,
        )
        await self._log(run.id, None, f"Run queued for pipeline {pipeline.name}", trigger=trigger)
        await self._publish(
            {
                "kind": "run",
                "phase": "queued",
                "run_id": run.id,
                "pipeline_id": pipeline.id,
                "pipeline_version": run.pipeline_version,
                "status": str(run.status),
                "progress": run.progress(),
            }
        )
        self._record_audit(
            "runner",
            "run queued",
            target=pipeline.id,
            run_id=run.id,
            trigger=trigger,
            version=run.pipeline_version,
        )
        if wait:
            return await self.execute(run.id)
        self._tasks[run.id] = asyncio.create_task(
            self._guarded_execute(run.id), name=f"tradinglib-run-{run.id}"
        )
        return self.get(run.id)

    def _create_run(
        self,
        pipeline: Pipeline,
        *,
        trigger: str,
        inputs: dict[str, Any],
        objective: str | None,
        name: str,
        overrides: dict[str, Any] | None = None,
        outputs: dict[str, Any] | None = None,
        seeded: Mapping[str, Mapping[str, Any]] | None = None,
        replay_of: str | None = None,
        replay_from: str | None = None,
    ) -> Run:
        """Insert the run and its stage rows.

        Args:
            seeded: stage id -> the payload copied from a run being replayed.
                Those stages are recorded as ``skipped`` with the copied output
                attached, so the replay never claims to have paid for work it
                inherited.
        """
        now = utcnow()
        run_id = new_run_id()
        document = pipeline.as_dict()
        carried = dict(seeded or {})
        with self._session() as session:
            session.add(
                AgentRunRow(
                    id=run_id,
                    pipeline_id=pipeline.id,
                    pipeline_version=pipeline.version,
                    name=name,
                    objective=objective,
                    status=str(RunStatus.QUEUED),
                    trigger=trigger,
                    inputs=inputs,
                    outputs=dict(outputs or {}),
                    usage=Usage().as_dict(),
                    cost_usd=0.0,
                    document=document,
                    overrides=dict(overrides or {}),
                    replay_of=replay_of,
                    replay_from=replay_from,
                    created_at=now,
                )
            )
            for index, stage in enumerate(pipeline.stages):
                session.add(
                    AgentStageRunRow(
                        run_id=run_id,
                        stage_id=stage.id,
                        name=stage.name,
                        idx=index,
                        kind=str(stage.kind),
                        agent_id=stage.agent_id,
                        provider=stage.provider,
                        model=stage.model,
                        status=str(
                            StageStatus.SKIPPED
                            if (stage.id in carried or not stage.include_in_run)
                            else StageStatus.PENDING
                        ),
                        inputs={},
                        outputs=dict(carried.get(stage.id) or {}),
                        tool_calls=[],
                        usage=Usage().as_dict(),
                        cost_usd=0.0,
                    )
                )
        return self.get(run_id)

    # --- reads ------------------------------------------------------------
    def get(self, run_id: str, *, include_logs: bool = True) -> Run:
        """Return one run.

        Raises:
            KeyError: when the identifier is unknown.
        """
        with self._session() as session:
            row = session.get(AgentRunRow, run_id)
            if row is None:
                raise KeyError(f"unknown run {run_id!r}")
            stage_rows = list(
                session.scalars(
                    select(AgentStageRunRow)
                    .where(AgentStageRunRow.run_id == run_id)
                    .order_by(AgentStageRunRow.idx)
                )
            )
            artifact_rows = list(
                session.scalars(
                    select(AgentArtifactRow).where(AgentArtifactRow.run_id == run_id)
                )
            )
            log_rows = (
                list(
                    session.scalars(
                        select(AgentRunLogRow)
                        .where(AgentRunLogRow.run_id == run_id)
                        .order_by(AgentRunLogRow.id)
                    )
                )
                if include_logs
                else []
            )
        artifacts_by_stage: dict[str, list[Artifact]] = {}
        for artifact in artifact_rows:
            artifacts_by_stage.setdefault(artifact.stage_id, []).append(
                _artifact_from_row(artifact)
            )
        logs_by_stage: dict[str | None, list[dict[str, Any]]] = {}
        for log_row in log_rows:
            logs_by_stage.setdefault(log_row.stage_id, []).append(_log_from_row(log_row))
        stages = [
            StageRun(
                stage_id=stage.stage_id,
                name=stage.name,
                index=stage.idx,
                kind=StageKind(stage.kind),
                agent_id=stage.agent_id,
                provider=stage.provider,
                model=stage.model,
                status=StageStatus(stage.status),
                started_at=_aware(stage.started_at),
                finished_at=_aware(stage.finished_at),
                usage=Usage.from_dict(stage.usage),
                inputs=dict(stage.inputs or {}),
                outputs=dict(stage.outputs or {}),
                tool_calls=list(stage.tool_calls or []),
                logs=logs_by_stage.get(stage.stage_id, []),
                artifacts=artifacts_by_stage.get(stage.stage_id, []),
                approval_id=stage.approval_id,
                error=stage.error,
            )
            for stage in stage_rows
        ]
        return Run(
            id=row.id,
            pipeline_id=row.pipeline_id,
            pipeline_version=int(row.pipeline_version),
            name=row.name,
            objective=row.objective,
            status=RunStatus(row.status),
            trigger=row.trigger,
            created_at=_aware(row.created_at),
            started_at=_aware(row.started_at),
            finished_at=_aware(row.finished_at),
            stages=stages,
            usage=Usage.from_dict(row.usage),
            inputs=dict(row.inputs or {}),
            outputs=dict(row.outputs or {}),
            error=row.error,
            current_stage_id=row.current_stage_id,
            resume_index=row.resume_index,
            replay_of=row.replay_of,
            replay_from=row.replay_from,
            overrides=dict(row.overrides or {}),
            paused_at=_aware(row.paused_at),
        )

    def list(
        self,
        *,
        limit: int = 50,
        status: RunStatus | None = None,
        pipeline_id: str | None = None,
    ) -> list[Run]:
        """Return runs, newest first."""
        statement = (
            select(AgentRunRow.id).order_by(AgentRunRow.created_at.desc()).limit(max(1, limit))
        )
        if status is not None:
            statement = statement.where(AgentRunRow.status == str(status))
        if pipeline_id:
            statement = statement.where(AgentRunRow.pipeline_id == pipeline_id)
        with self._session() as session:
            ids = [row for row in session.scalars(statement)]
        return [self.get(run_id, include_logs=False) for run_id in ids]

    def logs(self, run_id: str, *, stage_id: str | None = None) -> list[dict[str, Any]]:
        """Return log lines for a run, optionally for one stage only."""
        statement = (
            select(AgentRunLogRow)
            .where(AgentRunLogRow.run_id == run_id)
            .order_by(AgentRunLogRow.id)
        )
        if stage_id is not None:
            statement = statement.where(AgentRunLogRow.stage_id == stage_id)
        with self._session() as session:
            return [_log_from_row(row) for row in session.scalars(statement)]

    def artifacts(self, run_id: str, *, stage_id: str | None = None) -> list[Artifact]:
        """Return every artifact produced by a run."""
        statement = (
            select(AgentArtifactRow)
            .where(AgentArtifactRow.run_id == run_id)
            .order_by(AgentArtifactRow.created_at)
        )
        if stage_id is not None:
            statement = statement.where(AgentArtifactRow.stage_id == stage_id)
        with self._session() as session:
            return [_artifact_from_row(row) for row in session.scalars(statement)]

    def artifact(self, artifact_id: str) -> Artifact:
        """Return one artifact, reading its file when it was spilled to disk.

        Raises:
            KeyError: when the identifier is unknown.
        """
        with self._session() as session:
            row = session.get(AgentArtifactRow, artifact_id)
            if row is None:
                raise KeyError(f"unknown artifact {artifact_id!r}")
            artifact = _artifact_from_row(row)
        if artifact.data is None and artifact.path:
            try:
                artifact.data = json.loads(Path(artifact.path).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                logger.warning("artifact file %s could not be read", artifact.path, exc_info=True)
        return artifact

    def approvals(self, *, state: ApprovalState | None = None, run_id: str | None = None) -> list[ApprovalRecord]:
        """Return approval records, newest first."""
        statement = select(AgentApprovalRow).order_by(AgentApprovalRow.requested_at.desc())
        if state is not None:
            statement = statement.where(AgentApprovalRow.state == str(state))
        if run_id is not None:
            statement = statement.where(AgentApprovalRow.run_id == run_id)
        with self._session() as session:
            return [_approval_from_row(row) for row in session.scalars(statement)]

    def approval(self, approval_id: str) -> ApprovalRecord:
        """Return one approval record.

        Raises:
            KeyError: when the identifier is unknown.
        """
        with self._session() as session:
            row = session.get(AgentApprovalRow, approval_id)
            if row is None:
                raise KeyError(f"unknown approval {approval_id!r}")
            return _approval_from_row(row)

    def pending_approvals(self) -> int:
        """Number of approval records still awaiting a decision."""
        return len(self.approvals(state=ApprovalState.PENDING))

    def counts(self) -> dict[str, int]:
        """Return real run counts for the Agent Center header."""
        with self._session() as session:
            rows = list(session.scalars(select(AgentRunRow.status)))
        counts = {str(state): 0 for state in RunStatus}
        for value in rows:
            counts[str(value)] = counts.get(str(value), 0) + 1
        active = sum(counts[str(state)] for state in ACTIVE_RUN_STATES)
        return {
            **counts,
            "total": len(rows),
            "active": active,
            "idle": len(rows) - active,
            "pending_approvals": self.pending_approvals(),
        }

    def active_count(self) -> int:
        """Number of runs currently executing or waiting on a person."""
        return self.counts()["active"]

    def active_agent_ids(self) -> set[str]:
        """Ids of the agents executing a stage at this instant."""
        return {agent_id for agent_id in self._active.values() if agent_id}

    def agent_stats(self, agent_id: str) -> dict[str, Any]:
        """Return an agent's real execution record, counted from the run tables."""
        statement = select(AgentStageRunRow).where(AgentStageRunRow.agent_id == agent_id)
        with self._session() as session:
            rows = list(session.scalars(statement))
        finished = [row for row in rows if row.status in {StageStatus.SUCCEEDED, StageStatus.FAILED}]
        failures = [row for row in finished if row.status == StageStatus.FAILED]
        durations = [
            (_aware(row.finished_at) - _aware(row.started_at)).total_seconds() * 1000.0
            for row in finished
            if row.started_at and row.finished_at
        ]
        last = max((row.finished_at for row in rows if row.finished_at), default=None)
        return {
            "agent_id": agent_id,
            "runs": len({row.run_id for row in rows}),
            "stage_runs": len(rows),
            "completed": len(finished) - len(failures),
            "failed": len(failures),
            "failure_rate": round(len(failures) / len(finished), 4) if finished else None,
            "cost_usd": round(sum(row.cost_usd for row in rows), 6),
            "avg_duration_ms": round(sum(durations) / len(durations), 1) if durations else None,
            "last_run_at": _iso(_aware(last)),
            "active": agent_id in self.active_agent_ids(),
        }

    def refresh_spend(self) -> None:
        """Re-seed the ledger from the persisted stage runs.

        Not called from :meth:`spend`: a reload replaces the in-memory entries
        with what is on disk, and a stage that is in flight has not written its
        cost yet - reloading mid-run would under-count exactly the spend the
        budget is meant to stop.
        """
        self.ledger.reload()

    def spend(self, *, window: str = "day", run_id: str | None = None) -> dict[str, Any]:
        """Return ``{"used": ..., "budget": ...}`` for the datastrip - real figures."""
        snapshot = self.ledger.snapshot(run_id=run_id)
        if window in {"day", "month"}:
            key = "daily" if window == "day" else "monthly"
            snapshot["used"] = snapshot[key]["used"]
            snapshot["budget"] = snapshot[key]["budget"]
            snapshot["window"] = window
        return snapshot

    def check_budget(self, projected_cost_usd: float, *, run_id: str | None = None) -> None:
        """Raise :class:`BudgetExceeded` when a projected cost breaks a budget."""
        self.ledger.check(run_id=run_id, projected_usd=projected_cost_usd)

    # --- policy -----------------------------------------------------------
    @property
    def policy(self) -> Any:
        """The workspace autonomy policy (read fresh so an edit takes effect)."""
        from tradinglib.agents.approvals import get_policy

        return get_policy()

    def check_concurrency(self) -> None:
        """Enforce the workspace-wide "Maximum concurrent runs" setting.

        Raises:
            GuardrailBlocked: when starting another run would exceed the limit.
        """
        from tradinglib.agents.approvals import GuardrailBlocked

        limit = int(self.policy.max_concurrent_runs)
        active = self.active_count()
        if active < limit:
            return
        detail = (
            f"{active} run(s) already active and the workspace limit is {limit}; "
            "wait for one to finish or raise the limit in Configuration"
        )
        self._record_audit(
            "runner",
            "guardrail block",
            capability="max_concurrent_runs",
            result="blocked",
            active=active,
            limit=limit,
            detail=detail,
        )
        raise GuardrailBlocked("max_concurrent_runs", detail)

    # --- execution --------------------------------------------------------
    async def _guarded_execute(self, run_id: str) -> None:
        """Background wrapper: a failing run must never raise into the loop."""
        try:
            await self.execute(run_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - already recorded on the run
            logger.exception("run %s failed outside the stage loop", run_id)
        finally:
            self._tasks.pop(run_id, None)

    async def execute(self, run_id: str, *, start_index: int = 0) -> Run:
        """Run every enabled stage to completion, honouring approval gates."""
        run = self.get(run_id)
        if run.status in TERMINAL_RUN_STATES:
            return run
        self._update_run(
            run_id,
            status=RunStatus.RUNNING,
            started_at=run.started_at or utcnow(),
        )
        await self._publish(
            {
                "kind": "run",
                "phase": "started",
                "run_id": run_id,
                "pipeline_id": run.pipeline_id,
                "status": str(RunStatus.RUNNING),
                "progress": run.progress(),
            }
        )
        pipeline = self._run_document(run_id)
        upstream: dict[str, Any] = dict(run.outputs or {})
        try:
            for index, stage in enumerate(pipeline.stages):
                if index < start_index:
                    continue
                if not stage.include_in_run:
                    self._update_stage(run_id, stage.id, status=StageStatus.SKIPPED)
                    await self._log(run_id, stage.id, f"Stage {stage.name} skipped (excluded)")
                    continue
                if run_id in self._pause_requests:
                    return await self._settle_pause(run_id, stage, index)
                self._update_run(run_id, current_stage_id=stage.id)
                if stage.kind is StageKind.HUMAN_APPROVAL:
                    decision = await self._run_approval_stage(run_id, stage, index, upstream)
                    if decision is ApprovalState.REJECTED:
                        return self._finish(
                            run_id, RunStatus.KILLED, error="rejected at the human-approval gate"
                        )
                    continue
                outputs = await self._run_agent_stage(run_id, stage, upstream)
                if outputs is not None:
                    upstream = {**upstream, stage.id: outputs}
                    self._update_run(run_id, outputs=upstream)
        except asyncio.CancelledError:
            self._finish(run_id, RunStatus.KILLED, error="run cancelled")
            await self._publish(
                {"kind": "run", "phase": "killed", "run_id": run_id, "status": str(RunStatus.KILLED)}
            )
            raise
        except BudgetExceeded as exc:
            await self._log(run_id, None, str(exc), level="error", **exc.as_dict())
            self._record_audit("runner", "run stopped on budget", run_id=run_id, **exc.as_dict())
            finished = self._finish(run_id, RunStatus.FAILED, error=str(exc))
            await self._publish(
                {
                    "kind": "run",
                    "phase": "failed",
                    "run_id": run_id,
                    "status": str(RunStatus.FAILED),
                    "error": str(exc),
                    "budget": exc.as_dict(),
                }
            )
            return finished
        except Exception as exc:  # noqa: BLE001 - a stage failure fails the run, honestly
            message = f"{type(exc).__name__}: {exc}"
            logger.warning("run %s failed: %s", run_id, message, exc_info=True)
            await self._log(run_id, None, message, level="error")
            finished = self._finish(run_id, RunStatus.FAILED, error=message)
            await self._publish(
                {
                    "kind": "run",
                    "phase": "failed",
                    "run_id": run_id,
                    "status": str(RunStatus.FAILED),
                    "error": message,
                }
            )
            return finished
        finally:
            self._active.pop(run_id, None)

        finished = self._finish(run_id, RunStatus.SUCCEEDED)
        await self._log(
            run_id,
            None,
            f"Run complete - {len(finished.stages)} stages, ${finished.cost_usd:.4f}",
        )
        self._record_audit(
            "runner",
            "run complete",
            target=finished.pipeline_id,
            run_id=run_id,
            cost_usd=round(finished.cost_usd, 6),
        )
        await self._publish(
            {
                "kind": "run",
                "phase": "complete",
                "run_id": run_id,
                "pipeline_id": finished.pipeline_id,
                "status": str(finished.status),
                "cost_usd": round(finished.cost_usd, 6),
                "usage": finished.usage.as_dict(),
                "progress": finished.progress(),
            }
        )
        return finished

    def _run_document(self, run_id: str) -> Pipeline:
        """Rebuild the pipeline document the run was created against."""
        with self._session() as session:
            row = session.get(AgentRunRow, run_id)
            if row is None:
                raise KeyError(f"unknown run {run_id!r}")
            document = dict(row.document or {})
        return Pipeline(
            id=str(document.get("id", "")),
            name=str(document.get("name", "")),
            version=int(document.get("version", 1)),
            objective=document.get("objective"),
            description=document.get("description"),
            stages=[Stage.from_dict(stage) for stage in document.get("stages") or []],
        )

    async def _run_agent_stage(
        self, run_id: str, stage: Stage, upstream: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        """Execute one agent stage: prompt, tool loop, contract check, accounting."""
        run = self.get(run_id, include_logs=False)
        agent = self._resolve_agent(stage)
        tool_names = self._stage_tools(stage, agent)
        specs = self.toolset.specs(tool_names)
        provider, model = await self.providers.resolve(
            stage.provider or (agent.provider if agent else None),
            stage.model or (agent.model if agent else None) or None,
        )
        workspace = await asyncio.to_thread(self.toolset.workspace) if tool_names else None
        system = build_stage_system_prompt(
            agent, stage, tool_names=tool_names, workspace=workspace
        )
        user = build_stage_user_prompt(run, stage, upstream, run.inputs)
        stage_budget = stage.budget_usd if stage.budget_usd is not None else (
            agent.budget_usd if agent else None
        )
        max_tokens = agent.max_tokens if agent else 4096
        temperature = agent.temperature if agent else 0.0
        timeout = stage.timeout_s if stage.timeout_s is not None else (
            agent.timeout_s if agent else None
        )
        projected = self._project_cost(provider, model, system + user, max_tokens)
        self.ledger.check(
            run_id=run_id,
            projected_usd=projected,
            stage_budget_usd=stage_budget,
            stage_used_usd=0.0,
        )
        await self._compute_guardrail(run_id, stage, projected)

        self._active[run_id] = agent.id if agent else None
        started = utcnow()
        inputs_payload = {
            "system": system,
            "prompt": user,
            "tools": tool_names,
            "scope": dict(stage.scope),
            "upstream": {key: value for key, value in upstream.items()},
        }
        self._update_stage(
            run_id,
            stage.id,
            status=StageStatus.RUNNING,
            started_at=started,
            provider=provider.name,
            model=model,
            agent_id=agent.id if agent else None,
            inputs=inputs_payload,
        )
        await self._log(
            run_id,
            stage.id,
            f"Stage {stage.name} started on {provider.name}/{model}"
            + (f" as {agent.name}" if agent else ""),
            tools=tool_names,
            projected_cost_usd=round(projected, 6),
        )
        await self._publish(
            {
                "kind": "stage",
                "phase": "started",
                "run_id": run_id,
                "stage_id": stage.id,
                "name": stage.name,
                "agent_id": agent.id if agent else None,
                "provider": provider.name,
                "model": model,
                "status": str(StageStatus.RUNNING),
            }
        )
        self._record_audit(
            agent.name if agent else "pipeline",
            "stage started",
            target=stage.id,
            run_id=run_id,
            stage_id=stage.id,
            model=f"{provider.name}/{model}",
        )

        scope = {**dict(stage.scope), "run_id": run_id, "stage_id": stage.id}
        recorded_calls: list[dict[str, Any]] = []
        usage_total = Usage()

        async def executor(name: str, arguments: dict[str, Any]) -> ToolResult:
            """Run one tool for the model and record what it did."""
            if name not in tool_names:
                return ToolResult(
                    f"tool {name!r} is not granted to this stage", is_error=True
                )
            result = await self.toolset.execute(name, arguments, scope=scope)
            entry = {
                "name": name,
                "arguments": arguments,
                "is_error": result.is_error,
                "ts": utcnow().isoformat(),
                "result_chars": len(result.content),
            }
            recorded_calls.append(entry)
            await self._log(
                run_id,
                stage.id,
                f"tool {name} {'failed' if result.is_error else 'ok'}",
                level="warning" if result.is_error else "info",
                arguments=arguments,
            )
            if result.data is not None:
                self._store_artifact(
                    run_id,
                    stage.id,
                    name=f"{name} result",
                    kind="tool_result",
                    data=result.data,
                )
            return result

        try:
            completion, usage_total = await self._converse(
                provider=provider,
                model=model,
                system=system,
                user=user,
                specs=specs,
                executor=executor,
                max_tokens=max_tokens,
                temperature=temperature,
                timeout=timeout,
                budget_usd=stage_budget,
                run_id=run_id,
                stage_id=stage.id,
                stage_budget=stage_budget,
            )
        except BudgetExceeded:
            self._update_stage(
                run_id,
                stage.id,
                status=StageStatus.FAILED,
                finished_at=utcnow(),
                error="budget exceeded",
                tool_calls=recorded_calls,
            )
            raise
        except Exception as exc:  # noqa: BLE001 - recorded, then re-raised to fail the run
            message = f"{type(exc).__name__}: {exc}"
            self._update_stage(
                run_id,
                stage.id,
                status=StageStatus.FAILED,
                finished_at=utcnow(),
                error=message,
                tool_calls=recorded_calls,
            )
            await self._log(run_id, stage.id, f"Stage {stage.name} failed: {message}", level="error")
            await self._publish(
                {
                    "kind": "stage",
                    "phase": "failed",
                    "run_id": run_id,
                    "stage_id": stage.id,
                    "status": str(StageStatus.FAILED),
                    "error": message,
                }
            )
            raise
        finally:
            self._active[run_id] = None

        structured = extract_structured_output(completion.text) or {}
        outputs: dict[str, Any] = {"text": completion.text, **structured}
        missing = stage.outputs.check(structured)
        if missing:
            policy = str(stage.config.get("on_contract_violation", "fail")).lower()
            message = (
                f"stage {stage.id!r} did not satisfy its output contract: missing "
                + ", ".join(missing)
            )
            if policy == "warn":
                await self._log(run_id, stage.id, message, level="warning")
                outputs["contract_warning"] = message
            else:
                self._update_stage(
                    run_id,
                    stage.id,
                    status=StageStatus.FAILED,
                    finished_at=utcnow(),
                    error=message,
                    outputs=outputs,
                    usage=usage_total,
                    tool_calls=recorded_calls,
                )
                await self._log(run_id, stage.id, message, level="error")
                raise ValueError(message)

        self._store_artifact(
            run_id, stage.id, name=f"{stage.name} output", kind="stage_output", data=outputs
        )
        self._update_stage(
            run_id,
            stage.id,
            status=StageStatus.SUCCEEDED,
            finished_at=utcnow(),
            outputs=outputs,
            usage=usage_total,
            tool_calls=recorded_calls,
        )
        self._accumulate_run_usage(run_id, usage_total)
        await self._log(
            run_id,
            stage.id,
            f"Stage {stage.name} complete - {usage_total.total_tokens} tokens, "
            f"${usage_total.cost_usd:.4f}",
            usage=usage_total.as_dict(),
        )
        self._record_audit(
            agent.name if agent else "pipeline",
            "stage complete",
            target=stage.id,
            run_id=run_id,
            stage_id=stage.id,
            cost_usd=round(usage_total.cost_usd, 6),
            tool_calls=len(recorded_calls),
        )
        await self._publish(
            {
                "kind": "stage",
                "phase": "complete",
                "run_id": run_id,
                "stage_id": stage.id,
                "name": stage.name,
                "status": str(StageStatus.SUCCEEDED),
                "usage": usage_total.as_dict(),
                "cost_usd": round(usage_total.cost_usd, 6),
                "tool_calls": len(recorded_calls),
                "progress": self.get(run_id, include_logs=False).progress(),
            }
        )
        return outputs

    async def _converse(
        self,
        *,
        provider: LLMProvider,
        model: str,
        system: str,
        user: str,
        specs: Sequence[ToolSpec],
        executor: Any,
        max_tokens: int,
        temperature: float,
        timeout: float | None,
        budget_usd: float | None,
        run_id: str,
        stage_id: str,
        stage_budget: float | None,
    ) -> tuple[Completion, Usage]:
        """Drive one stage's conversation, executing tools when the provider does not.

        Providers that run their own tool loop (the Claude Agent SDK) are called
        once with the executor attached. Providers that return pending calls
        (OpenAI) are looped here: execute, feed the results back, ask again -
        with the budget re-checked before every extra round trip.
        """
        messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
        total = Usage()
        completion: Completion | None = None
        for iteration in range(MAX_TOOL_ITERATIONS):
            completion = await provider.complete(
                model=model,
                system=system,
                messages=messages,
                tools=list(specs) or None,
                tool_executor=executor if specs else None,
                max_tokens=max_tokens,
                temperature=temperature,
                budget_usd=budget_usd,
                timeout=timeout,
            )
            total = total + completion.usage
            self.ledger.record(
                provider=provider.name,
                model=model,
                usage=completion.usage,
                run_id=run_id,
                stage_id=stage_id,
            )
            if provider.executes_tools or not completion.tool_calls:
                return completion, total
            messages.append(
                {
                    "role": "assistant",
                    "content": completion.text,
                    "tool_calls": [call.as_dict() for call in completion.tool_calls],
                }
            )
            for call in completion.tool_calls:
                result = await executor(call.name, call.arguments)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id or new_tool_call_id(),
                        "name": call.name,
                        "content": result.content,
                    }
                )
            self.ledger.check(
                run_id=run_id,
                projected_usd=self._project_cost(provider, model, user, max_tokens),
                stage_budget_usd=stage_budget,
                stage_used_usd=total.cost_usd,
            )
            logger.debug("stage %s tool round %d complete", stage_id, iteration + 1)
        assert completion is not None  # noqa: S101 - the loop runs at least once
        await self._log(
            run_id,
            stage_id,
            f"stopped after {MAX_TOOL_ITERATIONS} tool rounds; using the last answer",
            level="warning",
        )
        return completion, total

    async def _compute_guardrail(self, run_id: str, stage: Stage, projected: float) -> None:
        """Turn an expensive stage into an approval request instead of a charge.

        The "Launch compute - approval if > $N" guardrail is enforced here: when
        a stage's *projected* cost exceeds the workspace threshold the run blocks
        on a real approval record, exactly like a ``human_approval`` stage does,
        and the attempt is written to the audit log either way.

        Raises:
            GuardrailBlocked: when the reviewer rejects the compute request.
        """
        from tradinglib.agents.approvals import GuardrailBlocked

        threshold = float(self.policy.compute_approval_usd)
        if threshold <= 0 or projected <= threshold:
            return
        stages = self.get(run_id, include_logs=False).stages
        index = next((s.index for s in stages if s.stage_id == stage.id), 0)
        summary = (
            f"Stage {stage.name} projects ${projected:,.2f} of model spend, over the "
            f"${threshold:,.2f} compute guardrail. Approve to let it run."
        )
        record = self._create_approval(
            run_id,
            stage,
            index,
            summary=summary,
            payload={"guardrail": "compute", "projected_usd": projected, "threshold_usd": threshold},
        )
        self._update_stage(run_id, stage.id, status=StageStatus.AWAITING_APPROVAL, approval_id=record.id)
        self._update_run(run_id, status=RunStatus.AWAITING_APPROVAL, current_stage_id=stage.id)
        await self._log(run_id, stage.id, summary, level="warn", approval_id=record.id)
        self._record_audit(
            "policy engine",
            "guardrail block",
            target=record.id,
            run_id=run_id,
            stage_id=stage.id,
            capability="compute_approval",
            result="blocked",
            projected_usd=round(projected, 6),
            threshold_usd=threshold,
        )
        await self._publish(
            {
                "kind": "run",
                "phase": "awaiting_approval",
                "run_id": run_id,
                "stage_id": stage.id,
                "approval_id": record.id,
                "guardrail": "compute",
                "status": str(RunStatus.AWAITING_APPROVAL),
            }
        )
        await self._publish(
            {
                "kind": "approval",
                "phase": "requested",
                "approval": record.as_dict(),
                "pending": self.pending_approvals(),
            },
            channel=APPROVALS_CHANNEL,
        )
        decision = await self._wait_for_decision(record.id)
        if decision is not ApprovalState.APPROVED:
            raise GuardrailBlocked(
                "compute_approval",
                f"the ${projected:,.2f} compute request for stage {stage.name} was rejected",
            )
        self._update_run(run_id, status=RunStatus.RUNNING)
        self._update_stage(run_id, stage.id, status=StageStatus.RUNNING, approval_id=record.id)
        await self._log(run_id, stage.id, "Compute approved; the stage runs")

    async def _run_approval_stage(
        self, run_id: str, stage: Stage, index: int, upstream: Mapping[str, Any]
    ) -> ApprovalState:
        """Create an approval record, pause the run and wait for the decision."""
        record = self._create_approval(
            run_id,
            stage,
            index,
            summary=stage.objective or "Human approval is required before promotion.",
            payload={"upstream": dict(upstream)},
        )
        self._update_stage(
            run_id,
            stage.id,
            status=StageStatus.AWAITING_APPROVAL,
            started_at=utcnow(),
            approval_id=record.id,
            inputs={"upstream": dict(upstream)},
        )
        self._update_run(run_id, status=RunStatus.AWAITING_APPROVAL, current_stage_id=stage.id)
        await self._log(
            run_id, stage.id, f"Waiting for human approval ({record.id})", approval_id=record.id
        )
        self._record_audit(
            "policy engine",
            "stopped automated execution",
            target=record.id,
            run_id=run_id,
            stage_id=stage.id,
        )
        await self._publish(
            {
                "kind": "run",
                "phase": "awaiting_approval",
                "run_id": run_id,
                "stage_id": stage.id,
                "approval_id": record.id,
                "status": str(RunStatus.AWAITING_APPROVAL),
            }
        )
        await self._publish(
            {"kind": "approval", "phase": "requested", "approval": record.as_dict(),
             "pending": self.pending_approvals()},
            channel=APPROVALS_CHANNEL,
        )

        decision = await self._wait_for_decision(record.id)
        now = utcnow()
        if decision is ApprovalState.APPROVED:
            self._update_stage(
                run_id,
                stage.id,
                status=StageStatus.SUCCEEDED,
                finished_at=now,
                outputs={"decision": "approved"},
            )
            self._update_run(run_id, status=RunStatus.RUNNING)
            await self._log(run_id, stage.id, "Approved; the run resumes")
            await self._publish(
                {
                    "kind": "run",
                    "phase": "resumed",
                    "run_id": run_id,
                    "stage_id": stage.id,
                    "status": str(RunStatus.RUNNING),
                }
            )
        else:
            self._update_stage(
                run_id,
                stage.id,
                status=StageStatus.KILLED,
                finished_at=now,
                outputs={"decision": "rejected"},
            )
            await self._log(run_id, stage.id, "Rejected; the run is terminated", level="warning")
        return decision

    async def _wait_for_decision(self, approval_id: str) -> ApprovalState:
        """Block on the approval's event until a person decides.

        If the decision was already recorded (the API answered before the stage
        got here), it is returned immediately.
        """
        record = self.approval(approval_id)
        if record.state is not ApprovalState.PENDING:
            return record.state
        event = asyncio.Event()
        self._waiters[approval_id] = event
        try:
            await event.wait()
        finally:
            self._waiters.pop(approval_id, None)
        return self._decisions.pop(approval_id, self.approval(approval_id).state)

    def _create_approval(
        self,
        run_id: str,
        stage: Stage,
        index: int,
        *,
        summary: str,
        payload: Mapping[str, Any],
    ) -> ApprovalRecord:
        """Persist a pending approval record for a stage."""
        identifier = new_approval_id()
        now = utcnow()
        with self._session() as session:
            session.add(
                AgentApprovalRow(
                    id=identifier,
                    run_id=run_id,
                    stage_id=stage.id,
                    title=f"{stage.name} - approval required",
                    summary=summary,
                    payload=dict(payload),
                    state=str(ApprovalState.PENDING),
                    resume_index=index + 1,
                    requested_at=now,
                )
            )
        return self.approval(identifier)

    # --- decisions --------------------------------------------------------
    async def approve(
        self, approval_id: str, *, by: str = "user", comment: str | None = None
    ) -> ApprovalRecord:
        """Approve a request, unblocking (or restarting) its run.

        Raises:
            KeyError: when the approval id is unknown.
            ValueError: when it was already decided.
        """
        return await self._decide(approval_id, ApprovalState.APPROVED, by=by, comment=comment)

    async def reject(
        self, approval_id: str, *, by: str = "user", comment: str | None = None
    ) -> ApprovalRecord:
        """Reject a request, terminating its run."""
        return await self._decide(approval_id, ApprovalState.REJECTED, by=by, comment=comment)

    async def _decide(
        self, approval_id: str, state: ApprovalState, *, by: str, comment: str | None
    ) -> ApprovalRecord:
        """Record a decision and wake (or restart) the run that waits on it."""
        with self._session() as session:
            row = session.get(AgentApprovalRow, approval_id)
            if row is None:
                raise KeyError(f"unknown approval {approval_id!r}")
            if row.state != str(ApprovalState.PENDING):
                raise ValueError(
                    f"approval {approval_id!r} was already {row.state} at {_iso(_aware(row.decided_at))}"
                )
            row.state = str(state)
            row.decided_by = by
            row.comment = comment
            row.decided_at = utcnow()
            run_id = row.run_id
            resume_index = int(row.resume_index)
        record = self.approval(approval_id)
        self._record_audit(
            by,
            f"approval {state}",
            target=approval_id,
            run_id=run_id,
            stage_id=record.stage_id,
            comment=comment,
        )
        await self._log(
            run_id,
            record.stage_id,
            f"Approval {approval_id} {state} by {by}"
            + (f": {comment}" if comment else ""),
            level="info" if state is ApprovalState.APPROVED else "warning",
        )
        await self._publish(
            {
                "kind": "approval",
                "phase": str(state),
                "approval": record.as_dict(),
                "pending": self.pending_approvals(),
            },
            channel=APPROVALS_CHANNEL,
        )
        waiter = self._waiters.get(approval_id)
        if waiter is not None:
            self._decisions[approval_id] = state
            waiter.set()
            return record
        # Nobody is waiting: the process that started this run is gone. Continue
        # (or terminate) the run from the stage that was blocked.
        if state is ApprovalState.REJECTED:
            self._update_stage(
                run_id, record.stage_id, status=StageStatus.KILLED, finished_at=utcnow()
            )
            self._finish(run_id, RunStatus.KILLED, error="rejected at the human-approval gate")
            await self._publish(
                {"kind": "run", "phase": "killed", "run_id": run_id, "status": str(RunStatus.KILLED)}
            )
            return record
        run = self.get(run_id, include_logs=False)
        if run.status is RunStatus.AWAITING_APPROVAL:
            self._update_stage(
                run_id,
                record.stage_id,
                status=StageStatus.SUCCEEDED,
                finished_at=utcnow(),
                outputs={"decision": "approved"},
            )
            self._tasks[run_id] = asyncio.create_task(
                self._guarded_execute_from(run_id, resume_index), name=f"tradinglib-run-{run_id}"
            )
        return record

    async def _guarded_execute_from(self, run_id: str, index: int) -> None:
        """Resume a run whose waiter is gone, from ``index``."""
        try:
            await self.execute(run_id, start_index=index)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - recorded on the run
            logger.exception("resumed run %s failed", run_id)
        finally:
            self._tasks.pop(run_id, None)

    async def resume(self, run_id: str) -> Run:
        """Resume a run that is blocked on a human-approval stage.

        Approves the outstanding request, which is what unblocks it.

        Raises:
            KeyError: when the run has no pending approval.
        """
        pending = self.approvals(state=ApprovalState.PENDING, run_id=run_id)
        if not pending:
            raise KeyError(f"run {run_id!r} has no pending approval")
        await self.approve(pending[0].id, by="user", comment="resumed")
        return self.get(run_id)

    async def _settle_pause(self, run_id: str, stage: Stage, index: int) -> Run:
        """Stop a run at a stage boundary and record where it must resume."""
        self._pause_requests.discard(run_id)
        self._resume_points[run_id] = index
        self._update_run(
            run_id,
            status=RunStatus.PAUSED,
            current_stage_id=stage.id,
            resume_index=index,
            paused_at=utcnow(),
        )
        await self._log(
            run_id, None, f"Run paused before stage {stage.name} ({index + 1})", level="warn"
        )
        self._record_audit("runner", "run paused", run_id=run_id, target=stage.id, stage_index=index)
        run = self.get(run_id, include_logs=False)
        await self._publish(
            {
                "kind": "run",
                "phase": "paused",
                "run_id": run_id,
                "status": str(RunStatus.PAUSED),
                "stage_id": stage.id,
                "resume_index": index,
                "progress": run.progress(),
            }
        )
        return run

    async def pause(self, run_id: str, *, reason: str = "paused by the user") -> Run:
        """Ask a running pipeline to stop at the next stage boundary.

        A stage that is already talking to a model is allowed to finish - its
        tokens are already spent and throwing the answer away would be both
        wasteful and dishonest about the cost. The run therefore pauses *before*
        the next stage starts.

        Raises:
            KeyError: when the run id is unknown.
            ValueError: when the run is not in a state that can be paused.
        """
        run = self.get(run_id, include_logs=False)
        if run.status is RunStatus.PAUSED:
            return run
        if run.status is RunStatus.AWAITING_APPROVAL:
            raise ValueError(
                f"run {run_id!r} is already blocked on a human-approval gate; "
                "decide the approval or cancel the run instead"
            )
        if run.status in TERMINAL_RUN_STATES:
            raise ValueError(f"run {run_id!r} has already finished ({run.status})")
        self._pause_requests.add(run_id)
        await self._log(run_id, None, f"Pause requested: {reason}", level="warn")
        await self._publish(
            {
                "kind": "run",
                "phase": "pause_requested",
                "run_id": run_id,
                "status": str(run.status),
                "reason": reason,
            }
        )
        if run.status is RunStatus.QUEUED and self._tasks.get(run_id) is None:
            # Nothing is executing it yet, so the boundary check will never run;
            # park it immediately at the first stage instead.
            pipeline = self._run_document(run_id)
            if pipeline.stages:
                return await self._settle_pause(run_id, pipeline.stages[0], 0)
        return self.get(run_id, include_logs=False)

    async def unpause(self, run_id: str) -> Run:
        """Continue a paused run from the stage it stopped in front of.

        Raises:
            KeyError: when the run id is unknown.
            ValueError: when the run is not paused.
        """
        run = self.get(run_id, include_logs=False)
        if run.status is not RunStatus.PAUSED:
            raise ValueError(f"run {run_id!r} is not paused (status {run.status})")
        index = run.resume_index if run.resume_index is not None else self._resume_points.get(run_id, 0)
        self._pause_requests.discard(run_id)
        self._update_run(run_id, status=RunStatus.RUNNING, paused_at=None)
        await self._log(run_id, None, f"Run resumed at stage {index + 1}")
        self._record_audit("runner", "run resumed", run_id=run_id, stage_index=index)
        await self._publish(
            {
                "kind": "run",
                "phase": "resumed",
                "run_id": run_id,
                "status": str(RunStatus.RUNNING),
                "resume_index": index,
            }
        )
        self._tasks[run_id] = asyncio.create_task(self._guarded_execute_from(run_id, index))
        return self.get(run_id, include_logs=False)

    async def retry(self, run_id: str, *, stage_id: str | None = None) -> Run:
        """Re-execute a run from its first unsuccessful stage.

        Everything already succeeded is kept - the point of a retry is to redo
        the stage that broke and the ones that never got to run, not to pay for
        the whole pipeline again. The stage being retried and every stage after
        it are reset to ``pending`` first, so the run's own record never claims a
        stale failure once the retry succeeds.

        Args:
            run_id: the run to restart.
            stage_id: retry from this stage instead of the first failed one.

        Raises:
            KeyError: when the run or the stage id is unknown.
            ValueError: when the run is still executing or has nothing to retry.
        """
        run = self.get(run_id, include_logs=False)
        if run.status in {RunStatus.RUNNING, RunStatus.AWAITING_APPROVAL}:
            raise ValueError(f"run {run_id!r} is still executing; cancel or pause it first")
        stages = sorted(run.stages, key=lambda item: item.index)
        if stage_id is not None:
            target = next((s for s in stages if s.stage_id == stage_id), None)
            if target is None:
                raise KeyError(f"run {run_id!r} has no stage {stage_id!r}")
        else:
            target = next((s for s in stages if s.status in RETRYABLE_STAGE_STATES), None)
            if target is None:
                raise ValueError(
                    f"run {run_id!r} has no failed or pending stage to retry - "
                    "every stage succeeded or was skipped"
                )
        index = int(target.index)
        for stage in stages:
            if stage.index >= index and stage.status is not StageStatus.SKIPPED:
                self._update_stage(
                    run_id,
                    stage.stage_id,
                    status=StageStatus.PENDING,
                    error=None,
                    started_at=None,
                    finished_at=None,
                )
        self._pause_requests.discard(run_id)
        self._resume_points[run_id] = index
        self._update_run(
            run_id,
            status=RunStatus.RUNNING,
            error=None,
            finished_at=None,
            paused_at=None,
            resume_index=index,
            current_stage_id=target.stage_id,
        )
        await self._log(run_id, target.stage_id, f"Retrying from stage {target.name}", level="warn")
        self._record_audit(
            "runner", "stage retried", run_id=run_id, target=target.stage_id, stage_index=index
        )
        await self._publish(
            {
                "kind": "run",
                "phase": "retrying",
                "run_id": run_id,
                "status": str(RunStatus.RUNNING),
                "stage_id": target.stage_id,
                "resume_index": index,
            }
        )
        self._tasks[run_id] = asyncio.create_task(self._guarded_execute_from(run_id, index))
        return self.get(run_id, include_logs=False)

    async def run_stage(
        self,
        pipeline_id: str,
        stage_id: str,
        *,
        inputs: Mapping[str, Any] | None = None,
        version: int | None = None,
        trigger: str = "single-stage",
        model: str | None = None,
        provider: str | None = None,
        wait: bool = True,
    ) -> Run:
        """Execute exactly one stage of a pipeline (the builder's *Run this stage*).

        A single-stage run is a real run: it gets a run id, a document, stage
        rows, logs, artifacts, token accounting and audit records, and it shows
        up in the run list like any other. Every stage except the requested one
        is switched off for this run only, so the saved pipeline is untouched.

        Because the other stages never execute, the stage runs with only the
        ``inputs`` the caller supplies as upstream context - it is a probe of one
        stage, not an abbreviated pipeline, and the run's log says so.

        Args:
            pipeline_id: the pipeline the stage belongs to.
            stage_id: the stage to execute.
            inputs: the brief handed to the stage in place of upstream output.
            version: run a frozen version instead of the working document.
            trigger: recorded as the run's trigger.
            model: run this stage against a different model, just this once.
            provider: the provider that model belongs to.
            wait: execute inline and return the finished run (the default: the
                builder's button reports the outcome).

        Raises:
            KeyError: when the pipeline or the stage is unknown.
            ValueError: when the stage cannot run on its own (it is the
                human-approval boundary, or it has no agent assigned).
            GuardrailBlocked: when the workspace concurrency ceiling is reached.
        """
        self.check_concurrency()
        pipeline = self.pipelines.get(pipeline_id, version=version)
        stage = pipeline.stage(stage_id)
        if stage.kind is StageKind.HUMAN_APPROVAL:
            raise ValueError(
                f"stage {stage_id!r} is the human-approval boundary; it has nothing to run on "
                "its own - approve or reject it from the Approval Review tab"
            )
        if not stage.agent_id:
            raise ValueError(f"stage {stage_id!r} has no agent assigned, so it cannot run")
        known_agents = {definition.id for definition in self.agents.list()}
        if stage.agent_id not in known_agents:
            raise ValueError(
                f"stage {stage_id!r} is assigned to unknown agent {stage.agent_id!r}"
            )
        overrides: dict[str, dict[str, Any]] = {
            other.id: {"include_in_run": other.id == stage_id} for other in pipeline.stages
        }
        if model or provider:
            overrides[stage_id].update(
                {"model": model, "provider": provider or stage.provider}
            )
        applied = apply_stage_overrides(pipeline, overrides)
        run = self._create_run(
            pipeline,
            trigger=trigger,
            inputs=dict(inputs or {}),
            objective=stage.objective or pipeline.objective,
            name=f"{pipeline.name} · {stage.name}",
            overrides=applied,
        )
        await self._log(
            run.id,
            stage_id,
            f"Single-stage run: only '{stage.name}' executes; upstream context comes from the "
            "supplied inputs, not from the stages before it",
            trigger=trigger,
        )
        self._record_audit(
            "runner",
            "single stage run queued",
            target=pipeline.id,
            run_id=run.id,
            stage_id=stage_id,
            trigger=trigger,
        )
        await self._publish(
            {
                "kind": "run",
                "phase": "queued",
                "run_id": run.id,
                "pipeline_id": pipeline.id,
                "stage_id": stage_id,
                "single_stage": True,
                "status": str(run.status),
                "progress": run.progress(),
            }
        )
        if wait:
            return await self.execute(run.id)
        self._tasks[run.id] = asyncio.create_task(
            self._guarded_execute(run.id), name=f"tradinglib-stage-{run.id}"
        )
        return self.get(run.id)

    async def replay(
        self,
        run_id: str,
        *,
        stage_id: str | None = None,
        inputs: Mapping[str, Any] | None = None,
        stage_overrides: Mapping[str, Mapping[str, Any]] | None = None,
        wait: bool = False,
    ) -> Run:
        """Re-run a finished run from one stage onwards, as a *new* run.

        This is the run inspector's *Replay from stage...*. It differs from
        :meth:`retry` in the one way that matters for an append-only ledger: the
        original run is never touched. A replay creates a new run against the
        same frozen document, inherits the outputs of every stage *before* the
        chosen one (recorded as ``skipped`` with the inherited payload attached,
        so the replay never claims to have paid for them), and executes from
        there. ``replay_of`` links the two.

        Args:
            run_id: the run to replay.
            stage_id: the stage to restart from. Defaults to the first stage
                that did not succeed, or the first stage on a run where they
                all did.
            inputs: replace the original run brief.
            stage_overrides: per-run stage changes for the replay (a different
                model for the stage that failed, typically).
            wait: execute inline and return the finished run.

        Returns:
            The new run.

        Raises:
            KeyError: when the run or the stage id is unknown.
            ValueError: when the run is still executing.
            GuardrailBlocked: when the workspace concurrency ceiling is reached.
        """
        original = self.get(run_id, include_logs=False)
        if original.status not in TERMINAL_RUN_STATES:
            raise ValueError(
                f"run {run_id!r} is still {original.status}; pause or cancel it before replaying"
            )
        self.check_concurrency()
        pipeline = self._run_document(run_id)
        stages = sorted(original.stages, key=lambda item: item.index)
        if stage_id is None:
            target = next(
                (s for s in stages if s.status is not StageStatus.SUCCEEDED),
                stages[0] if stages else None,
            )
            if target is None:
                raise ValueError(f"run {run_id!r} has no stages to replay")
        else:
            target = next((s for s in stages if s.stage_id == stage_id), None)
            if target is None:
                raise KeyError(f"run {run_id!r} has no stage {stage_id!r}")
        index = int(target.index)
        # Everything before the chosen stage is inherited rather than re-run.
        original_outputs = dict(original.outputs or {})
        carried = {
            stage.stage_id: original_outputs.get(stage.stage_id, {})
            for stage in stages
            if stage.index < index and stage.status is StageStatus.SUCCEEDED
        }
        applied = apply_stage_overrides(pipeline, stage_overrides)
        run = self._create_run(
            pipeline,
            trigger=f"replay:{run_id}",
            inputs=dict(inputs if inputs is not None else original.inputs),
            objective=original.objective,
            name=f"{original.name} · replay from {target.name}",
            overrides=applied,
            outputs={key: value for key, value in carried.items() if value},
            seeded=carried,
            replay_of=run_id,
            replay_from=target.stage_id,
        )
        await self._log(
            run.id,
            target.stage_id,
            f"Replay of {run_id} from stage {target.name}: "
            f"{len(carried)} earlier stage(s) inherited, not re-executed",
            replay_of=run_id,
        )
        self._record_audit(
            "runner",
            "run replayed",
            target=run_id,
            run_id=run.id,
            stage_id=target.stage_id,
            inherited=len(carried),
        )
        await self._publish(
            {
                "kind": "run",
                "phase": "queued",
                "run_id": run.id,
                "pipeline_id": run.pipeline_id,
                "replay_of": run_id,
                "replay_from": target.stage_id,
                "status": str(run.status),
                "progress": run.progress(),
            }
        )
        if wait:
            return await self.execute(run.id, start_index=index)
        self._tasks[run.id] = asyncio.create_task(
            self._guarded_execute_from(run.id, index), name=f"tradinglib-replay-{run.id}"
        )
        return self.get(run.id)

    async def estimate(
        self,
        pipeline_id: str,
        *,
        version: int | None = None,
        stage_overrides: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Estimate what a run of this pipeline would cost and how long it would take.

        The estimate is built from *this workspace's own history*: for each
        stage, the mean token usage and wall time of that stage's previous
        successful runs, priced at the model the stage would actually use. A
        stage with no history falls back to the pipeline-wide average, and then
        to :data:`ESTIMATE_FALLBACK_TOKENS` - and every row says which basis it
        used, so an estimate is never mistaken for a measurement.

        Args:
            pipeline_id: the pipeline to price.
            version: price a frozen version instead of the working document.
            stage_overrides: the per-run overrides that would be applied
                (a cheaper model changes the estimate).

        Returns:
            The payload the Pipeline/AutoPipeline pre-run line renders:
            ``stages``, ``total_usd``, ``seconds``, ``basis`` and ``priced``.

        Raises:
            KeyError: when the pipeline is unknown.
        """
        pipeline = self.pipelines.get(pipeline_id, version=version)
        apply_stage_overrides(pipeline, stage_overrides)
        history = await asyncio.to_thread(self._stage_history, pipeline_id)
        models = {info.id: info for info in await self.providers.available_models()}
        overall = history.get("__pipeline__")
        rows: list[dict[str, Any]] = []
        total_usd = 0.0
        total_seconds = 0.0
        priced = True
        for stage in pipeline.stages:
            if not stage.include_in_run:
                rows.append(
                    {
                        "stage_id": stage.id,
                        "name": stage.name,
                        "included": False,
                        "basis": "excluded from the run",
                        "cost_usd": 0.0,
                        "seconds": 0.0,
                    }
                )
                continue
            if stage.kind is StageKind.HUMAN_APPROVAL:
                rows.append(
                    {
                        "stage_id": stage.id,
                        "name": stage.name,
                        "included": True,
                        "basis": "human approval - no model call, no bound on the wait",
                        "cost_usd": 0.0,
                        "seconds": None,
                        "blocks": True,
                    }
                )
                continue
            agent = self._resolve_agent(stage)
            model_id = stage.model or (agent.model if agent else None)
            resolved_note = ""
            if not model_id:
                # A stage with no explicit model resolves the configured
                # provider's default at run time. Price *that*, so the estimate
                # describes the run that would actually happen rather than
                # reporting "unpriced" for the normal keyless configuration.
                try:
                    _provider, model_id = await self.providers.resolve(
                        stage.provider or (agent.provider if agent else None), None
                    )
                    resolved_note = f"; model resolved at run time to {model_id}"
                except Exception as exc:  # noqa: BLE001 - report, do not fail
                    model_id = None
                    detail = getattr(exc, "message", None) or str(exc) or type(exc).__name__
                    resolved_note = f"; no model could be resolved for this stage: {detail}"
            sample = history.get(stage.id)
            if sample and sample["runs"]:
                basis = f"mean of {sample['runs']} previous run(s) of this stage"
            elif overall and overall["runs"]:
                sample = overall
                basis = f"pipeline-wide mean of {overall['runs']} stage execution(s)"
            else:
                sample = {
                    "input_tokens": ESTIMATE_FALLBACK_TOKENS[0],
                    "output_tokens": ESTIMATE_FALLBACK_TOKENS[1],
                    "seconds": ESTIMATE_FALLBACK_SECONDS,
                    "runs": 0,
                }
                basis = "no history yet - default token budget for one tool-using stage"
            info = models.get(str(model_id)) if model_id else None
            cost: float | None = None
            if info is not None and info.priced:
                cost = (
                    sample["input_tokens"] / 1_000_000.0 * float(info.input_cost_per_mtok or 0.0)
                    + sample["output_tokens"]
                    / 1_000_000.0
                    * float(info.output_cost_per_mtok or 0.0)
                )
                total_usd += cost
            else:
                priced = False
            seconds = float(sample["seconds"])
            total_seconds += seconds
            rows.append(
                {
                    "stage_id": stage.id,
                    "name": stage.name,
                    "included": True,
                    "model": model_id,
                    "provider": stage.provider or (agent.provider if agent else None),
                    "input_tokens": int(sample["input_tokens"]),
                    "output_tokens": int(sample["output_tokens"]),
                    "seconds": seconds,
                    "cost_usd": cost,
                    "priced": cost is not None,
                    "basis": basis
                    + resolved_note
                    + (
                        ""
                        if cost is not None
                        else f"; {model_id or 'no model'} has no published price, so it is "
                        "left out of the total"
                    ),
                }
            )
        included = [row for row in rows if row.get("included")]
        approvals = [row for row in included if row.get("blocks")]
        return {
            "pipeline_id": pipeline.id,
            "pipeline_version": pipeline.version,
            "stages": rows,
            "stage_count": len(included),
            "total_usd": round(total_usd, 4),
            "seconds": round(total_seconds, 1),
            "priced": priced,
            "blocks_on_approval": len(approvals),
            "basis": (
                "priced from this workspace's own recorded token usage"
                if any(row.get("input_tokens") and "previous" in str(row.get("basis")) for row in rows)
                else "no run history yet - default token budget per stage"
            ),
            "budget": self.spend(window="day"),
        }

    def _stage_history(self, pipeline_id: str) -> dict[str, dict[str, Any]]:
        """Return mean token usage and wall time per stage for one pipeline.

        Only stages that actually succeeded are averaged: a stage that failed
        halfway spent tokens but is not representative of what a run costs.
        """
        with self._session() as session:
            rows = list(
                session.execute(
                    select(AgentStageRunRow, AgentRunRow.pipeline_id)
                    .join(AgentRunRow, AgentRunRow.id == AgentStageRunRow.run_id)
                    .where(
                        AgentRunRow.pipeline_id == pipeline_id,
                        AgentStageRunRow.status == str(StageStatus.SUCCEEDED),
                    )
                )
            )
        buckets: dict[str, list[tuple[int, int, float]]] = {}
        for stage_row, _pipeline in rows:
            usage = dict(stage_row.usage or {})
            seconds = 0.0
            if stage_row.started_at and stage_row.finished_at:
                seconds = max(
                    0.0, (stage_row.finished_at - stage_row.started_at).total_seconds()
                )
            entry = (
                int(usage.get("input_tokens") or 0),
                int(usage.get("output_tokens") or 0),
                seconds,
            )
            buckets.setdefault(stage_row.stage_id, []).append(entry)
            buckets.setdefault("__pipeline__", []).append(entry)
        return {
            key: {
                "runs": len(entries),
                "input_tokens": sum(item[0] for item in entries) / len(entries),
                "output_tokens": sum(item[1] for item in entries) / len(entries),
                "seconds": sum(item[2] for item in entries) / len(entries),
            }
            for key, entries in buckets.items()
            if entries
        }

    async def cancel(self, run_id: str, *, reason: str = "cancelled by the user") -> Run:
        """Cancel a running pipeline.

        A run waiting on an approval is killed through its approval record so
        the pending request does not outlive it.
        """
        run = self.get(run_id, include_logs=False)
        if run.status in TERMINAL_RUN_STATES:
            return run
        for record in self.approvals(state=ApprovalState.PENDING, run_id=run_id):
            await self.reject(record.id, by="system", comment=reason)
            await self._settle(run_id)
            return self.get(run_id)
        task = self._tasks.get(run_id)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            return self.get(run_id)
        finished = self._finish(run_id, RunStatus.KILLED, error=reason)
        await self._publish(
            {"kind": "run", "phase": "killed", "run_id": run_id, "status": str(RunStatus.KILLED)}
        )
        return finished

    #: ``kill`` reads better in the API layer; it is the same operation.
    kill = cancel

    async def _settle(self, run_id: str, *, timeout: float = 10.0) -> None:
        """Wait briefly for a run's task to notice a decision and finish."""
        task = self._tasks.get(run_id)
        if task is None or task.done():
            return
        with contextlib.suppress(TimeoutError, asyncio.CancelledError, Exception):
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)

    async def drain(self, *, timeout: float = 30.0) -> None:
        """Wait for in-flight runs to finish. Used by the application shutdown."""
        tasks = [task for task in self._tasks.values() if not task.done()]
        if not tasks:
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True), timeout=timeout
            )

    # --- persistence helpers ---------------------------------------------
    def _update_run(self, run_id: str, **changes: Any) -> None:
        """Write run-level fields."""
        with self._session() as session:
            row = session.get(AgentRunRow, run_id)
            if row is None:
                raise KeyError(f"unknown run {run_id!r}")
            for key, value in changes.items():
                if key == "status":
                    row.status = str(value)
                elif key == "usage":
                    usage = value if isinstance(value, Usage) else Usage.from_dict(value)
                    row.usage = usage.as_dict()
                    row.cost_usd = usage.cost_usd
                else:
                    setattr(row, key, value)

    def _update_stage(self, run_id: str, stage_id: str, **changes: Any) -> None:
        """Write stage-level fields."""
        with self._session() as session:
            row = session.scalars(
                select(AgentStageRunRow).where(
                    AgentStageRunRow.run_id == run_id, AgentStageRunRow.stage_id == stage_id
                )
            ).first()
            if row is None:
                raise KeyError(f"run {run_id!r} has no stage {stage_id!r}")
            for key, value in changes.items():
                if key == "status":
                    row.status = str(value)
                elif key == "usage":
                    usage = value if isinstance(value, Usage) else Usage.from_dict(value)
                    row.usage = usage.as_dict()
                    row.cost_usd = usage.cost_usd
                else:
                    setattr(row, key, value)

    def _accumulate_run_usage(self, run_id: str, usage: Usage) -> None:
        """Add a stage's usage to the run total."""
        with self._session() as session:
            row = session.get(AgentRunRow, run_id)
            if row is None:
                raise KeyError(f"unknown run {run_id!r}")
            total = Usage.from_dict(row.usage) + usage
            row.usage = total.as_dict()
            row.cost_usd = total.cost_usd

    def _finish(self, run_id: str, status: RunStatus, *, error: str | None = None) -> Run:
        """Close a run in a terminal state and return it.

        A run that already reached a terminal state is left alone: a late
        cancellation of an abandoned task must not rewrite the history of a run
        that another worker already finished.
        """
        with self._session() as session:
            row = session.get(AgentRunRow, run_id)
            if row is None:
                raise KeyError(f"unknown run {run_id!r}")
            if RunStatus(row.status) in TERMINAL_RUN_STATES:
                logger.debug("run %s is already %s; leaving it alone", run_id, row.status)
                return self.get(run_id, include_logs=False)
            row.status = str(status)
            row.finished_at = utcnow()
            row.current_stage_id = None
            if error:
                row.error = error
            if status is not RunStatus.SUCCEEDED:
                # A killed run kills everything it had not finished. A failed
                # run leaves stages that never started as pending - that is what
                # happened - but a stage still marked running when the run died
                # did fail.
                for stage in session.scalars(
                    select(AgentStageRunRow).where(AgentStageRunRow.run_id == run_id)
                ):
                    if stage.status not in {str(StageStatus.PENDING), str(StageStatus.RUNNING)}:
                        continue
                    if status is RunStatus.KILLED:
                        stage.status = str(StageStatus.KILLED)
                        stage.finished_at = stage.finished_at or utcnow()
                    elif stage.status == str(StageStatus.RUNNING):
                        stage.status = str(StageStatus.FAILED)
                        stage.finished_at = stage.finished_at or utcnow()
                        stage.error = stage.error or error
        self._active.pop(run_id, None)
        return self.get(run_id, include_logs=False)

    def _store_artifact(
        self, run_id: str, stage_id: str, *, name: str, kind: str, data: Any
    ) -> Artifact:
        """Persist one artifact, spilling large payloads to a file."""
        identifier = f"ART-{uuid.uuid4().hex[:12]}"
        serialized = json.dumps(data, default=str)
        now = utcnow()
        path: str | None = None
        inline: Any = data
        if len(serialized) > MAX_INLINE_ARTIFACT_CHARS:
            directory = self.settings.artifacts_dir / run_id
            directory.mkdir(parents=True, exist_ok=True)
            file_path = directory / f"{identifier}.json"
            file_path.write_text(serialized, encoding="utf-8")
            path = str(file_path)
            inline = None
        with self._session() as session:
            session.add(
                AgentArtifactRow(
                    id=identifier,
                    run_id=run_id,
                    stage_id=stage_id,
                    name=name,
                    kind=kind,
                    path=path,
                    data=inline,
                    size_bytes=len(serialized),
                    created_at=now,
                )
            )
        return Artifact(
            id=identifier,
            run_id=run_id,
            stage_id=stage_id,
            name=name,
            kind=kind,
            path=path,
            data=inline,
            size_bytes=len(serialized),
            created_at=now,
        )

    # --- stage configuration ---------------------------------------------
    def _resolve_agent(self, stage: Stage) -> AgentDefinition | None:
        """Return the agent a stage is assigned to, if it names one.

        Raises:
            KeyError: when the stage names an agent that no longer exists.
        """
        if not stage.agent_id:
            return None
        return self.agents.get(stage.agent_id)

    @staticmethod
    def _stage_tools(stage: Stage, agent: AgentDefinition | None) -> list[str]:
        """Return the tool names this stage may call.

        The stage's own list, when set, narrows the agent's grant - a stage can
        take tools away from an agent but never add one it was not given.
        """
        granted = list(agent.tools) if agent else list(TOOL_NAMES)
        if stage.tools:
            return [name for name in stage.tools if name in granted and name in TOOL_SPECS]
        return [name for name in granted if name in TOOL_SPECS]

    @staticmethod
    def _project_cost(provider: LLMProvider, model: str, prompt: str, max_tokens: int) -> float:
        """Estimate a stage's cost before running it.

        Deliberately conservative: the whole prompt priced as input plus the
        full ``max_tokens`` priced as output. An unpriced model projects 0.0 -
        the budget then cannot protect that stage, which
        :meth:`SpendLedger.snapshot` reports as an unpriced entry rather than
        hiding.
        """
        price = lookup_price(provider.name, model)
        if price is None:
            return 0.0
        return price.cost(
            input_tokens=max(1, len(prompt) // 4),
            output_tokens=max_tokens,
        )


def _artifact_from_row(row: AgentArtifactRow) -> Artifact:
    """Map an artifact row onto the dataclass."""
    return Artifact(
        id=row.id,
        run_id=row.run_id,
        stage_id=row.stage_id,
        name=row.name,
        kind=row.kind,
        path=row.path,
        data=row.data,
        size_bytes=int(row.size_bytes),
        created_at=_aware(row.created_at),
    )


def _log_from_row(row: AgentRunLogRow) -> dict[str, Any]:
    """Map a log row onto its wire shape."""
    return {
        "ts": _iso(_aware(row.ts)),
        "level": row.level,
        "message": row.message,
        "stage_id": row.stage_id,
        "detail": dict(row.detail or {}),
    }


def _approval_from_row(row: AgentApprovalRow) -> ApprovalRecord:
    """Map an approval row onto the dataclass."""
    return ApprovalRecord(
        id=row.id,
        run_id=row.run_id,
        stage_id=row.stage_id,
        title=row.title,
        summary=row.summary,
        payload=dict(row.payload or {}),
        state=ApprovalState(row.state),
        requested_at=_aware(row.requested_at),
        decided_at=_aware(row.decided_at),
        decided_by=row.decided_by,
        comment=row.comment,
    )


_runner: PipelineRunner | None = None


def get_pipeline_runner() -> PipelineRunner:
    """Return the process-wide pipeline runner, creating it on first use."""
    global _runner
    if _runner is None:
        _runner = PipelineRunner()
    return _runner


def reset_pipeline_runner(runner: PipelineRunner | None = None) -> PipelineRunner:
    """Install (or forget) the process-wide pipeline runner. Returns it."""
    global _runner
    _runner = runner if runner is not None else PipelineRunner()
    return _runner


__all__ = [
    "ACTIVE_RUN_STATES",
    "APPROVALS_CHANNEL",
    "Artifact",
    "ApprovalRecord",
    "ApprovalState",
    "AuditSink",
    "BudgetExceeded",
    "LibraryToolset",
    "MAX_TOOL_ITERATIONS",
    "PipelineRunner",
    "RUNS_CHANNEL",
    "RUN_SOURCE",
    "Run",
    "RunStatus",
    "StageRun",
    "StageStatus",
    "TERMINAL_RUN_STATES",
    "TOOL_NAMES",
    "TOOL_SPECS",
    "build_stage_system_prompt",
    "build_stage_user_prompt",
    "ensure_schema",
    "extract_structured_output",
    "get_pipeline_runner",
    "new_run_id",
    "reset_pipeline_runner",
]
