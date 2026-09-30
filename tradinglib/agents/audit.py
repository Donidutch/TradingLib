"""Append-only audit log of every agent action.

Records are never updated or deleted: corrections are appended. The Audit Log
tab reads this store directly, and the pipeline runner writes to it through the
``AuditSink`` protocol it declares - :func:`tradinglib.agents.runner.default_audit_sink`
constructs :class:`AuditLog`, so every queued run, every stage, every tool grant
and every human decision lands here without the runner knowing anything about
the storage.

"Append-only" is enforced by the database rather than by convention: the table
carries ``BEFORE UPDATE`` and ``BEFORE DELETE`` triggers that abort the
statement. There is deliberately no update or delete method on this class - a
mistake is corrected by appending a correcting record, which is what an audit
trail is for.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import JSON, DateTime, Integer, String, Text, func, select, text
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from tradinglib.db import Base, get_engine, get_session_factory

logger = logging.getLogger(__name__)


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


def new_audit_id() -> str:
    """Return a fresh audit identifier (``AUD-XXXXXXXX``)."""
    return f"AUD-{uuid.uuid4().hex[:8].upper()}"


#: Action name every guardrail refusal is recorded under. The Audit tab counts
#: rows with this action for its "blocked attempts" figure, so anything that
#: refuses an agent must use exactly this string.
GUARDRAIL_ACTION: Final[str] = "guardrail block"


class ActorKind(StrEnum):
    """Who performed an action.

    The Audit Log tab filters on this, and the distinction that matters for
    governance is human versus machine: a promotion approved by a person is not
    the same event as one an agent produced.
    """

    HUMAN = "human"
    AGENT = "agent"
    SYSTEM = "system"


#: Actor names the runner and the schedulers use for themselves. Anything in
#: this set is machinery rather than a person or a model.
SYSTEM_ACTORS: Final[frozenset[str]] = frozenset(
    {
        "runner",
        "system",
        "scheduler",
        "autopipeline",
        "policy engine",
        "pipeline engine",
        "approval gate",
        "ingest scheduler",
    }
)


def classify_actor(actor: str) -> ActorKind:
    """Infer whether ``actor`` is a person, an agent or the machinery.

    The rule is deliberately simple and documented rather than clever: known
    machinery names are ``system``, anything that calls itself an agent (or is a
    known agent id) is ``agent``, and everything else is a person. Callers that
    know better pass ``actor_kind`` explicitly to :meth:`AuditLog.record` and
    this heuristic is not used at all.
    """
    name = actor.strip().lower()
    if name in SYSTEM_ACTORS:
        return ActorKind.SYSTEM
    if "agent" in name or name.endswith("-bot"):
        return ActorKind.AGENT
    return ActorKind.HUMAN


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """One immutable audit entry."""

    id: str
    ts: datetime
    actor: str
    action: str
    target: str | None = None
    run_id: str | None = None
    stage_id: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    actor_kind: ActorKind = ActorKind.SYSTEM
    result: str | None = None
    seq: int = 0

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping for the API layer."""
        return {
            "id": self.id,
            "seq": self.seq,
            "ts": _iso(self.ts),
            "actor": self.actor,
            "actor_kind": str(self.actor_kind),
            "action": self.action,
            "target": self.target,
            "run_id": self.run_id,
            "stage_id": self.stage_id,
            "result": self.result,
            "detail": dict(self.detail),
        }


class AgentAuditRow(Base):
    """SQLite row for one audit record."""

    __tablename__ = "agent_audit"

    seq: Mapped[int] = mapped_column(Integer(), primary_key=True, autoincrement=True)
    id: Mapped[str] = mapped_column(String(24), nullable=False, unique=True, index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(), nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(120), nullable=False)
    actor_kind: Mapped[str] = mapped_column(String(16), nullable=False, default=ActorKind.SYSTEM)
    action: Mapped[str] = mapped_column(String(200), nullable=False)
    target: Mapped[str | None] = mapped_column(String(200), default=None, index=True)
    run_id: Mapped[str | None] = mapped_column(String(32), default=None, index=True)
    stage_id: Mapped[str | None] = mapped_column(String(64), default=None)
    result: Mapped[str | None] = mapped_column(String(200), default=None)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON(), nullable=False, default=dict)
    note: Mapped[str | None] = mapped_column(Text(), default=None)


_OWNED_TABLES = (AgentAuditRow.__table__,)

#: Triggers that make the append-only promise real instead of documentary.
_APPEND_ONLY_TRIGGERS: Final[tuple[str, ...]] = (
    """
    CREATE TRIGGER IF NOT EXISTS agent_audit_is_append_only_update
    BEFORE UPDATE ON agent_audit
    BEGIN
        SELECT RAISE(ABORT, 'the audit log is append-only: append a correcting record');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS agent_audit_is_append_only_delete
    BEFORE DELETE ON agent_audit
    BEGIN
        SELECT RAISE(ABORT, 'the audit log is append-only: records are never deleted');
    END
    """,
)


def ensure_schema() -> None:
    """Create the audit table and its append-only triggers if absent."""
    engine = get_engine()
    Base.metadata.create_all(engine, tables=list(_OWNED_TABLES))
    if engine.dialect.name != "sqlite":
        # The triggers are SQLite syntax. TradingLib ships on SQLite (SPEC D4); on
        # anything else the append-only rule is enforced by this class alone,
        # and saying so beats pretending the guarantee is still there.
        logger.warning(
            "audit append-only triggers are only installed on SQLite (dialect=%s)",
            engine.dialect.name,
        )
        return
    with engine.begin() as connection:
        for statement in _APPEND_ONLY_TRIGGERS:
            connection.execute(text(statement))


class AuditLog:
    """Append-only audit store backed by SQLite.

    The store is intentionally write-once: :meth:`record` appends, everything
    else reads. It is safe to construct in a process whose database is not ready
    yet - the schema is created on first use and a failure there is retried on
    the next write rather than crashing the caller, because an audit failure
    must never take a run down with it.
    """

    def __init__(self, session_factory: sessionmaker[Session] | None = None) -> None:
        """Create the log, optionally bound to a specific session factory."""
        self._session_factory = session_factory
        self._schema_ready = False
        self._ensure_schema()

    # --- plumbing ---------------------------------------------------------
    def _ensure_schema(self) -> None:
        """Create the table on first use; tolerate a database that is not up."""
        if self._schema_ready:
            return
        try:
            ensure_schema()
        except Exception:  # noqa: BLE001 - retried on the next write
            logger.debug("audit schema is not ready yet", exc_info=True)
            return
        self._schema_ready = True

    @contextmanager
    def _session(self) -> Iterator[Session]:
        """Transactional session scope bound to this log."""
        self._ensure_schema()
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
    def _to_dataclass(row: AgentAuditRow) -> AuditRecord:
        """Map an ORM row onto the immutable dataclass."""
        return AuditRecord(
            id=row.id,
            ts=_aware(row.ts) or utcnow(),
            actor=row.actor,
            action=row.action,
            target=row.target,
            run_id=row.run_id,
            stage_id=row.stage_id,
            detail=dict(row.detail or {}),
            actor_kind=ActorKind(row.actor_kind),
            result=row.result,
            seq=int(row.seq),
        )

    # --- writes -----------------------------------------------------------
    def record(
        self,
        actor: str,
        action: str,
        *,
        target: str | None = None,
        run_id: str | None = None,
        stage_id: str | None = None,
        actor_kind: ActorKind | str | None = None,
        result: str | None = None,
        note: str | None = None,
        ts: datetime | None = None,
        **detail: Any,
    ) -> AuditRecord:
        """Append one audit record and return it.

        Args:
            actor: who acted - a person, an agent name or a piece of machinery.
            action: what they did, in plain words ("approved run", "ran screen").
            target: the object acted on (run id, agent id, strategy id).
            run_id: the run the action belongs to, when there is one.
            stage_id: the stage inside that run.
            actor_kind: override the inferred human/agent/system classification.
            result: the outcome the Audit Log's Result column shows. Also read
                from ``detail["result"]`` when the caller passes it that way.
            note: free-text annotation (used by correcting records).
            ts: event time; defaults to now. Always stored UTC.
            **detail: anything else worth keeping, stored as JSON.

        Raises:
            ValueError: when ``actor`` or ``action`` is empty - an audit record
                that names neither is not a record of anything.
        """
        who = str(actor or "").strip()
        what = str(action or "").strip()
        if not who or not what:
            raise ValueError("an audit record needs both an actor and an action")
        payload = dict(detail)
        outcome = result if result is not None else payload.pop("result", None)
        kind = (
            ActorKind(str(actor_kind)) if actor_kind is not None else classify_actor(who)
        )
        row = AgentAuditRow(
            id=new_audit_id(),
            ts=ts.astimezone(timezone.utc) if ts else utcnow(),
            actor=who,
            actor_kind=str(kind),
            action=what,
            target=target,
            run_id=run_id,
            stage_id=stage_id,
            result=None if outcome is None else str(outcome),
            detail=payload,
            note=note,
        )
        with self._session() as session:
            session.add(row)
            session.flush()
            record = self._to_dataclass(row)
        logger.debug("audit: %s %s %s", record.actor, record.action, record.target or "")
        return record

    # --- reads ------------------------------------------------------------
    def _filtered(
        self,
        *,
        actor: str | None,
        actor_kind: ActorKind | str | None,
        action: str | None,
        target: str | None,
        run_id: str | None,
        since: datetime | None,
        until: datetime | None,
        query: str | None,
    ) -> Any:
        """Build the filtered SELECT the reads share."""
        statement = select(AgentAuditRow)
        if actor:
            statement = statement.where(AgentAuditRow.actor == actor)
        if actor_kind is not None:
            statement = statement.where(AgentAuditRow.actor_kind == str(ActorKind(str(actor_kind))))
        if action:
            statement = statement.where(AgentAuditRow.action == action)
        if target:
            statement = statement.where(AgentAuditRow.target == target)
        if run_id:
            statement = statement.where(AgentAuditRow.run_id == run_id)
        if since is not None:
            statement = statement.where(AgentAuditRow.ts >= since.astimezone(timezone.utc))
        if until is not None:
            statement = statement.where(AgentAuditRow.ts <= until.astimezone(timezone.utc))
        if query:
            needle = f"%{query.strip().lower()}%"
            statement = statement.where(
                func.lower(AgentAuditRow.actor).like(needle)
                | func.lower(AgentAuditRow.action).like(needle)
                | func.lower(func.coalesce(AgentAuditRow.target, "")).like(needle)
                | func.lower(func.coalesce(AgentAuditRow.result, "")).like(needle)
            )
        return statement

    def list(
        self,
        *,
        limit: int = 200,
        offset: int = 0,
        actor: str | None = None,
        actor_kind: ActorKind | str | None = None,
        action: str | None = None,
        target: str | None = None,
        run_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        query: str | None = None,
    ) -> list[AuditRecord]:
        """Return audit records, newest first, with optional filters."""
        statement = self._filtered(
            actor=actor,
            actor_kind=actor_kind,
            action=action,
            target=target,
            run_id=run_id,
            since=since,
            until=until,
            query=query,
        )
        statement = statement.order_by(AgentAuditRow.seq.desc()).limit(max(1, int(limit)))
        if offset:
            statement = statement.offset(max(0, int(offset)))
        with self._session() as session:
            return [self._to_dataclass(row) for row in session.scalars(statement)]

    def get(self, audit_id: str) -> AuditRecord:
        """Return one record by its identifier.

        Raises:
            KeyError: when the identifier is unknown.
        """
        statement = select(AgentAuditRow).where(AgentAuditRow.id == audit_id)
        with self._session() as session:
            row = session.scalars(statement).first()
            if row is None:
                raise KeyError(f"unknown audit record {audit_id!r}")
            return self._to_dataclass(row)

    def for_run(self, run_id: str, *, limit: int = 500) -> list[AuditRecord]:
        """Return the audit trail of one run, oldest first (it is a story)."""
        statement = (
            select(AgentAuditRow)
            .where(AgentAuditRow.run_id == run_id)
            .order_by(AgentAuditRow.seq.asc())
            .limit(max(1, int(limit)))
        )
        with self._session() as session:
            return [self._to_dataclass(row) for row in session.scalars(statement)]

    def count(
        self,
        *,
        actor: str | None = None,
        actor_kind: ActorKind | str | None = None,
        action: str | None = None,
        target: str | None = None,
        run_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        query: str | None = None,
    ) -> int:
        """Total number of audit records matching the filters."""
        statement = self._filtered(
            actor=actor,
            actor_kind=actor_kind,
            action=action,
            target=target,
            run_id=run_id,
            since=since,
            until=until,
            query=query,
        )
        with self._session() as session:
            return int(session.scalar(select(func.count()).select_from(statement.subquery())) or 0)

    def actors(self) -> list[str]:
        """Distinct actor names, for the Audit Log's filter control."""
        with self._session() as session:
            rows: Sequence[str] = list(session.scalars(select(AgentAuditRow.actor).distinct()))
        return sorted({str(value) for value in rows})

    def actions(self) -> list[str]:
        """Distinct action names, for the Audit Log's filter control."""
        with self._session() as session:
            rows: Sequence[str] = list(session.scalars(select(AgentAuditRow.action).distinct()))
        return sorted({str(value) for value in rows})

    def guardrail_blocks(self, *, since: datetime | None = None) -> int:
        """Count refusals the autonomy guardrails recorded.

        This is the number the Audit tab's "GUARDRAIL BLOCKS TODAY" metric and
        the guardrail table's "blocked attempts" row both read, so a boundary
        that never fires reports zero rather than a decorative figure.
        """
        return self.count(action=GUARDRAIL_ACTION, since=since)

    def summary(self, *, since: datetime | None = None) -> dict[str, Any]:
        """Return counts by actor kind plus the newest record's timestamp."""
        totals = {
            str(kind): self.count(actor_kind=kind, since=since) for kind in ActorKind
        }
        latest = self.list(limit=1, since=since)
        return {
            "total": self.count(since=since),
            "by_actor_kind": totals,
            "guardrail_blocks": self.guardrail_blocks(since=since),
            "last_ts": _iso(latest[0].ts) if latest else None,
            "since": _iso(since),
        }

    def export(
        self,
        *,
        fmt: str = "json",
        limit: int = 5000,
        **filters: Any,
    ) -> tuple[str, str, str]:
        """Serialise the filtered log for download.

        Args:
            fmt: ``"json"`` or ``"csv"``.
            limit: maximum records to include.
            **filters: the same filters :meth:`list` accepts.

        Returns:
            ``(body, media_type, filename)``.

        Raises:
            ValueError: when ``fmt`` is not a supported format.
        """
        shape = str(fmt or "json").strip().lower()
        if shape not in {"json", "csv"}:
            raise ValueError(f"unknown export format {fmt!r}; use json or csv")
        records = self.list(limit=limit, **filters)
        stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
        if shape == "json":
            body = json.dumps(
                {
                    "exported_at": _iso(utcnow()),
                    "count": len(records),
                    "records": [record.as_dict() for record in records],
                },
                indent=2,
                default=str,
            )
            return body, "application/json", f"tradinglib-audit-{stamp}.json"
        columns = [
            "id",
            "seq",
            "ts",
            "actor",
            "actor_kind",
            "action",
            "target",
            "run_id",
            "stage_id",
            "result",
            "detail",
        ]
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for record in records:
            payload = record.as_dict()
            row: dict[str, Any] = {key: payload.get(key) for key in columns}
            # ``detail`` is a mapping; a CSV cell must carry it as text, and JSON
            # is the only rendering that survives a round trip.
            row["detail"] = json.dumps(payload.get("detail") or {}, default=str, sort_keys=True)
            writer.writerow(row)
        return buffer.getvalue(), "text/csv", f"tradinglib-audit-{stamp}.csv"


_audit_log: AuditLog | None = None


def get_audit_log() -> AuditLog:
    """Return the process-wide audit log, creating it on first use."""
    global _audit_log
    if _audit_log is None:
        _audit_log = AuditLog()
    return _audit_log


def reset_audit_log(log: AuditLog | None = None) -> AuditLog:
    """Install (or forget) the process-wide audit log. Returns it."""
    global _audit_log
    _audit_log = log if log is not None else AuditLog()
    return _audit_log


__all__ = [
    "SYSTEM_ACTORS",
    "ActorKind",
    "AgentAuditRow",
    "AuditLog",
    "AuditRecord",
    "classify_actor",
    "ensure_schema",
    "get_audit_log",
    "new_audit_id",
    "reset_audit_log",
]
