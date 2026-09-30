"""Research hypotheses: the Strategy Lab's unit of investigation.

A hypothesis is a falsifiable statement plus the evidence attached to it. The
falsifier is mandatory - a statement that nothing could disprove is not a
hypothesis, and the UI enforces the same rule ("a hypothesis without a
falsifier cannot be saved").

Lifecycle::

    draft ──▶ active ──▶ testing ──▶ validated
      │          │          │            │
      └──────────┴──────────┴──▶ rejected ──▶ archived

Only the transitions in :data:`ALLOWED_TRANSITIONS` are legal; anything else
raises :class:`ValueError` rather than silently rewriting history. Every
transition is appended to the hypothesis' own status history, which is what the
UI's "stage history" panel renders.

Everything is persisted through :mod:`tradinglib.db` (SQLite) and changes are
published on the ``lab`` event channel.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    delete,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from tradinglib.data.base import to_utc, utcnow
from tradinglib.db import Base, get_engine, get_session_factory
from tradinglib.events import EventBus, get_event_bus
from tradinglib.research.backtest import LAB_CHANNEL

logger = logging.getLogger(__name__)

_ID_RE = re.compile(r"^H-(\d+)$")


class HypothesisStatus(StrEnum):
    """Lifecycle states of a hypothesis."""

    DRAFT = "draft"
    ACTIVE = "active"
    TESTING = "testing"
    VALIDATED = "validated"
    REJECTED = "rejected"
    ARCHIVED = "archived"


#: Legal status transitions. A hypothesis may always stay where it is.
ALLOWED_TRANSITIONS: Final[dict[HypothesisStatus, frozenset[HypothesisStatus]]] = {
    HypothesisStatus.DRAFT: frozenset(
        {HypothesisStatus.ACTIVE, HypothesisStatus.TESTING, HypothesisStatus.REJECTED,
         HypothesisStatus.ARCHIVED}
    ),
    HypothesisStatus.ACTIVE: frozenset(
        {HypothesisStatus.TESTING, HypothesisStatus.REJECTED, HypothesisStatus.ARCHIVED}
    ),
    HypothesisStatus.TESTING: frozenset(
        {HypothesisStatus.VALIDATED, HypothesisStatus.REJECTED, HypothesisStatus.ACTIVE,
         HypothesisStatus.ARCHIVED}
    ),
    HypothesisStatus.VALIDATED: frozenset(
        {HypothesisStatus.REJECTED, HypothesisStatus.TESTING, HypothesisStatus.ARCHIVED}
    ),
    HypothesisStatus.REJECTED: frozenset({HypothesisStatus.DRAFT, HypothesisStatus.ARCHIVED}),
    HypothesisStatus.ARCHIVED: frozenset({HypothesisStatus.DRAFT}),
}

#: Evidence kinds the UI offers when linking supporting material.
EVIDENCE_KINDS: Final[frozenset[str]] = frozenset(
    {"paper", "knowledge", "run", "backtest", "experiment", "finding", "screen", "note", "url"}
)


@dataclass(slots=True)
class EvidenceLink:
    """One piece of evidence attached to a hypothesis."""

    kind: str
    ref: str
    label: str | None = None
    url: str | None = None
    supports: bool = True
    id: int | None = None
    added_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of the link."""
        return {
            "id": self.id,
            "kind": self.kind,
            "ref": self.ref,
            "label": self.label,
            "url": self.url,
            "supports": self.supports,
            "added_at": self.added_at.isoformat() if self.added_at else None,
        }


@dataclass(slots=True)
class Hypothesis:
    """A falsifiable research statement with its supporting evidence."""

    id: str
    title: str
    statement: str
    falsifier: str
    rationale: str | None = None
    status: HypothesisStatus = HypothesisStatus.DRAFT
    owner: str | None = None
    origin: str | None = None
    universe_id: str | None = None
    tags: list[str] = field(default_factory=list)
    evidence: list[EvidenceLink] = field(default_factory=list)
    status_history: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    metrics: dict[str, Any] = field(default_factory=dict)

    def as_dict(self, *, include_evidence: bool = True) -> dict[str, Any]:
        """Return a JSON-serialisable mapping for the API layer."""
        payload: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "statement": self.statement,
            "falsifier": self.falsifier,
            "rationale": self.rationale,
            "status": str(self.status),
            "owner": self.owner,
            "origin": self.origin,
            "universe_id": self.universe_id,
            "tags": list(self.tags),
            "status_history": list(self.status_history),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "metrics": dict(self.metrics),
            "evidence_count": len(self.evidence),
        }
        if include_evidence:
            payload["evidence"] = [link.as_dict() for link in self.evidence]
        return payload


# --------------------------------------------------------------------------
# ORM rows
# --------------------------------------------------------------------------
class HypothesisRow(Base):
    """SQLite row for one hypothesis."""

    __tablename__ = "hypotheses"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    statement: Mapped[str] = mapped_column(Text(), nullable=False)
    falsifier: Mapped[str] = mapped_column(Text(), nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text(), default=None)
    status: Mapped[str] = mapped_column(String(16), default=HypothesisStatus.DRAFT, nullable=False)
    owner: Mapped[str | None] = mapped_column(String(64), default=None)
    origin: Mapped[str | None] = mapped_column(String(160), default=None)
    universe_id: Mapped[str | None] = mapped_column(String(64), default=None)
    tags: Mapped[list[str]] = mapped_column(JSON(), default=list, nullable=False)
    status_history: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON(), default=list, nullable=False
    )
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)


class HypothesisEvidenceRow(Base):
    """SQLite row for one evidence link."""

    __tablename__ = "hypothesis_evidence"

    id: Mapped[int] = mapped_column(Integer(), primary_key=True, autoincrement=True)
    hypothesis_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("hypotheses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    ref: Mapped[str] = mapped_column(String(200), nullable=False)
    label: Mapped[str | None] = mapped_column(Text(), default=None)
    url: Mapped[str | None] = mapped_column(Text(), default=None)
    supports: Mapped[bool] = mapped_column(Integer(), default=1, nullable=False)
    added_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)


_OWNED_TABLES = (HypothesisRow.__table__, HypothesisEvidenceRow.__table__)


def ensure_schema() -> None:
    """Create the hypothesis tables if they do not exist yet."""
    Base.metadata.create_all(get_engine(), tables=list(_OWNED_TABLES))


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------
class HypothesisRegistry:
    """CRUD, evidence linking and status transitions for hypotheses."""

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
            bus: event bus changes are published on.
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

    @property
    def bus(self) -> EventBus:
        """The event bus hypothesis changes are published on."""
        if self._bus is None:
            self._bus = get_event_bus()
        return self._bus

    def _evidence(self, session: Session, hypothesis_id: str) -> list[EvidenceLink]:
        """Load the evidence links of one hypothesis, oldest first."""
        rows = session.scalars(
            select(HypothesisEvidenceRow)
            .where(HypothesisEvidenceRow.hypothesis_id == hypothesis_id)
            .order_by(HypothesisEvidenceRow.id)
        )
        return [
            EvidenceLink(
                id=row.id,
                kind=row.kind,
                ref=row.ref,
                label=row.label,
                url=row.url,
                supports=bool(row.supports),
                added_at=to_utc(row.added_at),
            )
            for row in rows
        ]

    def _to_dataclass(self, session: Session, row: HypothesisRow) -> Hypothesis:
        """Map an ORM row (plus its evidence) onto the dataclass."""
        return Hypothesis(
            id=row.id,
            title=row.title,
            statement=row.statement,
            falsifier=row.falsifier,
            rationale=row.rationale,
            status=HypothesisStatus(row.status),
            owner=row.owner,
            origin=row.origin,
            universe_id=row.universe_id,
            tags=list(row.tags or []),
            evidence=self._evidence(session, row.id),
            status_history=list(row.status_history or []),
            created_at=to_utc(row.created_at),
            updated_at=to_utc(row.updated_at),
            metrics=dict(row.metrics or {}),
        )

    @staticmethod
    def _next_id(session: Session) -> str:
        """Return the next sequential ``H-n`` identifier."""
        highest = 0
        for existing in session.scalars(select(HypothesisRow.id)):
            match = _ID_RE.match(str(existing))
            if match:
                highest = max(highest, int(match.group(1)))
        return f"H-{highest + 1}"

    @staticmethod
    def _clean_tags(tags: Iterable[str] | None) -> list[str]:
        """Normalise a tag list: trimmed, de-duplicated, order preserved."""
        out: list[str] = []
        for tag in tags or []:
            cleaned = str(tag).strip()
            if cleaned and cleaned not in out:
                out.append(cleaned)
        return out

    # --- reads ------------------------------------------------------------
    def list(
        self,
        *,
        status: HypothesisStatus | None = None,
        owner: str | None = None,
        universe_id: str | None = None,
        query: str | None = None,
        include_evidence: bool = True,
    ) -> list[Hypothesis]:
        """Return hypotheses, newest first, optionally filtered.

        Args:
            status: keep only hypotheses in this state.
            owner: keep only this owner's hypotheses.
            universe_id: keep only hypotheses scoped to this universe.
            query: case-insensitive substring match on title and statement.
            include_evidence: load the evidence links (set ``False`` for lists).
        """
        statement = select(HypothesisRow).order_by(HypothesisRow.created_at.desc())
        if status is not None:
            statement = statement.where(HypothesisRow.status == str(status))
        if owner:
            statement = statement.where(HypothesisRow.owner == owner)
        if universe_id:
            statement = statement.where(HypothesisRow.universe_id == universe_id)
        with self._session() as session:
            rows = list(session.scalars(statement))
            items = [self._to_dataclass(session, row) for row in rows]
        if not include_evidence:
            for item in items:
                item.evidence = []
        if query:
            needle = query.strip().lower()
            items = [
                item
                for item in items
                if needle in item.title.lower() or needle in item.statement.lower()
            ]
        return items

    def get(self, hypothesis_id: str) -> Hypothesis:
        """Return one hypothesis.

        Raises:
            KeyError: when the identifier is unknown.
        """
        with self._session() as session:
            row = session.get(HypothesisRow, hypothesis_id)
            if row is None:
                raise KeyError(f"unknown hypothesis {hypothesis_id!r}")
            return self._to_dataclass(session, row)

    def exists(self, hypothesis_id: str) -> bool:
        """Return whether a hypothesis with this identifier exists."""
        with self._session() as session:
            return session.get(HypothesisRow, hypothesis_id) is not None

    # --- writes -----------------------------------------------------------
    def create(
        self,
        title: str,
        statement: str,
        falsifier: str,
        *,
        rationale: str | None = None,
        owner: str | None = None,
        origin: str | None = None,
        universe_id: str | None = None,
        tags: Sequence[str] | None = None,
        status: HypothesisStatus = HypothesisStatus.DRAFT,
        evidence: Sequence[dict[str, Any] | EvidenceLink] | None = None,
        hypothesis_id: str | None = None,
    ) -> Hypothesis:
        """Create a hypothesis.

        Raises:
            ValueError: when the title, statement or falsifier is empty - the
                falsifier is what makes the statement testable, so it is
                required rather than defaulted.
        """
        title_text = str(title).strip()
        statement_text = str(statement).strip()
        falsifier_text = str(falsifier).strip()
        if not title_text:
            raise ValueError("a hypothesis needs a title")
        if not statement_text:
            raise ValueError("a hypothesis needs a statement")
        if not falsifier_text:
            raise ValueError(
                "a hypothesis needs a falsifier: state what evidence would kill it"
            )
        now = utcnow()
        with self._session() as session:
            identifier = hypothesis_id or self._next_id(session)
            if session.get(HypothesisRow, identifier) is not None:
                raise ValueError(f"hypothesis {identifier!r} already exists")
            row = HypothesisRow(
                id=identifier,
                title=title_text,
                statement=statement_text,
                falsifier=falsifier_text,
                rationale=(rationale or None),
                status=str(status),
                owner=owner,
                origin=origin,
                universe_id=universe_id,
                tags=self._clean_tags(tags),
                status_history=[
                    {"from": None, "to": str(status), "at": now.isoformat(), "note": "created"}
                ],
                metrics={},
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            for item in evidence or []:
                link = item if isinstance(item, EvidenceLink) else _link_from_dict(item)
                session.add(_evidence_row(identifier, link, now))
            session.flush()
            created = self._to_dataclass(session, row)
        logger.info("hypothesis %s created (%s)", created.id, created.status)
        return created

    def update(self, hypothesis_id: str, **changes: Any) -> Hypothesis:
        """Update a hypothesis' editable fields.

        Editable: ``title``, ``statement``, ``falsifier``, ``rationale``,
        ``owner``, ``origin``, ``universe_id``, ``tags``, ``metrics``. Status is
        changed through :meth:`set_status` so the transition is validated and
        recorded.

        Raises:
            KeyError: when the identifier is unknown.
            ValueError: on an unknown field or an empty required value.
        """
        editable = {
            "title",
            "statement",
            "falsifier",
            "rationale",
            "owner",
            "origin",
            "universe_id",
            "tags",
            "metrics",
        }
        unknown = set(changes) - editable
        if unknown:
            raise ValueError(f"cannot update {', '.join(sorted(unknown))}")
        with self._session() as session:
            row = session.get(HypothesisRow, hypothesis_id)
            if row is None:
                raise KeyError(f"unknown hypothesis {hypothesis_id!r}")
            for key in ("title", "statement", "falsifier"):
                if key in changes:
                    value = str(changes[key]).strip()
                    if not value:
                        raise ValueError(f"{key} must not be empty")
                    setattr(row, key, value)
            if "rationale" in changes:
                row.rationale = str(changes["rationale"]).strip() or None
            for key in ("owner", "origin", "universe_id"):
                if key in changes:
                    value = changes[key]
                    setattr(row, key, str(value).strip() or None if value else None)
            if "tags" in changes:
                row.tags = self._clean_tags(changes["tags"])
            if "metrics" in changes:
                row.metrics = dict(changes["metrics"] or {})
            row.updated_at = utcnow()
            return self._to_dataclass(session, row)

    def delete(self, hypothesis_id: str) -> None:
        """Delete a hypothesis and its evidence links.

        Experiments keep their ``hypothesis_id`` reference; the Strategy Lab
        shows them as orphaned rather than deleting research history.

        Raises:
            KeyError: when the identifier is unknown.
        """
        with self._session() as session:
            row = session.get(HypothesisRow, hypothesis_id)
            if row is None:
                raise KeyError(f"unknown hypothesis {hypothesis_id!r}")
            session.execute(
                delete(HypothesisEvidenceRow).where(
                    HypothesisEvidenceRow.hypothesis_id == hypothesis_id
                )
            )
            session.delete(row)
        logger.info("hypothesis %s deleted", hypothesis_id)

    def set_status(
        self,
        hypothesis_id: str,
        status: HypothesisStatus,
        *,
        note: str | None = None,
        actor: str | None = None,
    ) -> Hypothesis:
        """Transition a hypothesis, validating that the transition is legal.

        Raises:
            KeyError: when the identifier is unknown.
            ValueError: when the transition is not in :data:`ALLOWED_TRANSITIONS`.
        """
        target = HypothesisStatus(status)
        with self._session() as session:
            row = session.get(HypothesisRow, hypothesis_id)
            if row is None:
                raise KeyError(f"unknown hypothesis {hypothesis_id!r}")
            current = HypothesisStatus(row.status)
            if target is not current and target not in ALLOWED_TRANSITIONS[current]:
                allowed = ", ".join(sorted(str(s) for s in ALLOWED_TRANSITIONS[current]))
                raise ValueError(
                    f"cannot move {hypothesis_id} from {current} to {target}; allowed: {allowed}"
                )
            now = utcnow()
            history = list(row.status_history or [])
            history.append(
                {
                    "from": str(current),
                    "to": str(target),
                    "at": now.isoformat(),
                    "note": note,
                    "actor": actor,
                }
            )
            row.status = str(target)
            row.status_history = history
            row.updated_at = now
            updated = self._to_dataclass(session, row)
        logger.info("hypothesis %s %s -> %s", hypothesis_id, current, target)
        return updated

    async def set_status_and_publish(
        self,
        hypothesis_id: str,
        status: HypothesisStatus,
        *,
        note: str | None = None,
        actor: str | None = None,
    ) -> Hypothesis:
        """Transition a hypothesis and announce it on the ``lab`` channel."""
        updated = self.set_status(hypothesis_id, status, note=note, actor=actor)
        await self.bus.publish(
            LAB_CHANNEL,
            {
                "kind": "hypothesis",
                "phase": "status",
                "id": updated.id,
                "title": updated.title,
                "status": str(updated.status),
                "note": note,
                "actor": actor,
                "ts": utcnow().isoformat(),
            },
        )
        return updated

    # --- evidence ---------------------------------------------------------
    def add_evidence(
        self,
        hypothesis_id: str,
        kind: str,
        ref: str,
        *,
        label: str | None = None,
        url: str | None = None,
        supports: bool = True,
    ) -> EvidenceLink:
        """Attach one piece of evidence to a hypothesis.

        Raises:
            KeyError: when the hypothesis is unknown.
            ValueError: when the kind is not in :data:`EVIDENCE_KINDS` or the
                reference is empty.
        """
        kind_text = str(kind).strip().lower()
        ref_text = str(ref).strip()
        if kind_text not in EVIDENCE_KINDS:
            raise ValueError(
                f"unknown evidence kind {kind!r}; use one of {', '.join(sorted(EVIDENCE_KINDS))}"
            )
        if not ref_text:
            raise ValueError("evidence needs a reference")
        now = utcnow()
        with self._session() as session:
            if session.get(HypothesisRow, hypothesis_id) is None:
                raise KeyError(f"unknown hypothesis {hypothesis_id!r}")
            link = EvidenceLink(
                kind=kind_text, ref=ref_text, label=label, url=url, supports=supports
            )
            row = _evidence_row(hypothesis_id, link, now)
            session.add(row)
            session.flush()
            link.id = row.id
            link.added_at = now
            return link

    def remove_evidence(self, hypothesis_id: str, evidence_id: int) -> None:
        """Detach one evidence link.

        Raises:
            KeyError: when the link does not belong to the hypothesis.
        """
        with self._session() as session:
            row = session.get(HypothesisEvidenceRow, evidence_id)
            if row is None or row.hypothesis_id != hypothesis_id:
                raise KeyError(f"unknown evidence {evidence_id!r} on {hypothesis_id!r}")
            session.delete(row)

    def evidence(self, hypothesis_id: str) -> list[EvidenceLink]:
        """Return the evidence attached to a hypothesis.

        Raises:
            KeyError: when the hypothesis is unknown.
        """
        with self._session() as session:
            if session.get(HypothesisRow, hypothesis_id) is None:
                raise KeyError(f"unknown hypothesis {hypothesis_id!r}")
            return self._evidence(session, hypothesis_id)

    # --- derived ----------------------------------------------------------
    def stats(self, hypothesis_id: str) -> dict[str, Any]:
        """Return the real experiment/backtest counts shown on a hypothesis card.

        The counts are queried from the experiment and backtest tables, so the
        "8 experiments · 2 passed" line is never a stored guess.
        """
        from tradinglib.research.experiments import ExperimentEngine, ExperimentStatus

        engine = ExperimentEngine(self._session_factory, bus=self._bus)
        experiments = engine.list(hypothesis_id=hypothesis_id)
        runs = [run for exp in experiments for run in engine.runs(exp.id, limit=1000)]
        complete = [r for r in runs if r.status is ExperimentStatus.COMPLETE]
        passed = [r for r in complete if bool(r.results.get("passed_gates"))]
        return {
            "hypothesis_id": hypothesis_id,
            "experiments": len(experiments),
            "runs": len(runs),
            "complete_runs": len(complete),
            "passed_runs": len(passed),
            "failed_runs": len(complete) - len(passed),
            "trials": len(runs),
        }

    def summary(self) -> dict[str, Any]:
        """Return per-status counts for the Strategy Lab's hypothesis tab badge."""
        counts = {str(state): 0 for state in HypothesisStatus}
        with self._session() as session:
            for value in session.scalars(select(HypothesisRow.status)):
                counts[str(value)] = counts.get(str(value), 0) + 1
        return {"total": sum(counts.values()), "by_status": counts}


def _link_from_dict(payload: dict[str, Any]) -> EvidenceLink:
    """Build an :class:`EvidenceLink` from an API payload."""
    kind = str(payload.get("kind") or "note").strip().lower()
    if kind not in EVIDENCE_KINDS:
        raise ValueError(f"unknown evidence kind {kind!r}")
    ref = str(payload.get("ref") or "").strip()
    if not ref:
        raise ValueError("evidence needs a reference")
    return EvidenceLink(
        kind=kind,
        ref=ref,
        label=payload.get("label"),
        url=payload.get("url"),
        supports=bool(payload.get("supports", True)),
    )


def _evidence_row(hypothesis_id: str, link: EvidenceLink, now: datetime) -> HypothesisEvidenceRow:
    """Map an :class:`EvidenceLink` onto its ORM row."""
    return HypothesisEvidenceRow(
        hypothesis_id=hypothesis_id,
        kind=link.kind,
        ref=link.ref,
        label=link.label,
        url=link.url,
        supports=1 if link.supports else 0,
        added_at=link.added_at or now,
    )


_registry: HypothesisRegistry | None = None


def get_hypothesis_registry() -> HypothesisRegistry:
    """Return the process-wide hypothesis registry, creating it on first use."""
    global _registry
    if _registry is None:
        _registry = HypothesisRegistry()
    return _registry


def reset_hypothesis_registry(registry: HypothesisRegistry | None = None) -> HypothesisRegistry:
    """Replace the process-wide registry. Intended for tests."""
    global _registry
    _registry = registry if registry is not None else HypothesisRegistry()
    return _registry


__all__ = [
    "ALLOWED_TRANSITIONS",
    "EVIDENCE_KINDS",
    "EvidenceLink",
    "Hypothesis",
    "HypothesisEvidenceRow",
    "HypothesisRegistry",
    "HypothesisRow",
    "HypothesisStatus",
    "ensure_schema",
    "get_hypothesis_registry",
    "reset_hypothesis_registry",
]
