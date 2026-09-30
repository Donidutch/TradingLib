"""Findings: durable research conclusions produced by agent runs.

A finding is what an agent stage concluded, with the evidence that supports it
and the provenance that lets a person retrace it: which run produced it, which
stage, which agent, and which hypothesis (if any) it grew into. The runner's
``save_finding`` tool writes here, the Findings tab reads here, and the Overview
inbox merges the unacknowledged ones into the research queue.

Findings move through a small triage lifecycle - ``new`` when an agent records
one, ``triaged`` once a person has looked at it, ``promoted`` when it becomes a
hypothesis or a strategy candidate, ``dismissed`` when it does not survive
review. Nothing here scores a finding on its behalf: ``confidence`` is what the
agent claimed, and it is stored as the agent's own band and (when it gave one)
its own percentage.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import JSON, Boolean, DateTime, Float, String, Text, func, select
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from tradinglib.db import Base, get_engine, get_session_factory
from tradinglib.events import EventBus

logger = logging.getLogger(__name__)

#: Channel new findings are announced on (the UI raises a toast from it).
NOTIFICATIONS_CHANNEL: Final[str] = "notifications"

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9._+-]*")


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


def new_finding_id() -> str:
    """Return a fresh finding identifier (``FND-XXXXXX``)."""
    return f"FND-{uuid.uuid4().hex[:6].upper()}"


def tokenize(text: str) -> list[str]:
    """Split free text into lowercase search tokens."""
    return _TOKEN_RE.findall(text.lower())


class Confidence(StrEnum):
    """Confidence bands attached to a finding."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class FindingStatus(StrEnum):
    """Triage lifecycle of a finding."""

    NEW = "new"
    TRIAGED = "triaged"
    PROMOTED = "promoted"
    DISMISSED = "dismissed"


#: Percentage thresholds used when an agent gives a number but no band. The
#: mapping is fixed and documented so the same claim always lands in the same
#: band - it is a rendering rule, not a judgement about the finding.
_BAND_THRESHOLDS: Final[tuple[tuple[float, Confidence], ...]] = (
    (75.0, Confidence.HIGH),
    (50.0, Confidence.MEDIUM),
)


def band_for_percent(percent: float) -> Confidence:
    """Return the confidence band a percentage falls into."""
    for threshold, band in _BAND_THRESHOLDS:
        if percent >= threshold:
            return band
    return Confidence.LOW


def coerce_confidence(value: Any) -> tuple[Confidence, float | None]:
    """Normalise whatever an agent supplied into ``(band, percent)``.

    Accepts a band (``"high"``), a percentage (``82``), a fraction (``0.82``) or
    ``None``. An unusable value raises rather than silently becoming "medium":
    a confidence nobody stated is not a confidence.
    """
    if value is None or value == "":
        return Confidence.MEDIUM, None
    if isinstance(value, Confidence):
        return value, None
    if isinstance(value, bool):
        raise ValueError("confidence must be a band or a number, not a boolean")
    if isinstance(value, (int, float)):
        percent = float(value) * 100.0 if 0.0 <= float(value) <= 1.0 else float(value)
        if not 0.0 <= percent <= 100.0:
            raise ValueError(f"confidence {value!r} is outside 0-100")
        return band_for_percent(percent), round(percent, 2)
    text = str(value).strip().lower().rstrip("%")
    try:
        return coerce_confidence(float(text))
    except ValueError:
        pass
    try:
        return Confidence(text), None
    except ValueError as exc:
        raise ValueError(
            f"unknown confidence {value!r}; use low/medium/high or a 0-100 number"
        ) from exc


@dataclass(slots=True)
class Finding:
    """A single conclusion with its evidence and provenance."""

    id: str
    title: str
    summary: str
    confidence: Confidence = Confidence.MEDIUM
    confidence_pct: float | None = None
    status: FindingStatus = FindingStatus.NEW
    symbols: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    run_id: str | None = None
    stage_id: str | None = None
    agent_id: str | None = None
    hypothesis_id: str | None = None
    evidence: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    acknowledged: bool = False
    acknowledged_by: str | None = None

    @property
    def symbol_label(self) -> str:
        """One-line symbol label for the Findings table."""
        if not self.symbols:
            return "Universe"
        if len(self.symbols) == 1:
            return self.symbols[0]
        return f"{self.symbols[0]} +{len(self.symbols) - 1}"

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping for the API layer."""
        return {
            "id": self.id,
            "title": self.title,
            "summary": self.summary,
            "confidence": str(self.confidence),
            "confidence_pct": self.confidence_pct,
            "status": str(self.status),
            "symbols": list(self.symbols),
            "symbol_label": self.symbol_label,
            "tags": list(self.tags),
            "run_id": self.run_id,
            "stage_id": self.stage_id,
            "agent_id": self.agent_id,
            "hypothesis_id": self.hypothesis_id,
            "evidence": [dict(item) for item in self.evidence],
            "evidence_count": len(self.evidence),
            "acknowledged": self.acknowledged,
            "acknowledged_by": self.acknowledged_by,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }


class AgentFindingRow(Base):
    """SQLite row for one finding."""

    __tablename__ = "agent_findings"

    id: Mapped[str] = mapped_column(String(24), primary_key=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    summary: Mapped[str] = mapped_column(Text(), nullable=False, default="")
    confidence: Mapped[str] = mapped_column(String(8), nullable=False, default=Confidence.MEDIUM)
    confidence_pct: Mapped[float | None] = mapped_column(Float(), default=None)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=FindingStatus.NEW)
    symbols: Mapped[list[str]] = mapped_column(JSON(), nullable=False, default=list)
    tags: Mapped[list[str]] = mapped_column(JSON(), nullable=False, default=list)
    run_id: Mapped[str | None] = mapped_column(String(32), default=None, index=True)
    stage_id: Mapped[str | None] = mapped_column(String(64), default=None)
    agent_id: Mapped[str | None] = mapped_column(String(64), default=None, index=True)
    hypothesis_id: Mapped[str | None] = mapped_column(String(64), default=None)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSON(), nullable=False, default=list)
    acknowledged: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    acknowledged_by: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)


_OWNED_TABLES = (AgentFindingRow.__table__,)


def ensure_schema() -> None:
    """Create the findings table if it does not exist yet."""
    Base.metadata.create_all(get_engine(), tables=list(_OWNED_TABLES))


class FindingsStore:
    """CRUD and search over findings; feeds the Findings tab and the inbox."""

    #: Fields :meth:`create` and :meth:`update` accept beyond title/summary.
    FIELDS: Final[frozenset[str]] = frozenset(
        {
            "confidence",
            "confidence_pct",
            "status",
            "symbols",
            "tags",
            "run_id",
            "stage_id",
            "agent_id",
            "hypothesis_id",
            "evidence",
            "acknowledged",
            "acknowledged_by",
        }
    )

    def __init__(
        self,
        session_factory: sessionmaker[Session] | None = None,
        *,
        bus: EventBus | None = None,
    ) -> None:
        """Create the store.

        Args:
            session_factory: SQLAlchemy session factory; defaults to the
                process-wide one from :mod:`tradinglib.db`.
            bus: event bus new findings are announced on.
        """
        self._session_factory = session_factory
        self._bus = bus
        ensure_schema()

    # --- plumbing ---------------------------------------------------------
    @contextmanager
    def _session(self) -> Iterator[Session]:
        """Transactional session scope bound to this store."""
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

    def _announce(self, action: str, finding: Finding) -> None:
        """Publish a finding change, when a bus is attached."""
        if self._bus is None:
            return
        try:
            self._bus.publish_nowait(
                NOTIFICATIONS_CHANNEL,
                {
                    "kind": "finding",
                    "action": action,
                    "finding_id": finding.id,
                    "title": finding.title,
                    "symbols": list(finding.symbols),
                    "run_id": finding.run_id,
                    "message": f"Finding {action}: {finding.title}",
                },
            )
        except RuntimeError:
            # No running loop (synchronous CLI use). The row is what matters.
            logger.debug("no event loop to announce finding %s on", finding.id)

    @staticmethod
    def _to_dataclass(row: AgentFindingRow) -> Finding:
        """Map an ORM row onto the dataclass."""
        return Finding(
            id=row.id,
            title=row.title,
            summary=row.summary,
            confidence=Confidence(row.confidence),
            confidence_pct=row.confidence_pct,
            status=FindingStatus(row.status),
            symbols=list(row.symbols or []),
            tags=list(row.tags or []),
            run_id=row.run_id,
            stage_id=row.stage_id,
            agent_id=row.agent_id,
            hypothesis_id=row.hypothesis_id,
            evidence=[dict(item) for item in (row.evidence or [])],
            created_at=_aware(row.created_at),
            updated_at=_aware(row.updated_at),
            acknowledged=bool(row.acknowledged),
            acknowledged_by=row.acknowledged_by,
        )

    @staticmethod
    def _clean_list(values: Iterable[Any] | None, *, upper: bool = False) -> list[str]:
        """Normalise a string list: trimmed, de-duplicated, order preserved."""
        out: list[str] = []
        for value in values or []:
            text = str(value).strip()
            if upper:
                text = text.upper()
            if text and text not in out:
                out.append(text)
        return out

    @staticmethod
    def _clean_evidence(values: Iterable[Any] | None) -> list[dict[str, Any]]:
        """Normalise evidence entries to mappings.

        A bare string is kept as ``{"note": ...}`` rather than dropped: an agent
        that cites its source in prose has still cited it.
        """
        out: list[dict[str, Any]] = []
        for value in values or []:
            if isinstance(value, Mapping):
                out.append({str(k): v for k, v in value.items()})
            else:
                out.append({"note": str(value)})
        return out

    # --- reads ------------------------------------------------------------
    def list(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        tag: str | None = None,
        symbol: str | None = None,
        status: FindingStatus | str | None = None,
        run_id: str | None = None,
        agent_id: str | None = None,
        acknowledged: bool | None = None,
        query: str | None = None,
    ) -> list[Finding]:
        """Return findings, newest first, optionally filtered.

        ``limit=0`` returns every match (used by :meth:`search`, which ranks the
        whole table); any other value caps the page.
        """
        statement = select(AgentFindingRow).order_by(
            AgentFindingRow.created_at.desc(), AgentFindingRow.id.desc()
        )
        if status is not None:
            statement = statement.where(AgentFindingRow.status == str(FindingStatus(str(status))))
        if run_id is not None:
            statement = statement.where(AgentFindingRow.run_id == run_id)
        if agent_id is not None:
            statement = statement.where(AgentFindingRow.agent_id == agent_id)
        if acknowledged is not None:
            statement = statement.where(AgentFindingRow.acknowledged.is_(acknowledged))
        with self._session() as session:
            items = [self._to_dataclass(row) for row in session.scalars(statement)]
        # JSON list columns are filtered in Python: SQLite cannot index into them
        # portably, and the finding count is a research artefact, not a feed.
        if tag:
            needle = tag.strip().lower()
            items = [item for item in items if needle in {t.lower() for t in item.tags}]
        if symbol:
            wanted = symbol.strip().upper()
            items = [item for item in items if wanted in item.symbols]
        if query:
            terms = tokenize(query)
            items = [item for item in items if self._score(item, terms) > 0]
        return items[offset : offset + max(1, int(limit))] if limit else items[offset:]

    def get(self, finding_id: str) -> Finding:
        """Return one finding.

        Raises:
            KeyError: when the identifier is unknown.
        """
        with self._session() as session:
            row = session.get(AgentFindingRow, finding_id)
            if row is None:
                raise KeyError(f"unknown finding {finding_id!r}")
            return self._to_dataclass(row)

    def exists(self, finding_id: str) -> bool:
        """Return whether a finding with this identifier exists."""
        with self._session() as session:
            return session.get(AgentFindingRow, finding_id) is not None

    # --- writes -----------------------------------------------------------
    def create(self, title: str, summary: str, **fields: Any) -> Finding:
        """Record a finding (the tool agents call to save conclusions).

        Raises:
            ValueError: when the title or summary is empty, when an unknown
                field is passed, or when a value does not validate.
        """
        headline = str(title or "").strip()
        body = str(summary or "").strip()
        if not headline:
            raise ValueError("a finding needs a title")
        if not body:
            raise ValueError("a finding needs a summary - the claim itself")
        unknown = set(fields) - self.FIELDS
        if unknown:
            raise ValueError(f"unknown field(s): {', '.join(sorted(unknown))}")
        band, percent = coerce_confidence(fields.get("confidence"))
        if fields.get("confidence_pct") is not None:
            percent = float(fields["confidence_pct"])
            if not 0.0 <= percent <= 100.0:
                raise ValueError("confidence_pct must be between 0 and 100")
            if fields.get("confidence") is None:
                band = band_for_percent(percent)
        now = utcnow()
        row = AgentFindingRow(
            id=new_finding_id(),
            title=headline,
            summary=body,
            confidence=str(band),
            confidence_pct=percent,
            status=str(FindingStatus(str(fields.get("status") or FindingStatus.NEW))),
            symbols=self._clean_list(fields.get("symbols"), upper=True),
            tags=self._clean_list(fields.get("tags")),
            run_id=fields.get("run_id"),
            stage_id=fields.get("stage_id"),
            agent_id=fields.get("agent_id"),
            hypothesis_id=fields.get("hypothesis_id"),
            evidence=self._clean_evidence(fields.get("evidence")),
            acknowledged=bool(fields.get("acknowledged", False)),
            acknowledged_by=fields.get("acknowledged_by"),
            created_at=now,
            updated_at=now,
        )
        with self._session() as session:
            session.add(row)
            session.flush()
            finding = self._to_dataclass(row)
        self._announce("created", finding)
        logger.info("finding %s recorded (run=%s)", finding.id, finding.run_id)
        return finding

    def update(self, finding_id: str, **changes: Any) -> Finding:
        """Update a finding.

        Raises:
            KeyError: when the identifier is unknown.
            ValueError: when a field is unknown or a value does not validate.
        """
        allowed = self.FIELDS | {"title", "summary"}
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unknown field(s): {', '.join(sorted(unknown))}")
        with self._session() as session:
            row = session.get(AgentFindingRow, finding_id)
            if row is None:
                raise KeyError(f"unknown finding {finding_id!r}")
            if "title" in changes:
                headline = str(changes["title"]).strip()
                if not headline:
                    raise ValueError("a finding needs a title")
                row.title = headline
            if "summary" in changes:
                body = str(changes["summary"]).strip()
                if not body:
                    raise ValueError("a finding needs a summary")
                row.summary = body
            if "confidence" in changes:
                band, percent = coerce_confidence(changes["confidence"])
                row.confidence = str(band)
                if percent is not None:
                    row.confidence_pct = percent
            if "confidence_pct" in changes:
                value = changes["confidence_pct"]
                if value is None:
                    row.confidence_pct = None
                else:
                    percent = float(value)
                    if not 0.0 <= percent <= 100.0:
                        raise ValueError("confidence_pct must be between 0 and 100")
                    row.confidence_pct = percent
                    if "confidence" not in changes:
                        row.confidence = str(band_for_percent(percent))
            if "status" in changes:
                row.status = str(FindingStatus(str(changes["status"])))
            if "symbols" in changes:
                row.symbols = self._clean_list(changes["symbols"], upper=True)
            if "tags" in changes:
                row.tags = self._clean_list(changes["tags"])
            for key in ("run_id", "stage_id", "agent_id", "hypothesis_id", "acknowledged_by"):
                if key in changes:
                    setattr(row, key, changes[key])
            if "evidence" in changes:
                row.evidence = self._clean_evidence(changes["evidence"])
            if "acknowledged" in changes:
                row.acknowledged = bool(changes["acknowledged"])
            row.updated_at = utcnow()
            session.flush()
            finding = self._to_dataclass(row)
        self._announce("updated", finding)
        return finding

    def delete(self, finding_id: str) -> None:
        """Delete a finding.

        Raises:
            KeyError: when the identifier is unknown.
        """
        with self._session() as session:
            row = session.get(AgentFindingRow, finding_id)
            if row is None:
                raise KeyError(f"unknown finding {finding_id!r}")
            finding = self._to_dataclass(row)
            session.delete(row)
        self._announce("deleted", finding)

    def acknowledge(self, finding_id: str, *, by: str = "user") -> Finding:
        """Mark a finding as seen, moving it out of the inbox."""
        finding = self.get(finding_id)
        status = (
            FindingStatus.TRIAGED if finding.status is FindingStatus.NEW else finding.status
        )
        return self.update(finding_id, acknowledged=True, acknowledged_by=by, status=status)

    def promote(self, finding_id: str, *, hypothesis_id: str | None = None) -> Finding:
        """Mark a finding as promoted, optionally linking the hypothesis it became."""
        changes: dict[str, Any] = {"status": FindingStatus.PROMOTED, "acknowledged": True}
        if hypothesis_id is not None:
            changes["hypothesis_id"] = hypothesis_id
        return self.update(finding_id, **changes)

    def dismiss(self, finding_id: str, *, by: str = "user") -> Finding:
        """Mark a finding as dismissed after review."""
        return self.update(
            finding_id,
            status=FindingStatus.DISMISSED,
            acknowledged=True,
            acknowledged_by=by,
        )

    # --- search -----------------------------------------------------------
    @staticmethod
    def _score(finding: Finding, terms: Sequence[str]) -> int:
        """Score a finding against search terms.

        Title matches weigh most, then symbols and tags (they are what a person
        searches by), then the body. A term nothing matches contributes nothing,
        so a query whose terms are all absent scores zero and is filtered out.
        """
        if not terms:
            return 0
        title = finding.title.lower()
        body = finding.summary.lower()
        labels = " ".join(finding.tags + finding.symbols).lower()
        identity = " ".join(
            value for value in (finding.id, finding.run_id, finding.hypothesis_id) if value
        ).lower()
        score = 0
        for term in terms:
            if term in identity:
                score += 6
            if term in title:
                score += 4
            if term in labels:
                score += 2
            if term in body:
                score += 1
        return score

    def search(self, query: str, *, limit: int = 25) -> list[Finding]:
        """Search titles, summaries, tags, symbols and ids.

        Ranked by where the terms hit; ties break on recency. Findings are short
        records with a handful of indexed words, so this is a scan-and-score over
        the table rather than an FTS index (the knowledge base, whose documents
        are long-form, uses SQLite FTS5).
        """
        terms = tokenize(query or "")
        if not terms:
            return []
        candidates = self.list(limit=0)
        scored = [(self._score(item, terms), item) for item in candidates]
        hits = [(score, item) for score, item in scored if score > 0]
        hits.sort(
            key=lambda pair: (
                -pair[0],
                -(pair[1].created_at or datetime.min.replace(tzinfo=timezone.utc)).timestamp(),
            )
        )
        return [item for _, item in hits[: max(1, int(limit))]]

    # --- feeds ------------------------------------------------------------
    def inbox(self, *, limit: int = 25) -> list[dict[str, Any]]:
        """Return unacknowledged findings shaped for the agent inbox feed.

        The Overview inbox merges these with the approval queue, so the payload
        carries the same fields that view renders: what it is, why it is here,
        where it came from and where clicking it should go.
        """
        items = self.list(limit=limit, acknowledged=False)
        now = utcnow()
        feed: list[dict[str, Any]] = []
        for finding in items:
            created = finding.created_at or now
            feed.append(
                {
                    "id": finding.id,
                    "type": "Finding",
                    "status": str(finding.status),
                    "urgency": "High" if finding.confidence is Confidence.HIGH else "Normal",
                    "symbol": finding.symbol_label,
                    "symbols": list(finding.symbols),
                    "title": finding.title,
                    "reason": finding.summary,
                    "source": finding.run_id,
                    "stage_id": finding.stage_id,
                    "agent_id": finding.agent_id,
                    "confidence": str(finding.confidence),
                    "confidence_pct": finding.confidence_pct,
                    "evidence_count": len(finding.evidence),
                    "created_at": _iso(created),
                    "age_seconds": max(0.0, (now - created).total_seconds()),
                    "action": "Review finding",
                    "route": "findings",
                    "context": {"findingId": finding.id, "runId": finding.run_id},
                }
            )
        return feed

    def counts(self) -> dict[str, int]:
        """Return real finding counts for the Findings tab header."""
        with self._session() as session:
            rows = list(session.scalars(select(AgentFindingRow.status)))
            unacknowledged = int(
                session.scalar(
                    select(func.count())
                    .select_from(AgentFindingRow)
                    .where(AgentFindingRow.acknowledged.is_(False))
                )
                or 0
            )
        counts = {str(status): 0 for status in FindingStatus}
        for value in rows:
            counts[str(value)] = counts.get(str(value), 0) + 1
        counts["total"] = len(rows)
        counts["unacknowledged"] = unacknowledged
        return counts

    def tags(self) -> list[str]:
        """Distinct tags across every finding, for the filter control."""
        with self._session() as session:
            rows = list(session.scalars(select(AgentFindingRow.tags)))
        seen: set[str] = set()
        for value in rows:
            seen.update(str(tag) for tag in (value or []))
        return sorted(seen)

    def symbols(self) -> list[str]:
        """Distinct symbols across every finding."""
        with self._session() as session:
            rows = list(session.scalars(select(AgentFindingRow.symbols)))
        seen: set[str] = set()
        for value in rows:
            seen.update(str(symbol).upper() for symbol in (value or []))
        return sorted(seen)


_store: FindingsStore | None = None


def get_findings_store() -> FindingsStore:
    """Return the process-wide findings store, creating it on first use."""
    global _store
    if _store is None:
        _store = FindingsStore()
    return _store


def reset_findings_store(store: FindingsStore | None = None) -> FindingsStore:
    """Install (or forget) the process-wide findings store. Returns it."""
    global _store
    _store = store if store is not None else FindingsStore()
    return _store


# --------------------------------------------------------------------------
# evidence lineage
# --------------------------------------------------------------------------
#: The seven columns of the Evidence Explorer, in the order research flows.
LINEAGE_TYPES: Final[tuple[str, ...]] = (
    "Sources",
    "Claims",
    "Findings",
    "Hypotheses",
    "Experiments",
    "Validation",
    "Decision",
)

#: Words in an evidence note that mark it as arguing *against* the claim.
_CONTRADICTS: Final[tuple[str, ...]] = (
    "weaken",
    "contradict",
    "against",
    "fails",
    "failed",
    "reduces",
    "outstanding",
    "risk",
    "counter",
)


@dataclass(slots=True)
class LineageNode:
    """One node of the evidence graph.

    ``upstream`` and ``downstream`` hold node ids, so the graph is navigable in
    both directions without a second index. ``relation`` is the node's stance
    toward the object it feeds - supports, contradicts, or a lifecycle word for
    the objects that do not take a side (an experiment neither supports nor
    contradicts; it either ran or it did not).
    """

    id: str
    type: str
    label: str
    detail: str = ""
    relation: str = "supports"
    confidence: float | None = None
    freshness: str | None = None
    agent: str | None = None
    run_id: str | None = None
    upstream: list[str] = field(default_factory=list)
    downstream: list[str] = field(default_factory=list)
    route: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping."""
        return {
            "id": self.id,
            "type": self.type,
            "label": self.label,
            "detail": self.detail,
            "relation": self.relation,
            "confidence": self.confidence,
            "freshness": self.freshness,
            "agent": self.agent,
            "run_id": self.run_id,
            "upstream": list(self.upstream),
            "downstream": list(self.downstream),
            "route": dict(self.route),
        }


def _relation_for(text: str) -> str:
    """Classify an evidence note as supporting or contradicting its claim."""
    lowered = text.lower()
    return "contradicts" if any(word in lowered for word in _CONTRADICTS) else "supports"


def _freshness(moment: datetime | None, *, now: datetime | None = None) -> str | None:
    """Render how old an object is, in the Explorer's compact wording."""
    if moment is None:
        return None
    reference = now or utcnow()
    seconds = max(0.0, (reference - _aware(moment)).total_seconds())
    if seconds < 3600:
        return f"{int(seconds // 60)}m old"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h old"
    return f"{int(seconds // 86400)}d old"


class EvidenceGraph:
    """Builds the Evidence Explorer's lineage graph from what is really stored.

    The chain is not invented: every edge already exists as a foreign key in
    the workspace. A finding names the run and stage that produced it and the
    hypothesis it was filed against; experiments name their hypothesis;
    backtests name their experiment; validation reports name their backtest;
    promotions and approvals name what they decided on. This class walks those
    references and materialises them as the seven-column graph the reference
    draws, so "trace this claim" answers with the real objects.

    Engines are injected (or looked up lazily) so the graph can be built in a
    process where only some of them are mounted; a missing engine drops its
    column with a note rather than fabricating nodes.
    """

    def __init__(
        self,
        findings: FindingsStore | None = None,
        *,
        hypotheses: Any | None = None,
        experiments: Any | None = None,
        backtests: Any | None = None,
        validation: Any | None = None,
        approvals: Any | None = None,
    ) -> None:
        """Create the builder bound to the stores it reads."""
        self._findings = findings
        self._hypotheses = hypotheses
        self._experiments = experiments
        self._backtests = backtests
        self._validation = validation
        self._approvals = approvals

    @property
    def findings(self) -> FindingsStore:
        """The findings store the graph starts from."""
        if self._findings is None:
            self._findings = get_findings_store()
        return self._findings

    def _engine(self, name: str) -> Any | None:
        """Return one optional engine, or ``None`` when it cannot be built.

        Passing ``False`` for an engine in the constructor disables that column
        explicitly, which is how a caller (or a test) says "do not reach into
        the research side" without relying on an import failing.
        """
        cached = getattr(self, f"_{name}")
        if cached is False:
            return None
        if cached is not None:
            return cached
        factories = {
            "hypotheses": ("tradinglib.research.hypotheses", "get_hypothesis_registry"),
            "experiments": ("tradinglib.research.experiments", "get_experiment_engine"),
            "backtests": ("tradinglib.research.backtest", "get_backtest_engine"),
            "validation": ("tradinglib.research.validation", "get_validation_engine"),
            "approvals": ("tradinglib.agents.runner", "get_pipeline_runner"),
        }
        module_name, factory_name = factories[name]
        try:
            module = __import__(module_name, fromlist=[factory_name])
            engine = getattr(module, factory_name)()
        except Exception:  # noqa: BLE001 - an unavailable engine drops its column
            logger.debug("lineage: %s engine unavailable", name, exc_info=True)
            return None
        setattr(self, f"_{name}", engine)
        return engine

    # --- building ---------------------------------------------------------
    def build(
        self, *, finding_id: str | None = None, run_id: str | None = None, limit: int = 40
    ) -> dict[str, Any]:
        """Return the lineage graph, optionally centred on one object.

        Args:
            finding_id: build the chain around this finding only.
            run_id: build the chain around every finding of one run.
            limit: cap on how many findings seed the graph.

        Returns:
            ``{types, nodes, selected, counts, notes}`` - ``notes`` lists the
            columns that could not be filled and why.
        """
        notes: list[str] = []
        seeds = self._seed_findings(finding_id=finding_id, run_id=run_id, limit=limit)
        nodes: dict[str, LineageNode] = {}
        for finding in seeds:
            self._add_finding(finding, nodes)
        self._add_research_chain(nodes, notes)
        self._link_downstream(nodes)
        selected = None
        if finding_id and f"FIND-{finding_id}" in nodes:
            selected = f"FIND-{finding_id}"
        elif nodes:
            selected = next(
                (n.id for n in nodes.values() if n.type == "Findings"), next(iter(nodes))
            )
        counts = {kind: sum(1 for n in nodes.values() if n.type == kind) for kind in LINEAGE_TYPES}
        return {
            "types": list(LINEAGE_TYPES),
            "nodes": [node.as_dict() for node in nodes.values()],
            "counts": counts,
            "selected": selected,
            "notes": notes,
        }

    def _seed_findings(
        self, *, finding_id: str | None, run_id: str | None, limit: int
    ) -> list[Finding]:
        """Return the findings the graph is built around."""
        if finding_id:
            try:
                return [self.findings.get(finding_id)]
            except KeyError:
                return []
        return self.findings.list(limit=max(1, limit), run_id=run_id)

    def _add_finding(self, finding: Finding, nodes: dict[str, LineageNode]) -> None:
        """Add one finding, its evidence claims and their sources."""
        node_id = f"FIND-{finding.id}"
        claim_ids: list[str] = []
        for index, item in enumerate(finding.evidence):
            claim_id = f"CLAIM-{finding.id}-{index}"
            text = str(
                item.get("note")
                or item.get("summary")
                or item.get("title")
                or item.get("ref")
                or "evidence"
            )
            origin = str(
                item.get("source") or item.get("provider") or item.get("dataset") or ""
            ).strip()
            source_id = f"SRC-{origin.lower().replace(' ', '-')}" if origin else None
            if source_id and source_id not in nodes:
                nodes[source_id] = LineageNode(
                    id=source_id,
                    type="Sources",
                    label=origin,
                    detail=f"Data source cited by {finding.id}.",
                    relation="input",
                    route={"page": "data", "sub": "source-catalog"},
                )
            nodes[claim_id] = LineageNode(
                id=claim_id,
                type="Claims",
                label=text[:90],
                detail=text,
                relation=_relation_for(text),
                confidence=finding.confidence_pct,
                freshness=_freshness(finding.created_at),
                agent=finding.agent_id,
                run_id=finding.run_id,
                upstream=[source_id] if source_id else [],
                downstream=[node_id],
            )
            claim_ids.append(claim_id)

        nodes[node_id] = LineageNode(
            id=node_id,
            type="Findings",
            label=finding.title,
            detail=finding.summary,
            relation=str(finding.status),
            confidence=finding.confidence_pct,
            freshness=_freshness(finding.updated_at or finding.created_at),
            agent=finding.agent_id,
            run_id=finding.run_id,
            upstream=claim_ids,
            downstream=[f"HYP-{finding.hypothesis_id}"] if finding.hypothesis_id else [],
            route={"page": "agents", "sub": "findings", "id": finding.id},
        )

    def _add_research_chain(self, nodes: dict[str, LineageNode], notes: list[str]) -> None:
        """Extend each finding's hypothesis into experiments, validation, decisions."""
        hypothesis_ids = {
            node.downstream[0][4:]
            for node in list(nodes.values())
            if node.type == "Findings" and node.downstream
        }
        if not hypothesis_ids:
            notes.append(
                "no finding is filed against a hypothesis yet, so the chain stops at Findings"
            )
            return

        registry = self._engine("hypotheses")
        experiments = self._engine("experiments")
        backtests = self._engine("backtests")
        validation = self._engine("validation")

        for hypothesis_id in sorted(hypothesis_ids):
            record = None
            if registry is not None:
                try:
                    record = registry.get(hypothesis_id)
                except Exception:  # noqa: BLE001 - a dangling reference is data
                    record = None
            nodes[f"HYP-{hypothesis_id}"] = LineageNode(
                id=f"HYP-{hypothesis_id}",
                type="Hypotheses",
                label=getattr(record, "title", hypothesis_id),
                detail=getattr(record, "statement", "referenced by a finding but not stored"),
                relation=str(getattr(record, "status", "unknown")),
                freshness=_freshness(getattr(record, "updated_at", None)),
                route={"page": "lab", "sub": "hypotheses", "id": hypothesis_id},
            )
            if experiments is None:
                continue
            try:
                runs = experiments.list(hypothesis_id=hypothesis_id)
            except Exception:  # noqa: BLE001
                logger.debug("lineage: experiments for %s", hypothesis_id, exc_info=True)
                continue
            for experiment in runs:
                exp_id = f"EXP-{experiment.id}"
                nodes[exp_id] = LineageNode(
                    id=exp_id,
                    type="Experiments",
                    label=getattr(experiment, "name", experiment.id),
                    detail=getattr(experiment, "objective", "") or "",
                    relation=str(getattr(experiment, "stage", "")),
                    freshness=_freshness(getattr(experiment, "updated_at", None)),
                    upstream=[f"HYP-{hypothesis_id}"],
                    route={"page": "lab", "sub": "experiments", "id": experiment.id},
                )
                if backtests is None or validation is None:
                    continue
                try:
                    runs_for_experiment = backtests.list_runs(experiment_id=experiment.id, limit=8)
                except Exception:  # noqa: BLE001
                    continue
                for run in runs_for_experiment:
                    try:
                        reports = validation.reports(backtest_id=run.id)
                    except Exception:  # noqa: BLE001
                        reports = []
                    for report in reports:
                        val_id = f"VAL-{report.id}"
                        nodes[val_id] = LineageNode(
                            id=val_id,
                            type="Validation",
                            label=f"{run.name} · {report.verdict}",
                            detail=(
                                f"{sum(1 for g in report.gates if g.passed)}/"
                                f"{len(report.gates)} gates passed"
                            ),
                            relation=report.verdict,
                            confidence=round(report.score, 1),
                            freshness=_freshness(report.evaluated_at),
                            upstream=[exp_id],
                            route={"page": "lab", "sub": "validation", "id": run.id},
                        )
                        if report.promoted:
                            dec_id = f"DEC-{report.id}"
                            nodes[dec_id] = LineageNode(
                                id=dec_id,
                                type="Decision",
                                label=f"Promoted {run.name}",
                                detail=report.comment or "promoted through the gate suite",
                                relation="promoted",
                                freshness=_freshness(report.evaluated_at),
                                upstream=[val_id],
                                route={"page": "lab", "sub": "pipeline", "id": run.id},
                            )
        self._add_decisions(nodes, notes)

    def _add_decisions(self, nodes: dict[str, LineageNode], notes: list[str]) -> None:
        """Attach human approvals as terminal decision nodes."""
        runner = self._engine("approvals")
        if runner is None:
            return
        run_ids = {node.run_id for node in nodes.values() if node.run_id}
        if not run_ids:
            return
        for run_id in sorted(run_ids):
            try:
                records = runner.approvals(run_id=run_id)
            except Exception:  # noqa: BLE001
                continue
            for record in records:
                dec_id = f"DEC-{record.id}"
                nodes[dec_id] = LineageNode(
                    id=dec_id,
                    type="Decision",
                    label=record.title,
                    detail=record.summary or "human approval gate",
                    relation=str(record.state),
                    freshness=_freshness(record.decided_at or record.created_at),
                    run_id=run_id,
                    upstream=[
                        node.id
                        for node in nodes.values()
                        if node.type == "Findings" and node.run_id == run_id
                    ],
                    route={"page": "agents", "sub": "approval", "id": record.id},
                )
        if not any(node.type == "Decision" for node in nodes.values()):
            notes.append("no decision has been recorded for these objects yet")

    @staticmethod
    def _link_downstream(nodes: dict[str, LineageNode]) -> None:
        """Make every declared upstream edge navigable from the other side."""
        for node in nodes.values():
            for parent in node.upstream:
                target = nodes.get(parent)
                if target is not None and node.id not in target.downstream:
                    target.downstream.append(node.id)
        for node in nodes.values():
            node.upstream = [ref for ref in node.upstream if ref in nodes]
            node.downstream = [ref for ref in node.downstream if ref in nodes]

    # --- traversal --------------------------------------------------------
    @staticmethod
    def related(graph: dict[str, Any], node_id: str, focus: str = "all") -> set[str]:
        """Return the ids reachable from ``node_id`` under one focus.

        Args:
            graph: the payload returned by :meth:`build`.
            node_id: the node to walk from.
            focus: ``all``, ``upstream`` or ``downstream``.
        """
        by_id = {node["id"]: node for node in graph["nodes"]}
        if node_id not in by_id:
            return set()
        seen = {node_id}
        directions = (
            ["upstream", "downstream"]
            if focus == "all"
            else [focus if focus in {"upstream", "downstream"} else "upstream"]
        )
        stack = [node_id]
        while stack:
            current = by_id.get(stack.pop())
            if current is None:
                continue
            for direction in directions:
                for ref in current.get(direction) or []:
                    if ref not in seen:
                        seen.add(ref)
                        stack.append(ref)
        return seen


__all__ = [
    "AgentFindingRow",
    "Confidence",
    "EvidenceGraph",
    "LINEAGE_TYPES",
    "LineageNode",
    "Finding",
    "FindingStatus",
    "FindingsStore",
    "band_for_percent",
    "coerce_confidence",
    "ensure_schema",
    "get_findings_store",
    "new_finding_id",
    "reset_findings_store",
    "tokenize",
]
