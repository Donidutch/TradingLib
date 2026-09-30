"""Attached analyses for a backtest: parameter sweeps and walk-forward folds.

A single backtest is one observation. The Strategy Lab's experiment detail
therefore offers to *attach* an analysis that turns it into a distribution:

* **Sweep** - re-runs the configuration across a grid of parameter values and
  reports the metric surface. Every cell is a trial, which is why the validation
  workspace's trial ledger counts sweep cells alongside manual runs and agent
  cycles: they are exactly the multiple comparisons a deflated Sharpe has to
  correct for.
* **Walk-forward** - splits the return history into consecutive train/test
  folds, picks the best parameter set on each *train* window only, and reports
  what it earned on the *test* window that follows. With no grid it degenerates
  into a rolling out-of-sample stability check of one configuration.
* **Monte Carlo** - block-bootstraps the realised return series into thousands
  of alternative equity paths. Short-horizon autocorrelation survives the
  resample; only the *order* the returns arrived in is destroyed, which is
  exactly the sequencing luck a single equity curve hides. Its resamples are
  deliberately NOT counted as trials: five thousand reorderings of one
  configuration are one comparison, not five thousand.

Both engines evaluate a parameter set by running the vectorised backtester once
over the whole price panel and then slicing the resulting return series. That is
sound because the backtester's timing convention guarantees a signal at bar *i*
uses only information available at the close of bar *i* (see the module
docstring of :mod:`tradinglib.research.backtest`), so a fold's statistics are not
contaminated by data outside it. It is also far cheaper than re-loading and
re-running the panel per fold.

Nothing here imports from ``tradinglib.api``; progress is published on the ``lab``
event channel and the REST layer fans it out.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import math
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

import numpy as np
import pandas as pd
from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    String,
    Text,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from tradinglib.data.base import utcnow
from tradinglib.db import Base, ensure_columns, get_engine, get_session_factory
from tradinglib.events import EventBus, get_event_bus
from tradinglib.research.backtest import (
    LAB_CHANNEL,
    TRADING_DAYS_PER_YEAR,
    BacktestConfig,
    BacktestEngine,
    BacktestResult,
    BacktestStats,
    compute_stats,
    get_backtest_engine,
)

logger = logging.getLogger(__name__)

#: Hard ceiling on grid size. A sweep is a multiple-comparison machine; letting
#: it run unbounded would silently destroy any deflated-Sharpe correction.
MAX_SWEEP_CELLS: Final[int] = 240

#: Fewest return observations a fold window must hold to be scored at all.
MIN_FOLD_OBSERVATIONS: Final[int] = 20


#: Default number of block-bootstrap resamples a Monte-Carlo analysis draws.
DEFAULT_MC_PATHS: Final[int] = 5000

#: Default number of individual resampled paths returned for drawing.
DEFAULT_MC_SHOWN: Final[int] = 200

#: Ceiling on resamples, so one request cannot occupy a worker indefinitely.
MAX_MC_PATHS: Final[int] = 20_000

#: Time-axis resolution of a returned equity cone.
MC_CURVE_POINTS: Final[int] = 240


class AnalysisKind(StrEnum):
    """The analysis types that can be attached to an experiment or backtest."""

    SWEEP = "sweep"
    WALK_FORWARD = "walk_forward"
    MONTE_CARLO = "monte_carlo"


class AnalysisStatus(StrEnum):
    """Lifecycle of an attached analysis."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"


#: Config paths a sweep axis may vary, mapped to their coercion function.
#: Anything else is rejected: a sweep must not be able to change the *data* it
#: is measured on, or the grid's cells would not be comparable.
SWEEPABLE: Final[dict[str, Any]] = {
    "signal.lookback": int,
    "signal.skip": int,
    "signal.fast": int,
    "signal.slow": int,
    "signal.top_n": int,
    "signal.min_signal": float,
    "signal.weighting": str,
    "signal.kind": str,
    "signal.expression": str,
    "signal.filter_expression": str,
    "rebalance": str,
    "gross_exposure": float,
    "max_positions": int,
    "long_only": bool,
    "costs.commission_bps": float,
    "costs.slippage_bps": float,
    "costs.spread_bps": float,
    "costs.borrow_bps": float,
    "risk_free_rate": float,
}

#: Metrics a sweep or walk-forward may optimise, and whether more is better.
METRICS: Final[dict[str, bool]] = {
    "sharpe": True,
    "sortino": True,
    "cagr": True,
    "total_return": True,
    "information_ratio": True,
    "hit_rate": True,
    "volatility": False,
    "max_drawdown": True,
    "turnover": False,
}


# --------------------------------------------------------------------------
# Monte-Carlo resampling
# --------------------------------------------------------------------------
def _iso_label(value: Any) -> str:
    """Render an index label as an ISO-8601 string."""
    formatter = getattr(value, "isoformat", None)
    return formatter() if callable(formatter) else str(value)


def _sample_indices(count: int, max_points: int) -> list[int]:
    """Return up to ``max_points`` evenly spaced offsets, keeping the last one."""
    if count <= max_points:
        return list(range(count))
    step = int(math.ceil(count / max_points))
    picked = list(range(0, count, step))
    if picked[-1] != count - 1:
        picked.append(count - 1)
    return picked


def _block_sample(
    values: np.ndarray, rng: np.random.Generator, block: int, count: int, paths: int
) -> np.ndarray:
    """Draw ``paths`` circular-block bootstrap resamples of ``values``."""
    n = values.size
    blocks = max(1, int(math.ceil(count / block)))
    starts = rng.integers(0, n, size=(paths, blocks))
    offsets = np.arange(block)
    picks = (starts[:, :, None] + offsets[None, None, :]) % n
    return values[picks.reshape(paths, blocks * block)][:, :count]


def monte_carlo_paths(
    returns: pd.Series,
    *,
    paths: int = 5000,
    block: int | None = None,
    seed: int = 20260828,
    shown: int = 200,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
    initial_capital: float = 1.0,
    max_points: int = 240,
    chunk: int = 500,
) -> dict[str, Any]:
    """Block-bootstrap the return series into an equity-path distribution.

    Circular blocks of consecutive returns are resampled with replacement so
    short-horizon autocorrelation survives; sequencing - the order the same
    returns arrive in - is what the resample destroys, which is precisely the
    luck the fan chart is meant to expose.

    Args:
        returns: net-of-cost period returns of the realised run.
        paths: how many resamples to calculate.
        block: block length in periods; defaults to ``ceil(sqrt(n))`` bounded
            to ``[5, 63]``, the usual stationary-bootstrap rule of thumb.
        seed: RNG seed, so the same run always yields the same cone.
        shown: how many individual paths to return for drawing.
        periods_per_year: annualisation factor for the Sharpe distribution.
        initial_capital: equity curves start here.
        max_points: time-axis resolution of the returned curves.
        chunk: resamples computed per batch (bounds peak memory).

    Returns:
        A JSON-serialisable payload with the percentile cone, the drawn paths,
        the realised path and the Sharpe/drawdown/terminal distributions.
    """
    series = returns.astype("float64").dropna()
    count = int(series.size)
    width = int(block or min(63, max(5, int(math.ceil(math.sqrt(max(count, 1)))))))
    if count < max(8, width):
        return {
            "paths": 0,
            "note": f"need at least {max(8, width)} return observations to resample",
        }

    values = series.to_numpy()
    rng = np.random.default_rng(seed)
    keep = _sample_indices(count, max_points)
    keep_array = np.asarray(keep, dtype="int64")
    dates = [_iso_label(series.index[i]) for i in keep]

    curves = np.empty((paths, keep_array.size), dtype="float32")
    sharpes = np.empty(paths, dtype="float64")
    drawdowns = np.empty(paths, dtype="float64")
    terminals = np.empty(paths, dtype="float64")

    done = 0
    while done < paths:
        size = min(chunk, paths - done)
        sample = _block_sample(values, rng, width, count, size)
        equity = np.cumprod(1.0 + sample, axis=1)
        peak = np.maximum.accumulate(equity, axis=1)
        drawdowns[done : done + size] = (equity / peak - 1.0).min(axis=1)
        terminals[done : done + size] = equity[:, -1] - 1.0
        std = sample.std(axis=1, ddof=1)
        mean = sample.mean(axis=1)
        with np.errstate(divide="ignore", invalid="ignore"):
            ratio = np.where(std > 0, mean / std * math.sqrt(periods_per_year), 0.0)
        sharpes[done : done + size] = ratio
        curves[done : done + size] = (initial_capital * equity[:, keep_array]).astype("float32")
        done += size

    realised_equity = initial_capital * np.cumprod(1.0 + values)
    realised_total = float(realised_equity[-1] / initial_capital - 1.0)
    realised_peak = np.maximum.accumulate(realised_equity)
    realised_dd = float((realised_equity / realised_peak - 1.0).min())
    realised_std = float(values.std(ddof=1))
    realised_sharpe = (
        float(values.mean() / realised_std * math.sqrt(periods_per_year))
        if realised_std > 0
        else 0.0
    )

    # Equity levels are currency amounts, so two decimals is already finer than
    # any chart can draw. Rounding here keeps a 200-path cone from becoming a
    # megabyte of 17-significant-digit floats in the analysis row.
    def band(percentile: float) -> list[float]:
        return [round(float(v), 2) for v in np.percentile(curves, percentile, axis=0)]

    drawn = min(int(shown), paths)
    return {
        "paths": int(paths),
        "shown": drawn,
        "block": width,
        "seed": int(seed),
        "observations": count,
        "periods_per_year": int(periods_per_year),
        "initial_capital": float(initial_capital),
        "dates": dates,
        "bands": {
            "p5": band(5),
            "p25": band(25),
            "p50": band(50),
            "p75": band(75),
            "p95": band(95),
        },
        "curves": [[round(float(v), 2) for v in row] for row in curves[:drawn]],
        "realised": [round(float(v), 2) for v in realised_equity[keep_array]],
        "sharpe": {
            "realised": realised_sharpe,
            "median": float(np.median(sharpes)),
            "p05": float(np.percentile(sharpes, 5)),
            "p95": float(np.percentile(sharpes, 95)),
            "prob_positive": float((sharpes > 0).mean()),
            "prob_above_one": float((sharpes > 1.0).mean()),
        },
        "drawdown": {
            "realised": realised_dd,
            "median": float(np.median(drawdowns)),
            "p95": float(np.percentile(drawdowns, 5)),
            "prob_worse_than_20pct": float((drawdowns < -0.20).mean()),
        },
        "terminal": {
            "realised": realised_total,
            "p05": float(np.percentile(terminals, 5)),
            "p50": float(np.percentile(terminals, 50)),
            "p95": float(np.percentile(terminals, 95)),
            "realised_percentile": float((terminals < realised_total).mean() * 100.0),
        },
    }


# --------------------------------------------------------------------------
# specs
# --------------------------------------------------------------------------
@dataclass(slots=True)
class ParameterAxis:
    """One dimension of a sweep grid.

    Attributes:
        path: dotted path into :meth:`BacktestConfig.as_dict`, restricted to
            :data:`SWEEPABLE`.
        values: the values to try along this axis.
    """

    path: str
    values: list[Any]

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping."""
        return {"path": self.path, "values": list(self.values)}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ParameterAxis:
        """Rebuild an axis, validating its path and coercing its values."""
        path = str(payload.get("path") or "").strip()
        coerce = SWEEPABLE.get(path)
        if coerce is None:
            raise ValueError(
                f"{path!r} is not a sweepable parameter; allowed: "
                + ", ".join(sorted(SWEEPABLE))
            )
        raw = payload.get("values")
        if not isinstance(raw, (list, tuple)) or not raw:
            raise ValueError(f"axis {path!r} needs a non-empty list of values")
        values: list[Any] = []
        for item in raw:
            if item is None:
                values.append(None)
                continue
            try:
                values.append(_coerce(coerce, item))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"axis {path!r}: {item!r} is not usable ({exc})") from exc
        deduped: list[Any] = []
        for value in values:
            if value not in deduped:
                deduped.append(value)
        return cls(path=path, values=deduped)


def _coerce(caster: Any, value: Any) -> Any:
    """Apply an axis' coercion function, treating ``bool`` specially."""
    if caster is bool:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
    return caster(value)


@dataclass(slots=True)
class SweepSpec:
    """A parameter sweep: a grid of configurations and the metric to rank them."""

    axes: list[ParameterAxis] = field(default_factory=list)
    metric: str = "sharpe"
    window: str = "full"
    name: str | None = None

    def cells(self) -> list[dict[str, Any]]:
        """Return every point of the cartesian product, as path -> value maps."""
        if not self.axes:
            return []
        products = itertools.product(*[axis.values for axis in self.axes])
        return [
            {axis.path: value for axis, value in zip(self.axes, combo, strict=True)}
            for combo in products
        ]

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping."""
        return {
            "axes": [axis.as_dict() for axis in self.axes],
            "metric": self.metric,
            "window": self.window,
            "name": self.name,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> SweepSpec:
        """Rebuild and validate a sweep spec from a request body."""
        axes = [ParameterAxis.from_dict(item) for item in payload.get("axes") or []]
        if not axes:
            raise ValueError("a sweep needs at least one axis")
        metric = str(payload.get("metric") or "sharpe")
        if metric not in METRICS:
            raise ValueError(f"unknown metric {metric!r}; allowed: {', '.join(sorted(METRICS))}")
        window = str(payload.get("window") or "full")
        if window not in {"full", "in_sample", "out_of_sample"}:
            raise ValueError("window must be one of: full, in_sample, out_of_sample")
        total = math.prod(len(axis.values) for axis in axes)
        if total > MAX_SWEEP_CELLS:
            raise ValueError(
                f"the grid has {total} cells; the ceiling is {MAX_SWEEP_CELLS} because every "
                "cell is a trial the deflated Sharpe has to correct for"
            )
        return cls(
            axes=axes,
            metric=metric,
            window=window,
            name=(str(payload["name"]).strip() or None) if payload.get("name") else None,
        )


@dataclass(slots=True)
class WalkForwardSpec:
    """A walk-forward analysis: consecutive train/test folds over the history."""

    folds: int = 5
    train_fraction: float = 0.6
    anchored: bool = True
    metric: str = "sharpe"
    axes: list[ParameterAxis] = field(default_factory=list)
    name: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping."""
        return {
            "folds": self.folds,
            "train_fraction": self.train_fraction,
            "anchored": self.anchored,
            "metric": self.metric,
            "axes": [axis.as_dict() for axis in self.axes],
            "name": self.name,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> WalkForwardSpec:
        """Rebuild and validate a walk-forward spec from a request body."""
        folds = int(payload.get("folds") or 5)
        if not 2 <= folds <= 12:
            raise ValueError("folds must be between 2 and 12")
        fraction = float(payload.get("train_fraction") or 0.6)
        if not 0.2 <= fraction <= 0.9:
            raise ValueError("train_fraction must be between 0.2 and 0.9")
        metric = str(payload.get("metric") or "sharpe")
        if metric not in METRICS:
            raise ValueError(f"unknown metric {metric!r}; allowed: {', '.join(sorted(METRICS))}")
        axes = [ParameterAxis.from_dict(item) for item in payload.get("axes") or []]
        grid = math.prod(len(axis.values) for axis in axes) if axes else 1
        if grid > MAX_SWEEP_CELLS:
            raise ValueError(f"the per-fold grid has {grid} cells; the ceiling is {MAX_SWEEP_CELLS}")
        return cls(
            folds=folds,
            train_fraction=fraction,
            anchored=bool(payload.get("anchored", True)),
            metric=metric,
            axes=axes,
            name=(str(payload["name"]).strip() or None) if payload.get("name") else None,
        )


# --------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------
class AnalysisRow(Base):
    """One attached analysis (sweep or walk-forward) and its results."""

    __tablename__ = "research_analyses"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=AnalysisStatus.QUEUED, nullable=False)
    experiment_id: Mapped[str | None] = mapped_column(String(64), default=None, index=True)
    hypothesis_id: Mapped[str | None] = mapped_column(String(64), default=None, index=True)
    backtest_id: Mapped[str | None] = mapped_column(String(64), default=None, index=True)
    spec: Mapped[dict[str, Any]] = mapped_column(JSON(), nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSON(), nullable=False)
    summary: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict, nullable=False)
    cells: Mapped[list[dict[str, Any]]] = mapped_column(JSON(), default=list, nullable=False)
    cell_count: Mapped[int] = mapped_column(Float(), default=0.0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(), default=None)
    duration_ms: Mapped[float | None] = mapped_column(Float(), default=None)
    warnings: Mapped[list[str]] = mapped_column(JSON(), default=list, nullable=False)
    error: Mapped[str | None] = mapped_column(Text(), default=None)


_OWNED_TABLES = (AnalysisRow.__table__,)


def ensure_schema() -> None:
    """Create (and migrate) the analyses table."""
    Base.metadata.create_all(get_engine(), tables=list(_OWNED_TABLES))
    for table in _OWNED_TABLES:
        ensure_columns(table)


@dataclass(slots=True)
class AnalysisRecord:
    """A persisted analysis as the API and the Strategy Lab see it."""

    id: str
    kind: AnalysisKind
    name: str
    status: AnalysisStatus
    spec: dict[str, Any]
    config: dict[str, Any]
    created_at: datetime
    experiment_id: str | None = None
    hypothesis_id: str | None = None
    backtest_id: str | None = None
    summary: dict[str, Any] = field(default_factory=dict)
    cells: list[dict[str, Any]] = field(default_factory=list)
    cell_count: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: float | None = None
    warnings: list[str] = field(default_factory=list)
    error: str | None = None

    def chip(self) -> str:
        """Return the short label the *attached analyses* chip row renders."""
        if self.status is not AnalysisStatus.COMPLETE:
            return f"{self.kind} · {self.status}"
        if self.kind is AnalysisKind.SWEEP:
            best = self.summary.get("best_metric")
            metric = self.summary.get("metric", "sharpe")
            shown = "—" if best is None else f"{float(best):.2f}"
            return f"{self.cell_count} cells · best {metric} {shown}"
        if self.kind is AnalysisKind.MONTE_CARLO:
            paths = int(self.summary.get("paths") or self.cell_count)
            percentile = self.summary.get("realised_percentile")
            where = "—" if percentile is None else f"p{float(percentile):.0f}"
            return f"{paths:,} paths · realised {where}"
        folds = self.summary.get("folds", self.cell_count)
        mean = self.summary.get("mean_test_metric")
        shown = "—" if mean is None else f"{float(mean):.2f}"
        return f"{folds} folds · mean OOS {self.summary.get('metric', 'sharpe')} {shown}"

    def tone(self) -> str:
        """Return the chip's colour class, matching the Lab's chip vocabulary."""
        if self.status is AnalysisStatus.FAILED:
            return "red"
        if self.status is not AnalysisStatus.COMPLETE:
            return "yel"
        if self.kind is AnalysisKind.SWEEP:
            robust = self.summary.get("robust")
            return "grn" if robust else "yel"
        if self.kind is AnalysisKind.MONTE_CARLO:
            # Green only when the edge survives the resample *and* the realised
            # path was not unusually lucky.
            positive = float(self.summary.get("prob_positive") or 0.0)
            lucky = bool(self.summary.get("favourable_sequencing"))
            if positive >= 0.9 and not lucky:
                return "grn"
            return "red" if positive < 0.6 else "yel"
        efficiency = self.summary.get("efficiency")
        if efficiency is None:
            return "yel"
        return "grn" if float(efficiency) >= 0.5 else "red"

    def as_dict(self, *, include_cells: bool = True) -> dict[str, Any]:
        """Return a JSON-serialisable mapping for the API layer."""
        payload: dict[str, Any] = {
            "id": self.id,
            "kind": str(self.kind),
            "name": self.name,
            "status": str(self.status),
            "experiment_id": self.experiment_id,
            "hypothesis_id": self.hypothesis_id,
            "backtest_id": self.backtest_id,
            "spec": dict(self.spec),
            "config": dict(self.config),
            "summary": dict(self.summary),
            "cell_count": int(self.cell_count),
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "duration_ms": self.duration_ms,
            "warnings": list(self.warnings),
            "error": self.error,
            "chip": self.chip(),
            "tone": self.tone(),
        }
        if include_cells:
            payload["cells"] = list(self.cells)
        return payload


def _record_from_row(row: AnalysisRow) -> AnalysisRecord:
    """Map an ORM row onto the dataclass the API serves."""
    return AnalysisRecord(
        id=row.id,
        kind=AnalysisKind(row.kind),
        name=row.name,
        status=AnalysisStatus(row.status),
        spec=dict(row.spec or {}),
        config=dict(row.config or {}),
        created_at=row.created_at,
        experiment_id=row.experiment_id,
        hypothesis_id=row.hypothesis_id,
        backtest_id=row.backtest_id,
        summary=dict(row.summary or {}),
        cells=list(row.cells or []),
        cell_count=int(row.cell_count or 0),
        started_at=row.started_at,
        finished_at=row.finished_at,
        duration_ms=row.duration_ms,
        warnings=list(row.warnings or []),
        error=row.error,
    )


#: Identifier prefix per analysis kind, so an id says what it is.
_ID_PREFIX: Final[dict[AnalysisKind, str]] = {
    AnalysisKind.SWEEP: "sw",
    AnalysisKind.WALK_FORWARD: "wf",
    AnalysisKind.MONTE_CARLO: "mc",
}


def new_analysis_id(kind: AnalysisKind) -> str:
    """Return a fresh analysis identifier (``sw-``/``wf-``/``mc-`` + 10 hex chars)."""
    return f"{_ID_PREFIX[kind]}-{uuid.uuid4().hex[:10]}"


# --------------------------------------------------------------------------
# config surgery
# --------------------------------------------------------------------------
def apply_overrides(config: BacktestConfig, overrides: dict[str, Any]) -> BacktestConfig:
    """Return a copy of ``config`` with the dotted ``overrides`` applied.

    Raises:
        ValueError: when a path is not in :data:`SWEEPABLE` or the value cannot
            be coerced to the field's type.
    """
    payload = config.as_dict()
    for path, value in overrides.items():
        if path not in SWEEPABLE:
            raise ValueError(f"{path!r} is not a sweepable parameter")
        target = payload
        parts = path.split(".")
        for part in parts[:-1]:
            nested = target.get(part)
            if not isinstance(nested, dict):
                raise ValueError(f"{path!r} does not address a configuration section")
            target = nested
        target[parts[-1]] = value
    return BacktestConfig.from_dict(payload)


def describe_cell(overrides: dict[str, Any]) -> str:
    """Render a cell's parameters the way the sweep table labels them."""
    return " · ".join(
        f"{path.split('.')[-1]}={'—' if value is None else value}"
        for path, value in sorted(overrides.items())
    )


def metric_value(stats: BacktestStats | dict[str, Any] | None, metric: str) -> float | None:
    """Extract one metric from a stats object/mapping, or ``None`` when absent."""
    if stats is None:
        return None
    source = stats.as_dict() if isinstance(stats, BacktestStats) else stats
    value = source.get(metric)
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) or math.isinf(number) else number


def _better(metric: str, candidate: float, incumbent: float) -> bool:
    """Return whether ``candidate`` beats ``incumbent`` for ``metric``."""
    return candidate > incumbent if METRICS.get(metric, True) else candidate < incumbent


# --------------------------------------------------------------------------
# the engine
# --------------------------------------------------------------------------
class AnalysisEngine:
    """Runs sweeps and walk-forward analyses, persists them, publishes progress.

    Args:
        backtests: the backtest engine used to load prices and evaluate cells.
        session_factory: SQLAlchemy session factory (defaults to the app's).
        bus: event bus progress is published on (channel ``lab``).
    """

    def __init__(
        self,
        *,
        backtests: BacktestEngine | None = None,
        session_factory: sessionmaker[Session] | None = None,
        bus: EventBus | None = None,
    ) -> None:
        self._backtests = backtests
        self._sessions = session_factory
        self._bus = bus
        ensure_schema()

    @property
    def backtests(self) -> BacktestEngine:
        """The backtest engine (resolved lazily so tests can install one)."""
        if self._backtests is None:
            self._backtests = get_backtest_engine()
        return self._backtests

    @property
    def bus(self) -> EventBus:
        """The event bus progress is published on."""
        if self._bus is None:
            self._bus = get_event_bus()
        return self._bus

    @contextmanager
    def _session(self) -> Iterator[Session]:
        """Yield a session that commits on success and rolls back on failure."""
        factory = self._sessions or get_session_factory()
        session = factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    async def _publish(self, payload: dict[str, Any]) -> None:
        """Publish a progress event, never failing the analysis because of it."""
        try:
            await self.bus.publish(LAB_CHANNEL, payload)
        except Exception:  # noqa: BLE001 - telemetry must not break the run
            logger.debug("publishing an analysis event failed", exc_info=True)

    # --- queries ----------------------------------------------------------
    def list_analyses(
        self,
        *,
        kind: AnalysisKind | None = None,
        experiment_id: str | None = None,
        backtest_id: str | None = None,
        hypothesis_id: str | None = None,
        limit: int = 100,
    ) -> list[AnalysisRecord]:
        """Return persisted analyses, newest first."""
        with self._session() as session:
            statement = select(AnalysisRow).order_by(AnalysisRow.created_at.desc()).limit(limit)
            if kind is not None:
                statement = statement.where(AnalysisRow.kind == str(kind))
            if experiment_id:
                statement = statement.where(AnalysisRow.experiment_id == experiment_id)
            if backtest_id:
                statement = statement.where(AnalysisRow.backtest_id == backtest_id)
            if hypothesis_id:
                statement = statement.where(AnalysisRow.hypothesis_id == hypothesis_id)
            return [_record_from_row(row) for row in session.scalars(statement)]

    def get(self, analysis_id: str) -> AnalysisRecord:
        """Return one analysis.

        Raises:
            KeyError: when the identifier is unknown.
        """
        with self._session() as session:
            row = session.get(AnalysisRow, analysis_id)
            if row is None:
                raise KeyError(f"unknown analysis {analysis_id!r}")
            return _record_from_row(row)

    def delete(self, analysis_id: str) -> None:
        """Delete one analysis.

        Raises:
            KeyError: when the identifier is unknown.
        """
        with self._session() as session:
            row = session.get(AnalysisRow, analysis_id)
            if row is None:
                raise KeyError(f"unknown analysis {analysis_id!r}")
            session.delete(row)

    def count_cells(self, *, kind: AnalysisKind | None = None) -> int:
        """Return how many cells every recorded analysis evaluated.

        This is the trial ledger's *sweep cells* figure: each cell is a distinct
        configuration that was scored, and therefore a comparison the deflated
        Sharpe must account for. Monte-Carlo resamples are deliberately excluded
        when ``kind`` is not given: 5,000 resamples of *one* configuration are
        one trial, not five thousand, and counting them would destroy the
        deflation rather than tighten it.
        """
        with self._session() as session:
            statement = select(AnalysisRow.cell_count)
            if kind is not None:
                statement = statement.where(AnalysisRow.kind == str(kind))
            else:
                statement = statement.where(
                    AnalysisRow.kind != str(AnalysisKind.MONTE_CARLO)
                )
            return int(sum(int(value or 0) for value in session.scalars(statement)))

    def counts(self) -> dict[str, int]:
        """Return per-kind analysis and cell counts for the trial ledger."""
        with self._session() as session:
            rows = list(session.scalars(select(AnalysisRow)))
        out = {
            "analyses": len(rows),
            "sweeps": 0,
            "sweep_cells": 0,
            "walk_forwards": 0,
            "walk_forward_folds": 0,
            "monte_carlos": 0,
            "monte_carlo_paths": 0,
        }
        for row in rows:
            if row.kind == str(AnalysisKind.SWEEP):
                out["sweeps"] += 1
                out["sweep_cells"] += int(row.cell_count or 0)
            elif row.kind == str(AnalysisKind.MONTE_CARLO):
                # Resamples are not comparisons; they are kept out of the
                # trial totals and reported separately.
                out["monte_carlos"] += 1
                out["monte_carlo_paths"] += int(row.cell_count or 0)
            else:
                out["walk_forwards"] += 1
                out["walk_forward_folds"] += int(row.cell_count or 0)
        return out

    # --- running ----------------------------------------------------------
    async def run_sweep(
        self,
        config: BacktestConfig,
        spec: SweepSpec,
        *,
        experiment_id: str | None = None,
        hypothesis_id: str | None = None,
        backtest_id: str | None = None,
    ) -> AnalysisRecord:
        """Run a parameter sweep and persist every cell's statistics."""
        cells = spec.cells()
        name = spec.name or f"Sweep · {', '.join(axis.path for axis in spec.axes)}"
        identifier = new_analysis_id(AnalysisKind.SWEEP)
        started = self._begin(
            identifier,
            AnalysisKind.SWEEP,
            name,
            spec.as_dict(),
            config,
            experiment_id=experiment_id,
            hypothesis_id=hypothesis_id,
            backtest_id=backtest_id,
        )
        await self._publish(
            {
                "kind": "analysis",
                "phase": "started",
                "analysis": "sweep",
                "id": identifier,
                "cells": len(cells),
                "ts": started.isoformat(),
            }
        )
        try:
            payload = await asyncio.to_thread(self._sweep_sync, config, spec, cells)
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            logger.warning("sweep %s failed: %s", identifier, message)
            self._fail(identifier, message)
            await self._publish(
                {
                    "kind": "analysis",
                    "phase": "failed",
                    "analysis": "sweep",
                    "id": identifier,
                    "error": message,
                    "ts": utcnow().isoformat(),
                }
            )
            raise
        record = self._complete(identifier, started, payload)
        await self._publish(
            {
                "kind": "analysis",
                "phase": "complete",
                "analysis": "sweep",
                "id": identifier,
                "summary": record.summary,
                "ts": utcnow().isoformat(),
            }
        )
        return record

    async def run_walk_forward(
        self,
        config: BacktestConfig,
        spec: WalkForwardSpec,
        *,
        experiment_id: str | None = None,
        hypothesis_id: str | None = None,
        backtest_id: str | None = None,
    ) -> AnalysisRecord:
        """Run a walk-forward analysis and persist every fold."""
        name = spec.name or f"Walk-forward · {spec.folds} folds"
        identifier = new_analysis_id(AnalysisKind.WALK_FORWARD)
        started = self._begin(
            identifier,
            AnalysisKind.WALK_FORWARD,
            name,
            spec.as_dict(),
            config,
            experiment_id=experiment_id,
            hypothesis_id=hypothesis_id,
            backtest_id=backtest_id,
        )
        await self._publish(
            {
                "kind": "analysis",
                "phase": "started",
                "analysis": "walk_forward",
                "id": identifier,
                "folds": spec.folds,
                "ts": started.isoformat(),
            }
        )
        try:
            payload = await asyncio.to_thread(self._walk_forward_sync, config, spec)
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            logger.warning("walk-forward %s failed: %s", identifier, message)
            self._fail(identifier, message)
            await self._publish(
                {
                    "kind": "analysis",
                    "phase": "failed",
                    "analysis": "walk_forward",
                    "id": identifier,
                    "error": message,
                    "ts": utcnow().isoformat(),
                }
            )
            raise
        record = self._complete(identifier, started, payload)
        await self._publish(
            {
                "kind": "analysis",
                "phase": "complete",
                "analysis": "walk_forward",
                "id": identifier,
                "summary": record.summary,
                "ts": utcnow().isoformat(),
            }
        )
        return record

    async def run_monte_carlo(
        self,
        config: BacktestConfig,
        *,
        paths: int = DEFAULT_MC_PATHS,
        shown: int = DEFAULT_MC_SHOWN,
        block: int | None = None,
        seed: int = 20260828,
        name: str | None = None,
        experiment_id: str | None = None,
        hypothesis_id: str | None = None,
        backtest_id: str | None = None,
    ) -> AnalysisRecord:
        """Resample a configuration's realised returns into an equity-path cone.

        The sweep asks "does the parameter matter"; the walk-forward asks "does
        the selection survive"; this asks the third question - "how much of the
        realised curve was the *order* the returns arrived in". Circular blocks
        of consecutive returns are resampled with replacement, so autocorrelation
        survives and only the sequencing is destroyed.

        Args:
            config: the configuration to run once and then resample.
            paths: how many resamples to draw.
            shown: how many individual paths to return for drawing.
            block: block length in periods (defaults to ``ceil(sqrt(n))``).
            seed: RNG seed, so the same run always yields the same cone.
            name: analysis label; defaults to a generated one.
            experiment_id: experiment the analysis is attached to.
            hypothesis_id: hypothesis the analysis is attached to.
            backtest_id: backtest the analysis is attached to.

        Raises:
            ValueError: when ``paths`` is out of range or the return series is
                too short to resample.
        """
        draws = int(paths)
        if not 100 <= draws <= MAX_MC_PATHS:
            raise ValueError(f"paths must be between 100 and {MAX_MC_PATHS:,}")
        label = name or f"Monte Carlo · {draws:,} block-bootstrap resamples"
        identifier = new_analysis_id(AnalysisKind.MONTE_CARLO)
        spec = {
            "paths": draws,
            "shown": int(shown),
            "block": block,
            "seed": int(seed),
        }
        started = self._begin(
            identifier,
            AnalysisKind.MONTE_CARLO,
            label,
            spec,
            config,
            experiment_id=experiment_id,
            hypothesis_id=hypothesis_id,
            backtest_id=backtest_id,
        )
        await self._publish(
            {
                "kind": "analysis",
                "phase": "started",
                "analysis": "monte_carlo",
                "id": identifier,
                "paths": draws,
                "ts": started.isoformat(),
            }
        )
        try:
            payload = await asyncio.to_thread(
                self._monte_carlo_sync, config, draws, int(shown), block, int(seed)
            )
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            logger.warning("monte carlo %s failed: %s", identifier, message)
            self._fail(identifier, message)
            await self._publish(
                {
                    "kind": "analysis",
                    "phase": "failed",
                    "analysis": "monte_carlo",
                    "id": identifier,
                    "error": message,
                    "ts": utcnow().isoformat(),
                }
            )
            raise
        record = self._complete(identifier, started, payload)
        await self._publish(
            {
                "kind": "analysis",
                "phase": "complete",
                "analysis": "monte_carlo",
                "id": identifier,
                "summary": record.summary,
                "ts": utcnow().isoformat(),
            }
        )
        return record

    def _monte_carlo_sync(
        self,
        config: BacktestConfig,
        paths: int,
        shown: int,
        block: int | None,
        seed: int,
    ) -> dict[str, Any]:
        """Run the configuration once, then resample its return series."""
        result = self.backtests.run_sync(config)
        cone = monte_carlo_paths(
            result.returns,
            paths=paths,
            shown=shown,
            block=block,
            seed=seed,
            periods_per_year=result.periods_per_year,
            initial_capital=config.initial_capital,
            max_points=MC_CURVE_POINTS,
        )
        if not cone.get("paths"):
            raise ValueError(cone.get("note") or "the run is too short to resample")
        summary = {
            "metric": "sharpe",
            "paths": cone["paths"],
            "shown": cone["shown"],
            "block": cone["block"],
            "seed": cone["seed"],
            "observations": cone["observations"],
            "realised_sharpe": cone["sharpe"]["realised"],
            "median_sharpe": cone["sharpe"]["median"],
            "prob_positive": cone["sharpe"]["prob_positive"],
            "prob_above_one": cone["sharpe"]["prob_above_one"],
            "median_max_drawdown": cone["drawdown"]["median"],
            "p95_max_drawdown": cone["drawdown"]["p95"],
            "prob_drawdown_over_20pct": cone["drawdown"]["prob_worse_than_20pct"],
            "realised_percentile": cone["terminal"]["realised_percentile"],
        }
        # A realised path far up the cone means the sequencing was kind, not
        # that the edge is large. Saying so is the point of the analysis.
        summary["favourable_sequencing"] = bool(
            cone["terminal"]["realised_percentile"] >= 75.0
        )
        warnings = list(result.warnings)
        if summary["favourable_sequencing"]:
            warnings.append(
                "the realised path sits at p"
                f"{cone['terminal']['realised_percentile']:.0f} of the resample cone - "
                "favourable sequencing, not only edge"
            )
        # The cone is one object, not a grid of cells; it is stored as a single
        # "cell" so the AnalysisRow shape stays uniform across every kind. At the
        # defaults (200 drawn paths x 240 points, rounded to cents) that row is
        # roughly half a megabyte - which is why GET /backtests/{id}/fan reuses a
        # stored cone with a matching path count instead of writing a new one on
        # every tearsheet open.
        return {
            "cells": [{"index": 0, "status": "complete", "label": "cone", "cone": cone}],
            "cell_count": cone["paths"],
            "summary": summary,
            "warnings": warnings,
        }

    # --- computation ------------------------------------------------------
    def _evaluate(
        self,
        config: BacktestConfig,
        overrides: dict[str, Any],
        prices: pd.DataFrame,
        volumes: pd.DataFrame | None,
        benchmark: pd.Series | None,
    ) -> tuple[BacktestResult | None, str | None]:
        """Run one parameter set over the shared panel.

        Returns:
            ``(result, None)`` or ``(None, reason)`` - a cell that cannot run
            (a lookback longer than the history, a filter that selects nothing)
            is reported as such instead of aborting the whole grid.
        """
        try:
            cell_config = apply_overrides(config, overrides)
            result = self.backtests.backtester.run(
                cell_config, prices, benchmark=benchmark, volumes=volumes
            )
        except Exception as exc:  # noqa: BLE001 - one bad cell must not kill the sweep
            return None, f"{type(exc).__name__}: {exc}"
        return result, None

    def _sweep_sync(
        self, config: BacktestConfig, spec: SweepSpec, cells: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Evaluate every grid cell over a single shared price panel."""
        prices, volumes, benchmark, warnings = self.backtests.prepare(config)
        rows: list[dict[str, Any]] = []
        best: dict[str, Any] | None = None
        for index, overrides in enumerate(cells):
            result, failure = self._evaluate(config, overrides, prices, volumes, benchmark)
            row: dict[str, Any] = {
                "index": index,
                "params": dict(overrides),
                "label": describe_cell(overrides),
                "status": "failed" if result is None else "complete",
                "error": failure,
            }
            if result is not None:
                stats = self._window_stats(result, spec.window)
                row["stats"] = stats.as_dict() if stats else None
                row["metric"] = metric_value(stats, spec.metric)
                row["full"] = result.stats.as_dict()
                row["out_of_sample"] = (
                    result.out_of_sample.as_dict() if result.out_of_sample else None
                )
                row["trades"] = len(result.trades)
                score = row["metric"]
                if score is not None and (
                    best is None or _better(spec.metric, score, float(best["metric"]))
                ):
                    best = row
            rows.append(row)
        scores = [row["metric"] for row in rows if row.get("metric") is not None]
        summary: dict[str, Any] = {
            "metric": spec.metric,
            "window": spec.window,
            "cells": len(rows),
            "evaluated": len(scores),
            "failed": sum(1 for row in rows if row["status"] == "failed"),
            "best_metric": best["metric"] if best else None,
            "best_params": best["params"] if best else None,
            "best_label": best["label"] if best else None,
        }
        if scores:
            array = np.asarray(scores, dtype="float64")
            summary.update(
                {
                    "median_metric": float(np.median(array)),
                    "mean_metric": float(array.mean()),
                    "worst_metric": float(array.min()),
                    "spread": float(array.max() - array.min()),
                    "positive_share": float((array > 0).mean()),
                }
            )
            # "Robust" means the parameter choice barely matters: the median cell
            # keeps most of the best cell's edge. A grid where only the peak
            # works is the classic overfit signature, and is flagged, not hidden.
            peak = float(array.max()) if METRICS.get(spec.metric, True) else float(array.min())
            median = summary["median_metric"]
            summary["robust"] = bool(
                peak != 0.0 and median / peak >= 0.5 and summary["positive_share"] >= 0.6
            )
        else:
            summary["robust"] = False
        return {
            "cells": rows,
            "cell_count": len(rows),
            "summary": summary,
            "warnings": warnings,
        }

    @staticmethod
    def _window_stats(result: BacktestResult, window: str) -> BacktestStats | None:
        """Pick the statistics block a sweep should rank a cell on."""
        if window == "in_sample":
            return result.in_sample or result.stats
        if window == "out_of_sample":
            return result.out_of_sample
        return result.stats

    def _walk_forward_sync(
        self, config: BacktestConfig, spec: WalkForwardSpec
    ) -> dict[str, Any]:
        """Run every fold: select on train, score on the test window that follows."""
        prices, volumes, benchmark, warnings = self.backtests.prepare(config)
        grid = (
            [
                {axis.path: value for axis, value in zip(spec.axes, combo, strict=True)}
                for combo in itertools.product(*[axis.values for axis in spec.axes])
            ]
            if spec.axes
            else [{}]
        )
        # Evaluate every candidate once over the whole panel; folds then slice the
        # resulting return series. The backtester never looks ahead, so a slice is
        # exactly what that parameter set would have earned in that window.
        evaluated: list[tuple[dict[str, Any], BacktestResult]] = []
        failures: list[str] = []
        for overrides in grid:
            result, failure = self._evaluate(config, overrides, prices, volumes, benchmark)
            if result is None:
                failures.append(f"{describe_cell(overrides) or 'base config'}: {failure}")
                continue
            evaluated.append((overrides, result))
        if not evaluated:
            raise ValueError(
                "no parameter set produced a usable backtest: " + "; ".join(failures[:3])
            )
        index = evaluated[0][1].returns.index
        splits = self._fold_windows(index, spec)
        if not splits:
            raise ValueError(
                f"{len(index)} return observations cannot be split into {spec.folds} folds with "
                f"at least {MIN_FOLD_OBSERVATIONS} observations in each window"
            )
        periods = evaluated[0][1].periods_per_year
        folds: list[dict[str, Any]] = []
        stitched: list[pd.Series] = []
        for number, (train_slice, test_slice) in enumerate(splits, start=1):
            chosen: dict[str, Any] | None = None
            chosen_result: BacktestResult | None = None
            best_train: float | None = None
            for overrides, result in evaluated:
                stats = self._slice_stats(result, train_slice, periods, config)
                score = metric_value(stats, spec.metric)
                if score is None:
                    continue
                if best_train is None or _better(spec.metric, score, best_train):
                    best_train, chosen, chosen_result = score, overrides, result
            if chosen_result is None:
                folds.append(
                    {
                        "fold": number,
                        "status": "skipped",
                        "error": "no parameter set scored on this train window",
                        "train": _window_label(train_slice),
                        "test": _window_label(test_slice),
                    }
                )
                continue
            train_stats = self._slice_stats(chosen_result, train_slice, periods, config)
            test_stats = self._slice_stats(chosen_result, test_slice, periods, config)
            test_returns = chosen_result.returns.loc[test_slice[0] : test_slice[1]]
            stitched.append(test_returns)
            folds.append(
                {
                    "fold": number,
                    "status": "complete",
                    "params": dict(chosen),
                    "label": describe_cell(chosen) or "base configuration",
                    "train": _window_label(train_slice),
                    "test": _window_label(test_slice),
                    "train_metric": best_train,
                    "test_metric": metric_value(test_stats, spec.metric),
                    "train_stats": train_stats.as_dict() if train_stats else None,
                    "test_stats": test_stats.as_dict() if test_stats else None,
                    "observations": int(test_returns.size),
                }
            )
        summary = self._walk_forward_summary(spec, folds, stitched, periods, config)
        return {
            "cells": folds,
            "cell_count": len(folds),
            "summary": summary,
            "warnings": warnings + failures,
        }

    @staticmethod
    def _fold_windows(
        index: pd.Index, spec: WalkForwardSpec
    ) -> list[tuple[tuple[Any, Any], tuple[Any, Any]]]:
        """Split ``index`` into ``folds`` consecutive (train, test) windows.

        The test windows tile the tail of the history end to end; each fold's
        train window is everything before it (anchored) or a rolling window of
        proportional length.
        """
        total = len(index)
        if total < MIN_FOLD_OBSERVATIONS * 2:
            return []
        test_total = int(total * (1.0 - spec.train_fraction))
        per_fold = test_total // spec.folds
        if per_fold < MIN_FOLD_OBSERVATIONS:
            per_fold = MIN_FOLD_OBSERVATIONS
        first_test = total - per_fold * spec.folds
        if first_test < MIN_FOLD_OBSERVATIONS:
            return []
        windows: list[tuple[tuple[Any, Any], tuple[Any, Any]]] = []
        for fold in range(spec.folds):
            test_start = first_test + fold * per_fold
            test_end = test_start + per_fold - 1
            if test_end >= total:
                break
            train_start = 0 if spec.anchored else max(0, test_start - first_test)
            train_end = test_start - 1
            if train_end - train_start + 1 < MIN_FOLD_OBSERVATIONS:
                continue
            windows.append(
                (
                    (index[train_start], index[train_end]),
                    (index[test_start], index[test_end]),
                )
            )
        return windows

    @staticmethod
    def _slice_stats(
        result: BacktestResult,
        window: tuple[Any, Any],
        periods_per_year: int,
        config: BacktestConfig,
    ) -> BacktestStats | None:
        """Compute statistics for one window of an already-run return series."""
        returns = result.returns.loc[window[0] : window[1]]
        if returns.size < MIN_FOLD_OBSERVATIONS:
            return None
        benchmark = None
        if result.benchmark_returns is not None:
            benchmark = result.benchmark_returns.loc[window[0] : window[1]]
            if benchmark.size != returns.size:
                benchmark = benchmark.reindex(returns.index)
        turnover = result.turnover.loc[window[0] : window[1]]
        trades = int(result.trade_counts.loc[window[0] : window[1]].sum())
        return compute_stats(
            returns,
            benchmark=benchmark,
            periods_per_year=periods_per_year,
            turnover=turnover,
            trades=trades,
            risk_free_rate=config.risk_free_rate,
        )

    def _walk_forward_summary(
        self,
        spec: WalkForwardSpec,
        folds: list[dict[str, Any]],
        stitched: Sequence[pd.Series],
        periods_per_year: int,
        config: BacktestConfig,
    ) -> dict[str, Any]:
        """Aggregate the folds into the headline figures the folds view shows."""
        scored = [f for f in folds if f.get("status") == "complete"]
        test_scores = [f["test_metric"] for f in scored if f.get("test_metric") is not None]
        train_scores = [f["train_metric"] for f in scored if f.get("train_metric") is not None]
        summary: dict[str, Any] = {
            "metric": spec.metric,
            "folds": len(folds),
            "scored": len(scored),
            "anchored": spec.anchored,
            "grid_size": max(1, math.prod(len(axis.values) for axis in spec.axes) if spec.axes else 1),
            "mean_test_metric": float(np.mean(test_scores)) if test_scores else None,
            "median_test_metric": float(np.median(test_scores)) if test_scores else None,
            "worst_test_metric": float(np.min(test_scores)) if test_scores else None,
            "mean_train_metric": float(np.mean(train_scores)) if train_scores else None,
            "positive_folds": sum(1 for value in test_scores if value > 0),
        }
        if test_scores and train_scores and float(np.mean(train_scores)) != 0.0:
            # Walk-forward efficiency: how much of the in-sample edge survived
            # out of sample. Below ~0.5 the selection step is fitting noise.
            summary["efficiency"] = float(np.mean(test_scores) / np.mean(train_scores))
        else:
            summary["efficiency"] = None
        if stitched:
            joined = pd.concat(list(stitched)).sort_index()
            joined = joined[~joined.index.duplicated(keep="last")]
            stats = compute_stats(
                joined,
                benchmark=None,
                periods_per_year=periods_per_year,
                turnover=None,
                trades=0,
                risk_free_rate=config.risk_free_rate,
            )
            summary["stitched"] = stats.as_dict()
            equity = (1.0 + joined).cumprod()
            summary["curve"] = {
                "dates": [ts.isoformat() for ts in equity.index],
                "equity": [float(value) for value in equity.to_numpy()],
            }
        return summary

    # --- row lifecycle ----------------------------------------------------
    def _begin(
        self,
        identifier: str,
        kind: AnalysisKind,
        name: str,
        spec: dict[str, Any],
        config: BacktestConfig,
        *,
        experiment_id: str | None,
        hypothesis_id: str | None,
        backtest_id: str | None,
    ) -> datetime:
        """Insert the running row and return its start timestamp."""
        started = utcnow()
        with self._session() as session:
            session.add(
                AnalysisRow(
                    id=identifier,
                    kind=str(kind),
                    name=name,
                    status=str(AnalysisStatus.RUNNING),
                    experiment_id=experiment_id,
                    hypothesis_id=hypothesis_id,
                    backtest_id=backtest_id,
                    spec=spec,
                    config=config.as_dict(),
                    summary={},
                    cells=[],
                    cell_count=0,
                    created_at=started,
                    started_at=started,
                    warnings=[],
                )
            )
        return started

    def _fail(self, identifier: str, message: str) -> None:
        """Record a failed analysis honestly rather than deleting the row."""
        with self._session() as session:
            row = session.get(AnalysisRow, identifier)
            if row is None:  # pragma: no cover - the row is inserted first
                return
            row.status = str(AnalysisStatus.FAILED)
            row.finished_at = utcnow()
            row.error = message

    def _complete(
        self, identifier: str, started: datetime, payload: dict[str, Any]
    ) -> AnalysisRecord:
        """Write the finished analysis and return the record."""
        finished = utcnow()
        with self._session() as session:
            row = session.get(AnalysisRow, identifier)
            if row is None:  # pragma: no cover - the row is inserted first
                raise KeyError(f"unknown analysis {identifier!r}")
            row.status = str(AnalysisStatus.COMPLETE)
            row.cells = payload["cells"]
            row.cell_count = float(payload["cell_count"])
            row.summary = payload["summary"]
            row.warnings = list(payload.get("warnings") or [])
            row.finished_at = finished
            row.duration_ms = (finished - started).total_seconds() * 1000.0
            row.error = None
            return _record_from_row(row)


def _window_label(window: tuple[Any, Any]) -> dict[str, str]:
    """Render a (start, end) index pair as ISO dates for the folds table."""
    return {
        "start": str(getattr(window[0], "isoformat", lambda: window[0])())[:10],
        "end": str(getattr(window[1], "isoformat", lambda: window[1])())[:10],
    }


_engine: AnalysisEngine | None = None


def get_analysis_engine() -> AnalysisEngine:
    """Return the process-wide analysis engine, creating it on first use."""
    global _engine
    if _engine is None:
        _engine = AnalysisEngine()
    return _engine


def reset_analysis_engine(engine: AnalysisEngine | None = None) -> AnalysisEngine:
    """Replace the process-wide analysis engine (app factory and tests)."""
    global _engine
    _engine = engine or AnalysisEngine()
    return _engine


__all__ = [
    "MAX_SWEEP_CELLS",
    "METRICS",
    "SWEEPABLE",
    "AnalysisEngine",
    "AnalysisKind",
    "AnalysisRecord",
    "AnalysisStatus",
    "ParameterAxis",
    "SweepSpec",
    "WalkForwardSpec",
    "apply_overrides",
    "get_analysis_engine",
    "reset_analysis_engine",
]
