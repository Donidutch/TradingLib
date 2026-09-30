"""Agent definitions - the Agent Studio backend.

An agent definition is configuration, not a run: who the agent is (name, role),
which model it thinks with, what it is told to do (the system prompt), which of
TradingLib's tools it is allowed to call, and the scope and limits it works inside.
Definitions are persisted in SQLite and edited through ``/api/agents/definitions``.

The inventory holds two populations, distinguished by :class:`AgentKind` exactly
as the reference Agent Studio does:

``stage``
    exists to be assigned to a pipeline stage; its execution history lives in
    the pipeline runs.
``worker``
    a standing agent that runs on its own schedule (AutoPipeline) with its own
    daily cap.
``human``
    the mandatory human-approval participant. It has no model and cannot be
    given tools; the runner blocks on it rather than calling anything.

Run statistics (how often an agent ran, how often it failed) are *not* stored
here - they are counted from the real run tables by
:meth:`tradinglib.agents.runner.PipelineRunner.agent_stats`, so the Studio can never
show an evaluation score that no run produced.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from tradinglib.db import Base, ensure_columns, get_engine, get_session_factory
from tradinglib.events import EventBus

logger = logging.getLogger(__name__)

#: Channel agent-configuration changes are announced on.
NOTIFICATIONS_CHANNEL: Final[str] = "notifications"

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def utcnow() -> datetime:
    """Return the current UTC time as an aware datetime."""
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    """Attach UTC to a naive datetime read back from SQLite."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def slugify(value: str, *, fallback: str = "agent") -> str:
    """Return a lowercase, dash-separated identifier for ``value``."""
    slug = _SLUG_RE.sub("-", value.strip().lower()).strip("-")
    return slug or fallback


class AgentKind(StrEnum):
    """The populations Agent Studio holds in one inventory."""

    STAGE = "stage"
    WORKER = "worker"
    HUMAN = "human"


class AgentStatus(StrEnum):
    """Operating state of an agent definition."""

    ACTIVE = "active"
    PAUSED = "paused"
    REVIEW = "review"
    LOCKED = "locked"


#: Provider name used by the human-approval participant, which calls no model.
NO_PROVIDER: Final[str] = "none"

#: The definition fields a preset is allowed to rewrite.
#:
#: A preset is an *effort dial*, not a second definition: it moves how much the
#: agent is allowed to spend on one call (tokens, wall clock, retries, dollars,
#: sampling temperature) and the operating scope it stays inside. It can never
#: change the prompt, the model or the tool grants, because those decide what
#: the agent *is* — swapping them behind a chip would make the Studio lie about
#: which agent ran.
PRESET_FIELDS: Final[frozenset[str]] = frozenset(
    {"max_tokens", "temperature", "timeout_s", "retries", "budget_usd", "scope"}
)


@dataclass(slots=True)
class AgentDefinition:
    """A configured agent: prompt, model, tools and operating scope."""

    id: str
    name: str
    role: str
    system_prompt: str
    provider: str
    model: str
    kind: AgentKind = AgentKind.STAGE
    status: AgentStatus = AgentStatus.ACTIVE
    fallback_model: str | None = None
    tools: list[str] = field(default_factory=list)
    permissions: list[str] = field(default_factory=list)
    temperature: float = 0.0
    max_tokens: int = 4096
    budget_usd: float | None = None
    timeout_s: float | None = None
    retries: int = 0
    scope: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    notes: str | None = None
    enabled: bool = True
    locked: bool = False
    version: int = 1
    #: Named effort dials, ``{preset_name: {field: value}}``. Each inner mapping
    #: is a partial override of :data:`PRESET_FIELDS`; applying one and saving
    #: is an ordinary edit, so a preset can never diverge from the stored state.
    presets: dict[str, dict[str, Any]] = field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def calls_a_model(self) -> bool:
        """Whether running this agent means talking to an LLM."""
        return self.kind is not AgentKind.HUMAN and self.provider != NO_PROVIDER

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping for the API layer."""
        return {
            "id": self.id,
            "name": self.name,
            "role": self.role,
            "kind": str(self.kind),
            "status": str(self.status),
            "system_prompt": self.system_prompt,
            "provider": self.provider,
            "model": self.model,
            "fallback_model": self.fallback_model,
            "tools": list(self.tools),
            "permissions": list(self.permissions),
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "budget_usd": self.budget_usd,
            "timeout_s": self.timeout_s,
            "retries": self.retries,
            "scope": dict(self.scope),
            "tags": list(self.tags),
            "notes": self.notes,
            "enabled": self.enabled,
            "locked": self.locked,
            "version": self.version,
            "presets": {name: dict(values) for name, values in self.presets.items()},
            "preset_fields": sorted(PRESET_FIELDS),
            "calls_a_model": self.calls_a_model,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class AgentDefinitionRow(Base):
    """SQLite row for one agent definition."""

    __tablename__ = "agent_definitions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    role: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default=AgentKind.STAGE)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=AgentStatus.ACTIVE)
    system_prompt: Mapped[str] = mapped_column(Text(), nullable=False, default="")
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="anthropic")
    model: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    fallback_model: Mapped[str | None] = mapped_column(String(120), default=None)
    tools: Mapped[list[str]] = mapped_column(JSON(), nullable=False, default=list)
    permissions: Mapped[list[str]] = mapped_column(JSON(), nullable=False, default=list)
    temperature: Mapped[float] = mapped_column(Float(), nullable=False, default=0.0)
    max_tokens: Mapped[int] = mapped_column(Integer(), nullable=False, default=4096)
    budget_usd: Mapped[float | None] = mapped_column(Float(), default=None)
    timeout_s: Mapped[float | None] = mapped_column(Float(), default=None)
    retries: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    scope: Mapped[dict[str, Any]] = mapped_column(JSON(), nullable=False, default=dict)
    tags: Mapped[list[str]] = mapped_column(JSON(), nullable=False, default=list)
    notes: Mapped[str | None] = mapped_column(Text(), default=None)
    enabled: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=True)
    locked: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    version: Mapped[int] = mapped_column(Integer(), nullable=False, default=1)
    presets: Mapped[dict[str, Any]] = mapped_column(JSON(), nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)


class AgentVersionRow(Base):
    """SQLite row for one frozen version of an agent definition.

    Agent Studio's *Version history* is only meaningful if an old version can
    actually be read back and restored; a version *number* on a mutable row is
    not a history. Each row here is the complete definition as it stood when the
    version was frozen, which is also what makes "which prompt produced this
    finding" answerable after the prompt has been edited.
    """

    __tablename__ = "agent_definition_versions"

    id: Mapped[int] = mapped_column(Integer(), primary_key=True, autoincrement=True)
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer(), nullable=False)
    document: Mapped[dict[str, Any]] = mapped_column(JSON(), nullable=False, default=dict)
    note: Mapped[str | None] = mapped_column(Text(), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)


_OWNED_TABLES = (AgentDefinitionRow.__table__, AgentVersionRow.__table__)


def ensure_schema() -> None:
    """Create the agent-definition tables if they do not exist yet, then migrate.

    ``ensure_columns`` closes the gap for a database written by an older build
    (the ``presets`` column was added after the first release), so an existing
    ``app.db`` keeps working without being rebuilt.
    """
    Base.metadata.create_all(get_engine(), tables=list(_OWNED_TABLES))
    for table in _OWNED_TABLES:
        ensure_columns(table)


#: The agents TradingLib ships with. They are configuration - real prompts, real
#: tool grants - not sample data: every one of them runs if a pipeline stage
#: uses it. ``model=""`` means "resolve the configured provider's default at run
#: time", which is what keeps a keyless install honest.
#: Effort dials offered by the shipped model-calling agents.
#:
#: The three names are the reference's (Fast / Quick / Full) and the numbers are
#: the real limits the runner enforces: ``max_tokens`` is the per-call cap sent
#: to the provider, ``timeout_s`` bounds the stage's wall clock, ``retries`` is
#: how often a failed call is re-issued, and ``scope`` is merged into the tool
#: scope and stated to the model verbatim. Picking one rewrites those fields in
#: the Config form; saving persists them like any other edit.
_EFFORT_PRESETS: Final[dict[str, dict[str, dict[str, Any]]]] = {
    "scanner-agent": {
        "Fast": {"max_tokens": 4096, "timeout_s": 90.0, "retries": 0,
                 "scope": {"max_candidates": 10, "lookback_days": 7}},
        "Quick": {"max_tokens": 8192, "timeout_s": 180.0, "retries": 1,
                  "scope": {"max_candidates": 30, "lookback_days": 30}},
        "Full": {"max_tokens": 16384, "timeout_s": 420.0, "retries": 2,
                 "scope": {"max_candidates": 60, "lookback_days": 180}},
    },
    "research-agent": {
        "Fast": {"max_tokens": 6144, "timeout_s": 120.0, "retries": 0,
                 "scope": {"max_symbols": 3, "evidence_per_claim": 1}},
        "Quick": {"max_tokens": 12288, "timeout_s": 300.0, "retries": 1,
                  "scope": {"max_symbols": 8, "evidence_per_claim": 2}},
        "Full": {"max_tokens": 24576, "timeout_s": 600.0, "retries": 2,
                 "scope": {"max_symbols": 20, "evidence_per_claim": 3}},
    },
    "hypothesis-agent": {
        "Fast": {"max_tokens": 4096, "timeout_s": 90.0, "retries": 0,
                 "scope": {"max_hypotheses": 1}},
        "Quick": {"max_tokens": 8192, "timeout_s": 210.0, "retries": 1,
                  "scope": {"max_hypotheses": 3}},
        "Full": {"max_tokens": 16384, "timeout_s": 480.0, "retries": 2,
                 "scope": {"max_hypotheses": 6}},
    },
    "quant-agent": {
        "Fast": {"max_tokens": 6144, "timeout_s": 180.0, "retries": 0,
                 "scope": {"max_backtests": 1, "folds": 2}},
        "Quick": {"max_tokens": 12288, "timeout_s": 480.0, "retries": 1,
                  "scope": {"max_backtests": 3, "folds": 5}},
        "Full": {"max_tokens": 24576, "timeout_s": 900.0, "retries": 2,
                 "scope": {"max_backtests": 8, "folds": 8}},
    },
    "validation-agent": {
        "Fast": {"max_tokens": 6144, "timeout_s": 180.0, "retries": 0,
                 "scope": {"stress_scenarios": 1}},
        "Quick": {"max_tokens": 12288, "timeout_s": 420.0, "retries": 1,
                  "scope": {"stress_scenarios": 3}},
        "Full": {"max_tokens": 24576, "timeout_s": 900.0, "retries": 2,
                 "scope": {"stress_scenarios": 6}},
    },
}

DEFAULT_AGENTS: Final[tuple[dict[str, Any], ...]] = (
    {
        "id": "scanner-agent",
        "name": "Scanner Agent",
        "role": "Opportunity discovery",
        "system_prompt": (
            "You scan a universe for candidates. Resolve the universe with the "
            "resolve_universe tool, run the screen you are given with run_screen, and "
            "return the surviving symbols with the reason each one passed. Never invent "
            "a symbol that the tools did not return, and say plainly when nothing passes."
        ),
        "tools": ["resolve_universe", "run_screen", "get_series"],
        "permissions": ["read market data", "read screens"],
        "tags": ["scanner"],
    },
    {
        "id": "research-agent",
        "name": "Research Agent",
        "role": "Evidence synthesis",
        "system_prompt": (
            "You build an evidence-backed brief for each candidate. Use get_company for "
            "fundamentals, filings, news and alternative data, search_knowledge for what "
            "is already known, and save_finding to record each conclusion with its "
            "evidence. Separate fact from inference and keep contradictions instead of "
            "resolving them silently."
        ),
        "tools": ["get_company", "get_series", "search_knowledge", "save_finding"],
        "permissions": ["read research data", "create findings"],
        "tags": ["research"],
    },
    {
        "id": "hypothesis-agent",
        "name": "Hypothesis Agent",
        "role": "Falsifiable thesis design",
        "system_prompt": (
            "You turn supported findings into falsifiable hypotheses. Every hypothesis "
            "needs a mechanism and an explicit falsifier - a statement nothing could "
            "disprove is not a hypothesis. Use search_knowledge and get_company for "
            "context and save_finding to record the resulting specification."
        ),
        "tools": ["search_knowledge", "get_company", "save_finding"],
        "permissions": ["read findings", "create hypotheses"],
        "tags": ["hypothesis"],
    },
    {
        "id": "quant-agent",
        "name": "Quant Agent",
        "role": "Experiments and backtests",
        "system_prompt": (
            "You test hypotheses. Build the signal specification, run it with "
            "run_backtest against the stated benchmark, and report the real statistics - "
            "including the ones that argue against the hypothesis. Never describe a "
            "backtest you did not run."
        ),
        "tools": ["resolve_universe", "get_series", "run_screen", "run_backtest", "save_finding"],
        "permissions": ["read point-in-time data", "create backtests"],
        "tags": ["quant"],
    },
    {
        "id": "validation-agent",
        "name": "Validation Agent",
        "role": "Independent challenge",
        "system_prompt": (
            "You challenge a candidate strategy rather than defend it. Re-run the "
            "backtest under stressed assumptions with run_backtest, compare against the "
            "benchmark, and state which gates fail. A recommendation to promote requires "
            "every required gate to pass."
        ),
        "tools": ["run_backtest", "get_series", "search_knowledge", "save_finding"],
        "permissions": ["read attempt ledger", "create validation reports"],
        "tags": ["validation"],
    },
    {
        "id": "human-review",
        "name": "Human Review",
        "role": "Mandatory decision authority",
        "kind": AgentKind.HUMAN,
        "provider": NO_PROVIDER,
        "system_prompt": (
            "A person reviews the evidence, the test history and the risks before any "
            "promotion. Automation always stops here."
        ),
        "tools": [],
        "permissions": ["approve for paper research", "reject", "request more validation"],
        "locked": True,
        "status": AgentStatus.LOCKED,
        "tags": ["approval"],
    },
)


class AgentRegistry:
    """CRUD over agent definitions, persisted in SQLite."""

    def __init__(
        self,
        session_factory: sessionmaker[Session] | None = None,
        *,
        bus: EventBus | None = None,
        activity: Any | None = None,
    ) -> None:
        """Create the registry.

        Args:
            session_factory: SQLAlchemy session factory; defaults to the
                process-wide one from :mod:`tradinglib.db`.
            bus: event bus configuration changes are announced on.
            activity: optional callable returning the ids of agents that are
                executing right now. The pipeline runner registers itself here,
                which is what makes :meth:`counts` real instead of guessed.
        """
        self._session_factory = session_factory
        self._bus = bus
        self._activity = activity
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

    def bind_activity(self, activity: Any) -> None:
        """Register the source of "which agents are busy right now"."""
        self._activity = activity

    def _announce(self, action: str, definition: AgentDefinition) -> None:
        """Publish a configuration change, when a bus is attached."""
        if self._bus is None:
            return
        try:
            self._bus.publish_nowait(
                NOTIFICATIONS_CHANNEL,
                {
                    "kind": "agent",
                    "action": action,
                    "agent_id": definition.id,
                    "name": definition.name,
                    "message": f"Agent {definition.name} {action}",
                },
            )
        except RuntimeError:
            # No running loop (synchronous CLI use). The database write is what
            # matters; the toast is a nicety.
            logger.debug("no event loop to announce agent %s on", definition.id)

    @staticmethod
    def _to_dataclass(row: AgentDefinitionRow) -> AgentDefinition:
        """Map an ORM row onto the dataclass."""
        return AgentDefinition(
            id=row.id,
            name=row.name,
            role=row.role,
            system_prompt=row.system_prompt,
            provider=row.provider,
            model=row.model,
            kind=AgentKind(row.kind),
            status=AgentStatus(row.status),
            fallback_model=row.fallback_model,
            tools=list(row.tools or []),
            permissions=list(row.permissions or []),
            temperature=float(row.temperature),
            max_tokens=int(row.max_tokens),
            budget_usd=row.budget_usd,
            timeout_s=row.timeout_s,
            retries=int(row.retries),
            scope=dict(row.scope or {}),
            tags=list(row.tags or []),
            notes=row.notes,
            enabled=bool(row.enabled),
            locked=bool(row.locked),
            version=int(row.version),
            presets={
                str(name): dict(values or {})
                for name, values in (row.presets or {}).items()
            },
            created_at=_aware(row.created_at),
            updated_at=_aware(row.updated_at),
        )

    @staticmethod
    def _clean_list(values: Iterable[str] | None) -> list[str]:
        """Normalise a string list: trimmed, de-duplicated, order preserved."""
        out: list[str] = []
        for value in values or []:
            text = str(value).strip()
            if text and text not in out:
                out.append(text)
        return out

    @staticmethod
    def _clean_presets(value: Any) -> dict[str, dict[str, Any]]:
        """Validate and normalise a ``{name: {field: value}}`` preset mapping.

        Every field is coerced to the type the definition stores, so a preset
        read back from the API can be applied with no further conversion.

        Raises:
            ValueError: when a preset is not a mapping, has an empty name, or
                names a field outside :data:`PRESET_FIELDS`.
        """
        if value in (None, ""):
            return {}
        if not isinstance(value, dict):
            raise ValueError("presets must be a mapping of name to field overrides")
        cleaned: dict[str, dict[str, Any]] = {}
        for raw_name, raw_values in value.items():
            name = str(raw_name).strip()
            if not name:
                raise ValueError("a preset needs a name")
            if not isinstance(raw_values, dict):
                raise ValueError(f"preset {name!r} must map field names to values")
            unknown = set(raw_values) - PRESET_FIELDS
            if unknown:
                raise ValueError(
                    f"preset {name!r} sets field(s) a preset may not change: "
                    + ", ".join(sorted(unknown))
                    + ". Allowed: "
                    + ", ".join(sorted(PRESET_FIELDS))
                )
            entry: dict[str, Any] = {}
            if "max_tokens" in raw_values:
                max_tokens = int(raw_values["max_tokens"])
                if max_tokens <= 0:
                    raise ValueError(f"preset {name!r}: max_tokens must be positive")
                entry["max_tokens"] = max_tokens
            if "temperature" in raw_values:
                entry["temperature"] = float(raw_values["temperature"])
            if "retries" in raw_values:
                retries = int(raw_values["retries"])
                if retries < 0:
                    raise ValueError(f"preset {name!r}: retries cannot be negative")
                entry["retries"] = retries
            for field_name in ("timeout_s", "budget_usd"):
                if field_name in raw_values:
                    raw = raw_values[field_name]
                    entry[field_name] = None if raw is None else float(raw)
            if "scope" in raw_values:
                scope = raw_values["scope"]
                if scope is not None and not isinstance(scope, dict):
                    raise ValueError(f"preset {name!r}: scope must be a mapping")
                entry["scope"] = dict(scope or {})
            cleaned[name] = entry
        return cleaned

    @staticmethod
    def known_tools() -> set[str]:
        """Return the tool names the runner can actually bind.

        Imported lazily: the runner imports this module, so the dependency has
        to point the other way at call time only.
        """
        try:
            from tradinglib.agents.runner import TOOL_NAMES
        except Exception:  # noqa: BLE001 - a partially built package must not break CRUD
            logger.debug("tool catalogue unavailable; tool names are not validated", exc_info=True)
            return set()
        return set(TOOL_NAMES)

    def _validate_tools(self, tools: Sequence[str]) -> list[str]:
        """Validate requested tool names against the runner's catalogue."""
        cleaned = self._clean_list(tools)
        catalogue = self.known_tools()
        if catalogue:
            unknown = [name for name in cleaned if name not in catalogue]
            if unknown:
                raise ValueError(
                    "unknown tool(s): "
                    + ", ".join(sorted(unknown))
                    + ". Available: "
                    + ", ".join(sorted(catalogue))
                )
        return cleaned

    @staticmethod
    def _unique_id(session: Session, base: str) -> str:
        """Return ``base``, suffixed until it is free."""
        candidate = base
        suffix = 2
        while session.get(AgentDefinitionRow, candidate) is not None:
            candidate = f"{base}-{suffix}"
            suffix += 1
        return candidate

    # --- reads ------------------------------------------------------------
    def list(
        self,
        *,
        kind: AgentKind | None = None,
        enabled: bool | None = None,
        query: str | None = None,
    ) -> list[AgentDefinition]:
        """Return agent definitions, newest first, optionally filtered."""
        statement = select(AgentDefinitionRow).order_by(AgentDefinitionRow.created_at.desc())
        if kind is not None:
            statement = statement.where(AgentDefinitionRow.kind == str(kind))
        if enabled is not None:
            statement = statement.where(AgentDefinitionRow.enabled.is_(enabled))
        with self._session() as session:
            items = [self._to_dataclass(row) for row in session.scalars(statement)]
        if query:
            needle = query.strip().lower()
            items = [
                item
                for item in items
                if needle in item.name.lower()
                or needle in item.role.lower()
                or needle in item.id.lower()
            ]
        return items

    def get(self, agent_id: str) -> AgentDefinition:
        """Return one definition.

        Raises:
            KeyError: when the identifier is unknown.
        """
        with self._session() as session:
            row = session.get(AgentDefinitionRow, agent_id)
            if row is None:
                raise KeyError(f"unknown agent {agent_id!r}")
            return self._to_dataclass(row)

    def exists(self, agent_id: str) -> bool:
        """Return whether a definition with this identifier exists."""
        with self._session() as session:
            return session.get(AgentDefinitionRow, agent_id) is not None

    def find_by_name(self, name: str) -> AgentDefinition | None:
        """Return the definition with this display name, if there is one."""
        with self._session() as session:
            row = session.scalars(
                select(AgentDefinitionRow).where(AgentDefinitionRow.name == name).limit(1)
            ).first()
            return None if row is None else self._to_dataclass(row)

    # --- writes -----------------------------------------------------------
    def create(
        self,
        name: str,
        role: str,
        system_prompt: str,
        provider: str,
        model: str,
        *,
        agent_id: str | None = None,
        kind: AgentKind | str = AgentKind.STAGE,
        status: AgentStatus | str | None = None,
        fallback_model: str | None = None,
        tools: Sequence[str] | None = None,
        permissions: Sequence[str] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        budget_usd: float | None = None,
        timeout_s: float | None = None,
        retries: int = 0,
        scope: dict[str, Any] | None = None,
        tags: Sequence[str] | None = None,
        notes: str | None = None,
        enabled: bool = True,
        locked: bool = False,
        presets: dict[str, dict[str, Any]] | None = None,
    ) -> AgentDefinition:
        """Create an agent definition.

        Raises:
            ValueError: when the name or prompt is empty, when a model-calling
                agent has no provider, or when an unknown tool is requested.
        """
        display = str(name).strip()
        if not display:
            raise ValueError("an agent needs a name")
        prompt = str(system_prompt).strip()
        if not prompt:
            raise ValueError("an agent needs a system prompt - it is what it is told to do")
        resolved_kind = AgentKind(str(kind))
        provider_name = str(provider or "").strip() or NO_PROVIDER
        if resolved_kind is AgentKind.HUMAN:
            provider_name = NO_PROVIDER
        elif provider_name == NO_PROVIDER:
            raise ValueError(
                "a stage or worker agent needs a provider; only a human-review agent "
                "may have none"
            )
        tool_names = [] if resolved_kind is AgentKind.HUMAN else self._validate_tools(tools or [])
        if resolved_kind is AgentKind.HUMAN and tools:
            raise ValueError("a human-review agent cannot be given tools")
        now = utcnow()
        resolved_status = (
            AgentStatus(str(status))
            if status is not None
            else (AgentStatus.LOCKED if locked else AgentStatus.ACTIVE)
        )
        with self._session() as session:
            identifier = self._unique_id(session, slugify(agent_id or display))
            row = AgentDefinitionRow(
                id=identifier,
                name=display,
                role=str(role or "").strip(),
                kind=str(resolved_kind),
                status=str(resolved_status),
                system_prompt=prompt,
                provider=provider_name,
                model=str(model or "").strip(),
                fallback_model=(fallback_model or None),
                tools=tool_names,
                permissions=self._clean_list(permissions),
                temperature=float(temperature),
                max_tokens=int(max_tokens),
                budget_usd=None if budget_usd is None else float(budget_usd),
                timeout_s=None if timeout_s is None else float(timeout_s),
                retries=int(retries),
                scope=dict(scope or {}),
                tags=self._clean_list(tags),
                notes=notes,
                enabled=bool(enabled),
                locked=bool(locked),
                version=1,
                presets=self._clean_presets(presets),
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            definition = self._to_dataclass(row)
        self._announce("created", definition)
        return definition

    #: Fields ``update`` accepts. Anything else is a caller error rather than a
    #: silently ignored key.
    UPDATABLE: Final[frozenset[str]] = frozenset(
        {
            "name",
            "role",
            "system_prompt",
            "provider",
            "model",
            "fallback_model",
            "kind",
            "status",
            "tools",
            "permissions",
            "temperature",
            "max_tokens",
            "budget_usd",
            "timeout_s",
            "retries",
            "scope",
            "tags",
            "notes",
            "enabled",
            "locked",
            "presets",
        }
    )

    def update(self, agent_id: str, **changes: Any) -> AgentDefinition:
        """Update an agent definition, bumping its version.

        Raises:
            KeyError: when the identifier is unknown.
            ValueError: when a field is unknown or a value is invalid.
            PermissionError: when the definition is locked. Unlocking it is the
                only change a locked agent accepts.
        """
        unknown = set(changes) - self.UPDATABLE
        if unknown:
            raise ValueError(f"unknown field(s): {', '.join(sorted(unknown))}")
        with self._session() as session:
            row = session.get(AgentDefinitionRow, agent_id)
            if row is None:
                raise KeyError(f"unknown agent {agent_id!r}")
            if row.locked and set(changes) - {"locked"}:
                raise PermissionError(
                    f"agent {agent_id!r} is locked; unlock it before changing its configuration"
                )
            # Freeze the definition as it stands *before* the edit, so version N
            # in the archive is always the configuration that version N actually
            # ran with. Nobody has to remember to press "save version" for the
            # history to be complete.
            self._archive(session, row, note=f"superseded by an edit to {', '.join(sorted(changes))}")
            if "name" in changes:
                display = str(changes["name"]).strip()
                if not display:
                    raise ValueError("an agent needs a name")
                row.name = display
            if "role" in changes:
                row.role = str(changes["role"] or "").strip()
            if "system_prompt" in changes:
                prompt = str(changes["system_prompt"]).strip()
                if not prompt:
                    raise ValueError("an agent needs a system prompt")
                row.system_prompt = prompt
            if "kind" in changes:
                row.kind = str(AgentKind(str(changes["kind"])))
            if "status" in changes:
                row.status = str(AgentStatus(str(changes["status"])))
            if "provider" in changes:
                provider = str(changes["provider"] or "").strip() or NO_PROVIDER
                if provider == NO_PROVIDER and AgentKind(row.kind) is not AgentKind.HUMAN:
                    raise ValueError("only a human-review agent may have no provider")
                row.provider = provider
            if "model" in changes:
                row.model = str(changes["model"] or "").strip()
            if "fallback_model" in changes:
                row.fallback_model = (str(changes["fallback_model"]).strip() or None) if changes["fallback_model"] else None
            if "tools" in changes:
                if AgentKind(row.kind) is AgentKind.HUMAN and changes["tools"]:
                    raise ValueError("a human-review agent cannot be given tools")
                row.tools = self._validate_tools(changes["tools"] or [])
            if "permissions" in changes:
                row.permissions = self._clean_list(changes["permissions"])
            if "temperature" in changes:
                row.temperature = float(changes["temperature"])
            if "max_tokens" in changes:
                max_tokens = int(changes["max_tokens"])
                if max_tokens <= 0:
                    raise ValueError("max_tokens must be positive")
                row.max_tokens = max_tokens
            if "budget_usd" in changes:
                value = changes["budget_usd"]
                row.budget_usd = None if value is None else float(value)
            if "timeout_s" in changes:
                value = changes["timeout_s"]
                row.timeout_s = None if value is None else float(value)
            if "retries" in changes:
                retries = int(changes["retries"])
                if retries < 0:
                    raise ValueError("retries cannot be negative")
                row.retries = retries
            if "scope" in changes:
                row.scope = dict(changes["scope"] or {})
            if "tags" in changes:
                row.tags = self._clean_list(changes["tags"])
            if "notes" in changes:
                row.notes = changes["notes"]
            if "enabled" in changes:
                row.enabled = bool(changes["enabled"])
            if "locked" in changes:
                row.locked = bool(changes["locked"])
            if "presets" in changes:
                row.presets = self._clean_presets(changes["presets"])
            row.version += 1
            row.updated_at = utcnow()
            session.flush()
            definition = self._to_dataclass(row)
        self._announce("updated", definition)
        return definition

    # --- version archive --------------------------------------------------
    def _archive(
        self, session: Session, row: AgentDefinitionRow, *, note: str | None = None
    ) -> None:
        """Freeze ``row``'s current state as version ``row.version``.

        Re-freezing the same version number overwrites it rather than creating a
        duplicate, so an explicit ``save_version`` after an edit annotates the
        snapshot that edit already produced.
        """
        document = self._to_dataclass(row).as_dict()
        existing = session.scalars(
            select(AgentVersionRow).where(
                AgentVersionRow.agent_id == row.id,
                AgentVersionRow.version == row.version,
            )
        ).first()
        if existing is not None:
            existing.document = document
            if note:
                existing.note = note
            existing.created_at = utcnow()
            return
        session.add(
            AgentVersionRow(
                agent_id=row.id,
                version=row.version,
                document=document,
                note=note,
                created_at=utcnow(),
            )
        )

    def save_version(self, agent_id: str, *, note: str | None = None) -> dict[str, Any]:
        """Freeze the agent's current configuration as a named version.

        Raises:
            KeyError: when the identifier is unknown.
        """
        with self._session() as session:
            row = session.get(AgentDefinitionRow, agent_id)
            if row is None:
                raise KeyError(f"unknown agent {agent_id!r}")
            self._archive(session, row, note=note or "saved by hand")
            session.flush()
            return {
                "agent_id": row.id,
                "version": row.version,
                "note": note or "saved by hand",
                "created_at": utcnow().isoformat(),
            }

    def versions(self, agent_id: str) -> list[dict[str, Any]]:
        """List the frozen versions of one agent, newest first.

        Raises:
            KeyError: when the identifier is unknown.
        """
        with self._session() as session:
            row = session.get(AgentDefinitionRow, agent_id)
            if row is None:
                raise KeyError(f"unknown agent {agent_id!r}")
            current = row.version
            rows = session.scalars(
                select(AgentVersionRow)
                .where(AgentVersionRow.agent_id == agent_id)
                .order_by(AgentVersionRow.version.desc())
            )
            history = [
                {
                    "agent_id": item.agent_id,
                    "version": item.version,
                    "note": item.note,
                    "model": (item.document or {}).get("model"),
                    "provider": (item.document or {}).get("provider"),
                    "tools": len((item.document or {}).get("tools") or []),
                    "prompt_chars": len(str((item.document or {}).get("system_prompt") or "")),
                    "current": item.version == current,
                    "frozen": True,
                    "created_at": _aware(item.created_at).isoformat() if item.created_at else None,
                }
                for item in rows
            ]
            # The working version is part of the history even before anything
            # supersedes it; it is marked unfrozen so the UI can say that it is
            # still editable rather than implying an archived snapshot exists.
            if not any(entry["version"] == current for entry in history):
                definition = self._to_dataclass(row)
                history.insert(
                    0,
                    {
                        "agent_id": row.id,
                        "version": current,
                        "note": "working version - not frozen yet",
                        "model": definition.model,
                        "provider": definition.provider,
                        "tools": len(definition.tools),
                        "prompt_chars": len(definition.system_prompt),
                        "current": True,
                        "frozen": False,
                        "created_at": (
                            definition.updated_at.isoformat() if definition.updated_at else None
                        ),
                    },
                )
            return history

    def version(self, agent_id: str, version: int) -> AgentDefinition:
        """Return one frozen version of an agent.

        Raises:
            KeyError: when the agent or that version is unknown.
        """
        with self._session() as session:
            item = session.scalars(
                select(AgentVersionRow).where(
                    AgentVersionRow.agent_id == agent_id,
                    AgentVersionRow.version == int(version),
                )
            ).first()
            if item is None:
                raise KeyError(f"agent {agent_id!r} has no frozen version {version}")
            document = dict(item.document or {})
        return AgentDefinition(
            id=str(document.get("id") or agent_id),
            name=str(document.get("name") or agent_id),
            role=str(document.get("role") or ""),
            system_prompt=str(document.get("system_prompt") or ""),
            provider=str(document.get("provider") or NO_PROVIDER),
            model=str(document.get("model") or ""),
            kind=AgentKind(str(document.get("kind", AgentKind.STAGE))),
            status=AgentStatus(str(document.get("status", AgentStatus.ACTIVE))),
            fallback_model=document.get("fallback_model"),
            tools=[str(tool) for tool in document.get("tools") or []],
            permissions=[str(item_) for item_ in document.get("permissions") or []],
            temperature=float(document.get("temperature") or 0.0),
            max_tokens=int(document.get("max_tokens") or 4096),
            budget_usd=document.get("budget_usd"),
            timeout_s=document.get("timeout_s"),
            retries=int(document.get("retries") or 0),
            scope=dict(document.get("scope") or {}),
            tags=[str(tag) for tag in document.get("tags") or []],
            notes=document.get("notes"),
            enabled=bool(document.get("enabled", True)),
            locked=bool(document.get("locked", False)),
            version=int(document.get("version") or version),
            presets={
                str(name): dict(values or {})
                for name, values in (document.get("presets") or {}).items()
            },
        )

    def restore(self, agent_id: str, version: int) -> AgentDefinition:
        """Restore a frozen version as the agent's current configuration.

        The restore is itself an edit: the pre-restore state is archived first
        and the version counter moves forward, so the history stays a record of
        what happened rather than being rewound.

        Raises:
            KeyError: when the agent or that version is unknown.
            PermissionError: when the definition is locked.
        """
        frozen = self.version(agent_id, version)
        current = self.get(agent_id)
        # Only the fields that actually differ are sent, so the archive note the
        # restore writes names the real change instead of listing every field.
        changes = {
            field_name: getattr(frozen, field_name)
            for field_name in sorted(self.UPDATABLE)
            if field_name != "locked"
            and getattr(frozen, field_name) != getattr(current, field_name)
        }
        if not changes:
            return current
        definition = self.update(agent_id, **changes)
        with self._session() as session:
            row = session.get(AgentDefinitionRow, agent_id)
            if row is not None:
                self._archive(session, row, note=f"restored from version {version}")
        return definition

    def delete(self, agent_id: str) -> None:
        """Delete an agent definition and its frozen versions.

        Raises:
            KeyError: when the identifier is unknown.
            PermissionError: when the definition is locked.
        """
        with self._session() as session:
            row = session.get(AgentDefinitionRow, agent_id)
            if row is None:
                raise KeyError(f"unknown agent {agent_id!r}")
            if row.locked:
                raise PermissionError(f"agent {agent_id!r} is locked and cannot be deleted")
            definition = self._to_dataclass(row)
            for frozen in session.scalars(
                select(AgentVersionRow).where(AgentVersionRow.agent_id == agent_id)
            ):
                session.delete(frozen)
            session.delete(row)
        self._announce("deleted", definition)

    def duplicate(self, agent_id: str, *, name: str | None = None) -> AgentDefinition:
        """Copy an agent definition under a new id.

        The copy is never locked - a duplicate exists to be edited.
        """
        original = self.get(agent_id)
        display = name or f"{original.name} copy"
        return self.create(
            display,
            original.role,
            original.system_prompt,
            original.provider,
            original.model,
            kind=original.kind,
            status=AgentStatus.ACTIVE,
            fallback_model=original.fallback_model,
            tools=original.tools,
            permissions=original.permissions,
            temperature=original.temperature,
            max_tokens=original.max_tokens,
            budget_usd=original.budget_usd,
            timeout_s=original.timeout_s,
            retries=original.retries,
            scope=original.scope,
            tags=original.tags,
            notes=original.notes,
            enabled=original.enabled,
            locked=False,
            presets=original.presets,
        )

    # --- bootstrap --------------------------------------------------------
    def ensure_defaults(self) -> list[AgentDefinition]:
        """Create the shipped agent roster if it is not there yet.

        Idempotent: an agent whose id already exists is left exactly as the user
        configured it, with one exception — a shipped agent that carries *no*
        presets at all is given the shipped ones. That is a backfill of a field
        that did not exist when the row was written, not an overwrite: an agent
        whose presets have been edited (including edited to a deliberate single
        entry) is never touched.
        """
        created: list[AgentDefinition] = []
        for spec in DEFAULT_AGENTS:
            identifier = str(spec["id"])
            if self.exists(identifier):
                self._backfill_presets(identifier)
                continue
            payload = dict(spec)
            payload.pop("id")
            presets = _EFFORT_PRESETS.get(identifier)
            if presets is not None:
                payload.setdefault("presets", presets)
            created.append(
                self.create(
                    str(payload.pop("name")),
                    str(payload.pop("role")),
                    str(payload.pop("system_prompt")),
                    str(payload.pop("provider", "anthropic")),
                    str(payload.pop("model", "")),
                    agent_id=identifier,
                    **payload,
                )
            )
        if created:
            logger.info("seeded %d default agent definitions", len(created))
        return created

    def _backfill_presets(self, agent_id: str) -> None:
        """Give a shipped agent its presets if it has none.

        Written directly rather than through :meth:`update` so the backfill does
        not bump the agent's version or archive a "superseded" snapshot: nothing
        about the agent's behaviour changed, a UI affordance simply became
        available.
        """
        shipped = _EFFORT_PRESETS.get(agent_id)
        if not shipped:
            return
        with self._session() as session:
            row = session.get(AgentDefinitionRow, agent_id)
            if row is None or row.presets:
                return
            row.presets = self._clean_presets(shipped)
            logger.info("backfilled shipped presets for agent %s", agent_id)

    # --- reporting --------------------------------------------------------
    def active_ids(self) -> set[str]:
        """Ids of agents executing right now, from the bound activity source."""
        if self._activity is None:
            return set()
        try:
            source = self._activity
            values = source() if callable(source) else source.active_agent_ids()
            return {str(value) for value in values}
        except Exception:  # noqa: BLE001 - a broken activity source must not break the page
            logger.warning("agent activity source failed", exc_info=True)
            return set()

    def counts(self, *, active_agent_ids: Iterable[str] | None = None) -> dict[str, int]:
        """Return the real active/idle counts for the Agent Center header.

        "Active" means an agent that a run is executing at this instant, taken
        from the runner; every enabled agent that is not active is idle.
        """
        active = (
            {str(value) for value in active_agent_ids}
            if active_agent_ids is not None
            else self.active_ids()
        )
        definitions = self.list()
        enabled = [item for item in definitions if item.enabled]
        active_here = {item.id for item in definitions if item.id in active}
        return {
            "total": len(definitions),
            "enabled": len(enabled),
            "disabled": len(definitions) - len(enabled),
            "active": len(active_here),
            "idle": max(len(enabled) - len(active_here), 0),
            "stages": sum(1 for item in definitions if item.kind is AgentKind.STAGE),
            "workers": sum(1 for item in definitions if item.kind is AgentKind.WORKER),
            "human": sum(1 for item in definitions if item.kind is AgentKind.HUMAN),
        }


_registry: AgentRegistry | None = None


def get_agent_registry() -> AgentRegistry:
    """Return the process-wide agent registry, creating it on first use."""
    global _registry
    if _registry is None:
        _registry = AgentRegistry()
    return _registry


def reset_agent_registry(registry: AgentRegistry | None = None) -> AgentRegistry:
    """Install (or forget) the process-wide agent registry. Returns it."""
    global _registry
    _registry = registry if registry is not None else AgentRegistry()
    return _registry


__all__ = [
    "DEFAULT_AGENTS",
    "AgentDefinition",
    "AgentDefinitionRow",
    "AgentKind",
    "AgentRegistry",
    "AgentStatus",
    "NO_PROVIDER",
    "ensure_schema",
    "get_agent_registry",
    "reset_agent_registry",
    "slugify",
]
