"""Pipelines: staged agent DAGs with input/output contracts and versions.

A pipeline document is what the Pipeline Builder edits: an ordered list of
stages, each one carrying the agent it is assigned to, an optional model
override, the scope it works in, its objective, whether it is included in a run,
its input and output contracts and its handoff condition.

Two things about a pipeline are stateful and matter to the runner:

* **versions.** Editing a pipeline changes the working document and marks it
  ``dirty``; :meth:`PipelineRegistry.save_version` freezes the current document
  as version *n* and starts version *n+1*. A run records the version it
  executed, and :meth:`PipelineRegistry.get` can fetch any frozen version, so an
  old run can always be read against the configuration that produced it.
* **locking.** A pipeline that has been approved is ``locked``: editing it
  raises :class:`PermissionError` (the API turns that into ``409``), and the
  way forward is to duplicate it or unlock it deliberately. The human-approval
  stage is itself immutable inside any pipeline - it cannot be removed, disabled
  or re-pointed at a model, because that stage is the reason automation stops.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text, delete, select
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from tradinglib.db import Base, get_engine, get_session_factory
from tradinglib.events import EventBus

logger = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def utcnow() -> datetime:
    """Return the current UTC time as an aware datetime."""
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    """Attach UTC to a naive datetime read back from SQLite."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def slugify(value: str, *, fallback: str = "pipeline") -> str:
    """Return a lowercase, dash-separated identifier for ``value``."""
    slug = _SLUG_RE.sub("-", value.strip().lower()).strip("-")
    return slug or fallback


class StageKind(StrEnum):
    """The kinds of stage a pipeline can contain."""

    AGENT = "agent"
    TOOL = "tool"
    HUMAN_APPROVAL = "human_approval"
    BRANCH = "branch"


class PipelineStatus(StrEnum):
    """Lifecycle of a pipeline document."""

    DRAFT = "draft"
    ACTIVE = "active"
    LOCKED = "locked"
    ARCHIVED = "archived"


@dataclass(slots=True)
class IOContract:
    """Declared inputs/outputs of a stage, validated at handoff time.

    ``text`` is the human contract the builder shows ("Ranked candidates with
    reason codes"). ``required`` is the machine half: keys the stage's structured
    output must contain for the handoff to be considered satisfied. Both are
    optional, and a contract with neither is simply undeclared rather than
    silently "passing".
    """

    text: str | None = None
    schema: dict[str, Any] = field(default_factory=dict)
    required: list[str] = field(default_factory=list)
    description: str | None = None

    @property
    def declared(self) -> bool:
        """Whether this contract says anything at all."""
        return bool(self.text or self.schema or self.required)

    def check(self, payload: Mapping[str, Any] | None) -> list[str]:
        """Return the required keys that ``payload`` is missing."""
        if not self.required:
            return []
        data = payload or {}
        return [key for key in self.required if key not in data or data[key] in (None, "")]

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of the contract."""
        return {
            "text": self.text,
            "schema": dict(self.schema),
            "required": list(self.required),
            "description": self.description,
            "declared": self.declared,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | str | None) -> IOContract:
        """Rebuild a contract from stored JSON (or from plain text)."""
        if payload is None:
            return cls()
        if isinstance(payload, str):
            return cls(text=payload or None)
        return cls(
            text=payload.get("text") or None,
            schema=dict(payload.get("schema") or {}),
            required=[str(key) for key in payload.get("required") or []],
            description=payload.get("description"),
        )


@dataclass(slots=True)
class Stage:
    """One pipeline stage as configured in the Pipeline builder."""

    id: str
    name: str
    kind: StageKind = StageKind.AGENT
    agent_id: str | None = None
    provider: str | None = None
    model: str | None = None
    objective: str | None = None
    scope: dict[str, Any] = field(default_factory=dict)
    include_in_run: bool = True
    inputs: IOContract = field(default_factory=IOContract)
    outputs: IOContract = field(default_factory=IOContract)
    handoff: str | None = None
    handoff_to: list[str] = field(default_factory=list)
    pass_condition: str | None = None
    fail_condition: str | None = None
    stop_condition: str | None = None
    tools: list[str] = field(default_factory=list)
    budget_usd: float | None = None
    timeout_s: float | None = None
    retries: int = 0
    locked: bool = False
    config: dict[str, Any] = field(default_factory=dict)

    @property
    def is_approval(self) -> bool:
        """Whether this stage stops the run for a human decision."""
        return self.kind is StageKind.HUMAN_APPROVAL

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of the stage."""
        return {
            "id": self.id,
            "name": self.name,
            "kind": str(self.kind),
            "agent_id": self.agent_id,
            "provider": self.provider,
            "model": self.model,
            "objective": self.objective,
            "scope": dict(self.scope),
            "include_in_run": self.include_in_run,
            "inputs": self.inputs.as_dict(),
            "outputs": self.outputs.as_dict(),
            "handoff": self.handoff,
            "handoff_to": list(self.handoff_to),
            "pass_condition": self.pass_condition,
            "fail_condition": self.fail_condition,
            "stop_condition": self.stop_condition,
            "tools": list(self.tools),
            "budget_usd": self.budget_usd,
            "timeout_s": self.timeout_s,
            "retries": self.retries,
            "locked": self.locked,
            "config": dict(self.config),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Stage:
        """Rebuild a stage from :meth:`as_dict` output."""
        name = str(payload.get("name") or payload.get("id") or "stage").strip()
        return cls(
            id=str(payload.get("id") or slugify(name, fallback="stage")),
            name=name,
            kind=StageKind(str(payload.get("kind", StageKind.AGENT))),
            agent_id=payload.get("agent_id") or None,
            provider=payload.get("provider") or None,
            model=payload.get("model") or None,
            objective=payload.get("objective") or None,
            scope=dict(payload.get("scope") or {}),
            include_in_run=bool(payload.get("include_in_run", True)),
            inputs=IOContract.from_dict(payload.get("inputs")),
            outputs=IOContract.from_dict(payload.get("outputs")),
            handoff=payload.get("handoff") or None,
            handoff_to=[str(target) for target in payload.get("handoff_to") or []],
            pass_condition=payload.get("pass_condition") or None,
            fail_condition=payload.get("fail_condition") or None,
            stop_condition=payload.get("stop_condition") or None,
            tools=[str(tool) for tool in payload.get("tools") or []],
            budget_usd=(
                None if payload.get("budget_usd") in (None, "") else float(payload["budget_usd"])
            ),
            timeout_s=(
                None if payload.get("timeout_s") in (None, "") else float(payload["timeout_s"])
            ),
            retries=int(payload.get("retries") or 0),
            locked=bool(payload.get("locked", False)),
            config=dict(payload.get("config") or {}),
        )


@dataclass(slots=True)
class Pipeline:
    """A versioned, ordered set of stages."""

    id: str
    name: str
    version: int = 1
    status: PipelineStatus = PipelineStatus.DRAFT
    objective: str | None = None
    description: str | None = None
    stages: list[Stage] = field(default_factory=list)
    locked: bool = False
    dirty: bool = False
    tags: list[str] = field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def enabled_stages(self) -> list[Stage]:
        """Stages marked include-in-run, in execution order."""
        return [stage for stage in self.stages if stage.include_in_run]

    def stage(self, stage_id: str) -> Stage:
        """Return one stage.

        Raises:
            KeyError: when the stage id is unknown.
        """
        for stage in self.stages:
            if stage.id == stage_id:
                return stage
        raise KeyError(f"unknown stage {stage_id!r} in pipeline {self.id!r}")

    def as_dict(self, *, include_stages: bool = True) -> dict[str, Any]:
        """Return a JSON-serialisable mapping for the API layer."""
        payload: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "status": str(self.status),
            "objective": self.objective,
            "description": self.description,
            "locked": self.locked,
            "dirty": self.dirty,
            "tags": list(self.tags),
            "stage_count": len(self.stages),
            "enabled_stage_count": len(self.enabled_stages()),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_stages:
            payload["stages"] = [stage.as_dict() for stage in self.stages]
        return payload


class PipelineRow(Base):
    """SQLite row for the working document of one pipeline."""

    __tablename__ = "agent_pipelines"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    version: Mapped[int] = mapped_column(Integer(), nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=PipelineStatus.DRAFT)
    objective: Mapped[str | None] = mapped_column(Text(), default=None)
    description: Mapped[str | None] = mapped_column(Text(), default=None)
    stages: Mapped[list[dict[str, Any]]] = mapped_column(JSON(), nullable=False, default=list)
    locked: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    dirty: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    tags: Mapped[list[str]] = mapped_column(JSON(), nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)


class PipelineVersionRow(Base):
    """SQLite row for one frozen version of a pipeline."""

    __tablename__ = "agent_pipeline_versions"

    id: Mapped[int] = mapped_column(Integer(), primary_key=True, autoincrement=True)
    pipeline_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer(), nullable=False)
    document: Mapped[dict[str, Any]] = mapped_column(JSON(), nullable=False, default=dict)
    note: Mapped[str | None] = mapped_column(Text(), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)


_OWNED_TABLES = (PipelineRow.__table__, PipelineVersionRow.__table__)


def ensure_schema() -> None:
    """Create the pipeline tables if they do not exist yet."""
    Base.metadata.create_all(get_engine(), tables=list(_OWNED_TABLES))


#: The research cycle TradingLib ships with, matching the reference builder's six
#: stages. Agent ids refer to :data:`tradinglib.agents.registry.DEFAULT_AGENTS`.
DEFAULT_PIPELINE_STAGES: Final[tuple[dict[str, Any], ...]] = (
    {
        "id": "scanner",
        "name": "Scanner",
        "agent_id": "scanner-agent",
        "objective": (
            "Scan the selected universe for liquid companies with improving fundamental "
            "and catalyst signals."
        ),
        "inputs": {"text": "Universe definition, liquidity floor, catalyst window"},
        "outputs": {
            "text": "Ranked ticker candidates with reason codes",
            "required": ["candidates"],
        },
        "handoff": "Send the top candidates to Research",
        "pass_condition": "At least one eligible candidate",
        "fail_condition": "No eligible securities",
        "handoff_to": ["research"],
    },
    {
        "id": "research",
        "name": "Research",
        "agent_id": "research-agent",
        "objective": (
            "Build an evidence-backed research brief for each candidate using primary "
            "sources and market context."
        ),
        "inputs": {"text": "Ranked candidates and catalyst reason codes", "required": ["candidates"]},
        "outputs": {"text": "Evidence cards, risks and catalysts", "required": ["findings"]},
        "handoff": "Forward briefs whose evidence is source-linked",
        "pass_condition": "At least one evidence-backed thesis",
        "handoff_to": ["hypothesis"],
    },
    {
        "id": "hypothesis",
        "name": "Hypothesis",
        "agent_id": "hypothesis-agent",
        "objective": (
            "Transform supported findings into falsifiable hypotheses with explicit "
            "mechanisms and rejection criteria."
        ),
        "inputs": {"text": "Research briefs and findings", "required": ["findings"]},
        "outputs": {"text": "Draft hypotheses and experiment specifications", "required": ["hypotheses"]},
        "handoff": "Send testable hypotheses to Quant / Test",
        "pass_condition": "Mechanism and falsifier present",
        "handoff_to": ["quant"],
    },
    {
        "id": "quant",
        "name": "Quant / Test",
        "agent_id": "quant-agent",
        "objective": (
            "Run leakage-safe experiments and backtests for each hypothesis against the "
            "configured benchmark."
        ),
        "inputs": {"text": "Hypotheses, symbols and benchmark", "required": ["hypotheses"]},
        "outputs": {"text": "Experiment and backtest results", "required": ["backtests"]},
        "handoff": "Forward experiments that meet the minimum statistical gates",
        "pass_condition": "A backtest completed with out-of-sample statistics",
        "handoff_to": ["validation"],
    },
    {
        "id": "validation",
        "name": "Validation",
        "agent_id": "validation-agent",
        "objective": (
            "Independently challenge the candidate with robustness, cost-sensitivity and "
            "bias checks."
        ),
        "inputs": {"text": "Passing experiments and the attempt history", "required": ["backtests"]},
        "outputs": {"text": "Validation report and promotion recommendation", "required": ["verdict"]},
        "handoff": "Prepare the recommendation for human approval",
        "pass_condition": "Every required gate passes",
        "handoff_to": ["approval"],
    },
    {
        "id": "approval",
        "name": "Human Approval",
        "kind": StageKind.HUMAN_APPROVAL,
        "agent_id": "human-review",
        "objective": (
            "Review the evidence, the test history and the risks before any promotion to "
            "paper research."
        ),
        "inputs": {"text": "Validation report, candidate and audit trail"},
        "outputs": {"text": "Approve, reject, or request more validation"},
        "handoff": "No automated downstream handoff",
        "stop_condition": "Always stops automated execution",
        "locked": True,
    },
)


class PipelineRegistry:
    """CRUD, versioning and validation for pipelines."""

    def __init__(
        self,
        session_factory: sessionmaker[Session] | None = None,
        *,
        bus: EventBus | None = None,
    ) -> None:
        """Create the registry.

        Args:
            session_factory: SQLAlchemy session factory; defaults to the
                process-wide one from :mod:`tradinglib.db`.
            bus: event bus pipeline changes are announced on.
        """
        self._session_factory = session_factory
        self._bus = bus
        ensure_schema()

    # --- plumbing ---------------------------------------------------------
    @contextmanager
    def _session(self) -> Iterator[Session]:
        """Transactional session scope bound to this registry."""
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

    @staticmethod
    def _to_dataclass(row: PipelineRow) -> Pipeline:
        """Map an ORM row onto the dataclass."""
        return Pipeline(
            id=row.id,
            name=row.name,
            version=int(row.version),
            status=PipelineStatus(row.status),
            objective=row.objective,
            description=row.description,
            stages=[Stage.from_dict(stage) for stage in row.stages or []],
            locked=bool(row.locked),
            dirty=bool(row.dirty),
            tags=list(row.tags or []),
            created_at=_aware(row.created_at),
            updated_at=_aware(row.updated_at),
        )

    @staticmethod
    def _unique_id(session: Session, base: str) -> str:
        """Return ``base``, suffixed until it is free."""
        candidate = base
        suffix = 2
        while session.get(PipelineRow, candidate) is not None:
            candidate = f"{base}-{suffix}"
            suffix += 1
        return candidate

    def _row(self, session: Session, pipeline_id: str) -> PipelineRow:
        """Return a row or raise ``KeyError``."""
        row = session.get(PipelineRow, pipeline_id)
        if row is None:
            raise KeyError(f"unknown pipeline {pipeline_id!r}")
        return row

    @staticmethod
    def _guard_editable(row: PipelineRow) -> None:
        """Refuse to edit a locked pipeline."""
        if row.locked:
            raise PermissionError(
                f"pipeline {row.id!r} is approval-locked; duplicate it or unlock it first"
            )

    def _touch(self, row: PipelineRow, *, keep_status: bool = False) -> None:
        """Mark the working document changed since the last saved version.

        An edit takes an ``active`` pipeline back to ``draft`` - what is running
        is the last saved version, and the document no longer matches it.
        """
        row.dirty = True
        row.updated_at = utcnow()
        if not keep_status and PipelineStatus(row.status) is PipelineStatus.ACTIVE:
            row.status = str(PipelineStatus.DRAFT)

    def _announce(self, action: str, pipeline: Pipeline) -> None:
        """Publish a pipeline change, when a bus is attached."""
        if self._bus is None:
            return
        try:
            self._bus.publish_nowait(
                "notifications",
                {
                    "kind": "pipeline",
                    "action": action,
                    "pipeline_id": pipeline.id,
                    "version": pipeline.version,
                    "message": f"Pipeline {pipeline.name} {action}",
                },
            )
        except RuntimeError:
            logger.debug("no event loop to announce pipeline %s on", pipeline.id)

    # --- reads ------------------------------------------------------------
    def list(self) -> list[Pipeline]:
        """Return every pipeline's working document, newest first."""
        statement = select(PipelineRow).order_by(PipelineRow.created_at.desc())
        with self._session() as session:
            return [self._to_dataclass(row) for row in session.scalars(statement)]

    def get(self, pipeline_id: str, *, version: int | None = None) -> Pipeline:
        """Return a pipeline, the working document unless ``version`` is given.

        Raises:
            KeyError: when the pipeline - or that version of it - is unknown.
        """
        with self._session() as session:
            row = self._row(session, pipeline_id)
            if version is None:
                return self._to_dataclass(row)
            if version == row.version and not row.dirty:
                return self._to_dataclass(row)
            snapshot = session.scalars(
                select(PipelineVersionRow)
                .where(
                    PipelineVersionRow.pipeline_id == pipeline_id,
                    PipelineVersionRow.version == version,
                )
                .limit(1)
            ).first()
            if snapshot is None:
                raise KeyError(f"pipeline {pipeline_id!r} has no version {version}")
            return self._from_document(snapshot.document)

    @staticmethod
    def _from_document(document: Mapping[str, Any]) -> Pipeline:
        """Rebuild a pipeline from a frozen version snapshot."""
        return Pipeline(
            id=str(document.get("id", "")),
            name=str(document.get("name", "")),
            version=int(document.get("version", 1)),
            status=PipelineStatus(str(document.get("status", PipelineStatus.DRAFT))),
            objective=document.get("objective"),
            description=document.get("description"),
            stages=[Stage.from_dict(stage) for stage in document.get("stages") or []],
            locked=bool(document.get("locked", False)),
            dirty=False,
            tags=[str(tag) for tag in document.get("tags") or []],
            created_at=None,
            updated_at=None,
        )

    def versions(self, pipeline_id: str) -> list[dict[str, Any]]:
        """List stored versions of a pipeline, newest first."""
        with self._session() as session:
            self._row(session, pipeline_id)
            rows = session.scalars(
                select(PipelineVersionRow)
                .where(PipelineVersionRow.pipeline_id == pipeline_id)
                .order_by(PipelineVersionRow.version.desc())
            )
            return [
                {
                    "pipeline_id": row.pipeline_id,
                    "version": row.version,
                    "note": row.note,
                    "stage_count": len(row.document.get("stages") or []),
                    "created_at": _aware(row.created_at).isoformat() if row.created_at else None,
                }
                for row in rows
            ]

    def exists(self, pipeline_id: str) -> bool:
        """Return whether a pipeline with this identifier exists."""
        with self._session() as session:
            return session.get(PipelineRow, pipeline_id) is not None

    # --- writes -----------------------------------------------------------
    def create(
        self,
        name: str,
        *,
        pipeline_id: str | None = None,
        objective: str | None = None,
        description: str | None = None,
        stages: Sequence[Stage | Mapping[str, Any]] | None = None,
        tags: Sequence[str] | None = None,
        status: PipelineStatus | str = PipelineStatus.DRAFT,
    ) -> Pipeline:
        """Create a pipeline.

        Raises:
            ValueError: when the name is empty or the stage list is invalid.
        """
        display = str(name).strip()
        if not display:
            raise ValueError("a pipeline needs a name")
        prepared = [
            stage if isinstance(stage, Stage) else Stage.from_dict(stage) for stage in stages or []
        ]
        self._require_unique_stage_ids(prepared)
        now = utcnow()
        with self._session() as session:
            identifier = self._unique_id(session, slugify(pipeline_id or display))
            row = PipelineRow(
                id=identifier,
                name=display,
                version=1,
                status=str(PipelineStatus(str(status))),
                objective=objective,
                description=description,
                stages=[stage.as_dict() for stage in prepared],
                locked=False,
                dirty=bool(prepared),
                tags=[str(tag) for tag in tags or []],
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            pipeline = self._to_dataclass(row)
        self._announce("created", pipeline)
        return pipeline

    #: Fields ``update`` accepts on the pipeline document itself.
    UPDATABLE: Final[frozenset[str]] = frozenset(
        {"name", "objective", "description", "tags", "status", "locked", "stages"}
    )

    def update(self, pipeline_id: str, **changes: Any) -> Pipeline:
        """Update a pipeline's working document.

        The change is *not* a new version: the document is marked ``dirty``
        until :meth:`save_version` freezes it, which is what the builder's
        "Save version" button does.

        Raises:
            KeyError: when the pipeline is unknown.
            ValueError: when a field is unknown or a value is invalid.
            PermissionError: when the pipeline is locked (unlocking is allowed).
        """
        unknown = set(changes) - self.UPDATABLE
        if unknown:
            raise ValueError(f"unknown field(s): {', '.join(sorted(unknown))}")
        with self._session() as session:
            row = self._row(session, pipeline_id)
            if set(changes) - {"locked"}:
                self._guard_editable(row)
            if "name" in changes:
                display = str(changes["name"]).strip()
                if not display:
                    raise ValueError("a pipeline needs a name")
                row.name = display
            if "objective" in changes:
                row.objective = changes["objective"] or None
            if "description" in changes:
                row.description = changes["description"] or None
            if "tags" in changes:
                row.tags = [str(tag) for tag in changes["tags"] or []]
            if "status" in changes:
                row.status = str(PipelineStatus(str(changes["status"])))
            if "locked" in changes:
                row.locked = bool(changes["locked"])
                if row.locked:
                    row.status = str(PipelineStatus.LOCKED)
                elif PipelineStatus(row.status) is PipelineStatus.LOCKED:
                    row.status = str(PipelineStatus.DRAFT)
            if "stages" in changes:
                prepared = [
                    stage if isinstance(stage, Stage) else Stage.from_dict(stage)
                    for stage in changes["stages"] or []
                ]
                self._require_unique_stage_ids(prepared)
                row.stages = [stage.as_dict() for stage in prepared]
            if set(changes) - {"locked", "status"}:
                self._touch(row, keep_status="status" in changes or "locked" in changes)
            else:
                row.updated_at = utcnow()
            session.flush()
            pipeline = self._to_dataclass(row)
        self._announce("updated", pipeline)
        return pipeline

    def delete(self, pipeline_id: str) -> None:
        """Delete a pipeline and its versions.

        Raises:
            KeyError: when the pipeline is unknown.
            PermissionError: when it is locked.
        """
        with self._session() as session:
            row = self._row(session, pipeline_id)
            self._guard_editable(row)
            pipeline = self._to_dataclass(row)
            session.execute(
                delete(PipelineVersionRow).where(PipelineVersionRow.pipeline_id == pipeline_id)
            )
            session.delete(row)
        self._announce("deleted", pipeline)

    def duplicate(self, pipeline_id: str, *, name: str | None = None) -> Pipeline:
        """Copy a pipeline under a new id. The copy is never locked."""
        original = self.get(pipeline_id)
        return self.create(
            name or f"{original.name} copy",
            objective=original.objective,
            description=original.description,
            stages=[Stage.from_dict(stage.as_dict()) for stage in original.stages],
            tags=original.tags,
            status=PipelineStatus.DRAFT,
        )

    def save_version(self, pipeline_id: str, *, note: str | None = None) -> Pipeline:
        """Freeze the working document as the current version and bump it.

        Returns the pipeline at its new (empty, clean) working version. The
        frozen snapshot is what :meth:`get` returns for that version number and
        what a run recorded against it can always be read back with.
        """
        with self._session() as session:
            row = self._row(session, pipeline_id)
            document = {
                "id": row.id,
                "name": row.name,
                "version": row.version,
                "status": row.status,
                "objective": row.objective,
                "description": row.description,
                "stages": list(row.stages or []),
                "locked": row.locked,
                "tags": list(row.tags or []),
            }
            existing = session.scalars(
                select(PipelineVersionRow).where(
                    PipelineVersionRow.pipeline_id == pipeline_id,
                    PipelineVersionRow.version == row.version,
                )
            ).first()
            if existing is not None:
                existing.document = document
                existing.note = note
                existing.created_at = utcnow()
            else:
                session.add(
                    PipelineVersionRow(
                        pipeline_id=pipeline_id,
                        version=row.version,
                        document=document,
                        note=note,
                        created_at=utcnow(),
                    )
                )
            row.version += 1
            row.dirty = False
            row.updated_at = utcnow()
            session.flush()
            pipeline = self._to_dataclass(row)
        self._announce("version saved", pipeline)
        return pipeline

    # --- stage editing ----------------------------------------------------
    @staticmethod
    def _require_unique_stage_ids(stages: Sequence[Stage]) -> None:
        """Reject a stage list with duplicate identifiers."""
        seen: set[str] = set()
        for stage in stages:
            if stage.id in seen:
                raise ValueError(f"duplicate stage id {stage.id!r}")
            seen.add(stage.id)

    def _write_stages(self, session: Session, row: PipelineRow, stages: Sequence[Stage]) -> None:
        """Persist a new stage list on the working document."""
        self._require_unique_stage_ids(stages)
        row.stages = [stage.as_dict() for stage in stages]
        self._touch(row)

    def add_stage(
        self,
        pipeline_id: str,
        stage: Stage | Mapping[str, Any],
        *,
        index: int | None = None,
    ) -> Pipeline:
        """Insert a stage at ``index`` (append when omitted)."""
        with self._session() as session:
            row = self._row(session, pipeline_id)
            self._guard_editable(row)
            pipeline = self._to_dataclass(row)
            prepared = stage if isinstance(stage, Stage) else Stage.from_dict(stage)
            if any(existing.id == prepared.id for existing in pipeline.stages):
                prepared.id = self._free_stage_id(pipeline, prepared.id)
            stages = list(pipeline.stages)
            if index is None or index >= len(stages):
                stages.append(prepared)
            else:
                stages.insert(max(index, 0), prepared)
            self._write_stages(session, row, stages)
            session.flush()
            return self._to_dataclass(row)

    @staticmethod
    def _free_stage_id(pipeline: Pipeline, base: str) -> str:
        """Return ``base`` suffixed until it is free inside this pipeline."""
        taken = {stage.id for stage in pipeline.stages}
        candidate = base
        suffix = 2
        while candidate in taken:
            candidate = f"{base}-{suffix}"
            suffix += 1
        return candidate

    #: Stage fields the builder may change.
    STAGE_UPDATABLE: Final[frozenset[str]] = frozenset(
        {
            "name",
            "kind",
            "agent_id",
            "provider",
            "model",
            "objective",
            "scope",
            "include_in_run",
            "inputs",
            "outputs",
            "handoff",
            "handoff_to",
            "pass_condition",
            "fail_condition",
            "stop_condition",
            "tools",
            "budget_usd",
            "timeout_s",
            "retries",
            "config",
            "locked",
        }
    )

    #: What a locked stage (the human-approval boundary) still allows.
    LOCKED_STAGE_UPDATABLE: Final[frozenset[str]] = frozenset(
        {"objective", "inputs", "outputs", "handoff", "scope", "config"}
    )

    def update_stage(self, pipeline_id: str, stage_id: str, **changes: Any) -> Pipeline:
        """Update one stage's configuration.

        Raises:
            KeyError: when the pipeline or stage is unknown.
            ValueError: when a field is unknown or a value is invalid.
            PermissionError: when the pipeline, or the stage itself, is locked
                against this change. The human-approval stage never accepts a
                model, an agent change or being switched off.
        """
        unknown = set(changes) - self.STAGE_UPDATABLE
        if unknown:
            raise ValueError(f"unknown stage field(s): {', '.join(sorted(unknown))}")
        with self._session() as session:
            row = self._row(session, pipeline_id)
            self._guard_editable(row)
            pipeline = self._to_dataclass(row)
            stage = pipeline.stage(stage_id)
            if stage.locked and set(changes) - self.LOCKED_STAGE_UPDATABLE:
                raise PermissionError(
                    f"stage {stage_id!r} is immutable: automation always stops there for an "
                    "explicit human decision"
                )
            for field_name, value in changes.items():
                if field_name == "kind":
                    stage.kind = StageKind(str(value))
                elif field_name in {"inputs", "outputs"}:
                    setattr(stage, field_name, IOContract.from_dict(value))
                elif field_name == "scope":
                    stage.scope = dict(value or {})
                elif field_name == "config":
                    stage.config = dict(value or {})
                elif field_name == "handoff_to":
                    stage.handoff_to = [str(target) for target in value or []]
                elif field_name == "tools":
                    stage.tools = [str(tool) for tool in value or []]
                elif field_name == "include_in_run":
                    stage.include_in_run = bool(value)
                elif field_name == "locked":
                    stage.locked = bool(value)
                elif field_name == "retries":
                    stage.retries = int(value or 0)
                elif field_name in {"budget_usd", "timeout_s"}:
                    setattr(stage, field_name, None if value in (None, "") else float(value))
                elif field_name == "name":
                    display = str(value).strip()
                    if not display:
                        raise ValueError("a stage needs a name")
                    stage.name = display
                else:
                    setattr(stage, field_name, value or None)
            self._write_stages(session, row, pipeline.stages)
            session.flush()
            return self._to_dataclass(row)

    def duplicate_stage(self, pipeline_id: str, stage_id: str) -> Pipeline:
        """Duplicate a stage in place, right after the original."""
        with self._session() as session:
            row = self._row(session, pipeline_id)
            self._guard_editable(row)
            pipeline = self._to_dataclass(row)
            original = pipeline.stage(stage_id)
            copy = Stage.from_dict(original.as_dict())
            copy.id = self._free_stage_id(pipeline, f"{original.id}-copy")
            copy.name = f"{original.name} copy"
            # A duplicate of the approval boundary is an ordinary stage: only
            # the original is the mandatory gate.
            copy.locked = False
            stages = list(pipeline.stages)
            stages.insert(stages.index(original) + 1, copy)
            self._write_stages(session, row, stages)
            session.flush()
            return self._to_dataclass(row)

    def remove_stage(self, pipeline_id: str, stage_id: str) -> Pipeline:
        """Remove a stage and repair handoffs that referenced it.

        Raises:
            PermissionError: when the stage is the locked approval boundary.
        """
        with self._session() as session:
            row = self._row(session, pipeline_id)
            self._guard_editable(row)
            pipeline = self._to_dataclass(row)
            stage = pipeline.stage(stage_id)
            if stage.locked:
                raise PermissionError(
                    f"stage {stage_id!r} is immutable and cannot be removed"
                )
            stages = [item for item in pipeline.stages if item.id != stage_id]
            for remaining in stages:
                if stage_id in remaining.handoff_to:
                    remaining.handoff_to = [
                        target for target in remaining.handoff_to if target != stage_id
                    ] + [target for target in stage.handoff_to if target != remaining.id]
            self._write_stages(session, row, stages)
            session.flush()
            return self._to_dataclass(row)

    def reorder_stages(self, pipeline_id: str, order: Sequence[str]) -> Pipeline:
        """Reorder stages to match ``order``.

        Raises:
            ValueError: when ``order`` is not a permutation of the stage ids.
        """
        with self._session() as session:
            row = self._row(session, pipeline_id)
            self._guard_editable(row)
            pipeline = self._to_dataclass(row)
            by_id = {stage.id: stage for stage in pipeline.stages}
            if set(order) != set(by_id):
                raise ValueError("the new order must list every stage exactly once")
            self._write_stages(session, row, [by_id[stage_id] for stage_id in order])
            session.flush()
            return self._to_dataclass(row)

    # --- validation -------------------------------------------------------
    def validate(
        self, pipeline: Pipeline, *, agents: Iterable[str] | None = None
    ) -> list[str]:
        """Return contract/DAG problems; an empty list means the pipeline runs.

        Args:
            pipeline: the document to check.
            agents: known agent ids. When given, every stage's assigned agent is
                checked against it - a stage pointing at a deleted agent is a
                real problem the builder must show.
        """
        problems: list[str] = []
        stages = pipeline.stages
        if not stages:
            problems.append("the pipeline has no stages")
        ids = [stage.id for stage in stages]
        duplicates = {stage_id for stage_id in ids if ids.count(stage_id) > 1}
        problems.extend(f"duplicate stage id {stage_id!r}" for stage_id in sorted(duplicates))
        known_agents = set(agents) if agents is not None else None
        enabled = [stage for stage in stages if stage.include_in_run]
        if not enabled:
            problems.append("every stage is excluded from the run")
        for stage in stages:
            if stage.kind is StageKind.AGENT and not stage.agent_id:
                problems.append(f"stage {stage.id!r} has no agent assigned")
            if (
                known_agents is not None
                and stage.agent_id
                and stage.agent_id not in known_agents
            ):
                problems.append(
                    f"stage {stage.id!r} is assigned to unknown agent {stage.agent_id!r}"
                )
            for target in stage.handoff_to:
                if target not in ids:
                    problems.append(
                        f"stage {stage.id!r} hands off to unknown stage {target!r}"
                    )
            if stage.is_approval and not stage.include_in_run:
                problems.append(
                    f"stage {stage.id!r} is the human-approval boundary and cannot be skipped"
                )
        problems.extend(self._cycles(stages))
        # Contract chaining: a required input nobody upstream declares as an
        # output can only ever arrive from the run's own inputs.
        produced: set[str] = set()
        for stage in enabled:
            missing = [key for key in stage.inputs.required if key not in produced]
            if missing:
                problems.append(
                    f"stage {stage.id!r} requires {', '.join(sorted(missing))}, which no earlier "
                    "stage produces - it must come from the run inputs"
                )
            produced.update(stage.outputs.required)
        return problems

    @staticmethod
    def _cycles(stages: Sequence[Stage]) -> list[str]:
        """Return a message per handoff cycle found (depth-first search)."""
        graph = {stage.id: [t for t in stage.handoff_to] for stage in stages}
        problems: list[str] = []
        state: dict[str, int] = {}

        def visit(node: str, path: list[str]) -> None:
            """Walk the graph, reporting a cycle when one closes."""
            state[node] = 1
            for target in graph.get(node, []):
                if state.get(target) == 1:
                    problems.append(
                        "handoff cycle: " + " -> ".join([*path, node, target])
                    )
                elif state.get(target, 0) == 0:
                    visit(target, [*path, node])
            state[node] = 2

        for stage in stages:
            if state.get(stage.id, 0) == 0:
                visit(stage.id, [])
        return problems

    # --- bootstrap --------------------------------------------------------
    def ensure_defaults(self) -> Pipeline | None:
        """Create the shipped "Full Research Cycle" pipeline when absent."""
        if self.exists("full-research-cycle"):
            return None
        pipeline = self.create(
            "Full Research Cycle",
            pipeline_id="full-research-cycle",
            objective=(
                "Find durable operating-leverage and estimate-revision opportunities across "
                "quality equities."
            ),
            description=(
                "Scan, research, hypothesise, test and validate - then stop for a human "
                "decision."
            ),
            stages=[Stage.from_dict(stage) for stage in DEFAULT_PIPELINE_STAGES],
            tags=["default"],
        )
        return self.save_version(pipeline.id, note="shipped default")


_registry: PipelineRegistry | None = None


def get_pipeline_registry() -> PipelineRegistry:
    """Return the process-wide pipeline registry, creating it on first use."""
    global _registry
    if _registry is None:
        _registry = PipelineRegistry()
    return _registry


def reset_pipeline_registry(registry: PipelineRegistry | None = None) -> PipelineRegistry:
    """Install (or forget) the process-wide pipeline registry. Returns it."""
    global _registry
    _registry = registry if registry is not None else PipelineRegistry()
    return _registry


__all__ = [
    "DEFAULT_PIPELINE_STAGES",
    "IOContract",
    "Pipeline",
    "PipelineRegistry",
    "PipelineRow",
    "PipelineStatus",
    "PipelineVersionRow",
    "Stage",
    "StageKind",
    "ensure_schema",
    "get_pipeline_registry",
    "reset_pipeline_registry",
    "slugify",
]
