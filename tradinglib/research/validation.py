"""Promotion gates and statistical tests for strategy validation.

A backtest is not evidence until it survives a fixed, declared set of gates.
This module owns that suite and the statistics behind it:

``min_oos_sharpe``
    Sharpe ratio of the out-of-sample window.
``max_drawdown``
    Worst peak-to-trough loss over the whole run.
``min_trades``
    Enough trades for the statistics to mean anything.
``min_history``
    Enough calendar history (in years of return observations).
``cost_sensitivity``
    The *same* backtest re-run with the cost model scaled up. A strategy whose
    edge evaporates when costs double never had an edge.
``benchmark_outperformance``
    Out-of-sample excess return over the configured benchmark.
``excess_return_t_test``
    One-sided t-test that the mean excess return over the benchmark is
    positive (scipy, Welch not needed - it is a one-sample test on the
    difference series).
``deflated_sharpe``
    Bailey & Lopez de Prado's deflated Sharpe ratio, which discounts the
    observed Sharpe by the number of trials already charged to the hypothesis
    family. The trial count is read from the real backtest ledger, never
    assumed.

Every gate reports its threshold, the value actually observed and a pass/fail,
so the UI can show *why* something was blocked. Gates whose input is missing
(no benchmark stored, no out-of-sample window) come back ``pending`` with the
reason rather than silently passing.

Reports and promotions are persisted through :mod:`tradinglib.db`; both are
published on the ``lab`` event channel.
"""

from __future__ import annotations

import itertools
import logging
import math
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

import numpy as np
import pandas as pd
from scipy import stats as scipy_stats
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Integer,
    String,
    Text,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from tradinglib.data.base import to_utc, utcnow
from tradinglib.db import Base, ensure_columns, get_engine, get_session_factory
from tradinglib.events import EventBus, get_event_bus
from tradinglib.research.backtest import (
    LAB_CHANNEL,
    TRADING_DAYS_PER_YEAR,
    BacktestEngine,
    BacktestResult,
    BacktestStats,
    BacktestStatus,
    SignalKind,
    get_backtest_engine,
)
from tradinglib.research.experiments import (
    DEFAULT_ACCEPTANCE,
    ExperimentEngine,
    ExperimentStage,
)

logger = logging.getLogger(__name__)

#: Euler-Mascheroni constant, used by the deflated-Sharpe expected maximum.
EULER_MASCHERONI: Final[float] = 0.5772156649015329


#: How many *distinct* people must sign a promotion. Two is the whole point:
#: nobody advances their own candidate on their own signature.
PROMOTION_APPROVALS_REQUIRED: Final[int] = 2


class GateStatus(StrEnum):
    """Outcome of one validation gate."""

    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    PENDING = "pending"


@dataclass(frozen=True, slots=True)
class GateThresholds:
    """The workspace gate suite. Stored with every report that used it."""

    min_oos_sharpe: float = DEFAULT_ACCEPTANCE.min_oos_sharpe
    max_drawdown: float = DEFAULT_ACCEPTANCE.max_drawdown
    min_trades: int = DEFAULT_ACCEPTANCE.min_trades
    min_history_years: float = 2.0
    cost_multiplier: float = 2.0
    min_cost_retention: float = 0.6
    min_deflated_sharpe: float = 0.5
    max_p_value: float = 0.05
    min_excess_return: float = 0.0
    #: Highest tolerated probability of backtest overfitting (CSCV).
    max_pbo: float = 0.5
    #: Number of contiguous groups the CSCV split uses.
    cscv_groups: int = 8
    #: Worst-regime Sharpe a candidate must still clear.
    min_regime_sharpe: float = 0.0
    #: Number of volatility regimes the OOS window is split into.
    regime_buckets: int = 3

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping."""
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> GateThresholds:
        """Rebuild thresholds from :meth:`as_dict` output."""
        data = payload or {}
        defaults = cls()
        return cls(
            min_oos_sharpe=float(data.get("min_oos_sharpe", defaults.min_oos_sharpe)),
            max_drawdown=float(data.get("max_drawdown", defaults.max_drawdown)),
            min_trades=int(data.get("min_trades", defaults.min_trades)),
            min_history_years=float(data.get("min_history_years", defaults.min_history_years)),
            cost_multiplier=float(data.get("cost_multiplier", defaults.cost_multiplier)),
            min_cost_retention=float(data.get("min_cost_retention", defaults.min_cost_retention)),
            min_deflated_sharpe=float(
                data.get("min_deflated_sharpe", defaults.min_deflated_sharpe)
            ),
            max_p_value=float(data.get("max_p_value", defaults.max_p_value)),
            min_excess_return=float(data.get("min_excess_return", defaults.min_excess_return)),
            max_pbo=float(data.get("max_pbo", defaults.max_pbo)),
            cscv_groups=int(data.get("cscv_groups", defaults.cscv_groups)),
            min_regime_sharpe=float(
                data.get("min_regime_sharpe", defaults.min_regime_sharpe)
            ),
            regime_buckets=int(data.get("regime_buckets", defaults.regime_buckets)),
        )


#: The default gate suite (the "workspace defaults" panel in the UI).
DEFAULT_THRESHOLDS: Final[GateThresholds] = GateThresholds()


@dataclass(frozen=True, slots=True)
class Gate:
    """A single promotion criterion with its threshold and observed value."""

    id: str
    name: str
    metric: str
    threshold: float
    comparator: str
    observed: float | None
    status: GateStatus
    window: str = "full"
    hard: bool = False
    detail: str | None = None

    @property
    def passed(self) -> bool:
        """Whether this gate is satisfied."""
        return self.status is GateStatus.PASS

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of the gate."""
        payload = asdict(self)
        payload["status"] = str(self.status)
        payload["passed"] = self.passed
        return payload


@dataclass(slots=True)
class ValidationReport:
    """The full gate panel for one backtest."""

    id: str
    backtest_id: str
    evaluated_at: datetime
    gates: list[Gate] = field(default_factory=list)
    promoted: bool = False
    tests: dict[str, Any] = field(default_factory=dict)
    thresholds: GateThresholds = field(default_factory=GateThresholds)
    hypothesis_id: str | None = None
    experiment_id: str | None = None
    trials: int = 1
    overrides: list[str] = field(default_factory=list)
    comment: str | None = None
    #: Sign-offs recorded against this backtest's promotion, oldest first.
    approvals: list[dict[str, Any]] = field(default_factory=list)
    approvals_required: int = PROMOTION_APPROVALS_REQUIRED

    @property
    def promotion_pending(self) -> bool:
        """Whether the gates pass but not enough people have signed yet."""
        return (
            not self.promoted
            and bool(self.approvals)
            and len(self.approvals) < self.approvals_required
        )

    @property
    def passed(self) -> bool:
        """Whether every gate passed."""
        return bool(self.gates) and all(gate.passed for gate in self.gates)

    @property
    def failed_gates(self) -> list[Gate]:
        """Gates that did not pass (failures and unevaluable ones)."""
        return [gate for gate in self.gates if not gate.passed]

    @property
    def score(self) -> float:
        """Share of gates passed, as a 0-100 score (the UI's verdict ring)."""
        if not self.gates:
            return 0.0
        return 100.0 * sum(1 for gate in self.gates if gate.passed) / len(self.gates)

    @property
    def verdict(self) -> str:
        """``pass``, ``conditional`` (only soft gates outstanding) or ``fail``."""
        if self.passed:
            return "pass"
        if any(gate.hard and not gate.passed for gate in self.gates):
            return "fail"
        failures = [g for g in self.failed_gates if g.status is GateStatus.FAIL]
        return "fail" if len(failures) > 2 else "conditional"

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping for the API layer."""
        return {
            "id": self.id,
            "backtest_id": self.backtest_id,
            "hypothesis_id": self.hypothesis_id,
            "experiment_id": self.experiment_id,
            "evaluated_at": self.evaluated_at.isoformat(),
            "gates": [gate.as_dict() for gate in self.gates],
            "gates_passed": sum(1 for gate in self.gates if gate.passed),
            "gates_total": len(self.gates),
            "passed": self.passed,
            "score": self.score,
            "verdict": self.verdict,
            "promoted": self.promoted,
            "tests": dict(self.tests),
            "thresholds": self.thresholds.as_dict(),
            "trials": self.trials,
            "overrides": list(self.overrides),
            "comment": self.comment,
            "approvals": [dict(entry) for entry in self.approvals],
            "approvals_required": self.approvals_required,
            "approvals_signed": len(self.approvals),
            "approvals_outstanding": max(
                0, self.approvals_required - len(self.approvals)
            ),
            "promotion_pending": self.promotion_pending,
        }


def _ordinal(number: int) -> str:
    """Render a small positive integer as an English ordinal (``2nd``)."""
    if 10 <= number % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


# --------------------------------------------------------------------------
# statistics
# --------------------------------------------------------------------------
def excess_return_t_test(
    returns: pd.Series, benchmark: pd.Series | None
) -> dict[str, Any]:
    """One-sided t-test that mean excess return over the benchmark is positive.

    With no benchmark the strategy's own returns are tested against zero, which
    is the same question with a cash benchmark; the result records which of the
    two was actually done.

    Returns:
        ``{basis, observations, mean, t_stat, p_value, significant}``. The
        p-value is ``None`` when there are too few observations or no variance.
    """
    if benchmark is not None and not benchmark.empty:
        aligned = returns.astype("float64").align(benchmark.astype("float64"), join="inner")
        series = (aligned[0] - aligned[1]).dropna()
        basis = "excess_over_benchmark"
    else:
        series = returns.astype("float64").dropna()
        basis = "absolute_return"
    count = int(series.size)
    if count < 3 or float(series.std(ddof=1)) == 0.0:
        return {
            "basis": basis,
            "observations": count,
            "mean": float(series.mean()) if count else None,
            "t_stat": None,
            "p_value": None,
            "significant": False,
            "note": "not enough variation in the series to run a t-test",
        }
    outcome = scipy_stats.ttest_1samp(series.to_numpy(), 0.0, alternative="greater")
    return {
        "basis": basis,
        "observations": count,
        "mean": float(series.mean()),
        "t_stat": float(outcome.statistic),
        "p_value": float(outcome.pvalue),
        "significant": bool(outcome.pvalue < 0.05),
    }


def deflated_sharpe(
    returns: pd.Series,
    *,
    trials: int,
    trial_sharpe_std: float | None = None,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> dict[str, Any]:
    """Deflated Sharpe ratio (Bailey & Lopez de Prado, 2014).

    The observed Sharpe is compared against the Sharpe one would *expect* to
    see as the maximum of ``trials`` independent attempts under a zero-skill
    null, and the comparison is corrected for the return series' own skewness
    and kurtosis. The result is a probability that the strategy's true Sharpe is
    positive given how many times the family has been tried.

    Args:
        returns: the strategy's period returns.
        trials: how many trials the hypothesis family has already charged.
        trial_sharpe_std: dispersion of the (per-period) Sharpe ratios across
            those trials. When ``None`` the null's own ``1/sqrt(n-1)`` is used
            and the result records that substitution.
        periods_per_year: annualisation factor, used only for the reported
            annualised Sharpe.

    Returns:
        ``{sharpe, annualised_sharpe, expected_max_sharpe, deflated_sharpe,
        trials, skew, kurtosis, observations, basis}``. ``deflated_sharpe`` is
        ``None`` when the series is too short or has no variance.
    """
    series = returns.astype("float64").dropna()
    count = int(series.size)
    std = float(series.std(ddof=1)) if count > 1 else 0.0
    if count < 4 or std == 0.0:
        return {
            "sharpe": None,
            "annualised_sharpe": None,
            "expected_max_sharpe": None,
            "deflated_sharpe": None,
            "trials": int(max(1, trials)),
            "observations": count,
            "note": "series too short (or constant) to deflate",
        }
    sharpe = float(series.mean() / std)
    skew = float(scipy_stats.skew(series.to_numpy(), bias=False))
    kurtosis = float(scipy_stats.kurtosis(series.to_numpy(), fisher=False, bias=False))

    attempts = max(1, int(trials))
    basis = "observed trial dispersion"
    dispersion = trial_sharpe_std
    if dispersion is None or dispersion <= 0.0 or not math.isfinite(dispersion):
        dispersion = 1.0 / math.sqrt(max(2, count) - 1)
        basis = "null dispersion 1/sqrt(n-1) (fewer than two comparable trials on record)"

    if attempts <= 1:
        expected_max = 0.0
    else:
        left = scipy_stats.norm.ppf(1.0 - 1.0 / attempts)
        right = scipy_stats.norm.ppf(1.0 - 1.0 / (attempts * math.e))
        expected_max = dispersion * ((1.0 - EULER_MASCHERONI) * left + EULER_MASCHERONI * right)

    denominator = 1.0 - skew * sharpe + (kurtosis - 1.0) / 4.0 * sharpe**2
    if denominator <= 0.0:
        return {
            "sharpe": sharpe,
            "annualised_sharpe": sharpe * math.sqrt(periods_per_year),
            "expected_max_sharpe": expected_max,
            "deflated_sharpe": None,
            "trials": attempts,
            "skew": skew,
            "kurtosis": kurtosis,
            "observations": count,
            "basis": basis,
            "note": "the higher-moment correction is degenerate for this series",
        }
    statistic = (sharpe - expected_max) * math.sqrt(count - 1) / math.sqrt(denominator)
    return {
        "sharpe": sharpe,
        "annualised_sharpe": sharpe * math.sqrt(periods_per_year),
        "expected_max_sharpe": expected_max,
        "deflated_sharpe": float(scipy_stats.norm.cdf(statistic)),
        "trials": attempts,
        "skew": skew,
        "kurtosis": kurtosis,
        "observations": count,
        "basis": basis,
    }


def bootstrap_confidence(
    returns: pd.Series,
    *,
    metric: str = "sharpe",
    iterations: int = 1000,
    block: int = 21,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
    seed: int = 20260828,
) -> dict[str, Any]:
    """Block-bootstrap a confidence interval for a performance metric.

    Overlapping blocks of ``block`` consecutive returns are resampled with
    replacement, which preserves short-horizon autocorrelation that an i.i.d.
    bootstrap would destroy. The generator is seeded, so the interval is
    reproducible.

    Args:
        returns: the strategy's period returns.
        metric: ``sharpe``, ``total_return``, ``cagr`` or ``max_drawdown``.
        iterations: number of resamples.
        block: block length in periods.
        periods_per_year: annualisation factor.
        seed: RNG seed; change it only to explore sampling noise.

    Returns:
        ``{metric, observed, mean, p05, p50, p95, prob_positive, iterations}``.

    Raises:
        ValueError: when ``metric`` is not one of the supported names.
    """
    series = returns.astype("float64").dropna()
    count = int(series.size)
    supported = {"sharpe", "total_return", "cagr", "max_drawdown"}
    if metric not in supported:
        raise ValueError(f"unknown metric {metric!r}; use one of {', '.join(sorted(supported))}")
    if count < max(8, block):
        return {
            "metric": metric,
            "observed": None,
            "iterations": 0,
            "note": f"need at least {max(8, block)} observations to block-bootstrap",
        }

    values = series.to_numpy()
    blocks = max(1, int(math.ceil(count / block)))
    starts_high = count - block + 1
    rng = np.random.default_rng(seed)

    def score(sample: np.ndarray) -> float:
        if metric == "sharpe":
            std = float(sample.std(ddof=1))
            return float(sample.mean() / std * math.sqrt(periods_per_year)) if std > 0 else 0.0
        total = float(np.prod(1.0 + sample) - 1.0)
        if metric == "total_return":
            return total
        if metric == "cagr":
            years = sample.size / float(periods_per_year)
            return float((1.0 + total) ** (1.0 / years) - 1.0) if years > 0 and total > -1 else -1.0
        equity = np.cumprod(1.0 + sample)
        return float((equity / np.maximum.accumulate(equity) - 1.0).min())

    draws = np.empty(iterations, dtype="float64")
    for index in range(iterations):
        starts = rng.integers(0, starts_high, size=blocks)
        sample = np.concatenate([values[start : start + block] for start in starts])[:count]
        draws[index] = score(sample)

    return {
        "metric": metric,
        "observed": score(values),
        "mean": float(draws.mean()),
        "p05": float(np.percentile(draws, 5)),
        "p50": float(np.percentile(draws, 50)),
        "p95": float(np.percentile(draws, 95)),
        "prob_positive": float((draws > 0).mean()),
        "iterations": int(iterations),
        "block": int(block),
        "seed": int(seed),
    }


def cscv_pbo(
    candidates: Mapping[str, pd.Series],
    *,
    groups: int = 8,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> dict[str, Any]:
    """Probability of backtest overfitting by combinatorially purged CSCV.

    This is the Bailey/Borwein/Lopez de Prado/Zhu construction. The aligned
    return matrix of ``candidates`` is cut into ``groups`` contiguous blocks;
    every way of choosing half the blocks as the in-sample set (with the
    complement as out-of-sample) is enumerated; in each split the candidate with
    the best in-sample Sharpe is selected and its *rank* among the out-of-sample
    Sharpes is recorded. PBO is the share of splits in which that selection lands
    in the bottom half out of sample - i.e. how often picking the winner in
    sample would have been a mistake.

    It measures **selection** overfitting, so it needs more than one candidate.
    With a single strategy there is nothing to select between and the function
    says so rather than returning a number that would mean nothing.

    Args:
        candidates: ``{name: period returns}``. Series are aligned on their
            common index; candidates with no overlap are dropped.
        groups: number of contiguous blocks (must be even and at least 4).
        periods_per_year: annualisation factor for the Sharpe ratios.

    Returns:
        ``{pbo, splits, candidates, groups, logits, note}``. ``pbo`` is ``None``
        when the input cannot support the computation.
    """
    if groups < 4 or groups % 2:
        raise ValueError("cscv needs an even number of groups, at least 4")
    frame = pd.DataFrame(
        {name: series.astype("float64") for name, series in candidates.items()}
    ).dropna()
    if frame.shape[1] < 2:
        return {
            "pbo": None,
            "splits": 0,
            "candidates": int(frame.shape[1]),
            "groups": groups,
            "note": (
                "PBO measures selection overfitting and needs at least two "
                "candidate configurations sharing a window; "
                f"{frame.shape[1]} available"
            ),
        }
    if frame.shape[0] < groups * 4:
        return {
            "pbo": None,
            "splits": 0,
            "candidates": int(frame.shape[1]),
            "groups": groups,
            "note": (
                f"need at least {groups * 4} shared observations to cut "
                f"{groups} blocks; {frame.shape[0]} available"
            ),
        }

    values = frame.to_numpy()
    blocks = np.array_split(np.arange(values.shape[0]), groups)
    scale = math.sqrt(periods_per_year)

    def sharpe(rows: np.ndarray) -> np.ndarray:
        sample = values[rows]
        std = sample.std(axis=0, ddof=1)
        mean = sample.mean(axis=0)
        with np.errstate(divide="ignore", invalid="ignore"):
            return np.where(std > 0, mean / std * scale, 0.0)

    logits: list[float] = []
    below = 0
    splits = 0
    width = frame.shape[1]
    for chosen in itertools.combinations(range(groups), groups // 2):
        in_rows = np.concatenate([blocks[i] for i in chosen])
        out_rows = np.concatenate([blocks[i] for i in range(groups) if i not in chosen])
        best = int(np.argmax(sharpe(in_rows)))
        out_scores = sharpe(out_rows)
        # Rank of the in-sample winner among the out-of-sample scores, mapped
        # to (0, 1); 1.0 would be the best possible out-of-sample outcome.
        rank = float((out_scores <= out_scores[best]).sum()) / (width + 1)
        rank = min(max(rank, 1.0 / (width + 1)), width / (width + 1))
        logits.append(math.log(rank / (1.0 - rank)))
        below += int(rank <= 0.5)
        splits += 1
    return {
        "pbo": round(below / splits, 6) if splits else None,
        "splits": splits,
        "candidates": int(width),
        "candidate_names": [str(c) for c in frame.columns],
        "observations": int(frame.shape[0]),
        "groups": groups,
        "median_logit": round(float(np.median(logits)), 6) if logits else None,
        "note": None,
    }


def regime_breakdown(
    returns: pd.Series,
    *,
    buckets: int = 3,
    window: int = 21,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> dict[str, Any]:
    """Split a return series by realised-volatility regime and score each one.

    Each period is assigned to a volatility bucket by the *trailing* rolling
    standard deviation of the strategy's own returns - trailing, so the label
    of a period never depends on returns that came after it. A strategy that
    only works in one regime shows up as a bucket with a negative Sharpe.

    Args:
        returns: period returns.
        buckets: number of equal-frequency volatility buckets.
        window: rolling window used for the volatility estimate.
        periods_per_year: annualisation factor.

    Returns:
        ``{buckets: [...], worst_sharpe, positive_share, note}``; ``worst_sharpe``
        is ``None`` when the window is too short to bucket.
    """
    series = returns.astype("float64").dropna()
    minimum = max(window * buckets, buckets * 10)
    if series.size < minimum:
        return {
            "buckets": [],
            "worst_sharpe": None,
            "positive_share": None,
            "note": (
                f"need at least {minimum} observations to split into {buckets} "
                f"volatility regimes; {series.size} available"
            ),
        }
    vol = series.rolling(window).std()
    usable = series[vol.notna()]
    vol = vol.dropna()
    try:
        labels = pd.qcut(vol, buckets, labels=False, duplicates="drop")
    except ValueError:  # pragma: no cover - a constant-vol series
        return {
            "buckets": [],
            "worst_sharpe": None,
            "positive_share": None,
            "note": "realised volatility is constant; there are no distinct regimes",
        }
    names = ["low volatility", "mid volatility", "high volatility"]
    out: list[dict[str, Any]] = []
    distinct = sorted({int(v) for v in labels.dropna().unique()})
    for index in distinct:
        mask = labels == index
        sample = usable[mask]
        if sample.size < 2:
            continue
        std = float(sample.std(ddof=1))
        sharpe = (
            float(sample.mean() / std * math.sqrt(periods_per_year)) if std > 0 else 0.0
        )
        label = (
            names[index]
            if len(distinct) == 3 and index < len(names)
            else f"volatility bucket {index + 1}"
        )
        out.append(
            {
                "regime": label,
                "observations": int(sample.size),
                "sharpe": round(sharpe, 6),
                "total_return": round(float((1.0 + sample).prod() - 1.0), 6),
                "mean_volatility": round(float(vol[mask].mean() * math.sqrt(periods_per_year)), 6),
            }
        )
    if not out:
        return {
            "buckets": [],
            "worst_sharpe": None,
            "positive_share": None,
            "note": "no volatility bucket held enough observations to score",
        }
    sharpes = [entry["sharpe"] for entry in out]
    return {
        "buckets": out,
        "worst_sharpe": round(min(sharpes), 6),
        "best_sharpe": round(max(sharpes), 6),
        "positive_share": round(sum(1 for s in sharpes if s > 0) / len(sharpes), 6),
        "window": window,
        "note": None,
    }


def lookahead_scan(result: BacktestResult, *, now: datetime | None = None) -> dict[str, Any]:
    """Scan a finished backtest for the three ways look-ahead gets in.

    1. **Timing.** Bar 0 must carry no return: the engine establishes the
       position at that close, so a return there would mean the position earned
       the bar it was opened on.
    2. **Future data.** No bar the run consumed may be timestamped later than
       the moment the run happened - a price stamped in the future is a price
       the strategy could not have seen.
    3. **Warm-up.** A signal with a ``lookback`` of *n* cannot be computed
       before bar *n*; the traded window must start at or after it, unless the
       run explicitly kept the warm-up (``trim_warmup=False``), which is
       reported rather than silently accepted.

    Returns:
        ``{clean, findings, checks}`` - ``findings`` is empty when clean.
    """
    moment = to_utc(now) if now is not None else utcnow()
    findings: list[str] = []
    checks: dict[str, Any] = {}

    equity_index = pd.DatetimeIndex(result.equity_curve.index)
    return_index = pd.DatetimeIndex(result.returns.index)
    first_return_after_first_bar = bool(
        len(equity_index) and len(return_index) and return_index[0] > equity_index[0]
    )
    checks["return_series_starts_after_entry_bar"] = first_return_after_first_bar
    if not first_return_after_first_bar:
        findings.append(
            "the return series starts on the same bar as the position, so the "
            "entry bar would earn its own return"
        )

    stamps = [pd.Timestamp(ts) for ts in equity_index]
    future = [ts for ts in stamps if ts.tz is not None and ts.to_pydatetime() > moment]
    checks["future_dated_bars"] = len(future)
    if future:
        findings.append(
            f"{len(future)} bar(s) are timestamped after {moment.isoformat()} - "
            "data stamped ahead of its availability"
        )

    spec = result.config.signal
    warmup = 0
    if spec.kind in {SignalKind.MOMENTUM, SignalKind.MEAN_REVERSION, SignalKind.LOW_VOLATILITY}:
        warmup = int(spec.lookback) + int(spec.skip)
    elif spec.kind is SignalKind.MA_CROSS:
        warmup = int(max(spec.fast, spec.slow))
    checks["required_warmup_bars"] = warmup
    checks["trim_warmup"] = bool(result.config.trim_warmup)
    if warmup and not result.config.trim_warmup:
        findings.append(
            f"the signal needs {warmup} bars of history and trim_warmup is off, so "
            "the first bars traded on an incomplete signal"
        )

    return {"clean": not findings, "findings": findings, "checks": checks}


def variance_ratio_test(returns: pd.Series, *, q: int = 5) -> dict[str, Any]:
    """Lo-MacKinlay variance-ratio test for a random walk in log returns.

    ``VR(q) = Var(q-period return) / (q * Var(1-period return))``. Under a
    random walk ``VR = 1``; above 1 means trending (positive autocorrelation),
    below 1 means mean reverting. The z-statistic uses the
    heteroskedasticity-robust variance, so it is not fooled by volatility
    clustering.

    Returns:
        ``{q, variance_ratio, z_stat, p_value, interpretation, observations}``,
        with ``None`` statistics when the series is too short.
    """
    series = returns.astype("float64").dropna()
    count = int(series.size)
    if count < q * 4 or q < 2:
        return {
            "q": int(q),
            "variance_ratio": None,
            "z_stat": None,
            "p_value": None,
            "observations": count,
            "note": f"need at least {q * 4} observations for a variance-ratio test",
        }
    log_returns = np.log1p(series.to_numpy())
    mean = float(log_returns.mean())
    deviations = log_returns - mean
    variance_1 = float(np.sum(deviations**2) / (count - 1))
    if variance_1 <= 0:
        return {
            "q": int(q),
            "variance_ratio": None,
            "z_stat": None,
            "p_value": None,
            "observations": count,
            "note": "the series has no variance",
        }
    cumulative = np.cumsum(log_returns)
    q_returns = cumulative[q - 1 :] - np.concatenate(([0.0], cumulative[: -q]))
    # Lo & MacKinlay's bias-corrected denominator for overlapping q-period sums.
    correction = q * (count - q + 1) * (1.0 - q / count)
    variance_q = float(np.sum((q_returns - q * mean) ** 2) / correction)
    ratio = variance_q / variance_1

    robust = 0.0
    denominator = float(np.sum(deviations**2)) ** 2
    for lag in range(1, q):
        numerator = float(np.sum(deviations[lag:] ** 2 * deviations[:-lag] ** 2))
        delta = numerator / denominator if denominator > 0 else 0.0
        weight = 2.0 * (q - lag) / q
        robust += weight**2 * delta
    if robust <= 0:
        return {
            "q": int(q),
            "variance_ratio": ratio,
            "z_stat": None,
            "p_value": None,
            "observations": count,
            "note": "the robust variance estimate collapsed; z-statistic unavailable",
        }
    z_stat = (ratio - 1.0) / math.sqrt(robust)
    p_value = float(2.0 * scipy_stats.norm.sf(abs(z_stat)))
    return {
        "q": int(q),
        "variance_ratio": float(ratio),
        "z_stat": float(z_stat),
        "p_value": p_value,
        "observations": count,
        "interpretation": (
            "random walk not rejected"
            if p_value >= 0.05
            else ("trending" if ratio > 1 else "mean reverting")
        ),
    }


# --------------------------------------------------------------------------
# gates
# --------------------------------------------------------------------------
def _gate(
    identifier: str,
    name: str,
    metric: str,
    comparator: str,
    threshold: float,
    observed: float | None,
    *,
    window: str = "full",
    hard: bool = False,
    detail: str | None = None,
) -> Gate:
    """Build a gate, deciding its status from the comparator and the observation."""
    if observed is None or (isinstance(observed, float) and not math.isfinite(observed)):
        return Gate(
            id=identifier,
            name=name,
            metric=metric,
            threshold=threshold,
            comparator=comparator,
            observed=None,
            status=GateStatus.PENDING,
            window=window,
            hard=hard,
            detail=detail or "not enough data to evaluate this gate",
        )
    passed = observed >= threshold if comparator == ">=" else observed <= threshold
    return Gate(
        id=identifier,
        name=name,
        metric=metric,
        threshold=threshold,
        comparator=comparator,
        observed=float(observed),
        status=GateStatus.PASS if passed else GateStatus.FAIL,
        window=window,
        hard=hard,
        detail=detail,
    )


def default_gates(thresholds: GateThresholds = DEFAULT_THRESHOLDS) -> list[Gate]:
    """Return the gate definitions with no observation yet.

    This is what the "gate suite · workspace defaults" panel renders before any
    backtest has been selected.
    """
    return [
        Gate(
            id="min_oos_sharpe",
            name=f"OOS Sharpe >= {thresholds.min_oos_sharpe:g}",
            metric="sharpe",
            threshold=thresholds.min_oos_sharpe,
            comparator=">=",
            observed=None,
            status=GateStatus.PENDING,
            window="out_of_sample",
        ),
        Gate(
            id="max_drawdown",
            name=f"Max drawdown <= {thresholds.max_drawdown:.0%}",
            metric="max_drawdown",
            threshold=thresholds.max_drawdown,
            comparator="<=",
            observed=None,
            status=GateStatus.PENDING,
        ),
        Gate(
            id="min_trades",
            name=f"Trades >= {thresholds.min_trades}",
            metric="trades",
            threshold=float(thresholds.min_trades),
            comparator=">=",
            observed=None,
            status=GateStatus.PENDING,
        ),
        Gate(
            id="min_history",
            name=f"History >= {thresholds.min_history_years:g} years",
            metric="years",
            threshold=thresholds.min_history_years,
            comparator=">=",
            observed=None,
            status=GateStatus.PENDING,
            hard=True,
        ),
        Gate(
            id="cost_sensitivity",
            name=f"Sharpe retained at {thresholds.cost_multiplier:g}x costs",
            metric="sharpe_retention",
            threshold=thresholds.min_cost_retention,
            comparator=">=",
            observed=None,
            status=GateStatus.PENDING,
            window="out_of_sample",
        ),
        Gate(
            id="benchmark_outperformance",
            name="OOS excess return over benchmark > 0",
            metric="excess_return",
            threshold=thresholds.min_excess_return,
            comparator=">=",
            observed=None,
            status=GateStatus.PENDING,
            window="out_of_sample",
        ),
        Gate(
            id="excess_return_t_test",
            name=f"Excess-return t-test p <= {thresholds.max_p_value:g}",
            metric="p_value",
            threshold=thresholds.max_p_value,
            comparator="<=",
            observed=None,
            status=GateStatus.PENDING,
        ),
        Gate(
            id="deflated_sharpe",
            name=f"Deflated Sharpe >= {thresholds.min_deflated_sharpe:g}",
            metric="deflated_sharpe",
            threshold=thresholds.min_deflated_sharpe,
            comparator=">=",
            observed=None,
            status=GateStatus.PENDING,
        ),
        Gate(
            id="cpcv_pbo",
            name=f"CPCV PBO <= {thresholds.max_pbo:.0%}",
            metric="pbo",
            threshold=thresholds.max_pbo,
            comparator="<=",
            observed=None,
            status=GateStatus.PENDING,
            detail=(
                f"combinatorially purged CSCV over {thresholds.cscv_groups} blocks of "
                "the hypothesis family's logged trials"
            ),
        ),
        Gate(
            id="regime_robustness",
            name=f"Worst-regime Sharpe >= {thresholds.min_regime_sharpe:g}",
            metric="worst_regime_sharpe",
            threshold=thresholds.min_regime_sharpe,
            comparator=">=",
            observed=None,
            status=GateStatus.PENDING,
            window="out_of_sample",
            detail=f"{thresholds.regime_buckets} trailing-volatility regimes",
        ),
        Gate(
            id="lookahead_scan",
            name="Lookahead scan clean",
            metric="lookahead_findings",
            threshold=0.0,
            comparator="<=",
            observed=None,
            status=GateStatus.PENDING,
            hard=True,
            detail="entry-bar timing, future-dated bars and signal warm-up",
        ),
        Gate(
            id="artifact_bundle",
            name="Artifact bundle pinned",
            metric="bundle_gaps",
            threshold=0.0,
            comparator="<=",
            observed=None,
            status=GateStatus.PENDING,
            hard=True,
            detail="config + data + code digests must all be present",
        ),
    ]


def evaluate_gates(
    result: BacktestResult,
    *,
    stressed: BacktestResult | None = None,
    thresholds: GateThresholds = DEFAULT_THRESHOLDS,
    trials: int = 1,
    trial_sharpe_std: float | None = None,
    peer_returns: Mapping[str, pd.Series] | None = None,
) -> tuple[list[Gate], dict[str, Any]]:
    """Evaluate every gate against a completed backtest.

    Pure computation: it takes results and returns gates, so the suite can be
    exercised against crafted results without touching the database.

    Args:
        result: the backtest being judged.
        stressed: the same backtest re-run with scaled costs. Without it the
            cost-sensitivity gate stays ``pending``.
        thresholds: the gate suite to apply.
        trials: number of trials charged to the hypothesis family.
        trial_sharpe_std: dispersion of those trials' per-period Sharpe ratios.

    Returns:
        ``(gates, tests)`` - the gate list and the raw statistical test output
        that produced it (t-test, deflated Sharpe, bootstrap, variance ratio).
    """
    window: BacktestStats = result.out_of_sample or result.stats
    window_label = "out_of_sample" if result.out_of_sample else "full"
    oos_returns = _window_returns(result, out_of_sample=True)
    oos_benchmark = _window_returns(result, out_of_sample=True, benchmark=True)

    t_test = excess_return_t_test(oos_returns, oos_benchmark)
    deflation = deflated_sharpe(
        oos_returns,
        trials=trials,
        trial_sharpe_std=trial_sharpe_std,
        periods_per_year=result.periods_per_year,
    )
    bootstrap = bootstrap_confidence(
        oos_returns, metric="sharpe", periods_per_year=result.periods_per_year
    )
    variance_ratio = variance_ratio_test(result.returns)

    gates = [
        _gate(
            "min_oos_sharpe",
            f"OOS Sharpe >= {thresholds.min_oos_sharpe:g}",
            "sharpe",
            ">=",
            thresholds.min_oos_sharpe,
            window.sharpe if window.periods > 1 else None,
            window=window_label,
            detail=None
            if result.out_of_sample
            else "no out-of-sample split configured; the full window was used",
        ),
        _gate(
            "max_drawdown",
            f"Max drawdown <= {thresholds.max_drawdown:.0%}",
            "max_drawdown",
            "<=",
            thresholds.max_drawdown,
            abs(result.stats.max_drawdown),
        ),
        _gate(
            "min_trades",
            f"Trades >= {thresholds.min_trades}",
            "trades",
            ">=",
            float(thresholds.min_trades),
            float(result.stats.trades),
        ),
        _gate(
            "min_history",
            f"History >= {thresholds.min_history_years:g} years",
            "years",
            ">=",
            thresholds.min_history_years,
            result.stats.years,
            hard=True,
            detail=f"{result.stats.periods} return observations at "
            f"{result.periods_per_year} periods/year",
        ),
    ]

    if stressed is None:
        gates.append(
            Gate(
                id="cost_sensitivity",
                name=f"Sharpe retained at {thresholds.cost_multiplier:g}x costs",
                metric="sharpe_retention",
                threshold=thresholds.min_cost_retention,
                comparator=">=",
                observed=None,
                status=GateStatus.PENDING,
                window=window_label,
                detail="the cost-stressed re-run has not been executed",
            )
        )
    else:
        base_sharpe = window.sharpe
        stressed_window = stressed.out_of_sample or stressed.stats
        stressed_sharpe = stressed_window.sharpe
        if base_sharpe <= 0:
            retention: float | None = None
            detail = "the base Sharpe is not positive, so retention is undefined"
        else:
            retention = stressed_sharpe / base_sharpe
            detail = (
                f"Sharpe {base_sharpe:.2f} -> {stressed_sharpe:.2f} at "
                f"{thresholds.cost_multiplier:g}x costs"
            )
        gate = _gate(
            "cost_sensitivity",
            f"Sharpe retained at {thresholds.cost_multiplier:g}x costs",
            "sharpe_retention",
            ">=",
            thresholds.min_cost_retention,
            retention,
            window=window_label,
            detail=detail,
        )
        # Retaining a share of a Sharpe that has gone negative is not a pass.
        if gate.status is GateStatus.PASS and stressed_sharpe <= 0:
            gate = Gate(
                id=gate.id,
                name=gate.name,
                metric=gate.metric,
                threshold=gate.threshold,
                comparator=gate.comparator,
                observed=gate.observed,
                status=GateStatus.FAIL,
                window=gate.window,
                hard=gate.hard,
                detail=f"{detail} - the stressed Sharpe is not positive",
            )
        gates.append(gate)

    excess = window.excess_return
    gates.append(
        _gate(
            "benchmark_outperformance",
            "OOS excess return over benchmark > 0",
            "excess_return",
            ">=",
            thresholds.min_excess_return,
            excess,
            window=window_label,
            detail=None
            if excess is not None
            else "no benchmark history was available for this window",
        )
    )
    gates.append(
        _gate(
            "excess_return_t_test",
            f"Excess-return t-test p <= {thresholds.max_p_value:g}",
            "p_value",
            "<=",
            thresholds.max_p_value,
            t_test.get("p_value"),
            window=window_label,
            detail=f"one-sided t-test on {t_test.get('basis')} "
            f"({t_test.get('observations')} observations)",
        )
    )
    gates.append(
        _gate(
            "deflated_sharpe",
            f"Deflated Sharpe >= {thresholds.min_deflated_sharpe:g}",
            "deflated_sharpe",
            ">=",
            thresholds.min_deflated_sharpe,
            deflation.get("deflated_sharpe"),
            window=window_label,
            detail=f"deflated for {deflation.get('trials')} trial(s) using "
            f"{deflation.get('basis', 'no dispersion estimate')}",
        )
    )

    # --- CPCV PBO over the hypothesis family's logged trials ---------------
    pbo = cscv_pbo(
        {"this run": oos_returns, **(peer_returns or {})},
        groups=thresholds.cscv_groups,
        periods_per_year=result.periods_per_year,
    )
    if pbo.get("pbo") is None and int(pbo.get("candidates") or 0) < 2:
        # PBO measures overfitting introduced by *choosing* between candidates.
        # A hypothesis family with a single logged trial made no such choice, so
        # the risk is genuinely absent rather than merely unmeasured - which is
        # a pass, with the reason stated so nobody reads it as a clean bill of
        # health for a suite that was never run.
        gates.append(
            Gate(
                id="cpcv_pbo",
                name=f"CPCV PBO <= {thresholds.max_pbo:.0%}",
                metric="pbo",
                threshold=thresholds.max_pbo,
                comparator="<=",
                observed=0.0,
                status=GateStatus.PASS,
                window=window_label,
                detail=(
                    "only one trial in this hypothesis family, so no selection "
                    "between candidates took place; run a second trial to make "
                    "this gate discriminating"
                ),
            )
        )
    else:
        gates.append(
            _gate(
                "cpcv_pbo",
                f"CPCV PBO <= {thresholds.max_pbo:.0%}",
                "pbo",
                "<=",
                thresholds.max_pbo,
                pbo.get("pbo"),
                window=window_label,
                detail=pbo.get("note")
                or (
                    f"{pbo['splits']} combinatorial splits over {pbo['candidates']} "
                    f"candidates sharing {pbo['observations']} observations"
                ),
            )
        )

    # --- regime robustness -------------------------------------------------
    regimes = regime_breakdown(
        oos_returns,
        buckets=thresholds.regime_buckets,
        periods_per_year=result.periods_per_year,
    )
    worst = regimes.get("worst_sharpe")
    gates.append(
        _gate(
            "regime_robustness",
            f"Worst-regime Sharpe >= {thresholds.min_regime_sharpe:g}",
            "worst_regime_sharpe",
            ">=",
            thresholds.min_regime_sharpe,
            worst,
            window=window_label,
            detail=regimes.get("note")
            or " · ".join(
                f"{entry['regime']} {entry['sharpe']:.2f}" for entry in regimes["buckets"]
            ),
        )
    )

    # --- lookahead scan ----------------------------------------------------
    scan = lookahead_scan(result)
    gates.append(
        _gate(
            "lookahead_scan",
            "Lookahead scan clean",
            "lookahead_findings",
            "<=",
            0.0,
            float(len(scan["findings"])),
            hard=True,
            detail="; ".join(scan["findings"])
            if scan["findings"]
            else "entry bar earns no return, no future-dated bars, warm-up satisfied",
        )
    )

    # --- artifact bundle ---------------------------------------------------
    bundle = result.artifact_bundle()
    required = {
        "config digest": bool((bundle.get("config") or {}).get("sha256")),
        "data digest": bool((bundle.get("data") or {}).get("sha256")),
        "code digest": bool((bundle.get("code") or {}).get("sha256")),
    }
    missing = [name for name, present in required.items() if not present]
    gates.append(
        _gate(
            "artifact_bundle",
            "Artifact bundle pinned",
            "bundle_gaps",
            "<=",
            0.0,
            float(len(missing)),
            hard=True,
            detail=(
                f"missing: {', '.join(missing)}"
                if missing
                else f"bundle {bundle.get('bundle_id')} pins config, data and code"
            ),
        )
    )

    tests = {
        "t_test": t_test,
        "deflated_sharpe": deflation,
        "bootstrap_sharpe": bootstrap,
        "variance_ratio": variance_ratio,
        "cpcv_pbo": pbo,
        "regimes": regimes,
        "lookahead": scan,
        "artifact_bundle": bundle,
        "cost_sensitivity": None
        if stressed is None
        else {
            "multiplier": thresholds.cost_multiplier,
            "base_sharpe": window.sharpe,
            "stressed_sharpe": (stressed.out_of_sample or stressed.stats).sharpe,
            "base_cagr": window.cagr,
            "stressed_cagr": (stressed.out_of_sample or stressed.stats).cagr,
            "base_costs_bps": result.config.costs.total_bps,
            "stressed_costs_bps": stressed.config.costs.total_bps,
        },
    }
    return gates, tests


def _window_returns(
    result: BacktestResult, *, out_of_sample: bool, benchmark: bool = False
) -> pd.Series:
    """Return the strategy (or benchmark) returns of one window of a result."""
    series = result.benchmark_returns if benchmark else result.returns
    if series is None:
        return pd.Series(dtype="float64")
    if result.oos_start is None or not out_of_sample:
        return series
    index = pd.DatetimeIndex(series.index)
    boundary = pd.Timestamp(result.oos_start)
    if index.tz is not None:
        boundary = boundary.tz_localize(index.tz)
    return series[np.asarray(index >= boundary)]


# --------------------------------------------------------------------------
# ORM rows
# --------------------------------------------------------------------------
class ValidationReportRow(Base):
    """SQLite row for one validation report."""

    __tablename__ = "validation_reports"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    backtest_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    hypothesis_id: Mapped[str | None] = mapped_column(String(64), default=None, index=True)
    experiment_id: Mapped[str | None] = mapped_column(String(64), default=None, index=True)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    gates: Mapped[list[dict[str, Any]]] = mapped_column(JSON(), nullable=False)
    tests: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict, nullable=False)
    thresholds: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict, nullable=False)
    trials: Mapped[int] = mapped_column(Integer(), default=1, nullable=False)
    promoted: Mapped[bool] = mapped_column(Boolean(), default=False, nullable=False)
    overrides: Mapped[list[str]] = mapped_column(JSON(), default=list, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text(), default=None)


class PromotedStrategyRow(Base):
    """SQLite row for a strategy that has been promoted out of research."""

    __tablename__ = "promoted_strategies"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    backtest_id: Mapped[str] = mapped_column(String(64), nullable=False)
    report_id: Mapped[str] = mapped_column(String(64), nullable=False)
    experiment_id: Mapped[str | None] = mapped_column(String(64), default=None)
    hypothesis_id: Mapped[str | None] = mapped_column(String(64), default=None)
    stage: Mapped[str] = mapped_column(String(16), default=ExperimentStage.PAPER, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    promoted_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    promoted_by: Mapped[str | None] = mapped_column(String(64), default=None)
    comment: Mapped[str | None] = mapped_column(Text(), default=None)
    overrides: Mapped[list[str]] = mapped_column(JSON(), default=list, nullable=False)
    spec: Mapped[dict[str, Any]] = mapped_column(JSON(), nullable=False)
    baseline: Mapped[dict[str, Any]] = mapped_column(JSON(), default=dict, nullable=False)
    tracking: Mapped[dict[str, Any] | None] = mapped_column(JSON(), default=None)
    tracked_at: Mapped[datetime | None] = mapped_column(DateTime(), default=None)


class PromotionApprovalRow(Base):
    """One person's sign-off on promoting a backtest.

    Promotion is a two-key operation: the row is written when the first
    approver signs, and the strategy is only created when a *second, different*
    approver signs the same backtest for the same stage. The rows survive the
    promotion so the audit trail can always answer "who released this".
    """

    __tablename__ = "promotion_approvals"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    backtest_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    stage: Mapped[str] = mapped_column(String(16), nullable=False, default=ExperimentStage.PAPER)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text(), default=None)
    overrides: Mapped[list[str]] = mapped_column(JSON(), default=list, nullable=False)
    approved_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, index=True)
    #: Set when this approval is the one that completed the promotion.
    strategy_id: Mapped[str | None] = mapped_column(String(64), default=None)


_OWNED_TABLES = (
    ValidationReportRow.__table__,
    PromotedStrategyRow.__table__,
    PromotionApprovalRow.__table__,
)

def ensure_schema() -> None:
    """Create the validation tables if they do not exist yet, then migrate them."""
    Base.metadata.create_all(get_engine(), tables=list(_OWNED_TABLES))
    for table in _OWNED_TABLES:
        ensure_columns(table)


# --------------------------------------------------------------------------
# engine
# --------------------------------------------------------------------------
class ValidationEngine:
    """Evaluates gates, runs the statistics behind them and records promotions."""

    def __init__(
        self,
        session_factory: sessionmaker[Session] | None = None,
        *,
        bus: EventBus | None = None,
        backtests: BacktestEngine | None = None,
        experiments: ExperimentEngine | None = None,
        thresholds: GateThresholds | None = None,
        approvals_required: int = PROMOTION_APPROVALS_REQUIRED,
    ) -> None:
        """Create the engine.

        Args:
            session_factory: SQLAlchemy session factory; defaults to the
                process-wide one.
            bus: event bus reports and promotions are published on.
            backtests: backtest engine used for the cost-stressed re-run.
            experiments: experiment engine used to advance funnel stages.
            thresholds: the gate suite; defaults to :data:`DEFAULT_THRESHOLDS`.
            approvals_required: how many distinct people must sign a promotion.

        Raises:
            ValueError: when fewer than one approver is required.
        """
        if int(approvals_required) < 1:
            raise ValueError("a promotion needs at least one approver")
        self._session_factory = session_factory
        self._bus = bus
        self._backtests = backtests
        self._experiments = experiments
        self.thresholds = thresholds or DEFAULT_THRESHOLDS
        self.approvals_required = int(approvals_required)
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
        """The event bus validation events are published on."""
        if self._bus is None:
            self._bus = get_event_bus()
        return self._bus

    @property
    def backtests(self) -> BacktestEngine:
        """The backtest engine used to reproduce and stress a run."""
        if self._backtests is None:
            self._backtests = get_backtest_engine()
        return self._backtests

    @property
    def experiments(self) -> ExperimentEngine:
        """The experiment engine used to advance funnel stages."""
        if self._experiments is None:
            self._experiments = ExperimentEngine(self._session_factory, bus=self._bus)
        return self._experiments

    def default_gates(self) -> list[Gate]:
        """Return the default gate definitions with no observation yet."""
        return default_gates(self.thresholds)

    # --- trial ledger -----------------------------------------------------
    def trial_ledger(self, hypothesis_id: str | None) -> dict[str, Any]:
        """Return the real trial count and Sharpe dispersion of a family.

        "Trials" are the completed backtest runs charged to the hypothesis
        (every experiment run creates exactly one, so nothing is double
        counted). Their out-of-sample Sharpe ratios give the dispersion the
        deflated Sharpe needs; with fewer than two the deflation falls back to
        the null's own dispersion and says so.
        """
        runs = self.backtests.list_runs(hypothesis_id=hypothesis_id, limit=1000)
        completed = [r for r in runs if r.stats]
        sharpes: list[float] = []
        periods_per_year = TRADING_DAYS_PER_YEAR
        for record in completed:
            stats = record.out_of_sample or record.stats or {}
            value = stats.get("sharpe")
            if value is None:
                continue
            periods_per_year = record.periods_per_year or periods_per_year
            sharpes.append(float(value) / math.sqrt(periods_per_year))
        dispersion = (
            float(np.std(np.array(sharpes), ddof=1)) if len(sharpes) > 1 else None
        )
        return {
            "hypothesis_id": hypothesis_id,
            "trials": max(1, len(completed)),
            "runs": len(runs),
            "sharpe_std": dispersion,
        }

    # --- evaluation -------------------------------------------------------
    def peer_returns(
        self, backtest_id: str, hypothesis_id: str | None, *, limit: int = 24
    ) -> dict[str, pd.Series]:
        """Return the sibling trials the CPCV PBO gate selects between.

        The candidates are the *other* completed backtests charged to the same
        hypothesis family - exactly the trials the deflated Sharpe already
        counts. Their return series are reconstructed from the equity curves
        persisted with each run, so no backtest is re-executed to build the
        matrix.

        Args:
            backtest_id: the run being validated (excluded from its own peers).
            hypothesis_id: the family; ``None`` yields no peers, because runs
                that share no hypothesis were never selected between.
            limit: at most this many peers, newest first.

        Returns:
            ``{run id: period returns}``, empty when the family has no siblings.
        """
        if not hypothesis_id:
            return {}
        out: dict[str, pd.Series] = {}
        for record in self.backtests.list_runs(hypothesis_id=hypothesis_id, limit=limit + 1):
            if record.id == backtest_id or record.status is not BacktestStatus.COMPLETE:
                continue
            curve = record.equity_curve or {}
            dates = curve.get("dates") or []
            equity = curve.get("equity") or []
            if len(dates) < 8 or len(dates) != len(equity):
                continue
            series = pd.Series(
                [float(v) for v in equity], index=pd.DatetimeIndex(dates), name=record.id
            )
            # A thinned curve is still a valid return path; the CSCV split works
            # on whatever grid the candidates actually share.
            returns = series.pct_change().dropna()
            if returns.size >= 8:
                out[record.id] = returns
        return out

    async def evaluate(
        self,
        backtest_id: str,
        *,
        thresholds: GateThresholds | None = None,
        persist: bool = True,
        publish: bool = True,
    ) -> ValidationReport:
        """Evaluate every gate against a completed backtest.

        The run is reproduced from its persisted specification (the backtester
        is deterministic, so this recovers the exact return series) and then
        re-run once more with the cost model scaled by
        ``thresholds.cost_multiplier`` for the cost-sensitivity gate.

        Raises:
            KeyError: when the backtest is unknown.
            ValueError: when the backtest never completed.
        """
        record = self.backtests.get(backtest_id)
        if record.stats is None:
            raise ValueError(f"backtest {backtest_id!r} has no results to validate")
        suite = thresholds or self.thresholds
        ledger = self.trial_ledger(record.hypothesis_id)

        result = await self.backtests.rerun(record.config, cost_multiplier=1.0)
        stressed = await self.backtests.rerun(
            record.config, cost_multiplier=suite.cost_multiplier
        )
        result.run_id = backtest_id
        gates, tests = evaluate_gates(
            result,
            stressed=stressed,
            thresholds=suite,
            trials=int(ledger["trials"]),
            trial_sharpe_std=ledger["sharpe_std"],
            peer_returns=self.peer_returns(backtest_id, record.hypothesis_id),
        )
        report = ValidationReport(
            id=new_report_id(),
            backtest_id=backtest_id,
            evaluated_at=utcnow(),
            gates=gates,
            tests=tests,
            thresholds=suite,
            hypothesis_id=record.hypothesis_id,
            experiment_id=record.experiment_id,
            trials=int(ledger["trials"]),
        )
        self._hydrate_approvals(report)
        if persist:
            self._save_report(report)
        if publish:
            await self.bus.publish(
                LAB_CHANNEL,
                {
                    "kind": "validation",
                    "phase": "evaluated",
                    "id": report.id,
                    "backtest_id": backtest_id,
                    "hypothesis_id": record.hypothesis_id,
                    "verdict": report.verdict,
                    "score": report.score,
                    "gates_passed": sum(1 for gate in gates if gate.passed),
                    "gates_total": len(gates),
                    "ts": report.evaluated_at.isoformat(),
                },
            )
        return report

    def get_report(self, report_id: str) -> ValidationReport:
        """Return one persisted report.

        Raises:
            KeyError: when the identifier is unknown.
        """
        with self._session() as session:
            row = session.get(ValidationReportRow, report_id)
            if row is None:
                raise KeyError(f"unknown validation report {report_id!r}")
            report = _report_from_row(row)
        return self._hydrate_approvals(report)

    def reports(
        self,
        *,
        backtest_id: str | None = None,
        hypothesis_id: str | None = None,
        limit: int = 100,
    ) -> list[ValidationReport]:
        """Return persisted reports, newest first."""
        statement = (
            select(ValidationReportRow)
            .order_by(ValidationReportRow.evaluated_at.desc())
            .limit(limit)
        )
        if backtest_id:
            statement = statement.where(ValidationReportRow.backtest_id == backtest_id)
        if hypothesis_id:
            statement = statement.where(ValidationReportRow.hypothesis_id == hypothesis_id)
        with self._session() as session:
            found = [_report_from_row(row) for row in session.scalars(statement)]
        return [self._hydrate_approvals(report) for report in found]

    def _hydrate_approvals(self, report: ValidationReport) -> ValidationReport:
        """Attach the live promotion sign-offs to a report read back from disk.

        Approvals live in their own table so they can be added and withdrawn
        without rewriting the report; a report is therefore only complete once
        they are joined back on.
        """
        report.approvals = self.promotion_approvals(report.backtest_id)
        report.approvals_required = self.approvals_required
        return report

    def latest_report(self, backtest_id: str) -> ValidationReport | None:
        """Return the newest report for a backtest, or ``None``."""
        found = self.reports(backtest_id=backtest_id, limit=1)
        return found[0] if found else None

    def _save_report(self, report: ValidationReport) -> None:
        """Persist a report."""
        with self._session() as session:
            session.add(
                ValidationReportRow(
                    id=report.id,
                    backtest_id=report.backtest_id,
                    hypothesis_id=report.hypothesis_id,
                    experiment_id=report.experiment_id,
                    evaluated_at=report.evaluated_at,
                    gates=[gate.as_dict() for gate in report.gates],
                    tests=report.tests,
                    thresholds=report.thresholds.as_dict(),
                    trials=report.trials,
                    promoted=report.promoted,
                    overrides=list(report.overrides),
                    comment=report.comment,
                )
            )

    # --- promotion --------------------------------------------------------
    async def promote(
        self,
        backtest_id: str,
        *,
        comment: str | None = None,
        overrides: Sequence[str] | None = None,
        stage: ExperimentStage = ExperimentStage.PAPER,
        actor: str | None = None,
        publish: bool = True,
    ) -> ValidationReport:
        """Promote a strategy once its gates pass **and two people have signed**.

        Promotion is a two-key operation. The first call records that approver's
        sign-off and returns the report with ``promotion_pending`` set - nothing
        is promoted yet. The second call, by a *different* named approver,
        creates the strategy. A person cannot sign twice, and an unnamed actor
        cannot sign at all: an approval nobody is accountable for is not an
        approval.

        Args:
            backtest_id: the run being promoted.
            comment: required when any gate is overridden - the UI's
                "notes · required for override" field.
            overrides: gate ids the promoter accepts despite the failure. Hard
                gates can never be overridden.
            stage: the funnel stage to advance the experiment to.
            actor: who approved it (recorded in the audit trail).

        Returns:
            The freshly evaluated report. ``promoted`` is true only once the
            required number of distinct approvers has signed; until then
            ``approvals`` lists who has, and ``promotion_pending`` is true.

        Raises:
            KeyError: when the backtest is unknown.
            PermissionError: when gates are outstanding and not overridden, a
                hard gate is failing, an override arrives without a note, the
                approver is unnamed, or the same person signs twice.
        """
        accepted = {str(item) for item in overrides or []}
        # Evaluated without persisting: the report is written once below, with
        # the promotion decision on it, so a blocked attempt leaves no orphan.
        report = await self.evaluate(backtest_id, publish=False, persist=False)
        blocking = [gate for gate in report.failed_gates if gate.id not in accepted]
        hard_overridden = [
            gate for gate in report.failed_gates if gate.hard and gate.id in accepted
        ]
        if hard_overridden:
            names = ", ".join(gate.id for gate in hard_overridden)
            raise PermissionError(f"these gates cannot be overridden: {names}")
        if blocking:
            names = ", ".join(f"{gate.id} ({gate.status})" for gate in blocking)
            raise PermissionError(f"promotion blocked by: {names}")
        if accepted and not (comment and comment.strip()):
            raise PermissionError("overriding a gate requires a note explaining why")

        signer = (actor or "").strip()
        if not signer:
            raise PermissionError(
                "promotion needs a named approver - an approval nobody is "
                "accountable for is not an approval"
            )
        existing = self.promotion_approvals(backtest_id, stage=stage)
        if any(entry["actor"].lower() == signer.lower() for entry in existing):
            raise PermissionError(
                f"{signer} has already signed this promotion; a second, different "
                "approver is required"
            )
        signatures = [entry["actor"] for entry in existing] + [signer]
        approval_id = f"pa-{uuid.uuid4().hex[:12]}"
        signed_at = utcnow()
        complete = len(signatures) >= self.approvals_required

        if not complete:
            with self._session() as session:
                session.add(
                    PromotionApprovalRow(
                        id=approval_id,
                        backtest_id=backtest_id,
                        stage=str(stage),
                        actor=signer,
                        comment=comment,
                        overrides=sorted(accepted),
                        approved_at=signed_at,
                    )
                )
            report.overrides = sorted(accepted)
            report.comment = comment
            report.approvals = self.promotion_approvals(backtest_id, stage=stage)
            report.approvals_required = self.approvals_required
            self._save_report(report)
            outstanding = self.approvals_required - len(signatures)
            logger.info(
                "promotion of %s signed by %s; %d more approval(s) needed",
                backtest_id,
                signer,
                outstanding,
            )
            if publish:
                await self.bus.publish(
                    LAB_CHANNEL,
                    {
                        "kind": "promotion",
                        "phase": "pending",
                        "backtest_id": backtest_id,
                        "report_id": report.id,
                        "stage": str(stage),
                        "actor": signer,
                        "approvals": report.approvals,
                        "approvals_required": self.approvals_required,
                        "outstanding": outstanding,
                        "ts": signed_at.isoformat(),
                    },
                )
            return report

        record = self.backtests.get(backtest_id)
        strategy_id = new_strategy_id()
        now = utcnow()
        with self._session() as session:
            session.add(
                PromotionApprovalRow(
                    id=approval_id,
                    backtest_id=backtest_id,
                    stage=str(stage),
                    actor=signer,
                    comment=comment,
                    overrides=sorted(accepted),
                    approved_at=signed_at,
                    strategy_id=strategy_id,
                )
            )
            # Every signature that released this strategy is stamped with it,
            # not just the last one: they all approved it, and none of them may
            # be withdrawn afterwards.
            for row in session.scalars(
                select(PromotionApprovalRow).where(
                    PromotionApprovalRow.backtest_id == backtest_id,
                    PromotionApprovalRow.stage == str(stage),
                    PromotionApprovalRow.strategy_id.is_(None),
                )
            ).all():
                row.strategy_id = strategy_id
        report.promoted = True
        report.overrides = sorted(accepted)
        report.comment = comment
        report.approvals = self.promotion_approvals(backtest_id, stage=stage)
        report.approvals_required = self.approvals_required
        self._save_report(report)

        with self._session() as session:
            session.add(
                PromotedStrategyRow(
                    id=strategy_id,
                    name=record.name,
                    backtest_id=backtest_id,
                    report_id=report.id,
                    experiment_id=record.experiment_id,
                    hypothesis_id=record.hypothesis_id,
                    stage=str(stage),
                    status="active",
                    promoted_at=now,
                    # Both signatures, in the order they were given: the row can
                    # never claim a single person released this.
                    promoted_by=" + ".join(signatures),
                    comment=comment,
                    overrides=sorted(accepted),
                    spec=record.config.as_dict(),
                    baseline=record.out_of_sample or record.stats or {},
                )
            )
        if record.experiment_id:
            try:
                self.experiments.set_stage(record.experiment_id, stage)
            except KeyError:  # pragma: no cover - the experiment may be gone
                logger.warning("experiment %s vanished before promotion", record.experiment_id)
        if record.hypothesis_id:
            from tradinglib.research.hypotheses import HypothesisRegistry, HypothesisStatus

            registry = HypothesisRegistry(self._session_factory, bus=self._bus)
            try:
                current = registry.get(record.hypothesis_id)
                if current.status is not HypothesisStatus.VALIDATED:
                    if current.status is HypothesisStatus.DRAFT:
                        registry.set_status(record.hypothesis_id, HypothesisStatus.TESTING)
                    registry.set_status(
                        record.hypothesis_id,
                        HypothesisStatus.VALIDATED,
                        note=f"promoted via {backtest_id}",
                        actor=actor,
                    )
            except (KeyError, ValueError) as exc:
                logger.warning("could not validate hypothesis on promotion: %s", exc)

        if publish:
            await self.bus.publish(
                LAB_CHANNEL,
                {
                    "kind": "promotion",
                    "phase": "promoted",
                    "id": strategy_id,
                    "backtest_id": backtest_id,
                    "report_id": report.id,
                    "hypothesis_id": record.hypothesis_id,
                    "stage": str(stage),
                    "overrides": sorted(accepted),
                    "actor": signer,
                    "approvals": report.approvals,
                    "approvals_required": self.approvals_required,
                    "ts": now.isoformat(),
                },
            )
        logger.info(
            "strategy %s promoted from %s to %s by %s",
            strategy_id,
            backtest_id,
            stage,
            " + ".join(signatures),
        )
        return report

    # --- promotion approvals ---------------------------------------------
    def promotion_approvals(
        self, backtest_id: str, *, stage: ExperimentStage | str | None = None
    ) -> list[dict[str, Any]]:
        """Return the sign-offs recorded against one backtest, oldest first."""
        statement = (
            select(PromotionApprovalRow)
            .where(PromotionApprovalRow.backtest_id == backtest_id)
            .order_by(PromotionApprovalRow.approved_at.asc())
        )
        if stage is not None:
            statement = statement.where(PromotionApprovalRow.stage == str(stage))
        with self._session() as session:
            return [
                {
                    "id": row.id,
                    "backtest_id": row.backtest_id,
                    "stage": row.stage,
                    "actor": row.actor,
                    "comment": row.comment,
                    "overrides": list(row.overrides or []),
                    "approved_at": to_utc(row.approved_at).isoformat(),
                    "strategy_id": row.strategy_id,
                }
                for row in session.scalars(statement)
            ]

    def promotion_state(
        self, backtest_id: str, *, stage: ExperimentStage | str = ExperimentStage.PAPER
    ) -> dict[str, Any]:
        """Return the two-key state the promotion queue renders.

        The payload is what the Pipeline tab's "needs 2nd approval ·
        ana ✓ · marco pending" line is built from.
        """
        approvals = self.promotion_approvals(backtest_id, stage=stage)
        signed = [entry["actor"] for entry in approvals]
        outstanding = max(0, self.approvals_required - len(signed))
        return {
            "backtest_id": backtest_id,
            "stage": str(stage),
            "required": self.approvals_required,
            "signed": signed,
            "approvals": approvals,
            "outstanding": outstanding,
            "complete": outstanding == 0,
            "label": (
                "approved"
                if outstanding == 0
                else (
                    f"needs {_ordinal(len(signed) + 1)} approval"
                    if signed
                    else f"needs {self.approvals_required} approvals"
                )
            ),
        }

    def withdraw_promotion_approval(self, backtest_id: str, actor: str) -> int:
        """Withdraw one person's outstanding sign-off. Returns rows removed.

        Only an approval that has not yet released a strategy can be withdrawn -
        a promotion that already happened is history, not a draft.
        """
        removed = 0
        with self._session() as session:
            rows = session.scalars(
                select(PromotionApprovalRow).where(
                    PromotionApprovalRow.backtest_id == backtest_id,
                    PromotionApprovalRow.strategy_id.is_(None),
                )
            ).all()
            for row in rows:
                if row.actor.lower() == actor.strip().lower():
                    session.delete(row)
                    removed += 1
        return removed

    def pending_promotions(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Return backtests that carry a partial set of sign-offs."""
        with self._session() as session:
            rows = session.scalars(
                select(PromotionApprovalRow)
                .where(PromotionApprovalRow.strategy_id.is_(None))
                .order_by(PromotionApprovalRow.approved_at.desc())
                .limit(max(1, limit) * self.approvals_required)
            ).all()
            seen: dict[tuple[str, str], None] = {}
            for row in rows:
                seen.setdefault((row.backtest_id, row.stage), None)
        return [
            self.promotion_state(backtest_id, stage=stage)
            for backtest_id, stage in list(seen)[:limit]
        ]

    def promoted(self, *, include_killed: bool = True) -> list[dict[str, Any]]:
        """Return the promoted strategies, newest first."""
        statement = select(PromotedStrategyRow).order_by(PromotedStrategyRow.promoted_at.desc())
        if not include_killed:
            statement = statement.where(PromotedStrategyRow.status == "active")
        with self._session() as session:
            return [_strategy_dict(row) for row in session.scalars(statement)]

    def get_strategy(self, strategy_id: str) -> dict[str, Any]:
        """Return one promoted strategy.

        Raises:
            KeyError: when the identifier is unknown.
        """
        with self._session() as session:
            row = session.get(PromotedStrategyRow, strategy_id)
            if row is None:
                raise KeyError(f"unknown strategy {strategy_id!r}")
            return _strategy_dict(row)

    def set_strategy_status(
        self, strategy_id: str, status: str, *, note: str | None = None
    ) -> dict[str, Any]:
        """Mark a promoted strategy ``active``, ``held`` or ``killed``.

        Raises:
            KeyError: when the identifier is unknown.
            ValueError: on an unknown status.
        """
        allowed = {"active", "held", "killed"}
        if status not in allowed:
            raise ValueError(f"unknown status {status!r}; use one of {', '.join(sorted(allowed))}")
        with self._session() as session:
            row = session.get(PromotedStrategyRow, strategy_id)
            if row is None:
                raise KeyError(f"unknown strategy {strategy_id!r}")
            row.status = status
            if note:
                row.comment = note
            return _strategy_dict(row)


def _report_from_row(row: ValidationReportRow) -> ValidationReport:
    """Map a report row onto the dataclass."""
    return ValidationReport(
        id=row.id,
        backtest_id=row.backtest_id,
        evaluated_at=to_utc(row.evaluated_at),
        gates=[
            Gate(
                id=item["id"],
                name=item["name"],
                metric=item["metric"],
                threshold=float(item["threshold"]),
                comparator=item["comparator"],
                observed=item.get("observed"),
                status=GateStatus(item["status"]),
                window=item.get("window", "full"),
                hard=bool(item.get("hard", False)),
                detail=item.get("detail"),
            )
            for item in row.gates or []
        ],
        promoted=bool(row.promoted),
        tests=dict(row.tests or {}),
        thresholds=GateThresholds.from_dict(row.thresholds),
        hypothesis_id=row.hypothesis_id,
        experiment_id=row.experiment_id,
        trials=int(row.trials or 1),
        overrides=list(row.overrides or []),
        comment=row.comment,
    )


def _strategy_dict(row: PromotedStrategyRow) -> dict[str, Any]:
    """Map a promoted-strategy row onto its API payload."""
    return {
        "id": row.id,
        "name": row.name,
        "backtest_id": row.backtest_id,
        "report_id": row.report_id,
        "experiment_id": row.experiment_id,
        "hypothesis_id": row.hypothesis_id,
        "stage": row.stage,
        "status": row.status,
        "promoted_at": to_utc(row.promoted_at).isoformat(),
        "promoted_by": row.promoted_by,
        "comment": row.comment,
        "overrides": list(row.overrides or []),
        "spec": dict(row.spec or {}),
        "baseline": dict(row.baseline or {}),
        "tracking": row.tracking,
        "tracked_at": to_utc(row.tracked_at).isoformat() if row.tracked_at else None,
    }


def new_report_id() -> str:
    """Return a fresh validation-report identifier."""
    return f"val-{uuid.uuid4().hex[:12]}"


def new_strategy_id() -> str:
    """Return a fresh promoted-strategy identifier."""
    return f"strat-{uuid.uuid4().hex[:10]}"


_engine: ValidationEngine | None = None


def get_validation_engine() -> ValidationEngine:
    """Return the process-wide validation engine, creating it on first use."""
    global _engine
    if _engine is None:
        _engine = ValidationEngine()
    return _engine


def reset_validation_engine(engine: ValidationEngine | None = None) -> ValidationEngine:
    """Replace the process-wide engine. Intended for tests."""
    global _engine
    _engine = engine if engine is not None else ValidationEngine()
    return _engine


__all__ = [
    "DEFAULT_THRESHOLDS",
    "Gate",
    "GateStatus",
    "GateThresholds",
    "PromotedStrategyRow",
    "ValidationEngine",
    "ValidationReport",
    "ValidationReportRow",
    "bootstrap_confidence",
    "default_gates",
    "deflated_sharpe",
    "ensure_schema",
    "evaluate_gates",
    "excess_return_t_test",
    "get_validation_engine",
    "new_report_id",
    "new_strategy_id",
    "reset_validation_engine",
    "variance_ratio_test",
]
