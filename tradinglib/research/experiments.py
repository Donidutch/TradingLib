"""Experiments: configured, repeatable tests attached to a hypothesis.

An *experiment definition* is a hypothesis plus a universe, a signal
specification and a period - everything needed to reproduce a test. Running one
executes a real computation against the Parquet + DuckDB store:

* a full backtest through :mod:`tradinglib.research.backtest` (equity curve, costs,
  benchmark-relative statistics, train/OOS split), and
* an information-coefficient study of the signal itself: the rank correlation
  between the cross-section of the signal and the forward returns it is
  supposed to predict, sampled on non-overlapping windows so the t-statistic is
  not inflated by overlapping observations.

The run's metrics, its ordered log and its acceptance-criteria outcome are
persisted; progress is published on the ``lab`` event channel as it happens.

Acceptance criteria here are the *experiment-level* quick check (does this run
deserve to move on?). The authoritative promotion gate suite - cost
sensitivity, benchmark outperformance, the t-test on excess returns - lives in
:mod:`tradinglib.research.validation`, which reuses the defaults declared here.
"""

from __future__ import annotations

import asyncio
import logging
import math
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Final

import numpy as np
import pandas as pd
from sqlalchemy import (
    JSON,
    DateTime,
    Float,
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
from tradinglib.research.backtest import (
    LAB_CHANNEL,
    BacktestConfig,
    BacktestEngine,
    BacktestResult,
    get_backtest_engine,
)

logger = logging.getLogger(__name__)

#: Minimum number of names needed in a cross-section for a meaningful IC.
MIN_IC_BREADTH: Final[int] = 5


class ExperimentStatus(StrEnum):
    """Lifecycle states of an experiment run."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ExperimentKind(StrEnum):
    """What an experiment actually computes."""

    #: Backtest only - portfolio statistics, no signal diagnostics.
    BACKTEST = "backtest"
    #: Signal diagnostics only - information coefficient, no portfolio.
    SIGNAL = "signal"
    #: Both (the default): a backtest plus the IC study of its signal.
    COMBINED = "combined"


class ExperimentStage(StrEnum):
    """Where an experiment sits in the research funnel."""

    RESEARCH = "research"
    VALIDATED = "validated"
    PAPER = "paper"
    APPROVED = "approved"
    LIVE = "live"
    KILLED = "killed"


@dataclass(slots=True)
class AcceptanceCriteria:
    """The experiment-level pass/fail thresholds.

    These are the workspace defaults the Strategy Lab shows next to a run
    ("OOS Sharpe >= 0.8 · Max DD <= 15% · trades >= 30"). They are stored on the
    experiment so a run always remembers which thresholds judged it.
    """

    min_oos_sharpe: float = 0.8
    max_drawdown: float = 0.15
    min_trades: int = 30

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping."""
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> AcceptanceCriteria:
        """Rebuild criteria from :meth:`as_dict` output."""
        data = payload or {}
        return cls(
            min_oos_sharpe=float(data.get("min_oos_sharpe", 0.8)),
            max_drawdown=float(data.get("max_drawdown", 0.15)),
            min_trades=int(data.get("min_trades", 30)),
        )


#: Workspace default acceptance criteria, reused by the promotion gates.
DEFAULT_ACCEPTANCE: Final[AcceptanceCriteria] = AcceptanceCriteria()


@dataclass(slots=True)
class Experiment:
    """An experiment definition: what to test, over what, with which settings."""

    id: str
    name: str
    hypothesis_id: str | None = None
    kind: ExperimentKind = ExperimentKind.COMBINED
    stage: ExperimentStage = ExperimentStage.RESEARCH
    universe_id: str | None = None
    config: BacktestConfig | None = None
    criteria: AcceptanceCriteria = field(default_factory=AcceptanceCriteria)
    horizon: int = 21
    owner: str | None = None
    notes: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping for the API layer."""
        return {
            "id": self.id,
            "name": self.name,
            "hypothesis_id": self.hypothesis_id,
            "kind": str(self.kind),
            "stage": str(self.stage),
            "universe_id": self.universe_id,
            "config": self.config.as_dict() if self.config else None,
            "signal": self.config.signal.describe() if self.config else None,
            "benchmark": self.config.benchmark if self.config else None,
            "period": {
                "start": self.config.start.isoformat()
                if self.config and self.config.start
                else None,
                "end": self.config.end.isoformat() if self.config and self.config.end else None,
            },
            "criteria": self.criteria.as_dict(),
            "horizon": self.horizon,
            "owner": self.owner,
            "notes": self.notes,
            "params": dict(self.params),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


@dataclass(slots=True)
class ExperimentRun:
    """One execution of an experiment, with its log and results."""

    id: str
    experiment_id: str
    status: ExperimentStatus = ExperimentStatus.QUEUED
    hypothesis_id: str | None = None
    backtest_id: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: float | None = None
    log: list[dict[str, Any]] = field(default_factory=list)
    results: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def as_dict(self, *, include_log: bool = True) -> dict[str, Any]:
        """Return a JSON-serialisable mapping for the API layer."""
        payload: dict[str, Any] = {
            "id": self.id,
            "experiment_id": self.experiment_id,
            "hypothesis_id": self.hypothesis_id,
            "backtest_id": self.backtest_id,
            "status": str(self.status),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "duration_ms": self.duration_ms,
            "results": dict(self.results),
            "error": self.error,
            "log_lines": len(self.log),
        }
        if include_log:
            payload["log"] = list(self.log)
        return payload


# --------------------------------------------------------------------------
# signal diagnostics
# --------------------------------------------------------------------------
def information_coefficient(
    signals: pd.DataFrame, prices: pd.DataFrame, *, horizon: int = 21
) -> dict[str, Any]:
    """Measure how well a signal ranks the next ``horizon`` bars of returns.

    For every sampled bar the Spearman rank correlation between that bar's
    signal cross-section and the forward return over ``horizon`` bars is
    computed. Bars are sampled every ``horizon`` steps so the observations do
    not overlap - overlapping windows would inflate the t-statistic by counting
    the same price moves several times.

    Args:
        signals: signal panel (rows = bars, columns = symbols).
        prices: matching close-price panel.
        horizon: forward-return horizon in bars.

    Returns:
        Mapping with ``mean_ic``, ``std_ic``, ``t_stat``, ``p_value``,
        ``hit_rate``, ``ic_ir``, ``observations``, ``breadth`` and the
        ``series`` of ``{date, ic, breadth}`` points for the chart. When there
        are too few usable cross-sections everything is ``None`` and
        ``observations`` is the honest count.
    """
    step = max(1, int(horizon))
    forward = prices.shift(-step) / prices - 1.0
    points: list[dict[str, Any]] = []
    for position in range(0, len(prices.index) - step, step):
        stamp = prices.index[position]
        signal_row = signals.iloc[position]
        forward_row = forward.iloc[position]
        pair = pd.DataFrame({"signal": signal_row, "forward": forward_row}).dropna()
        if pair.shape[0] < MIN_IC_BREADTH or pair["signal"].nunique() < 2:
            continue
        value = pair["signal"].corr(pair["forward"], method="spearman")
        if value is None or (isinstance(value, float) and math.isnan(value)):
            continue
        points.append(
            {
                "date": pd.Timestamp(stamp).isoformat(),
                "ic": float(value),
                "breadth": int(pair.shape[0]),
            }
        )

    values = np.array([p["ic"] for p in points], dtype="float64")
    count = int(values.size)
    if count == 0:
        return {
            "horizon": step,
            "observations": 0,
            "mean_ic": None,
            "std_ic": None,
            "t_stat": None,
            "p_value": None,
            "hit_rate": None,
            "ic_ir": None,
            "breadth": 0,
            "series": [],
            "note": "no cross-section had enough names with both a signal and a forward return",
        }
    mean = float(values.mean())
    std = float(values.std(ddof=1)) if count > 1 else 0.0
    t_stat = float(mean / (std / math.sqrt(count))) if std > 0 and count > 1 else None
    p_value: float | None = None
    if t_stat is not None:
        from scipy import stats as scipy_stats

        p_value = float(2.0 * scipy_stats.t.sf(abs(t_stat), df=count - 1))
    return {
        "horizon": step,
        "observations": count,
        "mean_ic": mean,
        "std_ic": std,
        "t_stat": t_stat,
        "p_value": p_value,
        "hit_rate": float((values > 0).mean()),
        "ic_ir": float(mean / std) if std > 0 else None,
        "breadth": int(np.median([p["breadth"] for p in points])),
        "series": points,
    }


def evaluate_acceptance(
    result: BacktestResult, criteria: AcceptanceCriteria
) -> tuple[bool, list[dict[str, Any]]]:
    """Check a backtest against the experiment-level acceptance criteria.

    The out-of-sample window is used when the run has one, because judging a
    strategy on the data it was chosen on is how research fools itself.

    Returns:
        ``(passed, checks)`` where each check carries its threshold, the
        observed value and its own pass flag.
    """
    window = result.out_of_sample or result.stats
    window_label = "out_of_sample" if result.out_of_sample else "full"
    checks = [
        {
            "id": "min_oos_sharpe",
            "label": f"OOS Sharpe >= {criteria.min_oos_sharpe:g}",
            "window": window_label,
            "threshold": criteria.min_oos_sharpe,
            "observed": window.sharpe,
            "passed": window.sharpe >= criteria.min_oos_sharpe,
        },
        {
            "id": "max_drawdown",
            "label": f"Max drawdown <= {criteria.max_drawdown:.0%}",
            "window": "full",
            "threshold": criteria.max_drawdown,
            "observed": abs(result.stats.max_drawdown),
            "passed": abs(result.stats.max_drawdown) <= criteria.max_drawdown,
        },
        {
            "id": "min_trades",
            "label": f"Trades >= {criteria.min_trades}",
            "window": "full",
            "threshold": float(criteria.min_trades),
            "observed": float(result.stats.trades),
            "passed": result.stats.trades >= criteria.min_trades,
        },
    ]
    return all(bool(check["passed"]) for check in checks), checks


# --------------------------------------------------------------------------
# ORM rows
# --------------------------------------------------------------------------
class ExperimentRow(Base):
    """SQLite row for one experiment definition."""

    __tablename__ = "experiments"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    hypothesis_id: Mapped[str | None] = mapped_column(String(64), default=None, index=True)
    kind: Mapped[str] = mapped_column(String(16), default=ExperimentKind.COMBINED, nullable=False)
    stage: Mapped[str] = mapped_column(
        String(16), default=ExperimentStage.RESEARCH, nullable=False
    )
    universe_id: Mapped[str | None] = mapped_column(String(64), default=None)
    config: Mapped[dict[str, Any]] = mapped_column(JSON(), nullable=False)
    criteria: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict, nullable=False)
    horizon: Mapped[int] = mapped_column(Integer(), default=21, nullable=False)
    owner: Mapped[str | None] = mapped_column(String(64), default=None)
    notes: Mapped[str | None] = mapped_column(Text(), default=None)
    params: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)


class ExperimentRunRow(Base):
    """SQLite row for one experiment run. Counted as a trial by ``/api/status``."""

    __tablename__ = "experiment_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    experiment_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    hypothesis_id: Mapped[str | None] = mapped_column(String(64), default=None, index=True)
    backtest_id: Mapped[str | None] = mapped_column(String(64), default=None)
    status: Mapped[str] = mapped_column(String(16), default=ExperimentStatus.QUEUED, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(), default=None)
    duration_ms: Mapped[float | None] = mapped_column(Float(), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    log: Mapped[list[dict[str, Any]]] = mapped_column(JSON(), default=list, nullable=False)
    results: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict, nullable=False)
    error: Mapped[str | None] = mapped_column(Text(), default=None)


_OWNED_TABLES = (ExperimentRow.__table__, ExperimentRunRow.__table__)


def ensure_schema() -> None:
    """Create the experiment tables if they do not exist yet."""
    Base.metadata.create_all(get_engine(), tables=list(_OWNED_TABLES))


# --------------------------------------------------------------------------
# engine
# --------------------------------------------------------------------------
class ExperimentEngine:
    """Creates, runs and reports experiments; streams progress on ``lab``."""

    def __init__(
        self,
        session_factory: sessionmaker[Session] | None = None,
        *,
        bus: EventBus | None = None,
        backtests: BacktestEngine | None = None,
    ) -> None:
        """Create the engine.

        Args:
            session_factory: SQLAlchemy session factory; defaults to the
                process-wide one.
            bus: event bus run progress is published on.
            backtests: backtest engine used to execute runs.
        """
        self._session_factory = session_factory
        self._bus = bus
        self._backtests = backtests
        ensure_schema()

    # --- plumbing ---------------------------------------------------------
    @contextmanager
    def _session(self) -> Iterator[Session]:
        """Transactional session scope bound to this engine."""
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
        """The event bus run progress is published on."""
        if self._bus is None:
            self._bus = get_event_bus()
        return self._bus

    @property
    def backtests(self) -> BacktestEngine:
        """The backtest engine experiment runs execute through."""
        if self._backtests is None:
            self._backtests = get_backtest_engine()
        return self._backtests

    @staticmethod
    def _to_dataclass(row: ExperimentRow) -> Experiment:
        """Map an ORM row onto the dataclass."""
        return Experiment(
            id=row.id,
            name=row.name,
            hypothesis_id=row.hypothesis_id,
            kind=ExperimentKind(row.kind),
            stage=ExperimentStage(row.stage),
            universe_id=row.universe_id,
            config=BacktestConfig.from_dict(row.config),
            criteria=AcceptanceCriteria.from_dict(row.criteria),
            horizon=int(row.horizon),
            owner=row.owner,
            notes=row.notes,
            params=dict(row.params or {}),
            created_at=to_utc(row.created_at),
            updated_at=to_utc(row.updated_at),
        )

    @staticmethod
    def _run_to_dataclass(row: ExperimentRunRow) -> ExperimentRun:
        """Map a run row onto the dataclass."""
        return ExperimentRun(
            id=row.id,
            experiment_id=row.experiment_id,
            hypothesis_id=row.hypothesis_id,
            backtest_id=row.backtest_id,
            status=ExperimentStatus(row.status),
            started_at=to_utc(row.started_at) if row.started_at else None,
            finished_at=to_utc(row.finished_at) if row.finished_at else None,
            duration_ms=row.duration_ms,
            log=list(row.log or []),
            results=dict(row.results or {}),
            error=row.error,
        )

    # --- definitions ------------------------------------------------------
    def list(
        self,
        *,
        hypothesis_id: str | None = None,
        stage: ExperimentStage | None = None,
        query: str | None = None,
    ) -> list[Experiment]:
        """Return experiment definitions, newest first, optionally filtered."""
        statement = select(ExperimentRow).order_by(ExperimentRow.created_at.desc())
        if hypothesis_id:
            statement = statement.where(ExperimentRow.hypothesis_id == hypothesis_id)
        if stage:
            statement = statement.where(ExperimentRow.stage == str(stage))
        with self._session() as session:
            items = [self._to_dataclass(row) for row in session.scalars(statement)]
        if query:
            needle = query.strip().lower()
            items = [
                item
                for item in items
                if needle in item.name.lower()
                or needle in (item.hypothesis_id or "").lower()
                or needle in (item.universe_id or "").lower()
            ]
        return items

    def get(self, experiment_id: str) -> Experiment:
        """Return one experiment definition.

        Raises:
            KeyError: when the identifier is unknown.
        """
        with self._session() as session:
            row = session.get(ExperimentRow, experiment_id)
            if row is None:
                raise KeyError(f"unknown experiment {experiment_id!r}")
            return self._to_dataclass(row)

    def create(
        self,
        name: str,
        config: BacktestConfig,
        *,
        hypothesis_id: str | None = None,
        kind: ExperimentKind = ExperimentKind.COMBINED,
        criteria: AcceptanceCriteria | None = None,
        horizon: int = 21,
        owner: str | None = None,
        notes: str | None = None,
        params: dict[str, Any] | None = None,
        experiment_id: str | None = None,
    ) -> Experiment:
        """Create an experiment definition.

        Raises:
            ValueError: when the name is empty or the configuration names
                neither symbols nor a universe.
            KeyError: when ``hypothesis_id`` does not exist.
        """
        label = str(name).strip()
        if not label:
            raise ValueError("an experiment needs a name")
        if not config.symbols and not config.universe_id and not config.signal.symbols:
            raise ValueError("an experiment needs either explicit symbols or a universe")
        if hypothesis_id:
            from tradinglib.research.hypotheses import HypothesisRegistry

            registry = HypothesisRegistry(self._session_factory, bus=self._bus)
            if not registry.exists(hypothesis_id):
                raise KeyError(f"unknown hypothesis {hypothesis_id!r}")
        now = utcnow()
        identifier = experiment_id or new_experiment_id()
        with self._session() as session:
            if session.get(ExperimentRow, identifier) is not None:
                raise ValueError(f"experiment {identifier!r} already exists")
            row = ExperimentRow(
                id=identifier,
                name=label,
                hypothesis_id=hypothesis_id,
                kind=str(kind),
                stage=str(ExperimentStage.RESEARCH),
                universe_id=config.universe_id,
                config=config.as_dict(),
                criteria=(criteria or AcceptanceCriteria()).as_dict(),
                horizon=int(horizon),
                owner=owner,
                notes=notes,
                params=dict(params or {}),
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            return self._to_dataclass(row)

    def update(self, experiment_id: str, **changes: Any) -> Experiment:
        """Update an experiment definition.

        Editable: ``name``, ``hypothesis_id``, ``kind``, ``stage``, ``config``
        (a :class:`BacktestConfig` or its mapping), ``criteria``, ``horizon``,
        ``owner``, ``notes``, ``params``.

        Raises:
            KeyError: when the identifier is unknown.
            ValueError: on an unknown field.
        """
        editable = {
            "name",
            "hypothesis_id",
            "kind",
            "stage",
            "config",
            "criteria",
            "horizon",
            "owner",
            "notes",
            "params",
        }
        unknown = set(changes) - editable
        if unknown:
            raise ValueError(f"cannot update {', '.join(sorted(unknown))}")
        with self._session() as session:
            row = session.get(ExperimentRow, experiment_id)
            if row is None:
                raise KeyError(f"unknown experiment {experiment_id!r}")
            if "name" in changes:
                label = str(changes["name"]).strip()
                if not label:
                    raise ValueError("an experiment needs a name")
                row.name = label
            if "hypothesis_id" in changes:
                row.hypothesis_id = changes["hypothesis_id"] or None
            if "kind" in changes:
                row.kind = str(ExperimentKind(str(changes["kind"])))
            if "stage" in changes:
                row.stage = str(ExperimentStage(str(changes["stage"])))
            if "config" in changes:
                value = changes["config"]
                config = value if isinstance(value, BacktestConfig) else BacktestConfig.from_dict(value)
                row.config = config.as_dict()
                row.universe_id = config.universe_id
            if "criteria" in changes:
                value = changes["criteria"]
                row.criteria = (
                    value.as_dict()
                    if isinstance(value, AcceptanceCriteria)
                    else AcceptanceCriteria.from_dict(value).as_dict()
                )
            if "horizon" in changes:
                row.horizon = int(changes["horizon"])
            if "owner" in changes:
                row.owner = changes["owner"] or None
            if "notes" in changes:
                row.notes = changes["notes"] or None
            if "params" in changes:
                row.params = dict(changes["params"] or {})
            row.updated_at = utcnow()
            return self._to_dataclass(row)

    def delete(self, experiment_id: str) -> None:
        """Delete an experiment and its runs.

        Raises:
            KeyError: when the identifier is unknown.
        """
        with self._session() as session:
            row = session.get(ExperimentRow, experiment_id)
            if row is None:
                raise KeyError(f"unknown experiment {experiment_id!r}")
            session.execute(
                delete(ExperimentRunRow).where(ExperimentRunRow.experiment_id == experiment_id)
            )
            session.delete(row)

    def set_stage(self, experiment_id: str, stage: ExperimentStage) -> Experiment:
        """Move an experiment through the research funnel."""
        return self.update(experiment_id, stage=stage)

    # --- runs -------------------------------------------------------------
    def runs(self, experiment_id: str, *, limit: int = 50) -> list[ExperimentRun]:
        """Return recent runs of an experiment, newest first."""
        statement = (
            select(ExperimentRunRow)
            .where(ExperimentRunRow.experiment_id == experiment_id)
            .order_by(ExperimentRunRow.created_at.desc())
            .limit(limit)
        )
        with self._session() as session:
            return [self._run_to_dataclass(row) for row in session.scalars(statement)]

    def all_runs(
        self, *, hypothesis_id: str | None = None, limit: int = 200
    ) -> list[ExperimentRun]:
        """Return runs across every experiment, newest first."""
        statement = (
            select(ExperimentRunRow).order_by(ExperimentRunRow.created_at.desc()).limit(limit)
        )
        if hypothesis_id:
            statement = statement.where(ExperimentRunRow.hypothesis_id == hypothesis_id)
        with self._session() as session:
            return [self._run_to_dataclass(row) for row in session.scalars(statement)]

    def get_run(self, run_id: str) -> ExperimentRun:
        """Return one run.

        Raises:
            KeyError: when the identifier is unknown.
        """
        with self._session() as session:
            row = session.get(ExperimentRunRow, run_id)
            if row is None:
                raise KeyError(f"unknown experiment run {run_id!r}")
            return self._run_to_dataclass(row)

    def run_log(self, run_id: str) -> list[dict[str, Any]]:
        """Return the ordered log lines of one run."""
        return self.get_run(run_id).log

    def count_runs(self, *, hypothesis_id: str | None = None) -> int:
        """Return how many runs are recorded (the trial ledger's experiment half)."""
        statement = select(ExperimentRunRow.id)
        if hypothesis_id:
            statement = statement.where(ExperimentRunRow.hypothesis_id == hypothesis_id)
        with self._session() as session:
            return len(list(session.scalars(statement)))

    # --- execution --------------------------------------------------------
    async def run(self, experiment_id: str, *, publish: bool = True) -> ExperimentRun:
        """Execute an experiment, publishing progress on the ``lab`` channel.

        The run is persisted before, during and after execution, so a crash
        leaves a ``running`` row with its log rather than nothing at all.

        Returns:
            The completed :class:`ExperimentRun`. A failed run is returned with
            status ``failed`` and the error message - it is recorded, not
            hidden - and the exception is not re-raised, because a failed
            experiment is a legitimate research outcome.
        """
        experiment = self.get(experiment_id)
        if experiment.config is None:  # pragma: no cover - config is non-null in the schema
            raise ValueError(f"experiment {experiment_id!r} has no configuration")
        run_id = new_run_id()
        started = utcnow()
        log: list[dict[str, Any]] = []

        async def note(message: str, level: str = "info", **extra: Any) -> None:
            """Append a log line, persist it and stream it to the browser."""
            entry = {"ts": utcnow().isoformat(), "level": level, "message": message, **extra}
            log.append(entry)
            self._append_log(run_id, entry)
            if publish:
                await self.bus.publish(
                    LAB_CHANNEL,
                    {
                        "kind": "experiment",
                        "phase": "log",
                        "id": run_id,
                        "experiment_id": experiment_id,
                        **entry,
                    },
                )

        self._insert_run(run_id, experiment, started)
        if publish:
            await self.bus.publish(
                LAB_CHANNEL,
                {
                    "kind": "experiment",
                    "phase": "started",
                    "id": run_id,
                    "experiment_id": experiment_id,
                    "hypothesis_id": experiment.hypothesis_id,
                    "name": experiment.name,
                    "ts": started.isoformat(),
                },
            )

        try:
            await note(f"experiment {experiment.name} started ({experiment.kind})")
            config = experiment.config
            await note(f"signal: {config.signal.describe()}")
            symbols = await asyncio.to_thread(self.backtests.resolve_symbols, config)
            await note(f"universe resolved to {len(symbols)} symbols", symbols=len(symbols))

            prices, volumes, benchmark, warnings = await asyncio.to_thread(
                self.backtests.prepare, config
            )
            for warning in warnings:
                await note(warning, level="warn")
            await note(
                f"loaded {prices.shape[0]} bars for {prices.shape[1]} symbols "
                f"({prices.index[0].date()} → {prices.index[-1].date()})"
            )

            results: dict[str, Any] = {"warnings": list(warnings)}
            signals = await asyncio.to_thread(
                self.backtests.backtester.build_signals, config.signal, prices, volumes=volumes
            )

            if experiment.kind in (ExperimentKind.SIGNAL, ExperimentKind.COMBINED):
                await note(f"computing information coefficient at {experiment.horizon}-bar horizon")
                ic = await asyncio.to_thread(
                    information_coefficient, signals, prices, horizon=experiment.horizon
                )
                results["ic"] = ic
                if ic["observations"]:
                    await note(
                        f"IC mean {ic['mean_ic']:+.4f} over {ic['observations']} "
                        f"non-overlapping windows (t={ic['t_stat']:.2f})"
                        if ic["t_stat"] is not None
                        else f"IC mean {ic['mean_ic']:+.4f} over {ic['observations']} windows"
                    )
                else:
                    await note(str(ic.get("note")), level="warn")

            backtest_id: str | None = None
            if experiment.kind in (ExperimentKind.BACKTEST, ExperimentKind.COMBINED):
                await note("running backtest")
                record, result = await self.backtests.run_with_result(
                    config,
                    hypothesis_id=experiment.hypothesis_id,
                    experiment_id=experiment_id,
                    source="experiment",
                    publish=publish,
                )
                backtest_id = record.id
                results["backtest_id"] = record.id
                results["stats"] = record.stats
                results["in_sample"] = record.in_sample
                results["out_of_sample"] = record.out_of_sample
                results["symbols"] = record.symbols
                results["warnings"] = list(warnings) + list(record.warnings)
                passed, checks = evaluate_acceptance(result, experiment.criteria)
                results["passed_gates"] = passed
                results["checks"] = checks
                results["criteria"] = experiment.criteria.as_dict()
                for check in checks:
                    await note(
                        f"{'PASS' if check['passed'] else 'FAIL'} {check['label']} "
                        f"(observed {check['observed']:.4g})",
                        level="info" if check["passed"] else "warn",
                    )
                await note(
                    "acceptance criteria "
                    + ("passed" if passed else "not met - experiment stays in research")
                )

            finished = utcnow()
            duration = (finished - started).total_seconds() * 1000.0
            self._finish_run(
                run_id,
                status=ExperimentStatus.COMPLETE,
                finished_at=finished,
                duration_ms=duration,
                results=results,
                backtest_id=backtest_id,
            )
            await note(f"experiment finished in {duration / 1000.0:.2f}s")
            if publish:
                await self.bus.publish(
                    LAB_CHANNEL,
                    {
                        "kind": "experiment",
                        "phase": "complete",
                        "id": run_id,
                        "experiment_id": experiment_id,
                        "hypothesis_id": experiment.hypothesis_id,
                        "backtest_id": backtest_id,
                        "passed_gates": results.get("passed_gates"),
                        "stats": results.get("stats"),
                        "ic": results.get("ic", {}).get("mean_ic") if results.get("ic") else None,
                        "duration_ms": duration,
                        "ts": finished.isoformat(),
                    },
                )
            return self.get_run(run_id)
        except Exception as exc:  # noqa: BLE001 - a failed experiment is a result
            message = f"{type(exc).__name__}: {exc}"
            logger.warning("experiment run %s failed: %s", run_id, message)
            finished = utcnow()
            await note(message, level="error")
            self._finish_run(
                run_id,
                status=ExperimentStatus.FAILED,
                finished_at=finished,
                duration_ms=(finished - started).total_seconds() * 1000.0,
                results={"error": message},
                error=message,
            )
            if publish:
                await self.bus.publish(
                    LAB_CHANNEL,
                    {
                        "kind": "experiment",
                        "phase": "failed",
                        "id": run_id,
                        "experiment_id": experiment_id,
                        "error": message,
                        "ts": finished.isoformat(),
                    },
                )
            return self.get_run(run_id)

    # --- run persistence --------------------------------------------------
    def _insert_run(self, run_id: str, experiment: Experiment, started: datetime) -> None:
        """Insert the row for a run that is starting."""
        with self._session() as session:
            session.add(
                ExperimentRunRow(
                    id=run_id,
                    experiment_id=experiment.id,
                    hypothesis_id=experiment.hypothesis_id,
                    status=str(ExperimentStatus.RUNNING),
                    started_at=started,
                    created_at=started,
                    log=[],
                    results={},
                )
            )

    def _append_log(self, run_id: str, entry: dict[str, Any]) -> None:
        """Append one line to a run's persisted log."""
        with self._session() as session:
            row = session.get(ExperimentRunRow, run_id)
            if row is None:  # pragma: no cover - the row is created first
                return
            row.log = list(row.log or []) + [entry]

    def _finish_run(
        self,
        run_id: str,
        *,
        status: ExperimentStatus,
        finished_at: datetime,
        duration_ms: float,
        results: dict[str, Any],
        backtest_id: str | None = None,
        error: str | None = None,
    ) -> None:
        """Write the terminal state of a run."""
        with self._session() as session:
            row = session.get(ExperimentRunRow, run_id)
            if row is None:  # pragma: no cover - the row is created first
                return
            row.status = str(status)
            row.finished_at = finished_at
            row.duration_ms = duration_ms
            row.results = results
            row.backtest_id = backtest_id
            row.error = error


def new_experiment_id() -> str:
    """Return a fresh experiment identifier (``exp-`` + 10 hex characters)."""
    return f"exp-{uuid.uuid4().hex[:10]}"


def new_run_id() -> str:
    """Return a fresh experiment-run identifier (``xr-`` + 12 hex characters)."""
    return f"xr-{uuid.uuid4().hex[:12]}"


def experiment_config(
    name: str,
    *,
    universe_id: str | None = None,
    symbols: Sequence[str] | None = None,
    start: date | None = None,
    end: date | None = None,
    **kwargs: Any,
) -> BacktestConfig:
    """Build a :class:`BacktestConfig` for an experiment from plain arguments.

    A thin convenience so the API layer can construct a configuration from a
    form payload without importing the backtest module directly.
    """
    return BacktestConfig(
        name=name,
        universe_id=universe_id,
        symbols=[s.upper() for s in symbols or []],
        start=start,
        end=end,
        **kwargs,
    )


_engine: ExperimentEngine | None = None


def get_experiment_engine() -> ExperimentEngine:
    """Return the process-wide experiment engine, creating it on first use."""
    global _engine
    if _engine is None:
        _engine = ExperimentEngine()
    return _engine


def reset_experiment_engine(engine: ExperimentEngine | None = None) -> ExperimentEngine:
    """Replace the process-wide engine. Intended for tests."""
    global _engine
    _engine = engine if engine is not None else ExperimentEngine()
    return _engine


__all__ = [
    "DEFAULT_ACCEPTANCE",
    "AcceptanceCriteria",
    "Experiment",
    "ExperimentEngine",
    "ExperimentKind",
    "ExperimentRow",
    "ExperimentRun",
    "ExperimentRunRow",
    "ExperimentStage",
    "ExperimentStatus",
    "ensure_schema",
    "evaluate_acceptance",
    "experiment_config",
    "get_experiment_engine",
    "information_coefficient",
    "new_experiment_id",
    "new_run_id",
    "reset_experiment_engine",
]
