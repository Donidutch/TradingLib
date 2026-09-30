"""Human-approval gates.

A ``human_approval`` stage blocks its run until a person approves or rejects it
in the Approval Review tab. Approval resumes the run; rejection terminates it.
Pending counts are published on the ``approvals`` channel.

The blocking itself lives in :class:`tradinglib.agents.runner.PipelineRunner`: the
stage persists an approval row, moves the run to ``awaiting_approval`` and waits
on an :class:`asyncio.Event`. This module is the **queue** in front of that
mechanism - it reads the same rows, joins each one to its run so the reviewer
sees what they are deciding about (pipeline, stage, contracts, upstream
outputs, cost so far), and routes every decision back through the runner so the
paused stage really is released. There is deliberately no second store: two
tables of approvals would mean two answers to "how many are pending".

When no runner can be reached (a maintenance script, an API process where the
runner subsystem is not mounted), the gate still records decisions directly on
the row and says so in the audit trail - the run will pick the decision up the
next time a runner reads it, because :meth:`PipelineRunner._wait_for_decision`
checks the persisted state before it blocks.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tradinglib.agents.pipeline import Stage
from tradinglib.agents.runner import (
    APPROVALS_CHANNEL,
    AgentApprovalRow,
    AgentRunRow,
    AgentStageRunRow,
    ApprovalRecord,
    ApprovalState,
    PipelineRunner,
    get_pipeline_runner,
    new_approval_id,
)
from tradinglib.agents.runner import ensure_schema as ensure_run_schema
from tradinglib.db import get_session_factory
from tradinglib.events import EventBus

logger = logging.getLogger(__name__)

#: The decision an approval stage asks a person for. The reference calls this
#: "approve only for paper research" - nothing here can place an order, and the
#: wording says so where the reviewer reads it.
DEFAULT_REQUESTED_ACTION: Final[str] = (
    "Approve this candidate for paper research, or reject it. Approval resumes "
    "the run; rejection terminates it. No live trading action exists."
)


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


# --------------------------------------------------------------------------
# workspace autonomy policy
# --------------------------------------------------------------------------
#: File the Configuration tab's autonomy/notification policy is stored in.
POLICY_FILENAME: Final[str] = "agent_policy.json"

#: What "Request more validation" may ask for. Every entry names evidence this
#: workspace can actually produce - a sweep, a walk-forward, a cost-sensitivity
#: re-run - so the ask is a work item rather than a wish.
VALIDATION_TARGETS: Final[dict[str, str]] = {
    "out_of_sample": "Out-of-sample window",
    "walk_forward": "Walk-forward folds",
    "parameter_sweep": "Parameter sweep (robustness)",
    "cost_sensitivity": "Cost sensitivity (2x costs)",
    "regime_robustness": "Regime robustness",
    "deflated_sharpe": "Deflated Sharpe against the trial ledger",
    "data_quality": "Data-quality review of the inputs",
    "independent_replication": "Independent replication by a second agent",
}

#: Choices offered by the two writable autonomy selects. ``ask_first`` makes the
#: matching tool refuse and log a guardrail block instead of acting.
AUTONOMY_CHOICES: Final[tuple[tuple[str, str], ...]] = (
    ("automatic", "Automatic within budget"),
    ("ask_first", "Ask first"),
)

#: Choices for the artifact-retention select.
RETENTION_CHOICES: Final[tuple[tuple[str, str], ...]] = (
    ("approved_indefinite", "Indefinite for approved research"),
    ("365_days", "365 days"),
)

#: Choices for each notification select, keyed by policy field.
NOTIFICATION_CHOICES: Final[dict[str, tuple[tuple[str, str], ...]]] = {
    "notify_approval_requests": (
        ("inbox_desktop", "Inbox + desktop"),
        ("inbox_only", "Inbox only"),
    ),
    "notify_failed_validation": (
        ("immediate", "Immediate"),
        ("daily_digest", "Daily digest"),
    ),
    "notify_stale_evidence": (
        ("24h", "At 24 hours"),
        ("72h", "At 72 hours"),
    ),
}


def _choice(value: Any, choices: Sequence[tuple[str, str]], field_name: str) -> str:
    """Validate ``value`` against ``choices``.

    Raises:
        ValueError: when the value is not one of the offered options - a select
            that silently keeps its old value is a lie about what was saved.
    """
    text = str(value).strip().lower().replace(" ", "_").replace("+", "")
    allowed = {key for key, _ in choices}
    if text in allowed:
        return text
    labels = {label.lower(): key for key, label in choices}
    if str(value).strip().lower() in labels:
        return labels[str(value).strip().lower()]
    raise ValueError(
        f"{field_name} must be one of {', '.join(sorted(allowed))}; got {value!r}"
    )


@dataclass(slots=True)
class AgentPolicy:
    """The workspace's autonomy, budget-boundary and notification policy.

    Every field here is *enforced* somewhere, which is the only reason it is
    offered as a control:

    * ``research_generation`` / ``sandbox_experiments`` gate the ``save_finding``
      and ``run_backtest`` tools - set to ``ask_first`` the tool refuses and the
      refusal is written to the audit log as a guardrail block;
    * ``candidate_disposition`` is read-only: a candidate can only leave the
      pipeline through a ``human_approval`` stage;
    * ``max_concurrent_runs`` is checked before a run is started;
    * ``compute_approval_usd`` turns an expensive stage into an approval request
      instead of a silent charge;
    * ``feature_publish_review`` keeps agent-written findings in ``new`` (the
      triage queue) rather than letting an agent promote its own claim;
    * the three ``notify_*`` fields select what is pushed on the ``notifications``
      WebSocket channel;
    * ``min_evidence_score`` is the confidence floor a finding must clear to be
      surfaced as actionable in the inbox.
    """

    research_generation: str = "automatic"
    sandbox_experiments: str = "automatic"
    candidate_disposition: str = "human_approval"
    max_concurrent_runs: int = 3
    artifact_retention: str = "approved_indefinite"
    compute_approval_usd: float = 10.0
    feature_publish_review: bool = True
    notify_approval_requests: str = "inbox_desktop"
    notify_failed_validation: str = "immediate"
    notify_stale_evidence: str = "24h"
    min_evidence_score: float = 0.70

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping."""
        return {
            "research_generation": self.research_generation,
            "sandbox_experiments": self.sandbox_experiments,
            "candidate_disposition": self.candidate_disposition,
            "max_concurrent_runs": int(self.max_concurrent_runs),
            "artifact_retention": self.artifact_retention,
            "compute_approval_usd": float(self.compute_approval_usd),
            "feature_publish_review": bool(self.feature_publish_review),
            "notify_approval_requests": self.notify_approval_requests,
            "notify_failed_validation": self.notify_failed_validation,
            "notify_stale_evidence": self.notify_stale_evidence,
            "min_evidence_score": float(self.min_evidence_score),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> AgentPolicy:
        """Rebuild (and validate) a policy from :meth:`as_dict` output."""
        data = dict(payload or {})
        base = cls()
        concurrency = int(data.get("max_concurrent_runs", base.max_concurrent_runs))
        if concurrency < 1:
            raise ValueError("max_concurrent_runs must be at least 1")
        threshold = float(data.get("compute_approval_usd", base.compute_approval_usd))
        if threshold < 0:
            raise ValueError("compute_approval_usd cannot be negative")
        score = float(data.get("min_evidence_score", base.min_evidence_score))
        if not 0.0 <= score <= 1.0:
            raise ValueError("min_evidence_score must be between 0 and 1")
        return cls(
            research_generation=_choice(
                data.get("research_generation", base.research_generation),
                AUTONOMY_CHOICES,
                "research_generation",
            ),
            sandbox_experiments=_choice(
                data.get("sandbox_experiments", base.sandbox_experiments),
                AUTONOMY_CHOICES,
                "sandbox_experiments",
            ),
            candidate_disposition="human_approval",
            max_concurrent_runs=concurrency,
            artifact_retention=_choice(
                data.get("artifact_retention", base.artifact_retention),
                RETENTION_CHOICES,
                "artifact_retention",
            ),
            compute_approval_usd=threshold,
            feature_publish_review=bool(
                data.get("feature_publish_review", base.feature_publish_review)
            ),
            notify_approval_requests=_choice(
                data.get("notify_approval_requests", base.notify_approval_requests),
                NOTIFICATION_CHOICES["notify_approval_requests"],
                "notify_approval_requests",
            ),
            notify_failed_validation=_choice(
                data.get("notify_failed_validation", base.notify_failed_validation),
                NOTIFICATION_CHOICES["notify_failed_validation"],
                "notify_failed_validation",
            ),
            notify_stale_evidence=_choice(
                data.get("notify_stale_evidence", base.notify_stale_evidence),
                NOTIFICATION_CHOICES["notify_stale_evidence"],
                "notify_stale_evidence",
            ),
            min_evidence_score=score,
        )

    def allows(self, capability: str) -> bool:
        """Whether ``capability`` may proceed without asking a person first.

        Args:
            capability: ``"research_generation"`` or ``"sandbox_experiments"``.
        """
        return getattr(self, capability, "automatic") == "automatic"

    def guardrails(self, *, blocked_today: int = 0) -> list[dict[str, Any]]:
        """Return the non-negotiable boundary table shown in the Audit tab.

        Every row names the mechanism that enforces it, so the panel can never
        drift into decoration: if a row has no enforcement point it does not
        belong in the list.
        """
        return [
            {
                "rule": "Self-approval",
                "value": "NEVER",
                "tone": "bad",
                "enforced_by": (
                    "ApprovalGate._decide refuses a decision whose actor is a "
                    "registered agent id"
                ),
            },
            {
                "rule": "Touch promotion gates",
                "value": "NEVER",
                "tone": "bad",
                "enforced_by": (
                    "no agent tool can write the gate suite - the thresholds "
                    "come from ValidationEngine and the toolset exposes no writer"
                ),
            },
            {
                "rule": "Modify trial ledger",
                "value": "NEVER",
                "tone": "bad",
                "enforced_by": (
                    "run_backtest only appends; no delete/update tool exists on "
                    "LibraryToolset"
                ),
            },
            {
                "rule": "Launch compute",
                "value": f"approval if > ${self.compute_approval_usd:,.2f}",
                "tone": "warn",
                "enforced_by": (
                    "PipelineRunner raises a human-approval request when a "
                    "stage's projected cost exceeds the threshold"
                ),
            },
            {
                "rule": "Feature publish",
                "value": "human review" if self.feature_publish_review else "automatic",
                "tone": "warn" if self.feature_publish_review else "ok",
                "enforced_by": (
                    "agent-written findings are created in the 'new' triage "
                    "state; only a person promotes them"
                ),
            },
            {
                "rule": "Read market data",
                "value": "scoped to the run's universe",
                "tone": "ok",
                "enforced_by": (
                    "LibraryToolset filters every data tool by the stage scope; "
                    "this workspace is single-user, so there are no per-seat "
                    "entitlements to apply on top"
                ),
            },
            {
                "rule": "Blocked attempts",
                "value": f"always logged + surfaced ({blocked_today} today)",
                "tone": "hi",
                "enforced_by": "AuditLog records every guardrail refusal",
            },
        ]


class GuardrailBlocked(PermissionError):
    """Raised when the autonomy policy refuses an agent action.

    It is a :class:`PermissionError` so the API layer already maps it to 409 -
    the request was well formed, the policy says no.
    """

    def __init__(self, capability: str, detail: str) -> None:
        """Record which capability was refused and why."""
        super().__init__(detail)
        self.capability = capability
        self.detail = detail


def policy_file(data_dir: Path | None = None) -> Path:
    """Return the path the workspace policy is stored in."""
    from tradinglib.config import get_settings

    root = data_dir if data_dir is not None else get_settings().data_dir
    return root / POLICY_FILENAME


def load_policy(data_dir: Path | None = None) -> AgentPolicy:
    """Read the saved policy, falling back to the documented defaults.

    An unreadable file is logged and ignored rather than silently replaced, so
    a corrupted policy never quietly loosens a boundary.
    """
    path = policy_file(data_dir)
    if not path.is_file():
        return AgentPolicy()
    try:
        return AgentPolicy.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        logger.warning("saved agent policy at %s is unreadable", path, exc_info=True)
        return AgentPolicy()


def save_policy(policy: AgentPolicy, data_dir: Path | None = None) -> Path:
    """Persist the policy so a restart keeps what the Configuration tab set."""
    path = policy_file(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(policy.as_dict(), indent=2) + "\n", encoding="utf-8")
    return path


_policy_cache: AgentPolicy | None = None


def get_policy(*, refresh: bool = False) -> AgentPolicy:
    """Return the process-wide policy, reading it from disk on first use."""
    global _policy_cache
    if _policy_cache is None or refresh:
        _policy_cache = load_policy()
    return _policy_cache


def set_policy(policy: AgentPolicy, data_dir: Path | None = None) -> AgentPolicy:
    """Persist ``policy`` and make it the process-wide one."""
    global _policy_cache
    save_policy(policy, data_dir)
    _policy_cache = policy
    return policy


@dataclass(slots=True)
class ApprovalRequest:
    """A blocked stage waiting on a human decision, with its run context."""

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
    # --- joined run context (empty when the run row is gone) --------------
    run_name: str | None = None
    run_status: str | None = None
    pipeline_id: str | None = None
    pipeline_version: int | None = None
    stage_name: str | None = None
    agent_id: str | None = None
    objective: str | None = None
    requested_action: str = DEFAULT_REQUESTED_ACTION
    contract: dict[str, Any] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)

    @property
    def pending(self) -> bool:
        """Whether this request is still waiting on a person."""
        return self.state is ApprovalState.PENDING

    def age_seconds(self, *, now: datetime | None = None) -> float | None:
        """How long the request has been waiting (or took to decide)."""
        if self.requested_at is None:
            return None
        end = self.decided_at or now or utcnow()
        return max(0.0, (end - self.requested_at).total_seconds())

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping for the API layer."""
        return {
            "id": self.id,
            "run_id": self.run_id,
            "stage_id": self.stage_id,
            "title": self.title,
            "summary": self.summary,
            "payload": dict(self.payload),
            "state": str(self.state),
            "pending": self.pending,
            "requested_at": _iso(self.requested_at),
            "decided_at": _iso(self.decided_at),
            "decided_by": self.decided_by,
            "comment": self.comment,
            "age_seconds": self.age_seconds(),
            "run_name": self.run_name,
            "run_status": self.run_status,
            "pipeline_id": self.pipeline_id,
            "pipeline_version": self.pipeline_version,
            "stage_name": self.stage_name,
            "agent_id": self.agent_id,
            "objective": self.objective,
            "requested_action": self.requested_action,
            "contract": dict(self.contract),
            "context": dict(self.context),
        }


class ApprovalGate:
    """Creates approval requests and blocks runs until they are decided."""

    #: How often :meth:`wait` re-reads the persisted state, in seconds.
    POLL_INTERVAL: Final[float] = 0.05

    def __init__(
        self,
        bus: EventBus | None = None,
        *,
        runner: PipelineRunner | None = None,
        session_factory: sessionmaker[Session] | None = None,
        audit: Any | None = None,
    ) -> None:
        """Create the gate bound to an event bus.

        Args:
            bus: bus the pending count is published on. Defaults to the
                process-wide bus at first use.
            runner: the runner whose paused stages this gate releases. Defaults
                to the process-wide runner, resolved lazily so a gate can exist
                in a process that never mounts one.
            session_factory: SQLAlchemy session factory; defaults to the
                process-wide one from :mod:`tradinglib.db`.
            audit: audit sink for decisions the gate records itself. Defaults to
                the process-wide audit log.
        """
        self._bus = bus
        self._runner = runner
        self._runner_resolved = runner is not None
        self._session_factory = session_factory
        self._audit = audit
        self._audit_resolved = audit is not None
        # The approval rows belong to the runner's schema; create them here too
        # so a gate in a process that never built a runner can still read them.
        ensure_run_schema()

    # --- plumbing ---------------------------------------------------------
    @property
    def bus(self) -> EventBus:
        """The event bus pending counts are published on."""
        if self._bus is None:
            from tradinglib.events import get_event_bus

            self._bus = get_event_bus()
        return self._bus

    @property
    def runner(self) -> PipelineRunner | None:
        """The runner whose stages this gate releases, when one is reachable."""
        if not self._runner_resolved:
            self._runner_resolved = True
            try:
                self._runner = get_pipeline_runner()
            except Exception:  # noqa: BLE001 - a gate without a runner still records
                logger.warning("no pipeline runner is reachable from this process", exc_info=True)
                self._runner = None
        return self._runner

    @property
    def audit(self) -> Any | None:
        """The audit log the gate writes to when it decides without a runner."""
        if not self._audit_resolved:
            self._audit_resolved = True
            try:
                from tradinglib.agents.audit import get_audit_log

                self._audit = get_audit_log()
            except Exception:  # noqa: BLE001 - auditing must not break a decision
                logger.warning("audit log unavailable to the approval gate", exc_info=True)
                self._audit = None
        return self._audit

    @contextmanager
    def _session(self) -> Iterator[Session]:
        """Transactional session scope bound to this gate."""
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

    async def _publish(self, phase: str, request: ApprovalRequest | None = None) -> None:
        """Publish the queue state on the ``approvals`` channel."""
        payload: dict[str, Any] = {
            "kind": "approval",
            "source": "approvals",
            "phase": phase,
            "pending": self.pending_count(),
            "ts": utcnow().isoformat(),
        }
        if request is not None:
            payload["approval"] = request.as_dict()
        try:
            await self.bus.publish(APPROVALS_CHANNEL, payload)
        except Exception:  # noqa: BLE001 - the decision is not the notification
            logger.warning("publishing the approval queue failed", exc_info=True)

    # --- row mapping ------------------------------------------------------
    @staticmethod
    def _from_row(row: AgentApprovalRow) -> ApprovalRequest:
        """Map an approval row onto the queue dataclass (without run context)."""
        return ApprovalRequest(
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

    @staticmethod
    def _from_record(record: ApprovalRecord) -> ApprovalRequest:
        """Map the runner's approval record onto the queue dataclass."""
        return ApprovalRequest(
            id=record.id,
            run_id=record.run_id,
            stage_id=record.stage_id,
            title=record.title,
            summary=record.summary,
            payload=dict(record.payload),
            state=record.state,
            requested_at=record.requested_at,
            decided_at=record.decided_at,
            decided_by=record.decided_by,
            comment=record.comment,
        )

    def _enrich(self, request: ApprovalRequest) -> ApprovalRequest:
        """Join a request to its run so the reviewer sees what they decide.

        Everything added here is read from the run's own rows and from the
        pipeline document the run froze when it started: nothing is inferred and
        nothing is filled in when the run row has gone.
        """
        with self._session() as session:
            run = session.get(AgentRunRow, request.run_id)
            if run is None:
                return request
            stage_rows = list(
                session.scalars(
                    select(AgentStageRunRow)
                    .where(AgentStageRunRow.run_id == request.run_id)
                    .order_by(AgentStageRunRow.idx.asc())
                )
            )
            document = dict(run.document or {})
            run_name = run.name
            run_status = run.status
            pipeline_id = run.pipeline_id
            pipeline_version = int(run.pipeline_version)
            run_objective = run.objective
            run_cost = float(run.cost_usd)
            run_inputs = dict(run.inputs or {})
            run_outputs = dict(run.outputs or {})

        stage = self._document_stage(document, request.stage_id)
        stage_row = next((row for row in stage_rows if row.stage_id == request.stage_id), None)
        upstream = {
            row.stage_id: dict(row.outputs or {})
            for row in stage_rows
            if row.outputs and row.stage_id != request.stage_id
        }
        request.run_name = run_name
        request.run_status = run_status
        request.pipeline_id = pipeline_id
        request.pipeline_version = pipeline_version
        request.stage_name = stage.name if stage else (stage_row.name if stage_row else None)
        request.agent_id = stage.agent_id if stage else (stage_row.agent_id if stage_row else None)
        request.objective = (stage.objective if stage else None) or run_objective
        request.contract = self._contract_summary(stage)
        request.context = {
            "run": {
                "id": request.run_id,
                "name": run_name,
                "status": run_status,
                "pipeline_id": pipeline_id,
                "pipeline_version": pipeline_version,
                "objective": run_objective,
                "cost_usd": round(run_cost, 6),
                "inputs": run_inputs,
                "outputs": run_outputs,
            },
            "stages": [
                {
                    "stage_id": row.stage_id,
                    "name": row.name,
                    "status": row.status,
                    "agent_id": row.agent_id,
                    "model": row.model,
                    "cost_usd": round(float(row.cost_usd), 6),
                }
                for row in stage_rows
            ],
            "upstream_outputs": upstream,
        }
        return request

    @staticmethod
    def _document_stage(document: Mapping[str, Any], stage_id: str) -> Stage | None:
        """Return the frozen stage definition from a run document."""
        for payload in document.get("stages") or []:
            if isinstance(payload, Mapping) and payload.get("id") == stage_id:
                try:
                    return Stage.from_dict(payload)
                except Exception:  # noqa: BLE001 - a malformed document is not fatal
                    logger.debug("stage %s could not be read back", stage_id, exc_info=True)
                    return None
        return None

    @staticmethod
    def _contract_summary(stage: Stage | None) -> dict[str, Any]:
        """Summarise the stage's contracts for the review panel."""
        if stage is None:
            return {}
        return {
            "inputs": stage.inputs.as_dict(),
            "outputs": stage.outputs.as_dict(),
            "handoff": stage.handoff,
            "handoff_to": list(stage.handoff_to),
            "pass_condition": stage.pass_condition,
            "fail_condition": stage.fail_condition,
            "stop_condition": stage.stop_condition,
            "locked": stage.locked,
        }

    # --- creation ---------------------------------------------------------
    async def request(
        self, run_id: str, stage_id: str, title: str, summary: str, **payload: Any
    ) -> ApprovalRequest:
        """Create a pending request and publish it on ``approvals``.

        The runner creates its own record when a ``human_approval`` stage is
        reached; this method exists for the callers that stop a run from
        outside it (an operator holding a run for review, a guardrail).

        Args:
            run_id: the run being held.
            stage_id: the stage the run stops at.
            title: headline shown in the queue.
            summary: what the reviewer is being asked to decide.
            **payload: extra context stored with the request. ``resume_index``
                names the stage index a runner should continue from.

        Raises:
            ValueError: when the run or stage identifier is empty.
        """
        if not str(run_id).strip() or not str(stage_id).strip():
            raise ValueError("an approval request needs a run id and a stage id")
        body = dict(payload)
        resume_index = int(body.pop("resume_index", 0) or 0)
        identifier = new_approval_id()
        now = utcnow()
        with self._session() as session:
            session.add(
                AgentApprovalRow(
                    id=identifier,
                    run_id=run_id,
                    stage_id=stage_id,
                    title=str(title).strip() or f"{stage_id} - approval required",
                    summary=str(summary or "").strip(),
                    payload=body,
                    state=str(ApprovalState.PENDING),
                    resume_index=resume_index,
                    requested_at=now,
                )
            )
        request = self.get(identifier)
        self._record_audit(
            "approval gate",
            "requested human approval",
            target=identifier,
            run_id=run_id,
            stage_id=stage_id,
            title=request.title,
        )
        await self._publish("requested", request)
        return request

    # --- waiting ----------------------------------------------------------
    async def wait(self, approval_id: str, *, timeout: float | None = None) -> ApprovalRequest:
        """Block until the request is decided (or the timeout expires).

        The decision is read from the database rather than from an in-memory
        event, so this works across processes: an API worker can wait on a
        request another worker decides.

        Raises:
            KeyError: when the identifier is unknown.
            TimeoutError: when ``timeout`` passes with the request still
                pending. The request stays pending - a run that is genuinely
                blocked is not "expired" by nobody looking at it.
        """
        deadline = None if timeout is None else asyncio.get_running_loop().time() + timeout
        while True:
            request = self.get(approval_id)
            if not request.pending:
                return request
            if deadline is not None and asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(
                    f"approval {approval_id!r} was still pending after {timeout:g}s"
                )
            await asyncio.sleep(self.POLL_INTERVAL)

    # --- decisions --------------------------------------------------------
    async def approve(
        self, approval_id: str, *, by: str = "user", comment: str | None = None
    ) -> ApprovalRequest:
        """Approve a request, unblocking its run.

        Raises:
            KeyError: when the identifier is unknown.
            ValueError: when the request was already decided.
        """
        return await self._decide(approval_id, ApprovalState.APPROVED, by=by, comment=comment)

    async def reject(
        self, approval_id: str, *, by: str = "user", comment: str | None = None
    ) -> ApprovalRequest:
        """Reject a request, terminating its run.

        Raises:
            KeyError: when the identifier is unknown.
            ValueError: when the request was already decided.
        """
        return await self._decide(approval_id, ApprovalState.REJECTED, by=by, comment=comment)

    async def request_validation(
        self,
        approval_id: str,
        target: str,
        *,
        by: str = "user",
        comment: str | None = None,
    ) -> ApprovalRequest:
        """Ask for more evidence instead of deciding (*Request more validation*).

        This is deliberately **not** a third outcome: the request stays pending
        and its run stays blocked, because "I am not convinced yet" is not a
        decision. What it does is record, on the approval itself and in the
        audit log, which further validation the reviewer wants before they will
        decide - so the ask is answerable rather than a comment nobody actions.

        Args:
            approval_id: the pending request.
            target: what to validate further; one of :data:`VALIDATION_TARGETS`.
            by: who is asking.
            comment: free-text detail shown alongside the ask.

        Returns:
            The (still pending) request, with the ask appended to
            ``payload["validation_requests"]``.

        Raises:
            KeyError: when the identifier is unknown.
            ValueError: when the request was already decided, or the target is
                not one this workspace can actually produce.
        """
        key = str(target or "").strip().lower().replace("-", "_").replace(" ", "_")
        if key not in VALIDATION_TARGETS:
            raise ValueError(
                f"unknown validation target {target!r}; expected one of: "
                + ", ".join(sorted(VALIDATION_TARGETS))
            )
        current = self.get(approval_id)
        if not current.pending:
            raise ValueError(
                f"approval {approval_id!r} was already {current.state} "
                f"at {_iso(current.decided_at)}; there is nothing left to validate for it"
            )
        entry = {
            "target": key,
            "label": VALIDATION_TARGETS[key],
            "requested_by": by,
            "comment": comment,
            "requested_at": _iso(utcnow()),
        }
        with self._session() as session:
            row = session.get(AgentApprovalRow, approval_id)
            if row is None:  # pragma: no cover - get() already proved it exists
                raise KeyError(f"unknown approval {approval_id!r}")
            payload = dict(row.payload or {})
            asks = list(payload.get("validation_requests") or [])
            asks.append(entry)
            payload["validation_requests"] = asks
            row.payload = payload
        self._record_audit(
            by,
            "requested more validation",
            target=approval_id,
            run_id=current.run_id,
            stage_id=current.stage_id,
            result="pending",
            comment=comment,
            validation_target=key,
        )
        request = self.get(approval_id)
        await self._publish("validation-requested", request)
        return request

    async def _decide(
        self, approval_id: str, state: ApprovalState, *, by: str, comment: str | None
    ) -> ApprovalRequest:
        """Route a decision through the runner, or record it when there is none."""
        self._refuse_self_approval(approval_id, by)
        current = self.get(approval_id)
        if not current.pending:
            raise ValueError(
                f"approval {approval_id!r} was already {current.state} "
                f"at {_iso(current.decided_at)}"
            )
        # A request the gate raised for something that is not a runner run has
        # no paused stage to release; deciding it through the runner would only
        # make the runner look for a run that was never there.
        runner = self.runner if self._run_exists(current.run_id) else None
        if runner is not None:
            decide = runner.approve if state is ApprovalState.APPROVED else runner.reject
            record = await decide(approval_id, by=by, comment=comment)
            request = self._enrich(self._from_record(record))
        else:
            request = self._decide_without_runner(approval_id, state, by=by, comment=comment)
            self._record_audit(
                by,
                f"approval {state}",
                target=approval_id,
                run_id=request.run_id,
                stage_id=request.stage_id,
                comment=comment,
                result=str(state),
                note="recorded without a runner in this process",
            )
        await self._publish(str(state), request)
        return request

    def _refuse_self_approval(self, approval_id: str, by: str) -> None:
        """Enforce the "self-approval NEVER" guardrail.

        An approval decided by a registered agent (or by an ``agent:`` prefixed
        actor) is refused and the attempt is written to the audit log, which is
        what makes the guardrail row in the Audit tab a statement of fact.

        Raises:
            GuardrailBlocked: when the actor is an agent rather than a person.
        """
        actor = (by or "").strip()
        identifier = actor.split(":", 1)[1] if actor.lower().startswith("agent:") else actor
        is_agent = actor.lower().startswith("agent:")
        if not is_agent and identifier:
            try:
                from tradinglib.agents.registry import get_agent_registry

                get_agent_registry().get(identifier)
                is_agent = True
            except Exception:  # noqa: BLE001 - an unknown id is simply a person
                is_agent = False
        if not is_agent:
            return
        detail = (
            f"{actor} is an agent; approvals are a human boundary and an agent "
            "may never release its own run"
        )
        self._record_audit(
            actor,
            "guardrail block",
            target=approval_id,
            capability="self_approval",
            result="blocked",
            detail=detail,
        )
        raise GuardrailBlocked("self_approval", detail)

    def _run_exists(self, run_id: str) -> bool:
        """Whether a pipeline run with this identifier is recorded."""
        with self._session() as session:
            return session.get(AgentRunRow, run_id) is not None

    def _decide_without_runner(
        self, approval_id: str, state: ApprovalState, *, by: str, comment: str | None
    ) -> ApprovalRequest:
        """Write the decision straight onto the row (no runner in this process)."""
        with self._session() as session:
            row = session.get(AgentApprovalRow, approval_id)
            if row is None:
                raise KeyError(f"unknown approval {approval_id!r}")
            if row.state != str(ApprovalState.PENDING):
                raise ValueError(f"approval {approval_id!r} was already {row.state}")
            row.state = str(state)
            row.decided_by = by
            row.comment = comment
            row.decided_at = utcnow()
        logger.info(
            "approval %s %s by %s recorded without a runner; the run resumes when one reads it",
            approval_id,
            state,
            by,
        )
        return self.get(approval_id)

    def _record_audit(self, actor: str, action: str, **fields: Any) -> None:
        """Append an audit record, tolerating an audit store that is not up."""
        log = self.audit
        if log is None:
            return
        try:
            log.record(actor, action, **fields)
        except Exception:  # noqa: BLE001 - auditing must never break a decision
            logger.warning("audit record failed for %s/%s", actor, action, exc_info=True)

    # --- reads ------------------------------------------------------------
    def get(self, approval_id: str) -> ApprovalRequest:
        """Return one request, joined to its run.

        Raises:
            KeyError: when the identifier is unknown.
        """
        with self._session() as session:
            row = session.get(AgentApprovalRow, approval_id)
            if row is None:
                raise KeyError(f"unknown approval {approval_id!r}")
            request = self._from_row(row)
        return self._enrich(request)

    def list(
        self,
        *,
        state: ApprovalState | str | None = None,
        run_id: str | None = None,
        limit: int = 100,
        enrich: bool = True,
    ) -> list[ApprovalRequest]:
        """Return approval requests, newest first.

        Args:
            state: filter by decision state.
            run_id: filter by run.
            limit: maximum number of requests to return.
            enrich: join each request to its run. Turning it off keeps a long
                history listing cheap.
        """
        statement = select(AgentApprovalRow).order_by(AgentApprovalRow.requested_at.desc())
        if state is not None:
            statement = statement.where(AgentApprovalRow.state == str(ApprovalState(str(state))))
        if run_id is not None:
            statement = statement.where(AgentApprovalRow.run_id == run_id)
        statement = statement.limit(max(1, int(limit)))
        with self._session() as session:
            requests = [self._from_row(row) for row in session.scalars(statement)]
        return [self._enrich(request) for request in requests] if enrich else requests

    def queue(self, *, limit: int = 50) -> list[ApprovalRequest]:
        """Return the pending queue, oldest first - what the reviewer works through."""
        pending = self.list(state=ApprovalState.PENDING, limit=limit)
        pending.sort(key=lambda item: item.requested_at or utcnow())
        return pending

    def pending_count(self) -> int:
        """Number of requests still awaiting a decision."""
        with self._session() as session:
            rows = list(
                session.scalars(
                    select(AgentApprovalRow.id).where(
                        AgentApprovalRow.state == str(ApprovalState.PENDING)
                    )
                )
            )
        return len(rows)

    def counts(self) -> dict[str, int]:
        """Return counts by state for the Approval Review header."""
        with self._session() as session:
            rows = list(session.scalars(select(AgentApprovalRow.state)))
        counts = {str(state): 0 for state in ApprovalState}
        for value in rows:
            counts[str(value)] = counts.get(str(value), 0) + 1
        counts["total"] = len(rows)
        return counts

    async def publish_pending(self) -> int:
        """Publish the current pending count and return it.

        Called by the application when a client connects, so a browser that
        opens mid-flight sees the real queue rather than waiting for the next
        decision to tell it.
        """
        count = self.pending_count()
        await self._publish("queue")
        return count


_gate: ApprovalGate | None = None


def get_approval_gate() -> ApprovalGate:
    """Return the process-wide approval gate, creating it on first use."""
    global _gate
    if _gate is None:
        _gate = ApprovalGate()
    return _gate


def reset_approval_gate(gate: ApprovalGate | None = None) -> ApprovalGate:
    """Install (or forget) the process-wide approval gate. Returns it."""
    global _gate
    _gate = gate if gate is not None else ApprovalGate()
    return _gate


__all__ = [
    "APPROVALS_CHANNEL",
    "DEFAULT_REQUESTED_ACTION",
    "ApprovalGate",
    "ApprovalRequest",
    "ApprovalState",
    "get_approval_gate",
    "reset_approval_gate",
]
