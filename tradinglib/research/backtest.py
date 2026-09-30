"""Vectorised backtester for signal-weighted portfolios.

The engine turns a price panel plus a *signal specification* into target
weights, walks the portfolio forward one bar at a time applying transaction
costs, and produces an equity curve plus the statistics the Strategy Lab shows.

Timing convention (stated once, applied everywhere)
---------------------------------------------------
* ``prices`` is a panel indexed by bar timestamp with one column per symbol.
* A signal computed on bar ``i`` uses information available *at the close of
  bar* ``i`` only. The trade implied by it is executed at that close, so the
  resulting weights earn the return of bar ``i+1``. There is therefore no
  look-ahead by construction.
* The cost of the trade executed at the close of bar ``i`` is charged to the
  return of bar ``i+1`` - the bar in which the new position is actually held.
  The first bar of the backtest establishes the position, so the entry cost is
  charged to bar 1 and bar 0 has no return at all: the equity curve starts
  exactly at ``initial_capital``.
* Between rebalances weights *drift* with the constituents' returns; no trade
  and therefore no cost is incurred on a non-rebalance bar.
* No trade is executed on the final bar (there is no following bar to hold the
  position through), so the backtest never charges a cost it cannot earn
  through.

Statistics are computed on the return series ``r_1 .. r_{n-1}`` (bar 0 carries
no return). ``periods_per_year`` annualises everything and is inferred from the
median bar spacing unless the caller passes it explicitly.

Everything here is deterministic: identical inputs always produce identical
numbers, which is what ``tests/test_research_backtest.py`` asserts against
hand-computed expectations.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import logging
import math
import uuid
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
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
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from tradinglib.data.base import BarInterval, to_utc, utcnow
from tradinglib.data.store import TimeSeriesStore, get_store
from tradinglib.db import Base, ensure_columns, get_engine, get_session_factory
from tradinglib.events import EventBus, get_event_bus
from tradinglib.research import BacktestRequest, register_backtest_queue

logger = logging.getLogger(__name__)

#: Event channel Strategy-Lab progress is published on. ``api/ws.py`` fans it
#: out to the browser; the UI's experiment/backtest run logs read from it.
LAB_CHANNEL: Final[str] = "lab"

#: Trading periods per year assumed for daily bars (US equity sessions).
TRADING_DAYS_PER_YEAR: Final[int] = 252

#: A weight change smaller than this is not counted as a trade.
TRADE_EPSILON: Final[float] = 1e-9

#: Single-bar return magnitude above which a bar is flagged as a probable
#: unadjusted corporate action rather than a real move.
CORPORATE_ACTION_RETURN: Final[float] = 0.40


class SignalKind(StrEnum):
    """How a signal panel is produced from prices."""

    EXPRESSION = "expression"
    MOMENTUM = "momentum"
    MEAN_REVERSION = "mean_reversion"
    MA_CROSS = "ma_cross"
    LOW_VOLATILITY = "low_volatility"
    STATIC = "static"


class Weighting(StrEnum):
    """How selected names are weighted."""

    SIGNAL = "signal"
    EQUAL = "equal"
    INVERSE_VOL = "inverse_vol"


class Rebalance(StrEnum):
    """How often target weights are re-applied."""

    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"


class BacktestStatus(StrEnum):
    """Lifecycle of a persisted backtest run."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


# --------------------------------------------------------------------------
# factor expressions
# --------------------------------------------------------------------------
#: Functions an expression may call. Each takes and returns a panel (or a
#: scalar) and is applied cross-sectionally where that is meaningful.
_EXPRESSION_FUNCTIONS: Final[frozenset[str]] = frozenset(
    {"rank", "zscore", "abs", "clip", "where", "log", "sqrt", "sign"}
)

_ALLOWED_NODES: Final[tuple[type[ast.AST], ...]] = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.BoolOp,
    ast.Compare,
    ast.Call,
    ast.Name,
    ast.Load,
    ast.Constant,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.Pow,
    ast.USub,
    ast.UAdd,
    ast.Not,
    ast.And,
    ast.Or,
    ast.Gt,
    ast.GtE,
    ast.Lt,
    ast.LtE,
    ast.Eq,
    ast.NotEq,
)


def _row_mean(frame: pd.DataFrame) -> pd.Series:
    """Cross-sectional mean of each row, ignoring missing values."""
    return frame.mean(axis=1, skipna=True)


def _cross_sectional_rank(frame: pd.DataFrame) -> pd.DataFrame:
    """Percentile rank within each row (0 = smallest, 1 = largest)."""
    return frame.rank(axis=1, pct=True, na_option="keep")


def _cross_sectional_zscore(frame: pd.DataFrame) -> pd.DataFrame:
    """Standardise each row: ``(x - row mean) / row stdev``."""
    mean = _row_mean(frame)
    std = frame.std(axis=1, ddof=1, skipna=True)
    std = std.replace(0.0, np.nan)
    return frame.sub(mean, axis=0).div(std, axis=0)


def _as_frame_like(value: Any, template: pd.DataFrame) -> pd.DataFrame:
    """Broadcast a scalar/Series to ``template``'s shape; pass frames through."""
    if isinstance(value, pd.DataFrame):
        return value
    if isinstance(value, pd.Series):
        return pd.DataFrame({column: value for column in template.columns})
    return pd.DataFrame(
        np.full(template.shape, float(value)), index=template.index, columns=template.columns
    )


def _call_function(name: str, args: list[Any]) -> Any:
    """Apply one whitelisted expression function to already-evaluated arguments."""
    if name == "rank":
        return _cross_sectional_rank(args[0])
    if name == "zscore":
        return _cross_sectional_zscore(args[0])
    if name == "abs":
        return args[0].abs() if isinstance(args[0], (pd.DataFrame, pd.Series)) else abs(args[0])
    if name == "log":
        return np.log(args[0])
    if name == "sqrt":
        return np.sqrt(args[0])
    if name == "sign":
        return np.sign(args[0])
    if name == "clip":
        if len(args) != 3:
            raise ValueError("clip(x, low, high) takes exactly three arguments")
        return args[0].clip(lower=float(args[1]), upper=float(args[2]))
    if name == "where":
        if len(args) != 3:
            raise ValueError("where(condition, a, b) takes exactly three arguments")
        condition = args[0]
        if not isinstance(condition, pd.DataFrame):
            raise ValueError("where() needs a panel-valued condition")
        left = _as_frame_like(args[1], condition)
        right = _as_frame_like(args[2], condition)
        return left.where(condition.fillna(False).astype(bool), right)
    raise ValueError(f"unknown function {name!r}")


class ExpressionError(ValueError):
    """Raised when a factor expression is malformed or references unknown names."""


def compile_expression(expression: str, namespace: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Evaluate a factor expression against a namespace of panels.

    The expression is parsed with :mod:`ast` and every node is checked against a
    whitelist, so nothing outside arithmetic, comparison and the functions in
    :data:`_EXPRESSION_FUNCTIONS` can execute - user text is never handed to
    :func:`eval` on raw input.

    Args:
        expression: e.g. ``0.7 * rank(mom_12_1) + 0.3 * rank(-vol_20d)``.
        namespace: factor name -> panel (rows = bars, columns = symbols).

    Returns:
        The resulting panel, aligned to the namespace panels.

    Raises:
        ExpressionError: on syntax errors, unknown names or disallowed nodes.
    """
    text = expression.strip()
    if not text:
        raise ExpressionError("expression is empty")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:  # pragma: no cover - message varies by Python
        raise ExpressionError(f"cannot parse expression: {exc.msg}") from exc

    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ExpressionError(f"{type(node).__name__} is not allowed in an expression")

    def evaluate(node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return evaluate(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise ExpressionError("only numeric constants are allowed")
            return float(node.value)
        if isinstance(node, ast.Name):
            if node.id not in namespace:
                known = ", ".join(sorted(namespace))
                raise ExpressionError(f"unknown factor {node.id!r}; available: {known}")
            return namespace[node.id]
        if isinstance(node, ast.UnaryOp):
            operand = evaluate(node.operand)
            if isinstance(node.op, ast.USub):
                return -operand
            if isinstance(node.op, ast.UAdd):
                return operand
            return ~operand.fillna(False).astype(bool)
        if isinstance(node, ast.BinOp):
            left, right = evaluate(node.left), evaluate(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                return left / right
            return left**right
        if isinstance(node, ast.BoolOp):
            values = [evaluate(v) for v in node.values]
            frames = [v.fillna(False).astype(bool) for v in values]
            result = frames[0]
            for frame in frames[1:]:
                result = result & frame if isinstance(node.op, ast.And) else result | frame
            return result
        if isinstance(node, ast.Compare):
            if len(node.ops) != 1 or len(node.comparators) != 1:
                raise ExpressionError("chained comparisons are not supported")
            left, right = evaluate(node.left), evaluate(node.comparators[0])
            op = node.ops[0]
            if isinstance(op, ast.Gt):
                return left > right
            if isinstance(op, ast.GtE):
                return left >= right
            if isinstance(op, ast.Lt):
                return left < right
            if isinstance(op, ast.LtE):
                return left <= right
            if isinstance(op, ast.Eq):
                return left == right
            return left != right
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _EXPRESSION_FUNCTIONS:
                allowed = ", ".join(sorted(_EXPRESSION_FUNCTIONS))
                raise ExpressionError(f"only these functions may be called: {allowed}")
            if node.keywords:
                raise ExpressionError("expression functions take positional arguments only")
            return _call_function(node.func.id, [evaluate(arg) for arg in node.args])
        raise ExpressionError(f"{type(node).__name__} is not allowed in an expression")

    result = evaluate(tree)
    if not isinstance(result, pd.DataFrame):
        raise ExpressionError("an expression must evaluate to a panel, not a scalar")
    return result.replace([np.inf, -np.inf], np.nan)


def build_factor_panels(
    prices: pd.DataFrame, volumes: pd.DataFrame | None = None
) -> dict[str, pd.DataFrame]:
    """Derive the factor namespace an expression may reference.

    Every panel shares ``prices``' index and columns. Factors are computed from
    closing prices only (and volume when it is supplied), so the namespace is
    honest about what the price store actually holds.

    Returns:
        Mapping of factor name to panel:

        ``close``, ``ret_1d``, ``mom_1m``, ``mom_3m``, ``mom_6_1``,
        ``mom_12_1``, ``rev_1m``, ``vol_20d``, ``vol_60d``, ``sma_50``,
        ``sma_200``, ``trend_50``, ``ma_ratio_50_200``, ``rsi_14`` and, when
        volume is available, ``dollar_vol_20d``.
    """
    close = prices.astype("float64")
    returns = close.pct_change()
    panels: dict[str, pd.DataFrame] = {
        "close": close,
        "ret_1d": returns,
        "mom_1m": close / close.shift(21) - 1.0,
        "mom_3m": close / close.shift(63) - 1.0,
        "mom_6_1": close.shift(21) / close.shift(126) - 1.0,
        "mom_12_1": close.shift(21) / close.shift(252) - 1.0,
        "rev_1m": -(close / close.shift(21) - 1.0),
        "vol_20d": returns.rolling(20).std(ddof=1) * math.sqrt(TRADING_DAYS_PER_YEAR),
        "vol_60d": returns.rolling(60).std(ddof=1) * math.sqrt(TRADING_DAYS_PER_YEAR),
    }
    sma_50 = close.rolling(50).mean()
    sma_200 = close.rolling(200).mean()
    panels["sma_50"] = sma_50
    panels["sma_200"] = sma_200
    panels["trend_50"] = close / sma_50 - 1.0
    panels["ma_ratio_50_200"] = sma_50 / sma_200 - 1.0

    gain = returns.clip(lower=0.0)
    loss = (-returns).clip(lower=0.0)
    avg_gain = gain.rolling(14).mean()
    avg_loss = loss.rolling(14).mean()
    relative_strength = avg_gain / avg_loss.replace(0.0, np.nan)
    panels["rsi_14"] = 100.0 - 100.0 / (1.0 + relative_strength)

    if volumes is not None and not volumes.empty:
        aligned = volumes.reindex(index=close.index, columns=close.columns).astype("float64")
        panels["dollar_vol_20d"] = (aligned * close).rolling(20).mean()
    return {name: panel.replace([np.inf, -np.inf], np.nan) for name, panel in panels.items()}


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------
@dataclass(slots=True)
class CostModel:
    """Transaction costs applied to every position change.

    ``commission_bps`` and ``slippage_bps`` are charged on the traded notional
    (a full round trip of the whole book therefore costs twice the one-way
    rate). ``borrow_bps_annual`` is charged on short exposure only and is
    accrued per period.
    """

    commission_bps: float = 1.0
    slippage_bps: float = 5.0
    borrow_bps_annual: float = 0.0

    @property
    def total_bps(self) -> float:
        """One-way trading cost in basis points of traded notional."""
        return float(self.commission_bps) + float(self.slippage_bps)

    @property
    def rate(self) -> float:
        """One-way trading cost as a fraction of traded notional."""
        return self.total_bps / 10_000.0

    def apply(self, turnover: pd.Series) -> pd.Series:
        """Return the per-period cost drag implied by traded notional."""
        return turnover.astype("float64") * self.rate

    def borrow_cost(self, short_exposure: float, periods_per_year: int) -> float:
        """Return one period's borrow cost for ``short_exposure`` (a positive fraction)."""
        if short_exposure <= 0.0 or self.borrow_bps_annual <= 0.0:
            return 0.0
        return short_exposure * (self.borrow_bps_annual / 10_000.0) / float(periods_per_year)

    def scaled(self, multiplier: float) -> CostModel:
        """Return a copy with every rate multiplied (cost-sensitivity re-runs)."""
        return CostModel(
            commission_bps=self.commission_bps * multiplier,
            slippage_bps=self.slippage_bps * multiplier,
            borrow_bps_annual=self.borrow_bps_annual * multiplier,
        )

    def as_dict(self) -> dict[str, float]:
        """Return a JSON-serialisable mapping."""
        return {
            "commission_bps": self.commission_bps,
            "slippage_bps": self.slippage_bps,
            "borrow_bps_annual": self.borrow_bps_annual,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> CostModel:
        """Rebuild a cost model from :meth:`as_dict` output."""
        data = payload or {}
        return cls(
            commission_bps=float(data.get("commission_bps", 1.0)),
            slippage_bps=float(data.get("slippage_bps", 5.0)),
            borrow_bps_annual=float(data.get("borrow_bps_annual", 0.0)),
        )


@dataclass(slots=True)
class SignalSpec:
    """How to turn a price panel into a signal, and the signal into weights.

    A spec is fully serialisable, which is what makes a backtest reproducible:
    the persisted run stores its spec and can be re-run bit-for-bit (that is how
    the cost-sensitivity gate in :mod:`tradinglib.research.validation` works).
    """

    kind: SignalKind = SignalKind.MOMENTUM
    expression: str | None = None
    filter_expression: str | None = None
    symbols: list[str] = field(default_factory=list)
    lookback: int = 252
    skip: int = 21
    fast: int = 50
    slow: int = 200
    top_n: int | None = None
    weighting: Weighting = Weighting.EQUAL
    min_signal: float | None = None

    def describe(self) -> str:
        """Human-readable one-line rendering for the UI's run header."""
        if self.kind is SignalKind.EXPRESSION:
            body = self.expression or "(no expression)"
        elif self.kind is SignalKind.MOMENTUM:
            body = f"{self.lookback}d momentum skipping {self.skip}d"
        elif self.kind is SignalKind.MEAN_REVERSION:
            body = f"{self.lookback}d reversal"
        elif self.kind is SignalKind.MA_CROSS:
            body = f"SMA {self.fast}/{self.slow}"
        elif self.kind is SignalKind.LOW_VOLATILITY:
            body = f"inverse {self.lookback}d volatility"
        else:
            body = f"static basket of {len(self.symbols)} symbols"
        parts = [body, f"{self.weighting} weighted"]
        if self.top_n:
            parts.append(f"top {self.top_n}")
        if self.filter_expression:
            parts.append(f"filter: {self.filter_expression}")
        return " · ".join(parts)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping."""
        payload = asdict(self)
        payload["kind"] = str(self.kind)
        payload["weighting"] = str(self.weighting)
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> SignalSpec:
        """Rebuild a spec from :meth:`as_dict` output."""
        data = dict(payload or {})
        top_n = data.get("top_n")
        min_signal = data.get("min_signal")
        return cls(
            kind=SignalKind(str(data.get("kind", SignalKind.MOMENTUM))),
            expression=data.get("expression"),
            filter_expression=data.get("filter_expression"),
            symbols=[str(s).upper() for s in data.get("symbols") or []],
            lookback=int(data.get("lookback", 252)),
            skip=int(data.get("skip", 21)),
            fast=int(data.get("fast", 50)),
            slow=int(data.get("slow", 200)),
            top_n=None if top_n in (None, "") else int(top_n),
            weighting=Weighting(str(data.get("weighting", Weighting.EQUAL))),
            min_signal=None if min_signal in (None, "") else float(min_signal),
        )


@dataclass(slots=True)
class BacktestConfig:
    """Everything needed to reproduce a backtest."""

    name: str
    universe_id: str | None = None
    symbols: list[str] = field(default_factory=list)
    signal: SignalSpec = field(default_factory=SignalSpec)
    start: date | None = None
    end: date | None = None
    benchmark: str | None = "SPY"
    rebalance: Rebalance = Rebalance.MONTHLY
    initial_capital: float = 1_000_000.0
    gross_exposure: float = 1.0
    max_positions: int | None = None
    long_only: bool = True
    costs: CostModel = field(default_factory=CostModel)
    oos_start: date | None = None
    oos_fraction: float | None = 0.3
    risk_free_rate: float = 0.0
    periods_per_year: int | None = None
    trim_warmup: bool = True
    interval: BarInterval = BarInterval.DAY_1
    params: dict[str, Any] = field(default_factory=dict)

    def effective_top_n(self) -> int | None:
        """Return the position cap implied by the spec and the config."""
        caps = [c for c in (self.signal.top_n, self.max_positions) if c]
        return min(caps) if caps else None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping (this is what gets persisted)."""
        return {
            "name": self.name,
            "universe_id": self.universe_id,
            "symbols": list(self.symbols),
            "signal": self.signal.as_dict(),
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "benchmark": self.benchmark,
            "rebalance": str(self.rebalance),
            "initial_capital": self.initial_capital,
            "gross_exposure": self.gross_exposure,
            "max_positions": self.max_positions,
            "long_only": self.long_only,
            "costs": self.costs.as_dict(),
            "oos_start": self.oos_start.isoformat() if self.oos_start else None,
            "oos_fraction": self.oos_fraction,
            "risk_free_rate": self.risk_free_rate,
            "periods_per_year": self.periods_per_year,
            "trim_warmup": self.trim_warmup,
            "interval": str(self.interval),
            "params": dict(self.params),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> BacktestConfig:
        """Rebuild a config from :meth:`as_dict` output."""
        data = dict(payload)
        oos_fraction = data.get("oos_fraction")
        return cls(
            name=str(data.get("name") or "backtest"),
            universe_id=data.get("universe_id"),
            symbols=[str(s).upper() for s in data.get("symbols") or []],
            signal=SignalSpec.from_dict(data.get("signal")),
            start=_parse_date(data.get("start")),
            end=_parse_date(data.get("end")),
            benchmark=data.get("benchmark"),
            rebalance=Rebalance(str(data.get("rebalance", Rebalance.MONTHLY))),
            initial_capital=float(data.get("initial_capital", 1_000_000.0)),
            gross_exposure=float(data.get("gross_exposure", 1.0)),
            max_positions=(
                None if data.get("max_positions") in (None, "") else int(data["max_positions"])
            ),
            long_only=bool(data.get("long_only", True)),
            costs=CostModel.from_dict(data.get("costs")),
            oos_start=_parse_date(data.get("oos_start")),
            oos_fraction=None if oos_fraction in (None, "") else float(oos_fraction),
            risk_free_rate=float(data.get("risk_free_rate", 0.0)),
            periods_per_year=(
                None if data.get("periods_per_year") in (None, "") else int(data["periods_per_year"])
            ),
            trim_warmup=bool(data.get("trim_warmup", True)),
            interval=BarInterval(str(data.get("interval", BarInterval.DAY_1))),
            params=dict(data.get("params") or {}),
        )


def _parse_date(value: Any) -> date | None:
    """Coerce an ISO string / datetime / date to a :class:`datetime.date`."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()


# --------------------------------------------------------------------------
# statistics
# --------------------------------------------------------------------------
@dataclass(slots=True)
class BacktestStats:
    """Headline statistics of one backtest window.

    All annualised figures use ``periods_per_year``; ``years`` is the number of
    return periods divided by that figure, so the numbers are exactly
    reproducible from the return series alone.
    """

    periods: int
    years: float
    total_return: float
    cagr: float
    volatility: float
    sharpe: float
    sortino: float
    max_drawdown: float
    hit_rate: float
    turnover: float
    trades: int
    best_period: float | None = None
    worst_period: float | None = None
    benchmark_return: float | None = None
    benchmark_cagr: float | None = None
    excess_return: float | None = None
    information_ratio: float | None = None
    alpha_vs_benchmark: float | None = None
    beta_vs_benchmark: float | None = None
    start: str | None = None
    end: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return the statistics as a JSON-serialisable mapping."""
        return asdict(self)


def infer_periods_per_year(index: pd.Index) -> int:
    """Infer the annualisation factor from the median spacing of ``index``.

    Daily bars (including business-day gaps) give 252; weekly 52; monthly 12;
    quarterly 4. Anything else is scaled from 365 calendar days. A series with
    fewer than three points cannot be inferred and falls back to daily.
    """
    if len(index) < 3:
        return TRADING_DAYS_PER_YEAR
    values = pd.DatetimeIndex(index)
    gaps = values.to_series().diff().dropna()
    if gaps.empty:
        return TRADING_DAYS_PER_YEAR
    median_days = float(gaps.dt.total_seconds().median()) / 86_400.0
    if median_days <= 0:
        return TRADING_DAYS_PER_YEAR
    if median_days <= 4.0:
        return TRADING_DAYS_PER_YEAR
    if median_days <= 10.0:
        return 52
    if median_days <= 45.0:
        return 12
    if median_days <= 120.0:
        return 4
    return max(1, int(round(365.0 / median_days)))


def max_drawdown(returns: pd.Series) -> float:
    """Return the worst peak-to-trough drawdown of a return series (``<= 0``)."""
    if returns.empty:
        return 0.0
    equity = (1.0 + returns.astype("float64")).cumprod()
    peak = equity.cummax()
    return float((equity / peak - 1.0).min())


def drawdown_series(equity: pd.Series) -> pd.Series:
    """Return the running drawdown of an equity curve (``<= 0``)."""
    peak = equity.cummax()
    return equity / peak - 1.0


def compute_stats(
    returns: pd.Series,
    *,
    benchmark: pd.Series | None = None,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
    turnover: pd.Series | float | None = None,
    trades: int = 0,
    risk_free_rate: float = 0.0,
) -> BacktestStats:
    """Compute headline statistics from a period return series.

    Args:
        returns: net-of-cost period returns (not cumulative).
        benchmark: benchmark period returns; aligned on the index intersection.
        periods_per_year: annualisation factor.
        turnover: traded notional per period (fraction of the book), or an
            already-annualised float.
        trades: number of individual symbol trades executed in the window.
        risk_free_rate: annual risk-free rate used by the Sharpe ratio.

    Returns:
        The populated :class:`BacktestStats`. An empty or single-observation
        series yields zeros rather than NaN - "not enough data" must render.
    """
    series = returns.astype("float64").dropna()
    count = int(series.size)
    years = count / float(periods_per_year) if count else 0.0
    if count == 0:
        return BacktestStats(
            periods=0,
            years=0.0,
            total_return=0.0,
            cagr=0.0,
            volatility=0.0,
            sharpe=0.0,
            sortino=0.0,
            max_drawdown=0.0,
            hit_rate=0.0,
            turnover=0.0,
            trades=int(trades),
        )

    total_return = float((1.0 + series).prod() - 1.0)
    cagr = (
        float((1.0 + total_return) ** (1.0 / years) - 1.0)
        if years > 0 and total_return > -1.0
        else (-1.0 if total_return <= -1.0 else 0.0)
    )
    std = float(series.std(ddof=1)) if count > 1 else 0.0
    volatility = std * math.sqrt(periods_per_year)
    rf_period = risk_free_rate / float(periods_per_year)
    mean = float(series.mean())
    sharpe = ((mean - rf_period) / std * math.sqrt(periods_per_year)) if std > 0 else 0.0
    downside = float(np.sqrt(np.mean(np.minimum(series.to_numpy(), 0.0) ** 2)))
    sortino = ((mean - rf_period) / downside * math.sqrt(periods_per_year)) if downside > 0 else 0.0
    hit_rate = float((series > 0).sum()) / count

    if isinstance(turnover, pd.Series):
        turnover_annual = float(turnover.astype("float64").mean()) * periods_per_year
    elif turnover is None:
        turnover_annual = 0.0
    else:
        turnover_annual = float(turnover)

    stats = BacktestStats(
        periods=count,
        years=years,
        total_return=total_return,
        cagr=cagr,
        volatility=volatility,
        sharpe=sharpe,
        sortino=sortino,
        max_drawdown=max_drawdown(series),
        hit_rate=hit_rate,
        turnover=turnover_annual,
        trades=int(trades),
        best_period=float(series.max()),
        worst_period=float(series.min()),
        start=_stamp(series.index[0]),
        end=_stamp(series.index[-1]),
    )

    if benchmark is not None and not benchmark.empty:
        bench = benchmark.astype("float64").reindex(series.index).dropna()
        joined = series.reindex(bench.index)
        if bench.size > 1 and joined.notna().all():
            bench_total = float((1.0 + bench).prod() - 1.0)
            bench_years = bench.size / float(periods_per_year)
            stats.benchmark_return = bench_total
            stats.benchmark_cagr = (
                float((1.0 + bench_total) ** (1.0 / bench_years) - 1.0)
                if bench_years > 0 and bench_total > -1.0
                else None
            )
            strategy_total = float((1.0 + joined).prod() - 1.0)
            stats.excess_return = strategy_total - bench_total
            excess = joined - bench
            excess_std = float(excess.std(ddof=1))
            stats.information_ratio = (
                float(excess.mean() / excess_std * math.sqrt(periods_per_year))
                if excess_std > 0
                else 0.0
            )
            bench_var = float(bench.var(ddof=1))
            if bench_var > 0:
                covariance = float(np.cov(joined.to_numpy(), bench.to_numpy(), ddof=1)[0, 1])
                beta = covariance / bench_var
                stats.beta_vs_benchmark = beta
                stats.alpha_vs_benchmark = (
                    float(joined.mean()) - beta * float(bench.mean())
                ) * periods_per_year
    return stats


def _stamp(value: Any) -> str:
    """Render an index label as an ISO-8601 string."""
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


# --------------------------------------------------------------------------
# trade ledger
# --------------------------------------------------------------------------
@dataclass(slots=True)
class TradeRecord:
    """One round trip in a single symbol.

    A trade *opens* on the first bar whose target weight for the symbol is
    non-zero and *closes* on the first later bar whose weight returns to zero.
    Weight changes that leave a non-zero position are rebalances, not trades, so
    they do not appear here - which is why ``len(trades)`` is normally smaller
    than :attr:`BacktestStats.trades` (that counts every weight change).

    ``return_pct`` is the symbol's own price return over the holding window,
    signed by the side. ``contribution`` is what the trade actually added to the
    portfolio return: the sum of ``weight_t x symbol_return_{t+1}`` across the
    bars the position was held, gross of costs.
    """

    number: int
    symbol: str
    side: str
    entry: str
    exit: str | None
    entry_price: float | None
    exit_price: float | None
    bars: int
    weight: float
    return_pct: float | None
    contribution: float
    reason: str
    open: bool

    def as_dict(self) -> dict[str, Any]:
        """Return the trade as a JSON-serialisable mapping."""
        return asdict(self)


def build_trade_ledger(
    positions: pd.DataFrame,
    prices: pd.DataFrame,
    *,
    epsilon: float = TRADE_EPSILON,
) -> list[TradeRecord]:
    """Derive the round-trip trade ledger from a held-weight panel.

    Args:
        positions: weights actually held, indexed by bar (the panel produced by
            :meth:`Backtester.run`, *not* the target weights).
        prices: the matching close-price panel.
        epsilon: weights at or below this magnitude count as flat.

    Returns:
        Trades ordered newest first (the order the Strategy Lab's trade log
        renders), numbered oldest-first so ``#1`` is the first trade taken.
    """
    if positions.empty:
        return []
    aligned = prices.reindex(index=positions.index, columns=positions.columns)
    index = list(positions.index)
    last = len(index) - 1
    collected: list[TradeRecord] = []
    for symbol in positions.columns:
        weights = positions[symbol].to_numpy(dtype="float64")
        closes = aligned[symbol].to_numpy(dtype="float64")
        symbol_returns = aligned[symbol].pct_change().to_numpy(dtype="float64")
        start: int | None = None
        for i in range(len(weights)):
            live = abs(weights[i]) > epsilon
            if live and start is None:
                start = i
            elif not live and start is not None:
                collected.append(
                    _close_trade(
                        str(symbol), weights, closes, symbol_returns, index, start, i, last
                    )
                )
                start = None
        if start is not None:
            collected.append(
                _close_trade(
                    str(symbol), weights, closes, symbol_returns, index, start, last, last
                )
            )
    collected.sort(key=lambda trade: (trade.entry, trade.symbol))
    for number, trade in enumerate(collected, start=1):
        trade.number = number
    collected.reverse()
    return collected


def _close_trade(
    symbol: str,
    weights: np.ndarray,
    closes: np.ndarray,
    symbol_returns: np.ndarray,
    index: Sequence[Any],
    start: int,
    end: int,
    last: int,
) -> TradeRecord:
    """Build one :class:`TradeRecord` for the window ``[start, end]``.

    Every field is coerced to a builtin scalar: these records go straight into a
    SQLite JSON column, and a ``numpy.bool_`` or ``numpy.float64`` leaking
    through fails to serialise there.
    """
    still_open = bool(end >= last and abs(float(weights[end])) > TRADE_EPSILON)
    entry_price = None if math.isnan(closes[start]) else float(closes[start])
    exit_price = None if math.isnan(closes[end]) else float(closes[end])
    side = "long" if float(weights[start]) > 0 else "short"
    held = weights[start:end]
    weight = float(np.abs(held).mean()) if held.size else float(abs(weights[start]))
    return_pct: float | None = None
    if entry_price and exit_price and entry_price != 0.0:
        raw = exit_price / entry_price - 1.0
        return_pct = raw if side == "long" else -raw
    slice_returns = np.nan_to_num(symbol_returns[start + 1 : end + 1], nan=0.0)
    contribution = float(np.dot(weights[start:end], slice_returns)) if held.size else 0.0
    if still_open:
        reason = "open"
    elif exit_price is None:
        reason = "no price"
    else:
        reason = "signal"
    return TradeRecord(
        number=0,
        symbol=symbol,
        side=side,
        entry=_stamp(index[start])[:10],
        exit=None if still_open else _stamp(index[end])[:10],
        entry_price=entry_price,
        exit_price=None if still_open else exit_price,
        bars=int(end - start),
        weight=weight,
        return_pct=return_pct,
        contribution=contribution,
        reason=reason,
        open=still_open,
    )


def _panel_fingerprint(panel: pd.DataFrame) -> dict[str, Any]:
    """Return a content hash plus shape metadata for a price panel.

    The digest covers the actual float bytes, so two runs share a fingerprint
    only when they saw byte-identical prices - which is the point of pinning a
    backtest's data half in an artifact bundle.
    """
    values = np.ascontiguousarray(panel.to_numpy(dtype="float64"))
    digest = hashlib.sha256(values.tobytes())
    digest.update("|".join(str(c) for c in panel.columns).encode())
    return {
        "sha256": digest.hexdigest(),
        "rows": int(panel.shape[0]),
        "columns": int(panel.shape[1]),
        "symbols": [str(c) for c in panel.columns],
        "first_bar": _stamp(panel.index[0]) if len(panel.index) else None,
        "last_bar": _stamp(panel.index[-1]) if len(panel.index) else None,
    }


def _code_fingerprint() -> dict[str, Any]:
    """Return a digest of the engine source that produced a result.

    Two runs are only comparable when the same code produced them; the Strategy
    Lab shows this hash so a stale comparison is visible rather than assumed.
    """
    global _CODE_FINGERPRINT
    if _CODE_FINGERPRINT is None:
        digest = hashlib.sha256()
        paths = sorted(Path(__file__).parent.glob("*.py"))
        for path in paths:
            try:
                digest.update(path.read_bytes())
            except OSError:  # pragma: no cover - unreadable source file
                logger.debug("could not hash %s for the code fingerprint", path)
        _CODE_FINGERPRINT = {
            "sha256": digest.hexdigest(),
            "modules": [path.name for path in paths],
            "tradinglib_version": _tradinglib_version(),
        }
    return dict(_CODE_FINGERPRINT)


def _tradinglib_version() -> str:
    """Return the installed TradingLib version string (``unknown`` if absent)."""
    try:
        from tradinglib import __version__

        return str(__version__)
    except Exception:  # noqa: BLE001 - version metadata is optional
        return "unknown"


_CODE_FINGERPRINT: dict[str, Any] | None = None


def _config_fingerprint(config: BacktestConfig) -> dict[str, Any]:
    """Return a canonical digest of the reproducible configuration."""
    canonical = json.dumps(config.as_dict(), sort_keys=True, separators=(",", ":"))
    return {
        "sha256": hashlib.sha256(canonical.encode()).hexdigest(),
        "bytes": len(canonical),
    }


# --------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------
@dataclass(slots=True)
class BacktestResult:
    """Complete backtest output: curves, stats and per-window breakdown."""

    config: BacktestConfig
    ran_at: datetime
    equity_curve: pd.Series
    benchmark_curve: pd.Series | None
    returns: pd.Series
    benchmark_returns: pd.Series | None
    positions: pd.DataFrame
    turnover: pd.Series
    trade_counts: pd.Series
    stats: BacktestStats
    periods_per_year: int
    in_sample: BacktestStats | None = None
    out_of_sample: BacktestStats | None = None
    oos_start: date | None = None
    symbols: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    run_id: str | None = None
    trades: list[TradeRecord] = field(default_factory=list)
    data_fingerprint: dict[str, Any] = field(default_factory=dict)

    @property
    def drawdown(self) -> pd.Series:
        """Running drawdown of the equity curve."""
        return drawdown_series(self.equity_curve)

    def curve_payload(self, *, max_points: int | None = None) -> dict[str, Any]:
        """Return the chartable equity-curve payload persisted with the run.

        Args:
            max_points: when set, the curve is thinned by taking every ``k``-th
                point (the last point is always kept) so a very long backtest
                stays cheap to ship to the browser.
        """
        equity = self.equity_curve
        benchmark = self.benchmark_curve
        step = 1
        if max_points and equity.size > max_points:
            step = int(math.ceil(equity.size / max_points))
        sampled = equity.iloc[::step]
        if sampled.index[-1] != equity.index[-1]:
            sampled = pd.concat([sampled, equity.iloc[[-1]]])
        payload: dict[str, Any] = {
            "dates": [_stamp(ts) for ts in sampled.index],
            "equity": [float(v) for v in sampled.to_numpy()],
            "drawdown": [float(v) for v in drawdown_series(equity).reindex(sampled.index)],
            "initial_capital": float(self.config.initial_capital),
            "oos_start": self.oos_start.isoformat() if self.oos_start else None,
            # The returns CSV export is derived from this curve, so it has to be
            # able to say whether it is every bar or an evenly thinned sample.
            "sampled": step > 1,
            "step": step,
            "bars": int(equity.size),
        }
        if benchmark is not None and not benchmark.empty:
            payload["benchmark"] = [
                None if pd.isna(v) else float(v)
                for v in benchmark.reindex(sampled.index).to_numpy()
            ]
            payload["benchmark_symbol"] = self.config.benchmark
        return payload

    def trade_log(self, *, limit: int | None = 500) -> dict[str, Any]:
        """Return the trade-log payload the Strategy Lab's ``Trade log`` renders.

        Args:
            limit: keep at most this many (most recent) trades in ``rows``; the
                totals always describe the whole ledger.
        """
        rows = self.trades if limit is None else self.trades[:limit]
        closed = [t for t in self.trades if not t.open and t.return_pct is not None]
        wins = [t for t in closed if (t.return_pct or 0.0) > 0]
        return {
            "total": len(self.trades),
            "closed": len(closed),
            "open": sum(1 for t in self.trades if t.open),
            "shown": len(rows),
            "win_rate": (len(wins) / len(closed)) if closed else None,
            "average_return": (
                float(np.mean([t.return_pct or 0.0 for t in closed])) if closed else None
            ),
            "average_bars": (float(np.mean([t.bars for t in closed])) if closed else None),
            "rows": [trade.as_dict() for trade in rows],
        }

    def artifact_bundle(self) -> dict[str, Any]:
        """Return the pinned ``cfg + data + code`` bundle for this run.

        The three digests together identify everything that determined the
        numbers: the configuration, the exact price bytes the engine saw, and the
        research-engine source that processed them. Re-running with all three
        unchanged reproduces the result exactly.
        """
        config = _config_fingerprint(self.config)
        data = dict(self.data_fingerprint)
        code = _code_fingerprint()
        combined = hashlib.sha256(
            "|".join(
                [config["sha256"], str(data.get("sha256", "")), code["sha256"]]
            ).encode()
        ).hexdigest()
        return {
            "bundle_id": combined[:16],
            "sha256": combined,
            "pinned_at": self.ran_at.isoformat(),
            "config": config,
            "data": data,
            "code": code,
        }

    def as_dict(self, *, max_points: int | None = 2000) -> dict[str, Any]:
        """Return the JSON payload the Strategy Lab renders."""
        return {
            "run_id": self.run_id,
            "name": self.config.name,
            "ran_at": self.ran_at.isoformat(),
            "config": self.config.as_dict(),
            "signal": self.config.signal.describe(),
            "periods_per_year": self.periods_per_year,
            "symbols": list(self.symbols),
            "stats": self.stats.as_dict(),
            "in_sample": self.in_sample.as_dict() if self.in_sample else None,
            "out_of_sample": self.out_of_sample.as_dict() if self.out_of_sample else None,
            "oos_start": self.oos_start.isoformat() if self.oos_start else None,
            "equity_curve": self.curve_payload(max_points=max_points),
            "warnings": list(self.warnings),
            "trade_log": self.trade_log(),
            "artifact_bundle": self.artifact_bundle(),
        }


# --------------------------------------------------------------------------
# the engine
# --------------------------------------------------------------------------
class Backtester:
    """Runs vectorised backtests over a price panel. Pure computation, no I/O."""

    def build_signals(
        self,
        spec: SignalSpec,
        prices: pd.DataFrame,
        *,
        volumes: pd.DataFrame | None = None,
    ) -> pd.DataFrame:
        """Return the raw signal panel implied by ``spec``.

        Higher is better in every built-in kind (a reversal signal is already
        negated), so selection is always "take the largest".

        Raises:
            ExpressionError: when an expression is malformed.
            ValueError: when the spec is internally inconsistent.
        """
        close = prices.astype("float64")
        if spec.kind is SignalKind.STATIC:
            if not spec.symbols:
                raise ValueError("a static signal needs at least one symbol")
            signal = equal_weight_signals(close, spec.symbols)
        elif spec.kind is SignalKind.MOMENTUM:
            signal = close.shift(spec.skip) / close.shift(spec.lookback) - 1.0
        elif spec.kind is SignalKind.MEAN_REVERSION:
            signal = -(close / close.shift(spec.lookback) - 1.0)
        elif spec.kind is SignalKind.MA_CROSS:
            signal = close.rolling(spec.fast).mean() / close.rolling(spec.slow).mean() - 1.0
        elif spec.kind is SignalKind.LOW_VOLATILITY:
            window = max(2, spec.lookback)
            signal = -(close.pct_change().rolling(window).std(ddof=1))
        else:
            if not spec.expression:
                raise ValueError("an expression signal needs an expression")
            namespace = build_factor_panels(close, volumes)
            signal = compile_expression(spec.expression, namespace)

        signal = signal.replace([np.inf, -np.inf], np.nan)
        if spec.filter_expression:
            namespace = build_factor_panels(close, volumes)
            mask = compile_expression(spec.filter_expression, namespace)
            signal = signal.where(mask.fillna(False).astype(bool))
        # A name without a price cannot be held, whatever the signal says.
        return signal.where(close.notna())

    def build_positions(
        self,
        signals: pd.DataFrame,
        spec: SignalSpec,
        config: BacktestConfig,
        *,
        prices: pd.DataFrame | None = None,
    ) -> pd.DataFrame:
        """Turn raw signals into normalised, constraint-respecting target weights.

        Selection per bar: drop missing signals, drop anything at or below
        ``min_signal``, keep the ``top_n`` largest. Long-only signal weighting
        additionally drops non-positive signals, because a positive-only budget
        cannot express them.

        Weighting: ``signal`` allocates proportionally to the signal value,
        ``equal`` splits the budget evenly, ``inverse_vol`` allocates
        proportionally to ``1 / 20-day realised volatility``. A long/short book
        (``long_only=False``) with signal weighting is built from the
        cross-sectionally demeaned signal and scaled to ``gross_exposure``.
        """
        frame = signals.astype("float64").replace([np.inf, -np.inf], np.nan)
        if spec.min_signal is not None:
            frame = frame.where(frame > spec.min_signal)
        if config.long_only and spec.weighting is Weighting.SIGNAL:
            frame = frame.where(frame > 0.0)

        cap = config.effective_top_n()
        if cap:
            ranks = frame.rank(axis=1, ascending=False, method="first", na_option="keep")
            frame = frame.where(ranks <= cap)

        selected = frame.notna()
        if spec.weighting is Weighting.EQUAL:
            counts = selected.sum(axis=1)
            weights = selected.astype("float64").div(counts.replace(0, np.nan), axis=0)
        elif spec.weighting is Weighting.INVERSE_VOL:
            if prices is None:
                raise ValueError("inverse-volatility weighting needs the price panel")
            vol = prices.astype("float64").pct_change().rolling(20).std(ddof=1)
            inverse = (1.0 / vol.replace(0.0, np.nan)).where(selected)
            weights = inverse.div(inverse.sum(axis=1).replace(0.0, np.nan), axis=0)
            # A name without a volatility estimate yet falls back to equal weight
            # rather than silently dropping out of an otherwise valid basket.
            missing = selected & inverse.isna()
            if bool(missing.to_numpy().any()):
                counts = selected.sum(axis=1)
                equal = selected.astype("float64").div(counts.replace(0, np.nan), axis=0)
                rows = missing.any(axis=1)
                weights.loc[rows] = equal.loc[rows]
        elif config.long_only:
            positive = frame.where(frame > 0.0)
            weights = positive.div(positive.sum(axis=1).replace(0.0, np.nan), axis=0)
        else:
            demeaned = frame.sub(frame.mean(axis=1), axis=0)
            weights = demeaned.div(demeaned.abs().sum(axis=1).replace(0.0, np.nan), axis=0)

        weights = weights.fillna(0.0) * float(config.gross_exposure)
        return weights.reindex(columns=signals.columns).fillna(0.0)

    def rebalance_mask(self, index: pd.DatetimeIndex, rebalance: Rebalance) -> np.ndarray:
        """Return a boolean array marking the bars on which trading happens.

        The first bar is always a rebalance (the position has to be opened).
        Weekly/monthly/quarterly rebalances happen on the first available bar of
        each calendar period, so a holiday shifts the trade to the next session
        rather than skipping it.
        """
        flags = np.zeros(len(index), dtype=bool)
        if len(index) == 0:
            return flags
        flags[0] = True
        if rebalance is Rebalance.DAILY:
            flags[:] = True
            return flags
        if rebalance is Rebalance.WEEKLY:
            keys = [(ts.isocalendar().year, ts.isocalendar().week) for ts in index]
        elif rebalance is Rebalance.MONTHLY:
            keys = [(ts.year, ts.month) for ts in index]
        else:
            keys = [(ts.year, (ts.month - 1) // 3) for ts in index]
        previous = keys[0]
        for position in range(1, len(index)):
            if keys[position] != previous:
                flags[position] = True
                previous = keys[position]
        return flags

    def run(
        self,
        config: BacktestConfig,
        prices: pd.DataFrame,
        *,
        signals: pd.DataFrame | None = None,
        benchmark: pd.Series | None = None,
        volumes: pd.DataFrame | None = None,
    ) -> BacktestResult:
        """Backtest ``config`` over ``prices``.

        Args:
            config: the reproducible configuration.
            prices: close prices, index = bar timestamps, columns = symbols.
            signals: pre-computed signals (screen survivors, agent output). When
                omitted they are built from ``config.signal``.
            benchmark: benchmark close prices aligned to ``prices``' index.
            volumes: matching volume panel; only needed by factors that use it.

        Returns:
            A fully populated :class:`BacktestResult`.

        Raises:
            ValueError: when the panel is empty or has fewer than two bars.
        """
        panel = _clean_panel(prices)
        if panel.empty or panel.shape[0] < 2:
            raise ValueError("a backtest needs at least two bars of price history")

        warnings: list[str] = []
        raw_signals = (
            self.build_signals(config.signal, panel, volumes=volumes)
            if signals is None
            else signals.reindex(index=panel.index, columns=panel.columns).astype("float64")
        )
        targets = self.build_positions(raw_signals, config.signal, config, prices=panel)

        active = targets.abs().sum(axis=1) > 0
        if not bool(active.any()):
            raise ValueError(
                "the signal never selects a position - check the expression, the "
                "filter and whether the price history covers the warm-up window"
            )
        if config.trim_warmup:
            first = int(np.argmax(active.to_numpy()))
            if first > 0:
                warnings.append(
                    f"warm-up: the first {first} bars held no position and were trimmed"
                )
                panel = panel.iloc[first:]
                targets = targets.iloc[first:]
                raw_signals = raw_signals.iloc[first:]

        returns_panel = panel.pct_change()
        extreme = returns_panel.abs() > CORPORATE_ACTION_RETURN
        if bool(extreme.to_numpy().any()):
            for symbol in panel.columns[extreme.any(axis=0)]:
                when = extreme.index[extreme[symbol].to_numpy()][0]
                warnings.append(
                    f"{symbol}: {abs(float(returns_panel.loc[when, symbol])):.0%} single-bar move "
                    f"on {_stamp(when)[:10]} - probable unadjusted corporate action"
                )
        missing = [str(c) for c in panel.columns if panel[c].notna().sum() < 2]
        if missing:
            warnings.append(f"no usable price history for: {', '.join(sorted(missing))}")

        periods_per_year = config.periods_per_year or infer_periods_per_year(panel.index)
        matrix_returns = returns_panel.fillna(0.0).to_numpy()
        matrix_targets = targets.to_numpy()
        flags = self.rebalance_mask(pd.DatetimeIndex(panel.index), config.rebalance)

        rows, columns = matrix_targets.shape
        held = matrix_targets[0].copy()
        positions = np.zeros((rows, columns), dtype="float64")
        positions[0] = held
        traded = np.zeros(rows, dtype="float64")
        traded[0] = float(np.abs(held).sum())
        net = np.zeros(rows, dtype="float64")
        trade_counts = np.zeros(rows, dtype="int64")
        trade_counts[0] = int((np.abs(held) > TRADE_EPSILON).sum())
        rate = config.costs.rate

        for i in range(1, rows):
            bar_returns = matrix_returns[i]
            gross = float(held @ bar_returns)
            growth = 1.0 + gross
            if abs(growth) > 1e-12:
                drifted = held * (1.0 + bar_returns) / growth
            else:  # pragma: no cover - a -100% bar wipes the book out
                drifted = np.zeros(columns, dtype="float64")
            short_exposure = float(-np.minimum(held, 0.0).sum())
            cost = traded[i - 1] * rate + config.costs.borrow_cost(
                short_exposure, periods_per_year
            )
            net[i] = gross - cost
            if flags[i] and i < rows - 1:
                target = matrix_targets[i]
                delta = np.abs(target - drifted)
                traded[i] = float(delta.sum())
                trade_counts[i] = int((delta > TRADE_EPSILON).sum())
                held = target.copy()
            else:
                traded[i] = 0.0
                held = drifted
            positions[i] = held

        index = panel.index
        returns = pd.Series(net[1:], index=index[1:], name="return")
        # A trade executed at the close of bar i is charged to bar i+1, so both
        # the traded notional and the trade count are aligned one bar forward.
        turnover = pd.Series(traded[:-1], index=index[1:], name="turnover")
        trades_series = pd.Series(trade_counts[:-1], index=index[1:], name="trades")
        equity = pd.Series(
            config.initial_capital * np.cumprod(1.0 + net), index=index, name="equity"
        )
        position_frame = pd.DataFrame(positions, index=index, columns=panel.columns)

        benchmark_returns: pd.Series | None = None
        benchmark_curve: pd.Series | None = None
        if benchmark is not None and not benchmark.empty:
            aligned = benchmark.astype("float64").reindex(index).ffill()
            benchmark_returns = aligned.pct_change().reindex(index[1:])
            if benchmark_returns.notna().sum() < 2:
                warnings.append(
                    f"benchmark {config.benchmark!r} has no usable history in this window"
                )
                benchmark_returns = None
            else:
                # The first bar has no prior close, so exactly one gap is
                # structural; anything more is a hole in the benchmark history
                # and must be said out loud rather than filled in silently.
                gaps = int(benchmark_returns.isna().sum())
                if gaps > 1:
                    warnings.append(
                        f"benchmark {config.benchmark!r} is missing {gaps - 1} bar(s) inside "
                        "the window; those periods were treated as flat"
                    )
                benchmark_returns = benchmark_returns.fillna(0.0)
                growth = pd.concat(
                    [pd.Series([0.0], index=index[:1]), benchmark_returns]
                )
                benchmark_curve = pd.Series(
                    config.initial_capital * (1.0 + growth).cumprod(),
                    index=index,
                    name="benchmark",
                )

        stats = compute_stats(
            returns,
            benchmark=benchmark_returns,
            periods_per_year=periods_per_year,
            turnover=turnover,
            trades=int(trades_series.sum()),
            risk_free_rate=config.risk_free_rate,
        )

        result = BacktestResult(
            config=config,
            ran_at=utcnow(),
            equity_curve=equity,
            benchmark_curve=benchmark_curve,
            returns=returns,
            benchmark_returns=benchmark_returns,
            positions=position_frame,
            turnover=turnover,
            trade_counts=trades_series,
            stats=stats,
            periods_per_year=periods_per_year,
            symbols=[str(c) for c in panel.columns],
            warnings=warnings,
            trades=build_trade_ledger(position_frame, panel),
            data_fingerprint=_panel_fingerprint(panel),
        )
        oos_start = self.resolve_oos_start(config, returns.index)
        if oos_start is not None:
            result.oos_start = oos_start
            result.in_sample, result.out_of_sample = self.split_oos(result, oos_start)
        return result

    @staticmethod
    def resolve_oos_start(config: BacktestConfig, index: pd.Index) -> date | None:
        """Return the out-of-sample start date implied by the config.

        An explicit ``oos_start`` wins. Otherwise ``oos_fraction`` splits the
        return series so that the last fraction of *observations* is held out.
        ``None`` is returned when neither is configured or the window is too
        short for both halves to hold at least two observations.
        """
        if config.oos_start is not None:
            return config.oos_start
        fraction = config.oos_fraction
        if not fraction or fraction <= 0.0 or fraction >= 1.0 or len(index) < 8:
            return None
        cut = int(round(len(index) * (1.0 - fraction)))
        cut = max(2, min(cut, len(index) - 2))
        stamp = pd.Timestamp(index[cut])
        return stamp.date()

    def split_oos(
        self, result: BacktestResult, oos_start: date
    ) -> tuple[BacktestStats, BacktestStats]:
        """Split a completed backtest into in-sample and out-of-sample statistics."""
        index = pd.DatetimeIndex(result.returns.index)
        boundary = pd.Timestamp(oos_start)
        if index.tz is not None:
            boundary = boundary.tz_localize(index.tz)
        in_mask = np.asarray(index < boundary)
        out_mask = ~in_mask

        def window(mask: np.ndarray) -> BacktestStats:
            returns = result.returns[mask]
            benchmark = (
                result.benchmark_returns[mask] if result.benchmark_returns is not None else None
            )
            turnover = result.turnover[mask]
            trades = int(result.trade_counts[mask].sum())
            return compute_stats(
                returns,
                benchmark=benchmark,
                periods_per_year=result.periods_per_year,
                turnover=turnover,
                trades=trades,
                risk_free_rate=result.config.risk_free_rate,
            )

        return window(in_mask), window(out_mask)


def _clean_panel(prices: pd.DataFrame) -> pd.DataFrame:
    """Return a sorted, de-duplicated, float price panel with a datetime index."""
    frame = prices.copy()
    frame.index = pd.DatetimeIndex(frame.index)
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    frame = frame.astype("float64")
    return frame.dropna(axis=1, how="all")


# --------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------
class BacktestRunRow(Base):
    """One executed (or queued) backtest. Counted as a trial by ``/api/status``."""

    __tablename__ = "backtest_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    hypothesis_id: Mapped[str | None] = mapped_column(String(64), default=None, index=True)
    experiment_id: Mapped[str | None] = mapped_column(String(64), default=None, index=True)
    universe_id: Mapped[str | None] = mapped_column(String(64), default=None)
    screen_id: Mapped[str | None] = mapped_column(String(64), default=None)
    status: Mapped[str] = mapped_column(String(16), default=BacktestStatus.QUEUED, nullable=False)
    source: Mapped[str] = mapped_column(String(32), default="manual", nullable=False)
    spec: Mapped[dict[str, Any]] = mapped_column(JSON(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(), default=None)
    duration_ms: Mapped[float | None] = mapped_column(Float(), default=None)
    symbols: Mapped[list[str]] = mapped_column(JSON(), default=list, nullable=False)
    stats: Mapped[dict[str, Any] | None] = mapped_column(JSON(), default=None)
    in_sample: Mapped[dict[str, Any] | None] = mapped_column(JSON(), default=None)
    out_of_sample: Mapped[dict[str, Any] | None] = mapped_column(JSON(), default=None)
    equity_curve: Mapped[dict[str, Any] | None] = mapped_column(JSON(), default=None)
    trade_log: Mapped[dict[str, Any] | None] = mapped_column(JSON(), default=None)
    artifact_bundle: Mapped[dict[str, Any] | None] = mapped_column(JSON(), default=None)
    warnings: Mapped[list[str]] = mapped_column(JSON(), default=list, nullable=False)
    error: Mapped[str | None] = mapped_column(Text(), default=None)
    periods_per_year: Mapped[int | None] = mapped_column(Integer(), default=None)


_OWNED_TABLES = (BacktestRunRow.__table__,)


def ensure_schema() -> None:
    """Create the backtest tables if they do not exist yet, then migrate them."""
    Base.metadata.create_all(get_engine(), tables=list(_OWNED_TABLES))
    for table in _OWNED_TABLES:
        ensure_columns(table)


@dataclass(slots=True)
class BacktestRecord:
    """A persisted backtest run, as the API and the UI see it."""

    id: str
    name: str
    status: BacktestStatus
    config: BacktestConfig
    created_at: datetime
    hypothesis_id: str | None = None
    experiment_id: str | None = None
    universe_id: str | None = None
    screen_id: str | None = None
    source: str = "manual"
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: float | None = None
    symbols: list[str] = field(default_factory=list)
    stats: dict[str, Any] | None = None
    in_sample: dict[str, Any] | None = None
    out_of_sample: dict[str, Any] | None = None
    equity_curve: dict[str, Any] | None = None
    trade_log: dict[str, Any] | None = None
    artifact_bundle: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    periods_per_year: int | None = None

    def as_dict(self, *, include_curve: bool = False) -> dict[str, Any]:
        """Return a JSON-serialisable mapping for the API layer."""
        payload: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "status": str(self.status),
            "source": self.source,
            "hypothesis_id": self.hypothesis_id,
            "experiment_id": self.experiment_id,
            "universe_id": self.universe_id,
            "screen_id": self.screen_id,
            "config": self.config.as_dict(),
            "signal": self.config.signal.describe(),
            "benchmark": self.config.benchmark,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "duration_ms": self.duration_ms,
            "symbols": list(self.symbols),
            "symbol_count": len(self.symbols),
            "stats": self.stats,
            "in_sample": self.in_sample,
            "out_of_sample": self.out_of_sample,
            "warnings": list(self.warnings),
            "error": self.error,
            "periods_per_year": self.periods_per_year,
        }
        payload["artifact_bundle"] = self.artifact_bundle
        if include_curve:
            payload["equity_curve"] = self.equity_curve
            payload["trade_log"] = self.trade_log
        return payload


def _record_from_row(row: BacktestRunRow) -> BacktestRecord:
    """Map an ORM row onto the dataclass the API serves."""
    return BacktestRecord(
        id=row.id,
        name=row.name,
        status=BacktestStatus(row.status),
        config=BacktestConfig.from_dict(row.spec),
        created_at=to_utc(row.created_at),
        hypothesis_id=row.hypothesis_id,
        experiment_id=row.experiment_id,
        universe_id=row.universe_id,
        screen_id=row.screen_id,
        source=row.source,
        started_at=to_utc(row.started_at) if row.started_at else None,
        finished_at=to_utc(row.finished_at) if row.finished_at else None,
        duration_ms=row.duration_ms,
        symbols=list(row.symbols or []),
        stats=row.stats,
        in_sample=row.in_sample,
        out_of_sample=row.out_of_sample,
        equity_curve=row.equity_curve,
        trade_log=row.trade_log,
        artifact_bundle=row.artifact_bundle,
        warnings=list(row.warnings or []),
        error=row.error,
        periods_per_year=row.periods_per_year,
    )


class BacktestEngine:
    """Loads real prices, runs backtests, persists them and reports progress.

    This is the I/O half of the backtester: it reads bars from the Parquet +
    DuckDB store, resolves universes through
    :class:`~tradinglib.data.universe.UniverseRegistry`, writes runs to SQLite and
    publishes progress on the ``lab`` event channel.
    """

    def __init__(
        self,
        *,
        store: TimeSeriesStore | None = None,
        session_factory: sessionmaker[Session] | None = None,
        bus: EventBus | None = None,
        registry: Any | None = None,
    ) -> None:
        """Create the engine.

        Args:
            store: time-series store; defaults to the process-wide one.
            session_factory: SQLAlchemy session factory; defaults to
                :func:`tradinglib.db.get_session_factory`.
            bus: event bus progress is published on.
            registry: universe registry used to resolve ``universe_id``.
        """
        self._store = store
        self._session_factory = session_factory
        self._bus = bus
        self._registry = registry
        self.backtester = Backtester()
        ensure_schema()

    # --- plumbing ---------------------------------------------------------
    @property
    def store(self) -> TimeSeriesStore:
        """The time-series store bars are read from."""
        if self._store is None:
            self._store = get_store()
        return self._store

    @property
    def bus(self) -> EventBus:
        """The event bus run progress is published on."""
        if self._bus is None:
            self._bus = get_event_bus()
        return self._bus

    @property
    def registry(self) -> Any:
        """The universe registry (imported lazily to keep the import graph flat)."""
        if self._registry is None:
            from tradinglib.data.universe import get_universe_registry

            self._registry = get_universe_registry()
        return self._registry

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

    async def _publish(self, payload: dict[str, Any]) -> None:
        """Publish one progress event on the ``lab`` channel."""
        await self.bus.publish(LAB_CHANNEL, payload)

    # --- data -------------------------------------------------------------
    def resolve_symbols(self, config: BacktestConfig) -> list[str]:
        """Return the symbols a config trades, resolving its universe if needed.

        Explicit ``symbols`` win. Otherwise the universe is resolved
        point-in-time at ``config.start`` so that a backtest replays the
        membership that was real at the time, not today's.
        """
        if config.symbols:
            return [s.upper() for s in dict.fromkeys(config.symbols)]
        if config.signal.kind is SignalKind.STATIC and config.signal.symbols:
            return [s.upper() for s in dict.fromkeys(config.signal.symbols)]
        if not config.universe_id:
            raise ValueError("a backtest needs either explicit symbols or a universe")
        return list(self.registry.symbols(config.universe_id, as_of=config.start))

    def load_panel(
        self,
        symbols: Sequence[str],
        *,
        start: date | None = None,
        end: date | None = None,
        interval: BarInterval = BarInterval.DAY_1,
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Read close and volume panels for ``symbols`` from the store.

        Symbols with no stored history are simply absent from the returned
        panels; the caller reports them as a warning rather than inventing data.
        """
        closes: dict[str, pd.Series] = {}
        volumes: dict[str, pd.Series] = {}
        start_dt = datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc) if start else None
        end_dt = (
            datetime.combine(end, datetime.max.time().replace(microsecond=0), tzinfo=timezone.utc)
            if end
            else None
        )
        for symbol in symbols:
            frame = self.store.read_bars(symbol, interval=interval, start=start_dt, end=end_dt)
            if frame.empty:
                continue
            closes[symbol.upper()] = frame["close"].astype("float64")
            volumes[symbol.upper()] = frame["volume"].astype("float64")
        if not closes:
            return pd.DataFrame(), pd.DataFrame()
        price_panel = pd.DataFrame(closes).sort_index()
        volume_panel = pd.DataFrame(volumes).reindex(index=price_panel.index)
        return price_panel, volume_panel

    def load_benchmark(
        self,
        symbol: str,
        index: pd.Index,
        *,
        interval: BarInterval = BarInterval.DAY_1,
    ) -> pd.Series | None:
        """Return benchmark closes aligned to ``index``, or ``None`` when absent."""
        frame = self.store.read_bars(symbol, interval=interval)
        if frame.empty:
            return None
        series = frame["close"].astype("float64")
        series.index = pd.DatetimeIndex(series.index)
        return series.reindex(pd.DatetimeIndex(index).union(series.index)).ffill().reindex(index)

    def prepare(self, config: BacktestConfig) -> tuple[pd.DataFrame, pd.DataFrame, pd.Series | None, list[str]]:
        """Load everything a run needs: prices, volumes, benchmark and warnings.

        Raises:
            ValueError: when no symbol in the configuration has stored history.
        """
        symbols = self.resolve_symbols(config)
        prices, volumes = self.load_panel(
            symbols, start=config.start, end=config.end, interval=config.interval
        )
        warnings: list[str] = []
        if prices.empty:
            raise ValueError(
                "no stored price history for any of "
                f"{', '.join(symbols[:8])}{'…' if len(symbols) > 8 else ''} - ingest bars first"
            )
        missing = [s for s in symbols if s.upper() not in prices.columns]
        if missing:
            warnings.append(
                f"{len(missing)} symbol(s) have no stored history and were excluded: "
                + ", ".join(sorted(missing)[:12])
            )
        covered_start = prices.index[0].date()
        covered_end = prices.index[-1].date()
        # A panel narrower than the requested window silently changes what the
        # statistics describe (config says 11 years, stats cover 13 months).
        # Warn so the stored config and the result can never disagree quietly.
        if config.start and covered_start > config.start + timedelta(days=7):
            warnings.append(
                f"stored history starts {covered_start.isoformat()}, after the requested "
                f"start {config.start.isoformat()}; statistics cover the narrower window"
            )
        if config.end and covered_end < config.end - timedelta(days=7):
            warnings.append(
                f"stored history ends {covered_end.isoformat()}, before the requested "
                f"end {config.end.isoformat()}; statistics cover the narrower window"
            )
        benchmark: pd.Series | None = None
        if config.benchmark:
            benchmark = self.load_benchmark(config.benchmark, prices.index, interval=config.interval)
            if benchmark is None:
                warnings.append(
                    f"benchmark {config.benchmark} has no stored history; "
                    "benchmark-relative statistics are unavailable"
                )
        return prices, volumes, benchmark, warnings

    # --- running ----------------------------------------------------------
    def run_sync(self, config: BacktestConfig) -> BacktestResult:
        """Load data and run a backtest synchronously (no persistence, no events)."""
        prices, volumes, benchmark, warnings = self.prepare(config)
        result = self.backtester.run(config, prices, benchmark=benchmark, volumes=volumes)
        result.warnings = warnings + result.warnings
        return result

    async def run(
        self,
        config: BacktestConfig,
        *,
        hypothesis_id: str | None = None,
        experiment_id: str | None = None,
        screen_id: str | None = None,
        source: str = "manual",
        run_id: str | None = None,
        persist: bool = True,
        publish: bool = True,
    ) -> BacktestRecord:
        """Run a backtest, persist it and publish progress on the ``lab`` channel.

        Returns:
            The persisted :class:`BacktestRecord`, including the equity curve.
        """
        record, _ = await self.run_with_result(
            config,
            hypothesis_id=hypothesis_id,
            experiment_id=experiment_id,
            screen_id=screen_id,
            source=source,
            run_id=run_id,
            persist=persist,
            publish=publish,
        )
        return record

    async def run_with_result(
        self,
        config: BacktestConfig,
        *,
        hypothesis_id: str | None = None,
        experiment_id: str | None = None,
        screen_id: str | None = None,
        source: str = "manual",
        run_id: str | None = None,
        persist: bool = True,
        publish: bool = True,
    ) -> tuple[BacktestRecord, BacktestResult]:
        """Run a backtest and return both the persisted record and the live result.

        The computation itself runs on a worker thread, so the event loop stays
        responsive while a long backtest is in flight. Callers that need the
        in-memory curves and return series (the experiment engine, the
        validation gates) use this instead of re-running the same work.

        A failure is *recorded* (status ``failed`` with the error message)
        rather than swallowed, and the exception is re-raised.
        """
        identifier = run_id or new_backtest_id()
        started = utcnow()
        if persist:
            self._insert(
                identifier,
                config,
                hypothesis_id=hypothesis_id,
                experiment_id=experiment_id,
                screen_id=screen_id,
                source=source,
                status=BacktestStatus.RUNNING,
                started_at=started,
            )
        if publish:
            await self._publish(
                {
                    "kind": "backtest",
                    "phase": "started",
                    "id": identifier,
                    "name": config.name,
                    "hypothesis_id": hypothesis_id,
                    "experiment_id": experiment_id,
                    "signal": config.signal.describe(),
                    "ts": started.isoformat(),
                }
            )
        try:
            result = await asyncio.to_thread(self.run_sync, config)
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            logger.warning("backtest %s failed: %s", identifier, message)
            if persist:
                self._finish_failed(identifier, message)
            if publish:
                await self._publish(
                    {
                        "kind": "backtest",
                        "phase": "failed",
                        "id": identifier,
                        "name": config.name,
                        "error": message,
                        "ts": utcnow().isoformat(),
                    }
                )
            raise
        result.run_id = identifier
        duration_ms = (utcnow() - started).total_seconds() * 1000.0
        record = self._finish_complete(
            identifier,
            config,
            result,
            duration_ms=duration_ms,
            persist=persist,
            hypothesis_id=hypothesis_id,
            experiment_id=experiment_id,
            screen_id=screen_id,
            source=source,
            started_at=started,
        )
        if publish:
            await self._publish(
                {
                    "kind": "backtest",
                    "phase": "complete",
                    "id": identifier,
                    "name": config.name,
                    "hypothesis_id": hypothesis_id,
                    "experiment_id": experiment_id,
                    "stats": result.stats.as_dict(),
                    "out_of_sample": (
                        result.out_of_sample.as_dict() if result.out_of_sample else None
                    ),
                    "warnings": result.warnings,
                    "duration_ms": duration_ms,
                    "ts": utcnow().isoformat(),
                }
            )
        return record, result

    async def rerun(
        self, config: BacktestConfig, *, cost_multiplier: float = 1.0
    ) -> BacktestResult:
        """Re-run a configuration with scaled costs, without persisting anything.

        This is what the cost-sensitivity gate uses: the same signals, the same
        window, only the cost model changes.
        """
        scaled = BacktestConfig.from_dict(config.as_dict())
        scaled.costs = config.costs.scaled(cost_multiplier)
        return await asyncio.to_thread(self.run_sync, scaled)

    # --- persistence ------------------------------------------------------
    def _insert(
        self,
        identifier: str,
        config: BacktestConfig,
        *,
        hypothesis_id: str | None,
        experiment_id: str | None,
        screen_id: str | None,
        source: str,
        status: BacktestStatus,
        started_at: datetime | None = None,
    ) -> None:
        """Insert (or reset) the row for a run that is about to execute."""
        now = utcnow()
        with self._session() as session:
            row = session.get(BacktestRunRow, identifier)
            if row is None:
                row = BacktestRunRow(id=identifier, name=config.name, created_at=now)
                session.add(row)
            row.name = config.name
            row.hypothesis_id = hypothesis_id
            row.experiment_id = experiment_id
            row.universe_id = config.universe_id
            row.screen_id = screen_id
            row.source = source
            row.status = str(status)
            row.spec = config.as_dict()
            row.started_at = started_at
            row.finished_at = None
            row.error = None
            row.warnings = []
            row.symbols = list(config.symbols)
            row.trade_log = None
            row.artifact_bundle = None

    def _finish_failed(self, identifier: str, message: str) -> None:
        """Record a failed run honestly instead of deleting it."""
        with self._session() as session:
            row = session.get(BacktestRunRow, identifier)
            if row is None:
                return
            row.status = str(BacktestStatus.FAILED)
            row.finished_at = utcnow()
            row.error = message

    def _finish_complete(
        self,
        identifier: str,
        config: BacktestConfig,
        result: BacktestResult,
        *,
        duration_ms: float,
        persist: bool,
        hypothesis_id: str | None,
        experiment_id: str | None,
        screen_id: str | None,
        source: str,
        started_at: datetime,
    ) -> BacktestRecord:
        """Write the finished run and return the record."""
        curve = result.curve_payload(max_points=4000)
        trade_log = result.trade_log()
        bundle = result.artifact_bundle()
        record = BacktestRecord(
            id=identifier,
            name=config.name,
            status=BacktestStatus.COMPLETE,
            config=config,
            created_at=started_at,
            hypothesis_id=hypothesis_id,
            experiment_id=experiment_id,
            universe_id=config.universe_id,
            screen_id=screen_id,
            source=source,
            started_at=started_at,
            finished_at=utcnow(),
            duration_ms=duration_ms,
            symbols=list(result.symbols),
            stats=result.stats.as_dict(),
            in_sample=result.in_sample.as_dict() if result.in_sample else None,
            out_of_sample=result.out_of_sample.as_dict() if result.out_of_sample else None,
            equity_curve=curve,
            trade_log=trade_log,
            artifact_bundle=bundle,
            warnings=list(result.warnings),
            periods_per_year=result.periods_per_year,
        )
        if not persist:
            return record
        with self._session() as session:
            row = session.get(BacktestRunRow, identifier)
            if row is None:  # pragma: no cover - only if persist was False on insert
                row = BacktestRunRow(id=identifier, name=config.name, created_at=started_at)
                session.add(row)
            row.status = str(BacktestStatus.COMPLETE)
            row.spec = config.as_dict()
            row.started_at = started_at
            row.finished_at = record.finished_at
            row.duration_ms = duration_ms
            row.symbols = record.symbols
            row.stats = record.stats
            row.in_sample = record.in_sample
            row.out_of_sample = record.out_of_sample
            row.equity_curve = curve
            row.trade_log = trade_log
            row.artifact_bundle = bundle
            row.warnings = record.warnings
            row.periods_per_year = record.periods_per_year
            row.error = None
        return record

    def queue(
        self,
        config: BacktestConfig,
        *,
        hypothesis_id: str | None = None,
        experiment_id: str | None = None,
        screen_id: str | None = None,
        source: str = "screen",
    ) -> BacktestRecord:
        """Persist a queued backtest without running it yet.

        The screener's "queue backtest" button lands here; :meth:`run_queued`
        executes it.
        """
        identifier = new_backtest_id()
        self._insert(
            identifier,
            config,
            hypothesis_id=hypothesis_id,
            experiment_id=experiment_id,
            screen_id=screen_id,
            source=source,
            status=BacktestStatus.QUEUED,
        )
        return self.get(identifier)

    async def run_queued(self, backtest_id: str, **kwargs: Any) -> BacktestRecord:
        """Execute a previously queued run, keeping its identifier."""
        record = self.get(backtest_id)
        return await self.run(
            record.config,
            hypothesis_id=record.hypothesis_id,
            experiment_id=record.experiment_id,
            screen_id=record.screen_id,
            source=record.source,
            run_id=record.id,
            **kwargs,
        )

    # --- reads ------------------------------------------------------------
    def get(self, backtest_id: str) -> BacktestRecord:
        """Return one persisted run.

        Raises:
            KeyError: when the identifier is unknown.
        """
        with self._session() as session:
            row = session.get(BacktestRunRow, backtest_id)
            if row is None:
                raise KeyError(f"unknown backtest {backtest_id!r}")
            return _record_from_row(row)

    def list_runs(
        self,
        *,
        hypothesis_id: str | None = None,
        experiment_id: str | None = None,
        status: BacktestStatus | None = None,
        limit: int = 100,
    ) -> list[BacktestRecord]:
        """Return persisted runs, newest first."""
        statement = select(BacktestRunRow).order_by(BacktestRunRow.created_at.desc()).limit(limit)
        if hypothesis_id:
            statement = statement.where(BacktestRunRow.hypothesis_id == hypothesis_id)
        if experiment_id:
            statement = statement.where(BacktestRunRow.experiment_id == experiment_id)
        if status:
            statement = statement.where(BacktestRunRow.status == str(status))
        with self._session() as session:
            return [_record_from_row(row) for row in session.scalars(statement)]

    def results(self, backtest_id: str) -> dict[str, Any]:
        """Return the full result payload (stats + equity curve) for the UI."""
        record = self.get(backtest_id)
        return record.as_dict(include_curve=True)

    def delete(self, backtest_id: str) -> None:
        """Delete a persisted run.

        Raises:
            KeyError: when the identifier is unknown.
        """
        with self._session() as session:
            row = session.get(BacktestRunRow, backtest_id)
            if row is None:
                raise KeyError(f"unknown backtest {backtest_id!r}")
            session.delete(row)

    def count(self, *, hypothesis_id: str | None = None) -> int:
        """Return how many runs are recorded (the trial ledger's backtest half)."""
        with self._session() as session:
            statement = select(BacktestRunRow.id)
            if hypothesis_id:
                statement = statement.where(BacktestRunRow.hypothesis_id == hypothesis_id)
            return len(list(session.scalars(statement)))

    def tearsheet(self, backtest_id: str) -> tuple[bytes, str]:
        """Render a run's tearsheet PDF.

        Returns:
            ``(pdf_bytes, filename)``.

        Raises:
            KeyError: when the run is unknown.
            ValueError: when the run has no results.
        """
        record = self.get(backtest_id)
        return build_tearsheet_pdf(record), f"tradinglib-tearsheet-{record.id}.pdf"


class _PdfWriter:
    """A minimal PDF-1.4 writer: base-14 text, lines, rectangles and polylines.

    A tearsheet is a page of numbers and two curves, which needs nothing a PDF
    reader does not already have built in. Writing the file directly keeps the
    export honest (the numbers come straight from the stored run) and avoids
    adding a rendering dependency for one button.
    """

    WIDTH: Final[float] = 612.0
    HEIGHT: Final[float] = 792.0

    def __init__(self) -> None:
        """Start a document with one empty page."""
        self._pages: list[list[str]] = [[]]

    @property
    def _current(self) -> list[str]:
        """Operators of the page being written."""
        return self._pages[-1]

    def page_break(self) -> None:
        """Start a new page."""
        self._pages.append([])

    @staticmethod
    def _escape(text: str) -> str:
        """Escape a string for a PDF literal, dropping unrepresentable glyphs."""
        safe = text.encode("latin-1", "replace").decode("latin-1")
        return safe.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")

    def text(
        self,
        x: float,
        y: float,
        value: str,
        *,
        size: float = 9.0,
        bold: bool = False,
        gray: float = 0.0,
    ) -> None:
        """Draw one line of text with its baseline at ``(x, y)``."""
        font = "F2" if bold else "F1"
        self._current.append(
            f"BT {gray:.3f} g /{font} {size:.1f} Tf 1 0 0 1 {x:.2f} {y:.2f} Tm "
            f"({self._escape(value)}) Tj ET"
        )

    def line(
        self, x1: float, y1: float, x2: float, y2: float, *, width: float = 0.5, gray: float = 0.6
    ) -> None:
        """Draw a straight line."""
        self._current.append(
            f"{gray:.3f} G {width:.2f} w {x1:.2f} {y1:.2f} m {x2:.2f} {y2:.2f} l S"
        )

    def rect(
        self,
        x: float,
        y: float,
        width: float,
        height: float,
        *,
        gray: float | None = None,
        stroke: float | None = 0.7,
    ) -> None:
        """Draw a rectangle, optionally filled."""
        ops = []
        if gray is not None:
            ops.append(f"{gray:.3f} g")
        if stroke is not None:
            ops.append(f"{stroke:.3f} G 0.5 w")
        ops.append(f"{x:.2f} {y:.2f} {width:.2f} {height:.2f} re")
        if gray is not None and stroke is not None:
            ops.append("B")
        elif gray is not None:
            ops.append("f")
        else:
            ops.append("S")
        self._current.append(" ".join(ops))

    def polyline(self, points: Sequence[tuple[float, float]], *, width: float = 1.0, gray: float = 0.0) -> None:
        """Draw a connected series of points."""
        if len(points) < 2:
            return
        head = f"{gray:.3f} G {width:.2f} w {points[0][0]:.2f} {points[0][1]:.2f} m"
        body = " ".join(f"{x:.2f} {y:.2f} l" for x, y in points[1:])
        self._current.append(f"{head} {body} S")

    def build(self, *, title: str) -> bytes:
        """Serialise the document."""
        objects: list[bytes] = []

        def add(body: bytes) -> int:
            objects.append(body)
            return len(objects)

        font_regular = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
        font_bold = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
        info = add(
            b"<< /Title (" + self._escape(title).encode("latin-1") + b") /Producer (TradingLib) >>"
        )
        pages_id = len(objects) + 1 + 2 * len(self._pages)
        page_ids: list[int] = []
        for operators in self._pages:
            stream = "\n".join(operators).encode("latin-1", "replace")
            content_id = add(
                b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
            )
            page_ids.append(
                add(
                    b"<< /Type /Page /Parent "
                    + str(pages_id).encode()
                    + b" 0 R /MediaBox [0 0 "
                    + f"{self.WIDTH:.0f} {self.HEIGHT:.0f}".encode()
                    + b"] /Resources << /Font << /F1 "
                    + str(font_regular).encode()
                    + b" 0 R /F2 "
                    + str(font_bold).encode()
                    + b" 0 R >> >> /Contents "
                    + str(content_id).encode()
                    + b" 0 R >>"
                )
            )
        kids = b" ".join(f"{pid} 0 R".encode() for pid in page_ids)
        actual_pages_id = add(
            b"<< /Type /Pages /Kids [" + kids + b"] /Count " + str(len(page_ids)).encode() + b" >>"
        )
        catalog = add(b"<< /Type /Catalog /Pages " + str(actual_pages_id).encode() + b" 0 R >>")

        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = [0]
        for number, body in enumerate(objects, start=1):
            offsets.append(len(out))
            out += str(number).encode() + b" 0 obj\n" + body + b"\nendobj\n"
        xref_at = len(out)
        out += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n"
        out += b"0000000000 65535 f \n"
        for offset in offsets[1:]:
            out += f"{offset:010d} 00000 n \n".encode()
        out += (
            b"trailer\n<< /Size "
            + str(len(objects) + 1).encode()
            + b" /Root "
            + str(catalog).encode()
            + b" 0 R /Info "
            + str(info).encode()
            + b" 0 R >>\nstartxref\n"
            + str(xref_at).encode()
            + b"\n%%EOF\n"
        )
        return bytes(out)


def _pct(value: Any, decimals: int = 2) -> str:
    """Render a fraction as a signed percentage, or ``n/a``."""
    if value is None:
        return "n/a"
    return f"{float(value) * 100.0:+.{decimals}f}%"


def _num(value: Any, decimals: int = 2) -> str:
    """Render a number, or ``n/a``."""
    if value is None:
        return "n/a"
    return f"{float(value):.{decimals}f}"


def build_tearsheet_pdf(record: BacktestRecord) -> bytes:
    """Render a completed run's tearsheet as a self-contained PDF.

    Everything on the page is read from the persisted record - the same numbers
    the Strategy Lab shows - so the exported file can never disagree with the
    screen it was exported from.

    Raises:
        ValueError: when the run has no results to render.
    """
    if record.status is not BacktestStatus.COMPLETE or not record.stats:
        raise ValueError(
            f"backtest {record.id!r} is {record.status}; only a completed run has a tearsheet"
        )
    pdf = _PdfWriter()
    stats = record.stats or {}
    left, right = 48.0, _PdfWriter.WIDTH - 48.0
    y = _PdfWriter.HEIGHT - 60.0

    pdf.text(left, y, "TradingLib backtest tearsheet", size=17, bold=True)
    y -= 18
    pdf.text(left, y, f"{record.id} - {record.name}", size=11, gray=0.25)
    y -= 13
    pdf.text(
        left,
        y,
        f"{record.config.signal.describe()} | {len(record.symbols)} symbols | benchmark "
        f"{record.config.benchmark or 'none'}",
        size=8.5,
        gray=0.4,
    )
    y -= 11
    pdf.text(
        left,
        y,
        f"window {stats.get('start') or '?'} to {stats.get('end') or '?'} | "
        f"{int(stats.get('periods') or 0)} periods at {record.periods_per_year}/year | "
        f"generated {utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
        size=8.5,
        gray=0.4,
    )
    y -= 18
    pdf.line(left, y, right, y, width=1.0, gray=0.2)
    y -= 22

    headline = [
        ("Total return", _pct(stats.get("total_return"))),
        ("CAGR", _pct(stats.get("cagr"))),
        ("Volatility", _pct(stats.get("volatility"))),
        ("Sharpe", _num(stats.get("sharpe"))),
        ("Sortino", _num(stats.get("sortino"))),
        ("Max drawdown", _pct(stats.get("max_drawdown"))),
        ("Hit rate", _pct(stats.get("hit_rate"), 1)),
        ("Trades", str(int(stats.get("trades") or 0))),
    ]
    column = (right - left) / 4.0
    for index, (label, value) in enumerate(headline):
        column_x = left + column * (index % 4)
        row_y = y - 30.0 * (index // 4)
        pdf.text(column_x, row_y, label.upper(), size=7, gray=0.45)
        pdf.text(column_x, row_y - 13, value, size=13, bold=True)
    y -= 74

    curve = record.equity_curve or {}
    dates = curve.get("dates") or []
    equity = [v for v in (curve.get("equity") or []) if v is not None]
    if len(equity) > 1:
        box_top, box_height = y, 150.0
        box_bottom = box_top - box_height
        pdf.text(left, box_top + 6, "EQUITY CURVE", size=7, gray=0.45)
        pdf.rect(left, box_bottom, right - left, box_height, stroke=0.8)
        low, high = min(equity), max(equity)
        span = (high - low) or 1.0
        step = max(1, len(equity) // 400)
        points = [
            (
                left + (right - left) * (i / max(1, len(equity) - 1)),
                box_bottom + box_height * ((equity[i] - low) / span),
            )
            for i in range(0, len(equity), step)
        ]
        pdf.polyline(points, width=1.2, gray=0.1)
        benchmark = curve.get("benchmark") or []
        marks = [v for v in benchmark if v is not None]
        if len(marks) > 1:
            b_points = [
                (
                    left + (right - left) * (i / max(1, len(benchmark) - 1)),
                    box_bottom
                    + box_height * ((float(benchmark[i]) - low) / span),
                )
                for i in range(0, len(benchmark), step)
                if benchmark[i] is not None and low <= float(benchmark[i]) <= high
            ]
            pdf.polyline(b_points, width=0.8, gray=0.6)
            pdf.text(right - 150, box_bottom + 6, f"grey = {curve.get('benchmark_symbol') or 'benchmark'}", size=7, gray=0.5)
        pdf.text(left + 4, box_bottom + 6, f"{low:,.0f}", size=7, gray=0.5)
        pdf.text(left + 4, box_top - 12, f"{high:,.0f}", size=7, gray=0.5)
        if dates:
            pdf.text(left, box_bottom - 11, str(dates[0])[:10], size=7, gray=0.5)
            pdf.text(right - 50, box_bottom - 11, str(dates[-1])[:10], size=7, gray=0.5)
        y = box_bottom - 32

    windows = [
        ("Full window", record.stats),
        ("In sample", record.in_sample),
        ("Out of sample", record.out_of_sample),
    ]
    pdf.text(left, y, "WINDOWS", size=7, gray=0.45)
    y -= 14
    headers = ["", "Return", "CAGR", "Vol", "Sharpe", "Max DD", "Trades"]
    widths = [130.0, 70.0, 70.0, 60.0, 60.0, 70.0, 50.0]
    x = left
    for header, width in zip(headers, widths, strict=True):
        pdf.text(x, y, header, size=7.5, bold=True, gray=0.35)
        x += width
    y -= 4
    pdf.line(left, y, right, y, gray=0.75)
    y -= 12
    for label, window in windows:
        if not window:
            continue
        cells = [
            label,
            _pct(window.get("total_return")),
            _pct(window.get("cagr")),
            _pct(window.get("volatility")),
            _num(window.get("sharpe")),
            _pct(window.get("max_drawdown")),
            str(int(window.get("trades") or 0)),
        ]
        x = left
        for cell, width in zip(cells, widths, strict=True):
            pdf.text(x, y, cell, size=8.5, bold=(width == widths[0]))
            x += width
        y -= 14
    y -= 8

    log = record.trade_log or {}
    if log:
        win_rate = log.get("win_rate")
        average = log.get("average_return")
        bars = log.get("average_bars")
        pdf.text(left, y, "TRADE LEDGER", size=7, gray=0.45)
        y -= 14
        pdf.text(
            left,
            y,
            f"{log.get('total', 0)} round trips ({log.get('open', 0)} still open)"
            # A win rate is a share, not a change: it is never signed.
            + (f" | win rate {float(win_rate) * 100.0:.1f}%" if win_rate is not None else "")
            + (f" | average return {_pct(average)}" if average is not None else "")
            + (f" | average hold {float(bars):.0f} bars" if bars is not None else ""),
            size=8.5,
        )
        y -= 20

    bundle = record.artifact_bundle or {}
    if bundle:
        pdf.text(left, y, "REPRODUCIBILITY", size=7, gray=0.45)
        y -= 13
        for key in ("data", "code", "config"):
            entry = bundle.get(key)
            if isinstance(entry, Mapping) and entry.get("sha256"):
                pdf.text(left, y, f"{key} sha256 {entry['sha256']}", size=7.5, gray=0.3)
                y -= 11
        y -= 6

    if record.warnings:
        pdf.text(left, y, "WARNINGS", size=7, gray=0.45)
        y -= 13
        for warning in record.warnings[:8]:
            pdf.text(left, y, f"- {warning}"[:118], size=7.5, gray=0.2)
            y -= 11

    pdf.text(
        left,
        40,
        "Every figure on this page is read from the stored run. Research only - no order was placed.",
        size=7,
        gray=0.5,
    )
    return pdf.build(title=f"TradingLib tearsheet {record.id}")


def new_backtest_id() -> str:
    """Return a fresh backtest identifier (``bt-`` + 12 hex characters)."""
    return f"bt-{uuid.uuid4().hex[:12]}"


#: History used when a queued request names neither a start nor an end date.
DEFAULT_QUEUE_HISTORY: Final[timedelta] = timedelta(days=1095)


class LabBacktestQueue:
    """The Strategy Lab's implementation of :class:`tradinglib.research.BacktestQueue`.

    The screener hands over a :class:`~tradinglib.research.BacktestRequest` (the
    names that survived a screen); this turns it into a real, persisted lab
    backtest of an equal-weighted basket of those names and - unless
    ``auto_run`` is off - starts it immediately, so the toast the UI shows
    corresponds to a run that is genuinely executing. Progress and completion
    arrive on the ``lab`` event channel like any other run.
    """

    def __init__(
        self,
        engine: BacktestEngine | None = None,
        *,
        auto_run: bool = True,
        history: timedelta = DEFAULT_QUEUE_HISTORY,
    ) -> None:
        """Create the queue.

        Args:
            engine: backtest engine; defaults to the process-wide one, resolved
                lazily so importing this module never touches the database.
            auto_run: start the queued run in the background straight away.
            history: how far back to look when the request names no window.
        """
        self._engine = engine
        self.auto_run = auto_run
        self.history = history
        self._tasks: set[asyncio.Task[Any]] = set()

    @property
    def engine(self) -> BacktestEngine:
        """The backtest engine queued runs are created in."""
        if self._engine is None:
            self._engine = get_backtest_engine()
        return self._engine

    def build_config(self, request: BacktestRequest) -> BacktestConfig:
        """Turn a screener request into a reproducible backtest configuration.

        Raises:
            ValueError: when the request carries no symbols or an unknown
                rebalance frequency - both are refused rather than defaulted,
                so the screener records an honest failure.
        """
        symbols = [str(symbol).strip().upper() for symbol in request.symbols if str(symbol).strip()]
        symbols = list(dict.fromkeys(symbols))
        if not symbols:
            raise ValueError("a queued backtest needs at least one symbol")
        try:
            rebalance = Rebalance(str(request.rebalance).lower())
        except ValueError as exc:
            allowed = ", ".join(str(item) for item in Rebalance)
            raise ValueError(
                f"unknown rebalance {request.rebalance!r}; use one of {allowed}"
            ) from exc
        start = request.start
        if start is None and request.end is None:
            start = date.today() - self.history
        params = {
            "origin": request.origin,
            "screen_run_id": request.screen_run_id,
            "requested_at": request.requested_at.isoformat(),
            **dict(request.params),
        }
        return BacktestConfig(
            name=request.name,
            universe_id=request.universe_id,
            symbols=symbols,
            signal=SignalSpec(
                kind=SignalKind.STATIC, symbols=symbols, weighting=Weighting.EQUAL
            ),
            start=start,
            end=request.end,
            benchmark=request.benchmark or None,
            rebalance=rebalance,
            initial_capital=float(request.initial_capital),
            params=params,
        )

    async def queue_backtest(self, request: BacktestRequest) -> dict[str, Any]:
        """Create - and, by default, start - a lab backtest for ``request``."""
        config = self.build_config(request)
        record = await asyncio.to_thread(
            self.engine.queue,
            config,
            screen_id=request.screen_id,
            source=request.origin or "screen",
        )
        payload = record.as_dict()
        if not self.auto_run:
            payload["detail"] = "queued; waiting for a runner to pick it up"
            return payload

        task = asyncio.create_task(self._execute(record.id), name=f"backtest:{record.id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        payload["detail"] = (
            f"queued and started over {len(config.symbols)} symbols; "
            "progress streams on the lab channel"
        )
        return payload

    async def _execute(self, backtest_id: str) -> None:
        """Run a queued backtest, leaving the failure on the record if it fails."""
        try:
            await self.engine.run_queued(backtest_id)
        except Exception:  # noqa: BLE001 - already recorded on the row
            logger.warning("queued backtest %s failed", backtest_id, exc_info=True)

    async def drain(self) -> None:
        """Await every run this queue started. Intended for tests and shutdown."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)


def install_backtest_queue(queue: LabBacktestQueue | None = None) -> LabBacktestQueue:
    """Register the lab as the process-wide backtest queue and return it.

    Called once when this module is imported: the presence of the lab *is* the
    condition the screener's contract describes, and registering here means no
    other module has to know how to wire the two together.
    """
    resolved = queue or LabBacktestQueue()
    register_backtest_queue(resolved)
    return resolved


#: The queue registered on import; replace it with :func:`install_backtest_queue`.
LAB_BACKTEST_QUEUE: Final[LabBacktestQueue] = install_backtest_queue()


_engine: BacktestEngine | None = None


def get_backtest_engine() -> BacktestEngine:
    """Return the process-wide backtest engine, creating it on first use."""
    global _engine
    if _engine is None:
        _engine = BacktestEngine()
    return _engine


def reset_backtest_engine(engine: BacktestEngine | None = None) -> BacktestEngine:
    """Replace the process-wide engine. Intended for tests and reconfiguration."""
    global _engine
    _engine = engine if engine is not None else BacktestEngine()
    return _engine


def equal_weight_signals(prices: pd.DataFrame, symbols: Iterable[str]) -> pd.DataFrame:
    """Return a static signal panel selecting ``symbols`` on every bar.

    Convenience for callers (the screener, agent tools) that already know which
    names they want to hold and only need the backtester's accounting.
    """
    wanted = {str(s).upper() for s in symbols}
    data = np.where(
        np.isin([str(c).upper() for c in prices.columns], list(wanted)), 1.0, np.nan
    )
    frame = pd.DataFrame(
        np.repeat(data[None, :], len(prices.index), axis=0),
        index=prices.index,
        columns=prices.columns,
    )
    return frame.where(prices.notna())


__all__ = [
    "DEFAULT_QUEUE_HISTORY",
    "LAB_BACKTEST_QUEUE",
    "LAB_CHANNEL",
    "TRADING_DAYS_PER_YEAR",
    "LabBacktestQueue",
    "install_backtest_queue",
    "build_tearsheet_pdf",
    "BacktestConfig",
    "BacktestEngine",
    "BacktestRecord",
    "BacktestResult",
    "BacktestRunRow",
    "BacktestStats",
    "BacktestStatus",
    "Backtester",
    "CostModel",
    "ExpressionError",
    "Rebalance",
    "SignalKind",
    "SignalSpec",
    "Weighting",
    "build_factor_panels",
    "compile_expression",
    "compute_stats",
    "drawdown_series",
    "ensure_schema",
    "equal_weight_signals",
    "get_backtest_engine",
    "infer_periods_per_year",
    "max_drawdown",
    "new_backtest_id",
    "reset_backtest_engine",
]
