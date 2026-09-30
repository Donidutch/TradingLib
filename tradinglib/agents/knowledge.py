"""Knowledge base: notes and documents agents read from and write to.

An entry is a markdown document with provenance: the run and stage that
produced it, the findings it was distilled from, and the sources it cites. The
runner's ``search_knowledge`` tool reads through :meth:`KnowledgeBase.search`,
and the Knowledge tab reads the same store.

Retrieval is real full-text search. The entries are long-form, so the store
maintains a SQLite **FTS5** index (``agent_knowledge_fts``) alongside the table
and ranks results with ``bm25``. FTS5 is compiled into virtually every SQLite
build, but when a build lacks it the store says so (:attr:`KnowledgeBase.fts`)
and falls back to a scored substring scan rather than silently returning
nothing.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Final

from sqlalchemy import JSON, DateTime, String, Text, func, select, text
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from tradinglib.db import Base, get_engine, get_session_factory
from tradinglib.events import EventBus

logger = logging.getLogger(__name__)

#: Channel knowledge changes are announced on.
NOTIFICATIONS_CHANNEL: Final[str] = "notifications"

#: Name of the FTS5 index shadowing :class:`AgentKnowledgeRow`.
FTS_TABLE: Final[str] = "agent_knowledge_fts"

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9._+-]*")

#: Entry kinds the UI groups by. Free text is allowed - this is the vocabulary
#: the shipped agents use, not a closed set.
KNOWN_KINDS: Final[tuple[str, ...]] = (
    "note",
    "brief",
    "definition",
    "methodology",
    "source",
    "lesson",
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


def new_entry_id() -> str:
    """Return a fresh knowledge identifier (``KB-XXXXXX``)."""
    return f"KB-{uuid.uuid4().hex[:6].upper()}"


def tokenize(value: str) -> list[str]:
    """Split free text into lowercase search tokens."""
    return _TOKEN_RE.findall(value.lower())


def fts_query(query: str, *, operator: str = "AND") -> str:
    """Build a safe FTS5 MATCH expression from a user query.

    Every token is quoted (so punctuation, ``NOT`` and stray quotes cannot turn
    into FTS operators) and given a prefix wildcard, then joined with ``AND`` or
    ``OR``. Returns an empty string when the query has no usable tokens.
    """
    tokens = tokenize(query)
    if not tokens:
        return ""
    joiner = f" {operator} "
    return joiner.join(f'"{token}"*' for token in tokens)


@dataclass(slots=True)
class KnowledgeEntry:
    """One knowledge-base document."""

    id: str
    title: str
    body: str
    kind: str = "note"
    tags: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    source_run_id: str | None = None
    source_stage_id: str | None = None
    finding_ids: list[str] = field(default_factory=list)
    sources: list[dict[str, Any]] = field(default_factory=list)
    author: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def as_dict(self, *, include_body: bool = True) -> dict[str, Any]:
        """Return a JSON-serialisable mapping for the API layer."""
        payload: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "kind": self.kind,
            "tags": list(self.tags),
            "symbols": list(self.symbols),
            "source_run_id": self.source_run_id,
            "source_stage_id": self.source_stage_id,
            "finding_ids": list(self.finding_ids),
            "sources": [dict(item) for item in self.sources],
            "author": self.author,
            "word_count": len(self.body.split()),
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }
        if include_body:
            payload["body"] = self.body
        return payload


class AgentKnowledgeRow(Base):
    """SQLite row for one knowledge entry."""

    __tablename__ = "agent_knowledge"

    id: Mapped[str] = mapped_column(String(24), primary_key=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str] = mapped_column(Text(), nullable=False, default="")
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="note", index=True)
    tags: Mapped[list[str]] = mapped_column(JSON(), nullable=False, default=list)
    symbols: Mapped[list[str]] = mapped_column(JSON(), nullable=False, default=list)
    source_run_id: Mapped[str | None] = mapped_column(String(32), default=None, index=True)
    source_stage_id: Mapped[str | None] = mapped_column(String(64), default=None)
    finding_ids: Mapped[list[str]] = mapped_column(JSON(), nullable=False, default=list)
    sources: Mapped[list[dict[str, Any]]] = mapped_column(JSON(), nullable=False, default=list)
    author: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)


_OWNED_TABLES = (AgentKnowledgeRow.__table__,)

_CREATE_FTS = text(
    f"CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE} "
    "USING fts5(entry_id UNINDEXED, title, body, tags, symbols, tokenize='unicode61')"
)


def ensure_schema() -> bool:
    """Create the knowledge table and its FTS5 index.

    Returns:
        Whether the FTS5 index is available. ``False`` means this SQLite build
        has no FTS5 and search will use the substring fallback.
    """
    engine = get_engine()
    Base.metadata.create_all(engine, tables=list(_OWNED_TABLES))
    if engine.dialect.name != "sqlite":
        logger.warning(
            "knowledge full-text search needs SQLite FTS5 (dialect=%s); using the fallback",
            engine.dialect.name,
        )
        return False
    try:
        with engine.begin() as connection:
            connection.execute(_CREATE_FTS)
    except DatabaseError:
        logger.warning("this SQLite build has no FTS5; knowledge search uses the fallback")
        return False
    return True


class KnowledgeBase:
    """CRUD and retrieval over knowledge entries."""

    #: Fields :meth:`create` and :meth:`update` accept beyond title/body.
    FIELDS: Final[frozenset[str]] = frozenset(
        {
            "kind",
            "tags",
            "symbols",
            "source_run_id",
            "source_stage_id",
            "finding_ids",
            "sources",
            "author",
        }
    )

    def __init__(
        self,
        session_factory: sessionmaker[Session] | None = None,
        *,
        bus: EventBus | None = None,
    ) -> None:
        """Create the knowledge base.

        Args:
            session_factory: SQLAlchemy session factory; defaults to the
                process-wide one from :mod:`tradinglib.db`.
            bus: event bus knowledge changes are announced on.
        """
        self._session_factory = session_factory
        self._bus = bus
        self._fts = ensure_schema()

    @property
    def fts(self) -> bool:
        """Whether searches run against the FTS5 index."""
        return self._fts

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

    def _announce(self, action: str, entry: KnowledgeEntry) -> None:
        """Publish a knowledge change, when a bus is attached."""
        if self._bus is None:
            return
        try:
            self._bus.publish_nowait(
                NOTIFICATIONS_CHANNEL,
                {
                    "kind": "knowledge",
                    "action": action,
                    "entry_id": entry.id,
                    "title": entry.title,
                    "message": f"Knowledge entry {action}: {entry.title}",
                },
            )
        except RuntimeError:
            logger.debug("no event loop to announce knowledge entry %s on", entry.id)

    @staticmethod
    def _to_dataclass(row: AgentKnowledgeRow) -> KnowledgeEntry:
        """Map an ORM row onto the dataclass."""
        return KnowledgeEntry(
            id=row.id,
            title=row.title,
            body=row.body,
            kind=row.kind,
            tags=list(row.tags or []),
            symbols=list(row.symbols or []),
            source_run_id=row.source_run_id,
            source_stage_id=row.source_stage_id,
            finding_ids=list(row.finding_ids or []),
            sources=[dict(item) for item in (row.sources or [])],
            author=row.author,
            created_at=_aware(row.created_at),
            updated_at=_aware(row.updated_at),
        )

    @staticmethod
    def _clean_list(values: Iterable[Any] | None, *, upper: bool = False) -> list[str]:
        """Normalise a string list: trimmed, de-duplicated, order preserved."""
        out: list[str] = []
        for value in values or []:
            item = str(value).strip()
            if upper:
                item = item.upper()
            if item and item not in out:
                out.append(item)
        return out

    @staticmethod
    def _clean_sources(values: Iterable[Any] | None) -> list[dict[str, Any]]:
        """Normalise cited sources to mappings, keeping bare strings as refs."""
        out: list[dict[str, Any]] = []
        for value in values or []:
            if isinstance(value, Mapping):
                out.append({str(k): v for k, v in value.items()})
            else:
                out.append({"ref": str(value)})
        return out

    # --- FTS maintenance --------------------------------------------------
    def _index_delete(self, session: Session, entry_id: str) -> None:
        """Drop an entry from the FTS index."""
        if not self._fts:
            return
        session.execute(
            text(f"DELETE FROM {FTS_TABLE} WHERE entry_id = :entry_id"), {"entry_id": entry_id}
        )

    def _index_write(self, session: Session, row: AgentKnowledgeRow) -> None:
        """Re-index one entry (delete + insert, which FTS5 external tables need)."""
        if not self._fts:
            return
        self._index_delete(session, row.id)
        session.execute(
            text(
                f"INSERT INTO {FTS_TABLE} (entry_id, title, body, tags, symbols) "
                "VALUES (:entry_id, :title, :body, :tags, :symbols)"
            ),
            {
                "entry_id": row.id,
                "title": row.title,
                "body": row.body,
                "tags": " ".join(row.tags or []),
                "symbols": " ".join(row.symbols or []),
            },
        )

    def reindex(self) -> int:
        """Rebuild the whole FTS index from the table. Returns the row count.

        Needed after restoring a database that was written by a build without
        FTS5, and used by the tests to prove the index and the table agree.
        """
        if not self._fts:
            return 0
        with self._session() as session:
            session.execute(text(f"DELETE FROM {FTS_TABLE}"))
            rows = list(session.scalars(select(AgentKnowledgeRow)))
            for row in rows:
                self._index_write(session, row)
        return len(rows)

    def index_size(self) -> int:
        """Number of documents currently in the FTS index."""
        if not self._fts:
            return 0
        with self._session() as session:
            return int(session.scalar(text(f"SELECT count(*) FROM {FTS_TABLE}")) or 0)

    # --- reads ------------------------------------------------------------
    def list(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        tag: str | None = None,
        kind: str | None = None,
        symbol: str | None = None,
        run_id: str | None = None,
    ) -> list[KnowledgeEntry]:
        """Return entries, newest first. ``limit=0`` returns every entry."""
        statement = select(AgentKnowledgeRow).order_by(
            AgentKnowledgeRow.updated_at.desc(), AgentKnowledgeRow.id.desc()
        )
        if kind is not None:
            statement = statement.where(AgentKnowledgeRow.kind == kind)
        if run_id is not None:
            statement = statement.where(AgentKnowledgeRow.source_run_id == run_id)
        with self._session() as session:
            items = [self._to_dataclass(row) for row in session.scalars(statement)]
        if tag:
            needle = tag.strip().lower()
            items = [item for item in items if needle in {t.lower() for t in item.tags}]
        if symbol:
            wanted = symbol.strip().upper()
            items = [item for item in items if wanted in item.symbols]
        return items[offset : offset + limit] if limit else items[offset:]

    def get(self, entry_id: str) -> KnowledgeEntry:
        """Return one entry.

        Raises:
            KeyError: when the identifier is unknown.
        """
        with self._session() as session:
            row = session.get(AgentKnowledgeRow, entry_id)
            if row is None:
                raise KeyError(f"unknown knowledge entry {entry_id!r}")
            return self._to_dataclass(row)

    def exists(self, entry_id: str) -> bool:
        """Return whether an entry with this identifier exists."""
        with self._session() as session:
            return session.get(AgentKnowledgeRow, entry_id) is not None

    # --- writes -----------------------------------------------------------
    def create(self, title: str, body: str, **fields: Any) -> KnowledgeEntry:
        """Create an entry and index it.

        Raises:
            ValueError: when the title or body is empty, or a field is unknown.
        """
        headline = str(title or "").strip()
        document = str(body or "").strip()
        if not headline:
            raise ValueError("a knowledge entry needs a title")
        if not document:
            raise ValueError("a knowledge entry needs a body - the note itself")
        unknown = set(fields) - self.FIELDS
        if unknown:
            raise ValueError(f"unknown field(s): {', '.join(sorted(unknown))}")
        now = utcnow()
        row = AgentKnowledgeRow(
            id=new_entry_id(),
            title=headline,
            body=document,
            kind=str(fields.get("kind") or "note").strip() or "note",
            tags=self._clean_list(fields.get("tags")),
            symbols=self._clean_list(fields.get("symbols"), upper=True),
            source_run_id=fields.get("source_run_id"),
            source_stage_id=fields.get("source_stage_id"),
            finding_ids=self._clean_list(fields.get("finding_ids")),
            sources=self._clean_sources(fields.get("sources")),
            author=fields.get("author"),
            created_at=now,
            updated_at=now,
        )
        with self._session() as session:
            session.add(row)
            session.flush()
            self._index_write(session, row)
            entry = self._to_dataclass(row)
        self._announce("created", entry)
        return entry

    def update(self, entry_id: str, **changes: Any) -> KnowledgeEntry:
        """Update an entry and re-index it.

        Raises:
            KeyError: when the identifier is unknown.
            ValueError: when a field is unknown or a value is empty.
        """
        allowed = self.FIELDS | {"title", "body"}
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unknown field(s): {', '.join(sorted(unknown))}")
        with self._session() as session:
            row = session.get(AgentKnowledgeRow, entry_id)
            if row is None:
                raise KeyError(f"unknown knowledge entry {entry_id!r}")
            if "title" in changes:
                headline = str(changes["title"]).strip()
                if not headline:
                    raise ValueError("a knowledge entry needs a title")
                row.title = headline
            if "body" in changes:
                document = str(changes["body"]).strip()
                if not document:
                    raise ValueError("a knowledge entry needs a body")
                row.body = document
            if "kind" in changes:
                row.kind = str(changes["kind"] or "note").strip() or "note"
            if "tags" in changes:
                row.tags = self._clean_list(changes["tags"])
            if "symbols" in changes:
                row.symbols = self._clean_list(changes["symbols"], upper=True)
            if "finding_ids" in changes:
                row.finding_ids = self._clean_list(changes["finding_ids"])
            if "sources" in changes:
                row.sources = self._clean_sources(changes["sources"])
            for key in ("source_run_id", "source_stage_id", "author"):
                if key in changes:
                    setattr(row, key, changes[key])
            row.updated_at = utcnow()
            session.flush()
            self._index_write(session, row)
            entry = self._to_dataclass(row)
        self._announce("updated", entry)
        return entry

    def delete(self, entry_id: str) -> None:
        """Delete an entry and drop it from the index.

        Raises:
            KeyError: when the identifier is unknown.
        """
        with self._session() as session:
            row = session.get(AgentKnowledgeRow, entry_id)
            if row is None:
                raise KeyError(f"unknown knowledge entry {entry_id!r}")
            entry = self._to_dataclass(row)
            self._index_delete(session, entry_id)
            session.delete(row)
        self._announce("deleted", entry)

    # --- search -----------------------------------------------------------
    def search(self, query: str, *, limit: int = 25) -> list[KnowledgeEntry]:
        """Search entries; used both by the UI and by the agent retrieval tool."""
        return [hit["entry"] for hit in self.search_ranked(query, limit=limit)]

    def search_ranked(self, query: str, *, limit: int = 25) -> list[dict[str, Any]]:
        """Search and return ``{entry, score, snippet, matched}`` per hit.

        All terms are required first; when that finds nothing the same terms are
        retried as ``OR`` so a long question still retrieves the documents that
        answer part of it. ``matched`` says which of the two ran, so the UI can
        be honest about a partial match.
        """
        count = max(1, int(limit))
        if not (query or "").strip():
            return []
        if not self._fts:
            return self._fallback_search(query, limit=count)
        for operator in ("AND", "OR"):
            expression = fts_query(query, operator=operator)
            if not expression:
                return []
            try:
                rows = self._match(expression, count)
            except DatabaseError:
                logger.warning("FTS query failed for %r; using the fallback", query, exc_info=True)
                return self._fallback_search(query, limit=count)
            if rows:
                entries = self._by_ids([entry_id for entry_id, _, _ in rows])
                return [
                    {
                        "entry": entries[entry_id],
                        "score": round(-float(rank), 4),
                        "snippet": snippet,
                        "matched": "all" if operator == "AND" else "any",
                    }
                    for entry_id, rank, snippet in rows
                    if entry_id in entries
                ]
        return []

    def _by_ids(self, entry_ids: list[str]) -> dict[str, KnowledgeEntry]:
        """Load several entries in one query, keyed by id.

        An id the index still holds but the table no longer does is simply
        absent from the result - the caller drops it rather than inventing it.
        """
        if not entry_ids:
            return {}
        statement = select(AgentKnowledgeRow).where(AgentKnowledgeRow.id.in_(entry_ids))
        with self._session() as session:
            return {row.id: self._to_dataclass(row) for row in session.scalars(statement)}

    def _match(self, expression: str, limit: int) -> list[tuple[str, float, str]]:
        """Run one FTS5 MATCH and return ``(entry_id, bm25 rank, snippet)``."""
        statement = text(
            f"SELECT entry_id, bm25({FTS_TABLE}) AS rank, "
            f"snippet({FTS_TABLE}, 2, '<mark>', '</mark>', '…', 16) AS snip "
            f"FROM {FTS_TABLE} WHERE {FTS_TABLE} MATCH :expression "
            "ORDER BY rank LIMIT :limit"
        )
        with self._session() as session:
            rows = session.execute(statement, {"expression": expression, "limit": limit}).all()
        return [(str(row[0]), float(row[1]), str(row[2] or "")) for row in rows]

    def _fallback_search(self, query: str, *, limit: int) -> list[dict[str, Any]]:
        """Scored substring search, used when FTS5 is not available."""
        terms = tokenize(query)
        if not terms:
            return []
        hits: list[tuple[int, KnowledgeEntry]] = []
        for entry in self.list(limit=0):
            title = entry.title.lower()
            body = entry.body.lower()
            labels = " ".join(entry.tags + entry.symbols).lower()
            score = 0
            for term in terms:
                score += 4 * title.count(term) + 2 * labels.count(term) + body.count(term)
            if score:
                hits.append((score, entry))
        hits.sort(key=lambda pair: -pair[0])
        return [
            {
                "entry": entry,
                "score": float(score),
                "snippet": self._excerpt(entry.body, terms),
                "matched": "any",
            }
            for score, entry in hits[:limit]
        ]

    @staticmethod
    def _excerpt(body: str, terms: list[str], *, width: int = 160) -> str:
        """Return a plain excerpt of ``body`` around the first matching term."""
        lowered = body.lower()
        position = min(
            (lowered.find(term) for term in terms if lowered.find(term) >= 0), default=0
        )
        start = max(0, position - width // 2)
        excerpt = body[start : start + width].strip()
        return f"…{excerpt}…" if start else excerpt

    # --- feeds ------------------------------------------------------------
    def collections(self) -> list[dict[str, Any]]:
        """Group entries by tag for the "Knowledge collections" panel.

        Every number here is counted from the rows: how many entries carry the
        tag, how many distinct symbols and source runs they cover, and when the
        newest of them was written.
        """
        entries = self.list(limit=0)
        buckets: dict[str, dict[str, Any]] = {}
        for entry in entries:
            for tag in entry.tags or ["untagged"]:
                bucket = buckets.setdefault(
                    tag,
                    {
                        "tag": tag,
                        "entries": 0,
                        "symbols": set(),
                        "runs": set(),
                        "updated_at": None,
                    },
                )
                bucket["entries"] += 1
                bucket["symbols"].update(entry.symbols)
                if entry.source_run_id:
                    bucket["runs"].add(entry.source_run_id)
                stamp = entry.updated_at or entry.created_at
                if stamp and (bucket["updated_at"] is None or stamp > bucket["updated_at"]):
                    bucket["updated_at"] = stamp
        return sorted(
            (
                {
                    "tag": bucket["tag"],
                    "entries": bucket["entries"],
                    "symbols": sorted(bucket["symbols"]),
                    "symbol_count": len(bucket["symbols"]),
                    "run_count": len(bucket["runs"]),
                    "updated_at": _iso(bucket["updated_at"]),
                }
                for bucket in buckets.values()
            ),
            key=lambda item: (-item["entries"], item["tag"]),
        )

    def recent(self, *, limit: int = 10) -> list[dict[str, Any]]:
        """Return the "Recently learned" feed: newest entries with provenance."""
        return [
            {
                "id": entry.id,
                "title": entry.title,
                "kind": entry.kind,
                "source_run_id": entry.source_run_id,
                "finding_ids": list(entry.finding_ids),
                "updated_at": _iso(entry.updated_at),
                "excerpt": entry.body[:200],
            }
            for entry in self.list(limit=limit)
        ]

    def counts(self) -> dict[str, Any]:
        """Return real knowledge counts for the Knowledge tab header."""
        with self._session() as session:
            total = int(
                session.scalar(select(func.count()).select_from(AgentKnowledgeRow)) or 0
            )
            kinds = list(session.scalars(select(AgentKnowledgeRow.kind)))
        by_kind: dict[str, int] = {}
        for value in kinds:
            by_kind[str(value)] = by_kind.get(str(value), 0) + 1
        return {
            "total": total,
            "by_kind": by_kind,
            "collections": len(self.collections()),
            "fts": self._fts,
            "indexed": self.index_size(),
        }


_knowledge: KnowledgeBase | None = None


def get_knowledge_base() -> KnowledgeBase:
    """Return the process-wide knowledge base, creating it on first use."""
    global _knowledge
    if _knowledge is None:
        _knowledge = KnowledgeBase()
    return _knowledge


def reset_knowledge_base(base: KnowledgeBase | None = None) -> KnowledgeBase:
    """Install (or forget) the process-wide knowledge base. Returns it."""
    global _knowledge
    _knowledge = base if base is not None else KnowledgeBase()
    return _knowledge


__all__ = [
    "FTS_TABLE",
    "KNOWN_KINDS",
    "AgentKnowledgeRow",
    "KnowledgeBase",
    "KnowledgeEntry",
    "ensure_schema",
    "fts_query",
    "get_knowledge_base",
    "new_entry_id",
    "reset_knowledge_base",
    "tokenize",
]
