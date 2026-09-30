"""Screening funnel: per-stage survivor counts, statistics and persistence.

This module runs a :class:`~tradinglib.research.screen.rules.Screen` against a real
universe and produces the numbers the Screener panel shows:

* the **pass funnel** - how many names entered and survived each rule, in the
  order the rules are written (percentile rules depend on that order);
* the **ranked result set** with every metric behind it;
* the **result profile** - sector mix, median market cap, turnover against the
  previous stored run of the same screen, and overlap with the live book (the
  positions actually open in ``tradinglib.trade``'s portfolio store, read-only;
  the default watchlist stands in, and says so, while no account holds
  anything).

Screens, their runs and the backtest requests they produce are persisted in
SQLite through :mod:`tradinglib.db`, so turnover is measured against a run that
genuinely happened rather than a number derived from the current result set.

Queueing a backtest hands a :class:`~tradinglib.research.BacktestRequest` to the
Strategy Lab through the queue registered in :mod:`tradinglib.research`. When no
lab runner is registered the request is still stored, and its status says
``pending_runner`` - the UI is told the truth instead of being shown a fake
"backtest started".
"""

from __future__ import annotations

import asyncio
import logging
import statistics
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Final

import pandas as pd
from sqlalchemy import JSON, DateTime, Float, Integer, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from tradinglib.data.base import utcnow
from tradinglib.data.universe import UniverseRegistry, get_universe_registry
from tradinglib.db import Base, get_engine, get_session_factory
from tradinglib.events import EventBus
from tradinglib.research import BacktestRequest, get_backtest_queue
from tradinglib.research.screen.rules import (
    METRICS,
    MetricBuilder,
    MetricFrame,
    RuleEngine,
    Screen,
    frame_records,
)

logger = logging.getLogger(__name__)

#: Channel screen notifications are published on (``docs/SPEC.md`` section 5).
NOTIFICATION_CHANNEL: Final[str] = "notifications"

#: Columns returned for every surviving name.
RESULT_COLUMNS: Final[tuple[str, ...]] = (
    "symbol",
    "name",
    "sector",
    "industry",
    "country",
    "region",
    "exchange",
    "price",
    "mkt_cap",
    "adv_usd",
    "rev_growth",
    "gross_margin",
    "ebitda_margin",
    "fcf_margin",
    "roic",
    "roe",
    "net_debt_ebitda",
    "ev_sales",
    "ev_ebitda",
    "pe",
    "mom_1m",
    "mom_3m",
    "mom_6m",
    "mom_12_1",
    "mom_12_1_pctl",
    "vol_3m",
    "quality",
    "composite",
)


# ---------------------------------------------------------------------------
# ORM rows
# ---------------------------------------------------------------------------
class ScreenRow(Base):
    """SQLite row for one saved screen."""

    __tablename__ = "screens"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    universe_id: Mapped[str] = mapped_column(String(64), nullable=False)
    rules: Mapped[list[dict[str, Any]]] = mapped_column(JSON(), nullable=False, default=list)
    expression: Mapped[str | None] = mapped_column(Text(), default=None)
    description: Mapped[str | None] = mapped_column(Text(), default=None)
    rank_by: Mapped[str] = mapped_column(String(32), nullable=False, default="composite")
    owner: Mapped[str | None] = mapped_column(String(64), default=None)
    tags: Mapped[list[str]] = mapped_column(JSON(), nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)


class ScreenRunRow(Base):
    """SQLite row for one screen run (the funnel and its survivors)."""

    __tablename__ = "screen_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    screen_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    universe_id: Mapped[str] = mapped_column(String(64), nullable=False)
    ran_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    as_of: Mapped[datetime | None] = mapped_column(DateTime(), default=None)
    base_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    pass_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    duration_ms: Mapped[float] = mapped_column(Float(), nullable=False, default=0.0)
    rank_by: Mapped[str] = mapped_column(String(32), nullable=False, default="composite")
    stages: Mapped[list[dict[str, Any]]] = mapped_column(JSON(), nullable=False, default=list)
    symbols: Mapped[list[str]] = mapped_column(JSON(), nullable=False, default=list)
    survivors: Mapped[list[dict[str, Any]]] = mapped_column(JSON(), nullable=False, default=list)
    stats: Mapped[dict[str, Any]] = mapped_column(JSON(), nullable=False, default=dict)
    coverage: Mapped[dict[str, Any]] = mapped_column(JSON(), nullable=False, default=dict)


class ScreenBacktestRow(Base):
    """SQLite row for a backtest requested from a screen."""

    __tablename__ = "screen_backtest_requests"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    screen_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    screen_run_id: Mapped[str | None] = mapped_column(String(64), default=None)
    universe_id: Mapped[str | None] = mapped_column(String(64), default=None)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending_runner")
    backtest_id: Mapped[str | None] = mapped_column(String(64), default=None)
    detail: Mapped[str | None] = mapped_column(Text(), default=None)
    request: Mapped[dict[str, Any]] = mapped_column(JSON(), nullable=False, default=dict)
    response: Mapped[dict[str, Any] | None] = mapped_column(JSON(), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)


_OWNED_TABLES = (
    ScreenRow.__table__,
    ScreenRunRow.__table__,
    ScreenBacktestRow.__table__,
)


def ensure_schema() -> None:
    """Create the screen tables if they do not exist yet."""
    Base.metadata.create_all(get_engine(), tables=list(_OWNED_TABLES))


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class FunnelStage:
    """Survivors after applying one more rule."""

    label: str
    rule_index: int
    input_count: int
    output_count: int
    field: str | None = None
    operator: str | None = None

    @property
    def pass_rate(self) -> float:
        """Fraction of inputs that survived this stage."""
        return self.output_count / self.input_count if self.input_count else 0.0

    @property
    def dropped(self) -> int:
        """How many names this stage removed."""
        return self.input_count - self.output_count

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of the stage."""
        return {
            "label": self.label,
            "rule_index": self.rule_index,
            "field": self.field,
            "operator": self.operator,
            "input_count": self.input_count,
            "output_count": self.output_count,
            "dropped": self.dropped,
            "pass_rate": self.pass_rate,
        }


@dataclass(slots=True)
class ScreenRun:
    """The result of running a screen against a universe."""

    screen_id: str | None
    universe_id: str
    ran_at: datetime
    id: str = field(default_factory=lambda: f"scr-{uuid.uuid4().hex[:10]}")
    as_of: date | None = None
    rank_by: str = "composite"
    base_count: int = 0
    #: Enabled rules that actually constrained the result.
    active_rule_count: int = 0
    stages: list[FunnelStage] = field(default_factory=list)
    survivors: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    coverage: dict[str, Any] = field(default_factory=dict)
    duration_ms: float = 0.0
    warnings: list[str] = field(default_factory=list)

    @property
    def symbols(self) -> list[str]:
        """Surviving symbols in rank order."""
        return [str(row["symbol"]) for row in self.survivors]

    @property
    def pass_count(self) -> int:
        """How many names passed the whole screen."""
        return len(self.survivors)

    def as_dict(self, *, include_survivors: bool = True) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of the run."""
        payload: dict[str, Any] = {
            "id": self.id,
            "screen_id": self.screen_id,
            "universe_id": self.universe_id,
            "ran_at": self.ran_at.isoformat(),
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "rank_by": self.rank_by,
            "base_count": self.base_count,
            "pass_count": self.pass_count,
            "active_rule_count": self.active_rule_count,
            "duration_ms": self.duration_ms,
            "stages": [stage.as_dict() for stage in self.stages],
            "stats": dict(self.stats),
            "coverage": dict(self.coverage),
            "warnings": list(self.warnings),
            "symbols": self.symbols,
        }
        if include_survivors:
            payload["survivors"] = list(self.survivors)
        return payload


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------
class ScreenRegistry:
    """CRUD for saved screens, their runs and their backtest requests."""

    def __init__(self, session_factory: sessionmaker[Session] | None = None) -> None:
        """Create the registry, ensuring its tables exist."""
        self._session_factory = session_factory
        ensure_schema()

    @property
    def session_factory(self) -> sessionmaker[Session] | None:
        """The session factory this registry was bound to (``None`` = default).

        Exposed so a sibling store can be opened against the *same* database as
        the screens - a test that passes a temporary factory must not have half
        its reads land in the process-wide one.
        """
        return self._session_factory

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
    def _to_screen(row: ScreenRow) -> Screen:
        """Map a row to the :class:`Screen` dataclass."""
        screen = Screen.from_dict(
            {
                "id": row.id,
                "name": row.name,
                "universe_id": row.universe_id,
                "rules": row.rules,
                "expression": row.expression,
                "description": row.description,
                "rank_by": row.rank_by,
                "owner": row.owner,
                "tags": row.tags,
            }
        )
        screen.created_at = row.created_at
        screen.updated_at = row.updated_at
        return screen

    # --- screens -----------------------------------------------------------
    def list_screens(self) -> list[Screen]:
        """Return every saved screen, newest first."""
        with self._session() as session:
            rows = session.scalars(
                select(ScreenRow).order_by(ScreenRow.updated_at.desc())
            ).all()
            return [self._to_screen(row) for row in rows]

    def get(self, screen_id: str) -> Screen:
        """Return one screen.

        Raises:
            KeyError: when ``screen_id`` is unknown.
        """
        with self._session() as session:
            row = session.get(ScreenRow, screen_id)
            if row is None:
                raise KeyError(f"unknown screen {screen_id!r}")
            return self._to_screen(row)

    def create(
        self,
        name: str,
        universe_id: str,
        *,
        rules: Sequence[dict[str, Any]] | None = None,
        expression: str | None = None,
        description: str | None = None,
        rank_by: str = "composite",
        owner: str | None = None,
        tags: Sequence[str] | None = None,
        screen_id: str | None = None,
    ) -> Screen:
        """Create and persist a screen after validating its rules."""
        title = name.strip()
        if not title:
            raise ValueError("a screen needs a name")
        candidate = Screen.from_dict(
            {
                "id": screen_id or _slug(title),
                "name": title,
                "universe_id": universe_id,
                "rules": list(rules or []),
                "expression": expression,
                "description": description,
                "rank_by": rank_by,
                "owner": owner,
                "tags": list(tags or []),
            }
        )
        RuleEngine().validate_screen(candidate)
        now = utcnow()
        with self._session() as session:
            candidate.id = self._unique_id(session, candidate.id)
            session.add(
                ScreenRow(
                    id=candidate.id,
                    name=candidate.name,
                    universe_id=candidate.universe_id,
                    rules=[rule.as_dict() for rule in candidate.rules],
                    expression=candidate.expression,
                    description=candidate.description,
                    rank_by=candidate.rank_by,
                    owner=candidate.owner,
                    tags=list(candidate.tags),
                    created_at=now,
                    updated_at=now,
                )
            )
        candidate.created_at = now
        candidate.updated_at = now
        return candidate

    def update(self, screen_id: str, **changes: Any) -> Screen:
        """Update a screen's fields (validating rules before storing them)."""
        with self._session() as session:
            row = session.get(ScreenRow, screen_id)
            if row is None:
                raise KeyError(f"unknown screen {screen_id!r}")
            if "name" in changes:
                title = str(changes["name"]).strip()
                if not title:
                    raise ValueError("a screen needs a name")
                row.name = title
            for key in ("universe_id", "expression", "description", "rank_by", "owner"):
                if key in changes:
                    value = changes[key]
                    setattr(row, key, str(value) if value is not None else None)
            if "rules" in changes:
                row.rules = [
                    rule if isinstance(rule, dict) else rule.as_dict()
                    for rule in (changes["rules"] or [])
                ]
            if "tags" in changes:
                row.tags = [str(tag) for tag in (changes["tags"] or [])]
            row.updated_at = utcnow()
            screen = self._to_screen(row)
        RuleEngine().validate_screen(screen)
        return screen

    def delete(self, screen_id: str) -> None:
        """Delete a screen. Its runs and backtest requests are kept as history."""
        with self._session() as session:
            row = session.get(ScreenRow, screen_id)
            if row is None:
                raise KeyError(f"unknown screen {screen_id!r}")
            session.delete(row)

    @staticmethod
    def _unique_id(session: Session, base: str) -> str:
        """Return an unused screen id derived from ``base``."""
        candidate = base
        suffix = 2
        while session.get(ScreenRow, candidate) is not None:
            candidate = f"{base}-{suffix}"
            suffix += 1
        return candidate

    # --- runs --------------------------------------------------------------
    def save_run(self, run: ScreenRun) -> None:
        """Persist a completed run."""
        with self._session() as session:
            session.add(
                ScreenRunRow(
                    id=run.id,
                    screen_id=run.screen_id,
                    universe_id=run.universe_id,
                    ran_at=run.ran_at,
                    as_of=(
                        datetime.combine(run.as_of, datetime.min.time()) if run.as_of else None
                    ),
                    base_count=run.base_count,
                    pass_count=run.pass_count,
                    duration_ms=run.duration_ms,
                    rank_by=run.rank_by,
                    stages=[stage.as_dict() for stage in run.stages],
                    symbols=run.symbols,
                    survivors=run.survivors,
                    stats=run.stats,
                    coverage=run.coverage,
                )
            )

    def list_runs(self, screen_id: str | None = None, *, limit: int = 20) -> list[dict[str, Any]]:
        """Return recent runs (without their survivor rows), newest first."""
        with self._session() as session:
            query = select(ScreenRunRow)
            if screen_id is not None:
                query = query.where(ScreenRunRow.screen_id == screen_id)
            query = query.order_by(ScreenRunRow.ran_at.desc()).limit(limit)
            return [_run_row_dict(row, include_survivors=False) for row in session.scalars(query)]

    def get_run(self, run_id: str) -> dict[str, Any]:
        """Return one stored run including its survivor rows."""
        with self._session() as session:
            row = session.get(ScreenRunRow, run_id)
            if row is None:
                raise KeyError(f"unknown screen run {run_id!r}")
            return _run_row_dict(row, include_survivors=True)

    def latest_run(self, screen_id: str, *, before: str | None = None) -> dict[str, Any] | None:
        """Return the most recent stored run of a screen, optionally excluding one."""
        with self._session() as session:
            query = (
                select(ScreenRunRow)
                .where(ScreenRunRow.screen_id == screen_id)
                .order_by(ScreenRunRow.ran_at.desc())
                .limit(2)
            )
            for row in session.scalars(query):
                if before is not None and row.id == before:
                    continue
                return _run_row_dict(row, include_survivors=False)
        return None

    # --- backtest requests -------------------------------------------------
    def save_backtest_request(
        self,
        *,
        request: BacktestRequest,
        status: str,
        detail: str | None,
        backtest_id: str | None,
        response: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Persist a backtest request and return its stored record."""
        now = utcnow()
        record_id = f"sbt-{uuid.uuid4().hex[:10]}"
        with self._session() as session:
            session.add(
                ScreenBacktestRow(
                    id=record_id,
                    screen_id=request.screen_id,
                    screen_run_id=request.screen_run_id,
                    universe_id=request.universe_id,
                    name=request.name,
                    status=status,
                    backtest_id=backtest_id,
                    detail=detail,
                    request=request.as_dict(),
                    response=response,
                    created_at=now,
                    updated_at=now,
                )
            )
        return {
            "id": record_id,
            "screen_id": request.screen_id,
            "screen_run_id": request.screen_run_id,
            "universe_id": request.universe_id,
            "name": request.name,
            "status": status,
            "backtest_id": backtest_id,
            "detail": detail,
            "request": request.as_dict(),
            "response": response,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }

    def list_backtest_requests(
        self, screen_id: str | None = None, *, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Return recent backtest requests, newest first."""
        from tradinglib.research.backtest import BacktestRunRow

        with self._session() as session:
            query = select(ScreenBacktestRow).order_by(ScreenBacktestRow.created_at.desc())
            if screen_id is not None:
                query = query.where(ScreenBacktestRow.screen_id == screen_id)
            rows = session.scalars(query.limit(limit)).all()
            # The request row's status is written once at queue time; the lab
            # backtest it spawned is the live truth, so resolve through it.
            live_status: dict[str, str] = {}
            backtest_ids = [row.backtest_id for row in rows if row.backtest_id]
            if backtest_ids:
                for run in session.scalars(
                    select(BacktestRunRow).where(BacktestRunRow.id.in_(backtest_ids))
                ):
                    live_status[run.id] = str(run.status)
            return [
                {
                    "id": row.id,
                    "screen_id": row.screen_id,
                    "screen_run_id": row.screen_run_id,
                    "universe_id": row.universe_id,
                    "name": row.name,
                    "status": live_status.get(row.backtest_id, row.status),
                    "backtest_id": row.backtest_id,
                    "detail": row.detail,
                    "request": row.request,
                    "response": row.response,
                    "created_at": row.created_at.isoformat(),
                    "updated_at": row.updated_at.isoformat(),
                }
                for row in rows
            ]


def _slug(value: str) -> str:
    """Return an id-safe slug for a screen name."""
    cleaned = "".join(char.lower() if char.isalnum() else "-" for char in value.strip())
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-")[:48] or f"screen-{uuid.uuid4().hex[:6]}"


def _run_row_dict(row: ScreenRunRow, *, include_survivors: bool) -> dict[str, Any]:
    """Render a stored run row as a JSON-safe mapping."""
    payload: dict[str, Any] = {
        "id": row.id,
        "screen_id": row.screen_id,
        "universe_id": row.universe_id,
        "ran_at": row.ran_at.isoformat(),
        "as_of": row.as_of.date().isoformat() if row.as_of else None,
        "base_count": row.base_count,
        "pass_count": row.pass_count,
        "duration_ms": row.duration_ms,
        "rank_by": row.rank_by,
        "stages": row.stages,
        "symbols": row.symbols,
        "stats": row.stats,
        "coverage": row.coverage,
    }
    if include_survivors:
        payload["survivors"] = row.survivors
    return payload


# ---------------------------------------------------------------------------
# evaluation
# ---------------------------------------------------------------------------
class FunnelEvaluator:
    """Runs screens and produces the funnel panel's real numbers."""

    def __init__(
        self,
        *,
        registry: ScreenRegistry | None = None,
        universes: UniverseRegistry | None = None,
        builder: MetricBuilder | None = None,
        engine: RuleEngine | None = None,
        bus: EventBus | None = None,
    ) -> None:
        """Create the evaluator.

        Args:
            registry: screen/run persistence (created on first use).
            universes: universe registry used to resolve the base population.
            builder: metric-panel builder (store + providers).
            engine: rule engine.
            bus: event bus notifications are published on; optional.
        """
        self._registry = registry
        self._universes = universes
        self._builder = builder
        self.engine = engine or RuleEngine()
        self._bus = bus
        self._frames: dict[tuple[str, str | None], MetricFrame] = {}
        self._frame_lock = asyncio.Lock()

    @property
    def registry(self) -> ScreenRegistry:
        """The screen registry (created on first use)."""
        if self._registry is None:
            self._registry = ScreenRegistry()
        return self._registry

    @property
    def universes(self) -> UniverseRegistry:
        """The universe registry."""
        if self._universes is None:
            self._universes = get_universe_registry()
        return self._universes

    @property
    def builder(self) -> MetricBuilder:
        """The metric builder."""
        if self._builder is None:
            self._builder = MetricBuilder()
        return self._builder

    # --- base population ---------------------------------------------------
    def _resolve_universe(self, universe_id: str | None) -> str:
        """Return the reference to screen, defaulting to the active scope.

        The active scope may be a watchlist, which `UniverseRegistry.resolve`
        understands through its ``watchlist:<id>`` form - so a screen with no
        explicit universe runs over whatever the topbar has selected.
        """
        if universe_id:
            return universe_id
        scope = self.universes.active_scope()
        if not scope.get("ref"):
            raise KeyError("no universe was given and no active scope is set")
        return str(scope["ref"])

    def base_symbols(
        self, universe_id: str, *, as_of: date | None = None
    ) -> tuple[list[str], dict[str, str], dict[str, str]]:
        """Return the universe's symbols plus its sector and name maps."""
        symbols = self.universes.resolve(universe_id, as_of=as_of)
        sectors: dict[str, str] = {}
        names: dict[str, str] = {}
        if self.universes.exists(universe_id):
            for member in self.universes.members(universe_id, as_of=as_of):
                if member.sector:
                    sectors[member.symbol] = member.sector
                if member.name:
                    names[member.symbol] = member.name
        return symbols, sectors, names

    async def metric_frame(
        self, universe_id: str, *, as_of: date | None = None, refresh: bool = False
    ) -> MetricFrame:
        """Build (or reuse) the metric panel for a universe.

        Panels are memoised per (universe, as_of) for the lifetime of the
        evaluator so editing a rule re-counts instantly instead of re-pricing
        the universe; ``refresh=True`` forces a rebuild.
        """
        key = (universe_id, as_of.isoformat() if as_of else None)
        async with self._frame_lock:
            cached = self._frames.get(key)
            if cached is not None and not refresh:
                return cached
        symbols, sectors, names = await asyncio.to_thread(
            self.base_symbols, universe_id, as_of=as_of
        )
        panel = await self.builder.build(symbols, as_of=as_of, sectors=sectors, names=names)
        async with self._frame_lock:
            self._frames[key] = panel
        return panel

    # --- running -----------------------------------------------------------
    def evaluate_frame(self, screen: Screen, frame: pd.DataFrame) -> tuple[
        list[FunnelStage], pd.DataFrame
    ]:
        """Apply the screen's rules in order, returning the funnel and survivors."""
        self.engine.validate_screen(screen)
        stages: list[FunnelStage] = []
        surviving = frame
        for index, rule in enumerate(screen.rules):
            if not rule.enabled:
                continue
            before = len(surviving)
            surviving = self.engine.apply_rule(surviving, rule)
            stages.append(
                FunnelStage(
                    label=rule.label or rule.describe(),
                    rule_index=index,
                    input_count=before,
                    output_count=len(surviving),
                    field=rule.field,
                    operator=str(rule.operator),
                )
            )
        if screen.expression:
            before = len(surviving)
            surviving = surviving[self.engine.compile_expression(screen.expression)(surviving)]
            stages.append(
                FunnelStage(
                    label=f"expression: {screen.expression}",
                    rule_index=len(screen.rules),
                    input_count=before,
                    output_count=len(surviving),
                    field=None,
                    operator="expression",
                )
            )
        if screen.rank_by in surviving.columns:
            surviving = surviving.sort_values(
                screen.rank_by,
                ascending=METRICS[screen.rank_by].higher_is_better is False,
                kind="stable",
                na_position="last",
            )
        return stages, surviving

    async def run(
        self,
        screen: Screen,
        *,
        as_of: date | None = None,
        persist: bool = True,
        refresh: bool = False,
        limit: int | None = None,
    ) -> ScreenRun:
        """Evaluate a screen over its universe and return the full funnel."""
        started = time.perf_counter()
        universe_id = self._resolve_universe(screen.universe_id)
        panel = await self.metric_frame(universe_id, as_of=as_of, refresh=refresh)
        stages, surviving = self.evaluate_frame(screen, panel.frame)
        if limit is not None:
            surviving = surviving.head(max(0, limit))

        run = ScreenRun(
            screen_id=screen.id,
            universe_id=universe_id,
            ran_at=utcnow(),
            as_of=as_of,
            rank_by=screen.rank_by,
            base_count=len(panel),
            active_rule_count=len(screen.active_rules),
            stages=stages,
            survivors=frame_records(surviving, columns=RESULT_COLUMNS),
            coverage=panel.as_dict(),
            duration_ms=(time.perf_counter() - started) * 1000.0,
        )
        if panel.missing:
            run.warnings.append(
                f"{len(panel.missing)} symbol(s) were dropped for lack of data: "
                + ", ".join(sorted(panel.missing)[:8])
            )
        if panel.coverage.get("truncated"):
            run.warnings.append(
                f"the universe was truncated to {panel.coverage.get('max_symbols')} symbols"
            )
        run.stats = self.stats(run)

        if persist:
            await asyncio.to_thread(self.registry.save_run, run)
        await self._notify(
            "screen_run",
            {
                "screen_id": screen.id,
                "run_id": run.id,
                "universe_id": universe_id,
                "base_count": run.base_count,
                "pass_count": run.pass_count,
                "message": f"Screen executed - {run.pass_count} names pass",
            },
        )
        return run

    async def run_screen_id(
        self, screen_id: str, *, as_of: date | None = None, refresh: bool = False
    ) -> ScreenRun:
        """Load a saved screen by id and run it."""
        screen = await asyncio.to_thread(self.registry.get, screen_id)
        return await self.run(screen, as_of=as_of, refresh=refresh)

    async def preview_count(self, screen: Screen, *, as_of: date | None = None) -> int:
        """Return the live survivor count while the user edits rules."""
        universe_id = self._resolve_universe(screen.universe_id)
        panel = await self.metric_frame(universe_id, as_of=as_of)
        _stages, surviving = self.evaluate_frame(screen, panel.frame)
        return int(len(surviving))

    # --- statistics --------------------------------------------------------
    def stats(self, run: ScreenRun) -> dict[str, Any]:
        """Compute the result-profile statistics of a completed run.

        * ``sector_mix`` - share of survivors per sector, largest first;
        * ``median_market_cap`` - median of the survivors that report one;
        * ``turnover`` - names entering and leaving versus the previous stored
          run of the same screen (``None`` when this is the first run: there is
          nothing to compare against and a made-up number would be worse);
        * ``live_book_overlap`` - survivors already held in the trading book
          (see :meth:`live_book_overlap`).
        """
        survivors = run.survivors
        total = len(survivors)
        sector_counts: dict[str, int] = {}
        caps: list[float] = []
        for row in survivors:
            sector = row.get("sector") or "Unclassified"
            sector_counts[sector] = sector_counts.get(sector, 0) + 1
            cap = row.get("mkt_cap")
            if isinstance(cap, (int, float)):
                caps.append(float(cap))
        sector_mix = [
            {"sector": sector, "count": count, "share": count / total if total else 0.0}
            for sector, count in sorted(sector_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ]

        turnover: dict[str, Any] | None = None
        if run.screen_id:
            try:
                previous = self.registry.latest_run(run.screen_id, before=run.id)
            except Exception:  # noqa: BLE001 - statistics must not break a run
                logger.warning("could not read the previous run of %s", run.screen_id, exc_info=True)
                previous = None
            if previous is not None:
                old = set(previous.get("symbols") or [])
                new = set(run.symbols)
                turnover = {
                    "compared_with_run": previous["id"],
                    "compared_with_ran_at": previous["ran_at"],
                    "entered": sorted(new - old),
                    "exited": sorted(old - new),
                    "entered_count": len(new - old),
                    "exited_count": len(old - new),
                    "retained_count": len(new & old),
                }

        return {
            "count": total,
            "base_count": run.base_count,
            "pass_rate": total / run.base_count if run.base_count else 0.0,
            "sector_mix": sector_mix,
            "median_market_cap": statistics.median(caps) if caps else None,
            "market_cap_coverage": len(caps),
            "turnover": turnover,
            "live_book_overlap": self.live_book_overlap(run.symbols),
        }

    def live_book_overlap(self, symbols: Sequence[str]) -> dict[str, Any]:
        """Return how many survivors are already held in the trading book.

        "Live book" means the positions actually open in the trading accounts -
        read through :mod:`tradinglib.trade`'s portfolio store, never invented. This
        workspace trades paper by default (``docs/SPEC.md`` decision D5), so the
        payload says which account the overlap came from and whether it is a
        paper book; a screen that keeps proposing what is already held is a
        screen that is not adding anything, and that has to be visible.

        When no trading account exists yet the default watchlist is used as the
        stand-in book and ``source`` says so, rather than reporting a zero that
        would read as "no overlap".
        """
        wanted = {str(symbol).upper() for symbol in symbols}
        empty: dict[str, Any] = {
            "source": None,
            "kind": None,
            "symbols": [],
            "count": 0,
            "share": 0.0,
            "reason": "no trading account and no default watchlist exist yet",
        }
        try:
            from tradinglib.trade.portfolio import PortfolioStore

            # Read-only: the research layer never writes to the trading store.
            store = PortfolioStore(self.registry.session_factory)
            accounts = store.list_accounts()
        except Exception:  # noqa: BLE001 - the profile panel must not break a run
            logger.warning("could not read the trading accounts", exc_info=True)
            accounts = []
        held: set[str] = set()
        names: list[str] = []
        paper_only = True
        for account in accounts:
            try:
                book = store.load(account.id, order_limit=0)
            except KeyError:  # pragma: no cover - listed then deleted
                continue
            open_positions = {
                position.symbol.upper()
                for position in book.positions.values()
                if abs(position.qty) > 0
            }
            if not open_positions:
                continue
            held |= open_positions
            names.append(account.id)
            paper_only = paper_only and str(account.broker).lower() == "paper"
        if names:
            overlapping = sorted(held & wanted)
            return {
                "source": "accounts:" + ",".join(sorted(names)),
                "kind": "paper" if paper_only else "live",
                "symbols": overlapping,
                "count": len(overlapping),
                "share": len(overlapping) / len(wanted) if wanted else 0.0,
                "book_size": len(held),
                "reason": None,
            }

        try:
            watchlist = self.universes.watchlists.default_watchlist()
        except Exception:  # noqa: BLE001 - the profile panel must not break a run
            logger.warning("could not read the default watchlist", exc_info=True)
            watchlist = None
        if watchlist is None:
            return empty
        overlapping = sorted({s.upper() for s in watchlist.symbols} & wanted)
        return {
            "source": f"watchlist:{watchlist.id}",
            "kind": "watchlist",
            "symbols": overlapping,
            "count": len(overlapping),
            "share": len(overlapping) / len(wanted) if wanted else 0.0,
            "book_size": len(watchlist.symbols),
            "reason": (
                "no trading account holds a position yet, so the default "
                "watchlist stands in for the book"
            ),
        }

    # --- backtest hand-off -------------------------------------------------
    async def queue_backtest(
        self,
        screen_id: str,
        *,
        run_id: str | None = None,
        name: str | None = None,
        start: date | None = None,
        end: date | None = None,
        benchmark: str = "SPY",
        rebalance: str = "monthly",
        initial_capital: float = 1_000_000.0,
        **params: Any,
    ) -> dict[str, Any]:
        """Queue a real backtest of a screen's survivors.

        The symbol list comes from a stored run (``run_id``, or the newest run
        of the screen). When the Strategy Lab has registered a queue the
        request is handed to it and the lab's record id is stored; otherwise
        the request is persisted with status ``pending_runner`` and says so.

        Raises:
            KeyError: when the screen or the run is unknown.
            ValueError: when the screen has never been run, so there is no
                symbol list to backtest.
        """
        screen = await asyncio.to_thread(self.registry.get, screen_id)
        if run_id is not None:
            stored = await asyncio.to_thread(self.registry.get_run, run_id)
        else:
            stored = await asyncio.to_thread(self.registry.latest_run, screen_id)
        if stored is None:
            raise ValueError(
                f"screen {screen_id!r} has no stored run; run it before queueing a backtest"
            )
        symbols = [str(s) for s in stored.get("symbols") or []]
        if not symbols:
            raise ValueError(
                f"the last run of screen {screen_id!r} produced no names, so there is "
                "nothing to backtest"
            )

        request = BacktestRequest(
            name=name or f"screen: {screen.name}",
            symbols=symbols,
            universe_id=stored.get("universe_id") or screen.universe_id,
            screen_id=screen_id,
            screen_run_id=stored["id"],
            start=start,
            end=end,
            benchmark=benchmark,
            rebalance=rebalance,
            initial_capital=initial_capital,
            origin="screener",
            params=dict(params),
        )

        queue = get_backtest_queue()
        if queue is None:
            record = await asyncio.to_thread(
                self.registry.save_backtest_request,
                request=request,
                status="pending_runner",
                detail=(
                    "stored: no Strategy Lab backtest runner is registered in this process yet"
                ),
                backtest_id=None,
                response=None,
            )
        else:
            try:
                response = await queue.queue_backtest(request)
            except Exception as exc:  # noqa: BLE001 - the lab may raise anything
                logger.warning("the lab rejected a queued backtest", exc_info=True)
                record = await asyncio.to_thread(
                    self.registry.save_backtest_request,
                    request=request,
                    status="failed",
                    detail=f"{type(exc).__name__}: {exc}",
                    backtest_id=None,
                    response=None,
                )
            else:
                record = await asyncio.to_thread(
                    self.registry.save_backtest_request,
                    request=request,
                    status=str(response.get("status") or "queued"),
                    detail=response.get("detail"),
                    backtest_id=(
                        str(response.get("id")) if response.get("id") is not None else None
                    ),
                    response=response,
                )

        await self._notify(
            "screen_backtest_queued",
            {
                "screen_id": screen_id,
                "run_id": request.screen_run_id,
                "status": record["status"],
                "backtest_id": record["backtest_id"],
                "symbol_count": len(symbols),
                "message": (
                    f"Backtest queued for {len(symbols)} names"
                    if record["status"] != "pending_runner"
                    else f"Backtest request stored for {len(symbols)} names "
                    "(no lab runner registered yet)"
                ),
            },
        )
        return record

    # --- notifications -----------------------------------------------------
    async def _notify(self, event: str, payload: dict[str, Any]) -> None:
        """Publish a screener event, when a bus was provided."""
        if self._bus is None:
            return
        await self._bus.publish(NOTIFICATION_CHANNEL, {"event": event, **payload})
