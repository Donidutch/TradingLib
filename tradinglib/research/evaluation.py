"""Aggregated research views: candidates, promotions and gate outcomes.

Nothing here computes new statistics from scratch - it reads what the
hypothesis registry, the experiment engine, the backtest ledger and the
validation reports actually recorded and arranges it into the four views the
Strategy Lab's evaluation tab shows:

* :meth:`EvaluationEngine.candidates` - every experiment that has been
  backtested, with the outcome of its newest validation report;
* :meth:`EvaluationEngine.scorecard` - the same candidates side by side on one
  set of rows (the "evidence" table);
* :meth:`EvaluationEngine.promoted` / :meth:`EvaluationEngine.update` -
  promoted strategies tracked against what their backtest promised. Tracking is
  a genuine out-of-sample measurement: the promoted specification is re-run
  over data that did not exist when it was promoted, and the realised window is
  compared with the promised one (including a block-bootstrap band, so "in
  band" means something);
* :meth:`EvaluationEngine.gate_outcomes` - how each gate has behaved over time,
  which is the honest answer to "are we getting better at this, or just
  running more trials?".
"""

from __future__ import annotations

import logging
import math
from collections import Counter, defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session, sessionmaker

from tradinglib.data.base import utcnow
from tradinglib.db import get_session_factory
from tradinglib.events import EventBus, get_event_bus
from tradinglib.research.backtest import (
    LAB_CHANNEL,
    BacktestConfig,
    BacktestEngine,
    compute_stats,
    get_backtest_engine,
)
from tradinglib.research.analyses import AnalysisEngine, AnalysisKind, AnalysisStatus
from tradinglib.research.capacity import (
    CapacityEngine,
    holdings_from_trade_log,
)
from tradinglib.research.experiments import ExperimentEngine, ExperimentStage
from tradinglib.research.hypotheses import HypothesisRegistry, HypothesisStatus
from tradinglib.research.livebook import LiveBook, LiveBookEngine, returns_from_curve
from tradinglib.research.validation import (
    PromotedStrategyRow,
    ValidationEngine,
    bootstrap_confidence,
)

logger = logging.getLogger(__name__)

#: Why a scorecard cell is empty when the candidate's backtest has no attached
#: sweep / Monte-Carlo / walk-forward analysis to join from.
NO_ANALYSIS: str = "no sweep/MC/WFA analysis recorded"

#: Fallback reason when the traded book cannot supply a return series at all.
NO_LIVE_BOOK: str = "no live book"

#: The evidence rows joined or computed on top of the plain backtest metrics,
#: as ``(key, label, format)``. ``format`` is the unit the UI renders the cell in.
EVIDENCE_ROWS: tuple[tuple[str, str, str], ...] = (
    ("sweep_stability", "Sweep stability", "text"),
    ("mc_prob_sharpe_above_one", "MC: P(Sharpe>1)", "percent"),
    ("mc_p95_max_drawdown", "MC: p95 max DD", "percent"),
    ("wfa_positive_folds", "WFA positive folds", "text"),
    ("live_book_correlation", "Corr. to live book", "ratio"),
    ("capacity_usd", "Capacity est.", "usd"),
)


def _missing_cell(reason: str) -> dict[str, Any]:
    """Return an empty scorecard cell that says why it is empty."""
    return {"value": None, "display": None, "note": reason, "tone": None}


def _metric_gap(label: str, key: str, candidate: dict[str, Any]) -> str:
    """Say why a plain backtest metric has no value for this candidate."""
    if key == "deflated_sharpe" and not candidate.get("report_id"):
        return "no validation report has been run on this backtest, so no deflated Sharpe"
    if key == "excess_return" and not candidate.get("backtest_id"):
        return "the run has no completed backtest"
    return f"the stored run recorded no {label.lower()}"


@dataclass(slots=True)
class EvaluationRecord:
    """Tracking record for a promoted strategy.

    ``live_*`` figures come from re-running the promoted specification over the
    window since promotion; ``expected_*`` are the figures its backtest
    promised. ``decay_score`` is ``(expected - live) / expected`` on the Sharpe
    ratio: ``0`` means the edge held, ``1`` means it is gone, negative means the
    strategy did better live than in the backtest.
    """

    strategy_id: str
    name: str
    promoted_on: date
    stage: str = "paper"
    status: str = "active"
    days_live: int = 0
    observations: int = 0
    live_return: float | None = None
    live_cagr: float | None = None
    live_sharpe: float | None = None
    live_max_drawdown: float | None = None
    expected_return: float | None = None
    expected_cagr: float | None = None
    expected_sharpe: float | None = None
    benchmark_return: float | None = None
    tracking_error: float | None = None
    decay_score: float | None = None
    in_band: bool | None = None
    band: dict[str, float] | None = None
    updated_at: datetime | None = None
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping for the API layer."""
        return {
            "strategy_id": self.strategy_id,
            "name": self.name,
            "promoted_on": self.promoted_on.isoformat(),
            "stage": self.stage,
            "status": self.status,
            "days_live": self.days_live,
            "observations": self.observations,
            "live_return": self.live_return,
            "live_cagr": self.live_cagr,
            "live_sharpe": self.live_sharpe,
            "live_max_drawdown": self.live_max_drawdown,
            "expected_return": self.expected_return,
            "expected_cagr": self.expected_cagr,
            "expected_sharpe": self.expected_sharpe,
            "benchmark_return": self.benchmark_return,
            "tracking_error": self.tracking_error,
            "decay_score": self.decay_score,
            "in_band": self.in_band,
            "band": self.band,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "notes": list(self.notes),
        }


class EvaluationEngine:
    """Compares realised performance with backtested expectations."""

    def __init__(
        self,
        session_factory: sessionmaker[Session] | None = None,
        *,
        bus: EventBus | None = None,
        backtests: BacktestEngine | None = None,
        experiments: ExperimentEngine | None = None,
        validation: ValidationEngine | None = None,
        hypotheses: HypothesisRegistry | None = None,
        analyses: AnalysisEngine | None = None,
        livebook: LiveBookEngine | None = None,
        capacity: CapacityEngine | None = None,
    ) -> None:
        """Create the engine, wiring it to the registries it aggregates."""
        self._session_factory = session_factory
        self._bus = bus
        self._backtests = backtests
        self._experiments = experiments
        self._validation = validation
        self._hypotheses = hypotheses
        self._analyses = analyses
        self._livebook = livebook
        self._capacity = capacity

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
        """The event bus tracking updates are published on."""
        if self._bus is None:
            self._bus = get_event_bus()
        return self._bus

    @property
    def backtests(self) -> BacktestEngine:
        """The backtest ledger."""
        if self._backtests is None:
            self._backtests = get_backtest_engine()
        return self._backtests

    @property
    def experiments(self) -> ExperimentEngine:
        """The experiment registry."""
        if self._experiments is None:
            self._experiments = ExperimentEngine(self._session_factory, bus=self._bus)
        return self._experiments

    @property
    def validation(self) -> ValidationEngine:
        """The validation engine holding reports and promotions."""
        if self._validation is None:
            self._validation = ValidationEngine(
                self._session_factory, bus=self._bus, backtests=self._backtests
            )
        return self._validation

    @property
    def hypotheses(self) -> HypothesisRegistry:
        """The hypothesis registry."""
        if self._hypotheses is None:
            self._hypotheses = HypothesisRegistry(self._session_factory, bus=self._bus)
        return self._hypotheses

    @property
    def analyses(self) -> AnalysisEngine:
        """The sweep / walk-forward / Monte-Carlo ledger joined into the scorecard."""
        if self._analyses is None:
            self._analyses = AnalysisEngine(
                backtests=self._backtests, session_factory=self._session_factory, bus=self._bus
            )
        return self._analyses

    @property
    def livebook(self) -> LiveBookEngine:
        """The read-only view of the traded book the scorecard correlates against."""
        if self._livebook is None:
            self._livebook = LiveBookEngine()
        return self._livebook

    @property
    def capacity(self) -> CapacityEngine:
        """The ADV-participation capacity estimator."""
        if self._capacity is None:
            self._capacity = CapacityEngine()
        return self._capacity

    # --- candidate views --------------------------------------------------
    def candidates(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Return every backtested experiment with its newest gate outcome.

        One row per experiment (its newest completed backtest). Experiments that
        have never produced a completed backtest are included with ``null``
        metrics rather than dropped - "never tested" is a research fact.
        """
        experiments = {exp.id: exp for exp in self.experiments.list()}
        newest: dict[str, Any] = {}
        for record in self.backtests.list_runs(limit=max(limit * 4, 200)):
            if record.experiment_id and record.experiment_id not in newest and record.stats:
                newest[record.experiment_id] = record

        rows: list[dict[str, Any]] = []
        for experiment in experiments.values():
            record = newest.get(experiment.id)
            report = self.validation.latest_report(record.id) if record else None
            window = (record.out_of_sample or record.stats) if record else None
            deflated = None
            if report is not None:
                deflated = (report.tests.get("deflated_sharpe") or {}).get("deflated_sharpe")
            rows.append(
                {
                    "experiment_id": experiment.id,
                    "name": experiment.name,
                    "hypothesis_id": experiment.hypothesis_id,
                    "stage": str(experiment.stage),
                    "universe_id": experiment.universe_id,
                    "signal": experiment.config.signal.describe() if experiment.config else None,
                    "backtest_id": record.id if record else None,
                    "ran_at": record.finished_at.isoformat()
                    if record and record.finished_at
                    else None,
                    "oos_sharpe": window.get("sharpe") if window else None,
                    "deflated_sharpe": deflated,
                    "max_drawdown": (record.stats or {}).get("max_drawdown") if record else None,
                    "trades": (record.stats or {}).get("trades") if record else None,
                    "excess_return": window.get("excess_return") if window else None,
                    "turnover": (record.stats or {}).get("turnover") if record else None,
                    "gates_passed": sum(1 for gate in report.gates if gate.passed)
                    if report
                    else None,
                    "gates_total": len(report.gates) if report else None,
                    "verdict": report.verdict if report else None,
                    "promoted": bool(report.promoted) if report else False,
                    "report_id": report.id if report else None,
                }
            )
        rows.sort(key=lambda row: (row["oos_sharpe"] is None, -(row["oos_sharpe"] or 0.0)))
        return rows[:limit]

    # --- scorecard evidence ------------------------------------------------
    def _newest_analysis(self, backtest_id: str, kind: AnalysisKind) -> Any | None:
        """Return the newest *completed* analysis of ``kind`` for one backtest."""
        try:
            records = self.analyses.list_analyses(backtest_id=backtest_id, kind=kind, limit=20)
        except Exception:  # noqa: BLE001 - a missing analyses table is simply "none"
            logger.debug("could not read %s analyses for %s", kind, backtest_id, exc_info=True)
            return None
        for record in records:  # newest first
            if record.status is AnalysisStatus.COMPLETE:
                return record
        return None

    def _sweep_cell(self, backtest_id: str) -> dict[str, Any]:
        """Parameter stability of the newest sweep attached to this backtest."""
        record = self._newest_analysis(backtest_id, AnalysisKind.SWEEP)
        if record is None:
            return _missing_cell(NO_ANALYSIS)
        summary = record.summary
        best = summary.get("best_metric")
        median = summary.get("median_metric")
        metric = summary.get("metric", "sharpe")
        if best in (None, 0) or median is None:
            return _missing_cell(
                f"sweep {record.id} scored no cell, so its plateau cannot be measured"
            )
        retention = float(median) / float(best)
        robust = bool(summary.get("robust"))
        share = summary.get("positive_share")
        note = (
            f"sweep {record.id}: the median of {int(summary.get('evaluated') or 0)} scored "
            f"cell(s) keeps {retention:.0%} of the best cell's {metric}"
            + (f", {float(share):.0%} of cells positive" if share is not None else "")
            + (
                "; the edge survives away from the peak"
                if robust
                else "; the edge is concentrated near one specification"
            )
        )
        return {
            "value": retention,
            "display": "IN REGION ✓" if robust else "PEAK ONLY",
            "note": note,
            "tone": "ok" if robust else "warn",
        }

    def _monte_carlo_cells(self, backtest_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """``P(Sharpe > 1)`` and the p95 max drawdown of the newest resample."""
        record = self._newest_analysis(backtest_id, AnalysisKind.MONTE_CARLO)
        if record is None:
            return _missing_cell(NO_ANALYSIS), _missing_cell(NO_ANALYSIS)
        summary = record.summary
        paths = int(summary.get("paths") or 0)
        block = summary.get("block")
        base = f"{record.id}: {paths:,} block-bootstrap resamples (block {block})"
        probability = summary.get("prob_above_one")
        drawdown = summary.get("p95_max_drawdown")
        probability_cell = (
            _missing_cell(f"{record.id} recorded no Sharpe distribution")
            if probability is None
            else {
                "value": float(probability),
                "display": None,
                "note": f"{base}; share of resampled paths whose Sharpe exceeds 1.0",
                "tone": "ok" if float(probability) >= 0.5 else "warn",
            }
        )
        drawdown_cell = (
            _missing_cell(f"{record.id} recorded no drawdown distribution")
            if drawdown is None
            else {
                "value": float(drawdown),
                "display": None,
                "note": f"{base}; only 5% of paths drew down worse than this",
                "tone": "warn" if float(drawdown) <= -0.20 else "ok",
            }
        )
        return probability_cell, drawdown_cell

    def _walk_forward_cell(self, backtest_id: str) -> dict[str, Any]:
        """Positive test folds of the newest walk-forward analysis."""
        record = self._newest_analysis(backtest_id, AnalysisKind.WALK_FORWARD)
        if record is None:
            return _missing_cell(NO_ANALYSIS)
        summary = record.summary
        scored = int(summary.get("scored") or 0)
        positive = summary.get("positive_folds")
        if not scored or positive is None:
            return _missing_cell(f"walk-forward {record.id} scored no fold")
        efficiency = summary.get("efficiency")
        note = (
            f"walk-forward {record.id}: {int(positive)} of {scored} out-of-sample fold(s) "
            f"had a positive {summary.get('metric', 'sharpe')}"
            + (
                f"; {float(efficiency):.0%} of the in-sample edge survived"
                if efficiency is not None
                else ""
            )
        )
        share = int(positive) / scored
        return {
            "value": share,
            "display": f"{int(positive)} / {scored}",
            "note": note,
            "tone": "ok" if share >= 0.6 else "warn",
        }

    def _correlation_cell(self, record: Any, book: LiveBook) -> dict[str, Any]:
        """Pearson correlation of the candidate's returns with the traded book."""
        series, reason = returns_from_curve(record.equity_curve if record else None)
        if reason is not None:
            return _missing_cell(reason)
        result = self.livebook.correlate(series, book=book)
        if result["correlation"] is None:
            return _missing_cell(result["reason"] or NO_LIVE_BOOK)
        value = float(result["correlation"])
        return {
            "value": value,
            "display": None,
            "note": (
                f"Pearson correlation of daily returns with the {result['account_id']} book "
                f"over {result['overlap_days']} overlapping session(s)"
            ),
            "tone": "warn" if abs(value) >= 0.5 else "ok",
        }

    def _capacity_cell(self, record: Any) -> dict[str, Any]:
        """ADV-participation capacity of the book the run ends holding."""
        holdings = holdings_from_trade_log(record.trade_log if record else None)
        try:
            estimate = self.capacity.estimate(holdings)
        except Exception:  # noqa: BLE001 - one unreadable store must not kill the table
            logger.debug("capacity estimate failed for %s", getattr(record, "id", None),
                         exc_info=True)
            return _missing_cell("the stored volume history could not be read")
        if estimate.capacity_usd is None:
            return _missing_cell(f"{estimate.reason}. {estimate.assumption}")
        binding = next(
            (name for name in estimate.names if name.symbol == estimate.binding_symbol), None
        )
        where = (
            f"{binding.symbol} binds it at {binding.weight:.1%} of the book "
            f"on ${binding.adv_usd:,.0f} ADV. "
            if binding is not None and binding.adv_usd is not None
            else ""
        )
        return {
            "value": estimate.capacity_usd,
            "display": None,
            "note": f"{where}{estimate.assumption}",
            "tone": None,
        }

    def scorecard(self, *, limit: int = 6) -> dict[str, Any]:
        """Return the candidate comparison table (evidence rows x candidates).

        The columns are the best candidates by out-of-sample Sharpe; the rows are
        the pieces of evidence the reference scorecard lists. The first six come
        straight off the backtest and its validation report; the next four are
        joined from the newest completed sweep / Monte-Carlo / walk-forward
        analysis recorded against that backtest; the last two are computed here
        (correlation against the traded book, ADV-participation capacity).

        Every cell that cannot be computed carries a ``note`` saying why, and a
        ``None`` value the UI renders as an em dash - no cell is ever filled with
        a placeholder number.
        """
        candidates = [row for row in self.candidates(limit=limit) if row["backtest_id"]]
        metrics = [
            ("OOS Sharpe", "oos_sharpe", "ratio"),
            ("Deflated Sharpe", "deflated_sharpe", "ratio"),
            ("Max drawdown", "max_drawdown", "percent"),
            ("Excess vs benchmark", "excess_return", "percent"),
            ("Trades", "trades", "count"),
            ("Annual turnover", "turnover", "ratio"),
        ]
        rows: list[dict[str, Any]] = [
            {
                "label": label,
                "key": key,
                "format": kind,
                "values": [row.get(key) for row in candidates],
                "displays": [None for _ in candidates],
                "notes": [
                    None if row.get(key) is not None else _metric_gap(label, key, row)
                    for row in candidates
                ],
                "tones": [None for _ in candidates],
            }
            for label, key, kind in metrics
        ]

        # The book is reconstructed once and reused for every candidate column.
        book = self.livebook.daily_returns()
        evidence: dict[str, list[dict[str, Any]]] = {key: [] for key, _, _ in EVIDENCE_ROWS}
        for candidate in candidates:
            backtest_id = candidate["backtest_id"]
            try:
                record = self.backtests.get(backtest_id)
            except KeyError:  # pragma: no cover - the id came from the ledger
                record = None
            sweep = self._sweep_cell(backtest_id)
            probability, drawdown = self._monte_carlo_cells(backtest_id)
            folds = self._walk_forward_cell(backtest_id)
            evidence["sweep_stability"].append(sweep)
            evidence["mc_prob_sharpe_above_one"].append(probability)
            evidence["mc_p95_max_drawdown"].append(drawdown)
            evidence["wfa_positive_folds"].append(folds)
            evidence["live_book_correlation"].append(self._correlation_cell(record, book))
            evidence["capacity_usd"].append(self._capacity_cell(record))

        for key, label, kind in EVIDENCE_ROWS:
            cells = evidence[key]
            rows.append(
                {
                    "label": label,
                    "key": key,
                    "format": kind,
                    "values": [cell["value"] for cell in cells],
                    "displays": [cell["display"] for cell in cells],
                    "notes": [cell["note"] for cell in cells],
                    "tones": [cell["tone"] for cell in cells],
                }
            )

        return {
            "candidates": [
                {
                    "experiment_id": row["experiment_id"],
                    "name": row["name"],
                    "backtest_id": row["backtest_id"],
                    "hypothesis_id": row["hypothesis_id"],
                    "stage": row["stage"],
                    "verdict": row["verdict"],
                    "gates": (
                        f"{row['gates_passed']} / {row['gates_total']}"
                        if row["gates_total"]
                        else None
                    ),
                }
                for row in candidates
            ],
            "rows": rows,
            "live_book": book.as_dict(),
        }

    def funnel(self) -> dict[str, Any]:
        """Return experiment counts per funnel stage (research → live)."""
        counts = Counter(str(exp.stage) for exp in self.experiments.list())
        return {
            "stages": [
                {"stage": str(stage), "count": counts.get(str(stage), 0)}
                for stage in ExperimentStage
            ],
            "total": sum(counts.values()),
        }

    def gate_outcomes(self, *, days: int = 180) -> dict[str, Any]:
        """Return gate outcomes over time.

        Args:
            days: how far back to look.

        Returns:
            ``by_gate`` (pass/fail/pending counts and pass rate per gate),
            ``timeline`` (one entry per evaluated report, oldest first) and
            ``daily`` (reports evaluated and gates passed per day) - enough for
            both the "blocking issues" panel and a trend chart.
        """
        cutoff = utcnow() - timedelta(days=days)
        reports = [
            report
            for report in self.validation.reports(limit=1000)
            if report.evaluated_at >= cutoff
        ]
        reports.sort(key=lambda report: report.evaluated_at)

        by_gate: dict[str, dict[str, Any]] = {}
        daily: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"reports": 0, "gates_passed": 0, "gates_total": 0, "promotions": 0}
        )
        timeline: list[dict[str, Any]] = []
        for report in reports:
            day = report.evaluated_at.date().isoformat()
            bucket = daily[day]
            bucket["reports"] += 1
            bucket["gates_total"] += len(report.gates)
            bucket["gates_passed"] += sum(1 for gate in report.gates if gate.passed)
            bucket["promotions"] += 1 if report.promoted else 0
            for gate in report.gates:
                entry = by_gate.setdefault(
                    gate.id,
                    {
                        "gate": gate.id,
                        "name": gate.name,
                        "pass": 0,
                        "fail": 0,
                        "pending": 0,
                        "threshold": gate.threshold,
                        "hard": gate.hard,
                    },
                )
                entry[str(gate.status)] = entry.get(str(gate.status), 0) + 1
            timeline.append(
                {
                    "report_id": report.id,
                    "backtest_id": report.backtest_id,
                    "hypothesis_id": report.hypothesis_id,
                    "evaluated_at": report.evaluated_at.isoformat(),
                    "verdict": report.verdict,
                    "score": report.score,
                    "promoted": report.promoted,
                    "failed": [gate.id for gate in report.failed_gates],
                }
            )
        for entry in by_gate.values():
            evaluated = entry["pass"] + entry["fail"]
            entry["pass_rate"] = entry["pass"] / evaluated if evaluated else None
        return {
            "window_days": days,
            "reports": len(reports),
            "by_gate": sorted(by_gate.values(), key=lambda item: item["gate"]),
            "daily": [{"date": day, **values} for day, values in sorted(daily.items())],
            "timeline": timeline,
        }

    def summary(self) -> dict[str, Any]:
        """Aggregate figures for the Evaluation tab header.

        Every number is counted from the tables, so an empty install honestly
        reports zeros rather than sample data.
        """
        experiments = self.experiments.list()
        runs = self.experiments.all_runs(limit=5000)
        backtests = self.backtests.list_runs(limit=5000)
        reports = self.validation.reports(limit=5000)
        promoted = self.validation.promoted()
        hypothesis_summary = self.hypotheses.summary()
        evaluated = [report for report in reports if report.gates]
        passing = [report for report in evaluated if report.passed]
        return {
            "hypotheses": hypothesis_summary,
            "experiments": {
                "total": len(experiments),
                "by_stage": {
                    str(stage): sum(1 for exp in experiments if exp.stage is stage)
                    for stage in ExperimentStage
                },
            },
            "runs": {
                "experiment_runs": len(runs),
                "backtest_runs": len(backtests),
                "trials": len(backtests),
            },
            "validation": {
                "reports": len(evaluated),
                "passed": len(passing),
                "gate_pass_rate": (len(passing) / len(evaluated)) if evaluated else None,
            },
            "promoted": {
                "total": len(promoted),
                "active": sum(1 for item in promoted if item["status"] == "active"),
                "by_stage": dict(Counter(item["stage"] for item in promoted)),
            },
            "validated_hypotheses": hypothesis_summary["by_status"].get(
                str(HypothesisStatus.VALIDATED), 0
            ),
        }

    # --- promoted strategy tracking ---------------------------------------
    def promoted(self) -> list[dict[str, Any]]:
        """Return the promoted strategies with their last stored tracking."""
        return self.validation.promoted()

    def list(self) -> list[EvaluationRecord]:
        """Return a tracking record for every promoted strategy.

        Records are built from the *stored* tracking snapshot; call
        :meth:`update` to recompute one against fresh prices.
        """
        return [_record_from_strategy(item) for item in self.validation.promoted()]

    def get(self, strategy_id: str) -> EvaluationRecord:
        """Return one tracking record.

        Raises:
            KeyError: when the strategy is unknown.
        """
        return _record_from_strategy(self.validation.get_strategy(strategy_id))

    async def update(self, strategy_id: str, *, publish: bool = True) -> EvaluationRecord:
        """Recompute a record from the newest live and backtest data.

        The promoted specification is re-run with its end date removed, so the
        run now covers the window *after* promotion too. The returns are then
        split at the promotion timestamp: everything before is what the
        strategy was promoted on, everything after is genuinely out-of-sample
        experience. A block bootstrap of the pre-promotion window provides the
        band the live Sharpe is judged against.

        Raises:
            KeyError: when the strategy is unknown.
        """
        strategy = self.validation.get_strategy(strategy_id)
        config = BacktestConfig.from_dict(strategy["spec"])
        config.end = None
        promoted_at = datetime.fromisoformat(strategy["promoted_at"])
        notes: list[str] = []

        result = await self.backtests.rerun(config, cost_multiplier=1.0)
        index = pd.DatetimeIndex(result.returns.index)
        boundary = pd.Timestamp(promoted_at)
        if index.tz is not None and boundary.tz is None:  # pragma: no cover - defensive
            boundary = boundary.tz_localize(index.tz)
        elif index.tz is None and boundary.tz is not None:
            boundary = boundary.tz_localize(None)
        live_mask = np.asarray(index >= boundary)

        live_returns = result.returns[live_mask]
        prior_returns = result.returns[~live_mask]
        live_benchmark = (
            result.benchmark_returns[live_mask] if result.benchmark_returns is not None else None
        )
        record = EvaluationRecord(
            strategy_id=strategy_id,
            name=strategy["name"],
            promoted_on=promoted_at.date(),
            stage=strategy["stage"],
            status=strategy["status"],
            days_live=max(0, (utcnow().date() - promoted_at.date()).days),
            observations=int(live_returns.size),
            expected_return=(strategy["baseline"] or {}).get("total_return"),
            expected_cagr=(strategy["baseline"] or {}).get("cagr"),
            expected_sharpe=(strategy["baseline"] or {}).get("sharpe"),
            updated_at=utcnow(),
        )

        if live_returns.size < 2:
            notes.append(
                "not enough price history since promotion to measure live performance yet"
            )
            record.notes = notes
            self._store_tracking(strategy_id, record)
            return record

        live_stats = compute_stats(
            live_returns,
            benchmark=live_benchmark,
            periods_per_year=result.periods_per_year,
            turnover=result.turnover[live_mask],
            trades=int(result.trade_counts[live_mask].sum()),
            risk_free_rate=config.risk_free_rate,
        )
        record.live_return = live_stats.total_return
        record.live_cagr = live_stats.cagr
        record.live_sharpe = live_stats.sharpe
        record.live_max_drawdown = live_stats.max_drawdown
        record.benchmark_return = live_stats.benchmark_return

        if live_benchmark is not None and live_benchmark.size > 1:
            difference = (live_returns - live_benchmark).dropna()
            if difference.size > 1:
                record.tracking_error = float(
                    difference.std(ddof=1) * math.sqrt(result.periods_per_year)
                )
        else:
            notes.append("no benchmark history in the live window; tracking error unavailable")

        expected = record.expected_sharpe
        if expected is not None and expected > 0:
            record.decay_score = float((expected - live_stats.sharpe) / expected)

        if prior_returns.size >= 40:
            band = bootstrap_confidence(
                prior_returns, metric="sharpe", periods_per_year=result.periods_per_year
            )
            if band.get("p05") is not None:
                record.band = {"p05": band["p05"], "p50": band["p50"], "p95": band["p95"]}
                record.in_band = bool(band["p05"] <= live_stats.sharpe <= band["p95"])
        else:
            notes.append("pre-promotion window too short to bootstrap a comparison band")

        record.notes = notes
        self._store_tracking(strategy_id, record)
        if publish:
            await self.bus.publish(
                LAB_CHANNEL,
                {
                    "kind": "evaluation",
                    "phase": "tracked",
                    "id": strategy_id,
                    "name": record.name,
                    "live_sharpe": record.live_sharpe,
                    "expected_sharpe": record.expected_sharpe,
                    "decay_score": record.decay_score,
                    "in_band": record.in_band,
                    "observations": record.observations,
                    "ts": record.updated_at.isoformat() if record.updated_at else None,
                },
            )
        return record

    async def update_all(self) -> list[EvaluationRecord]:
        """Recompute every promoted strategy's tracking record."""
        out: list[EvaluationRecord] = []
        for item in self.validation.promoted(include_killed=False):
            try:
                out.append(await self.update(item["id"]))
            except Exception:  # noqa: BLE001 - one bad strategy must not stop the rest
                logger.exception("tracking update failed for %s", item["id"])
        return out

    async def decay(self, strategy_id: str, *, window: int = 63) -> pd.Series:
        """Return the rolling Sharpe of the promoted strategy, for the decay chart.

        The whole re-run history is used (not only the live window) so the chart
        shows the edge decaying - or not - across the promotion boundary.

        Raises:
            KeyError: when the strategy is unknown.
        """
        strategy = self.validation.get_strategy(strategy_id)
        config = BacktestConfig.from_dict(strategy["spec"])
        config.end = None
        result = await self.backtests.rerun(config, cost_multiplier=1.0)
        returns = result.returns
        if returns.size < window:
            return pd.Series(dtype="float64")
        rolling_mean = returns.rolling(window).mean()
        rolling_std = returns.rolling(window).std(ddof=1)
        sharpe = (rolling_mean / rolling_std.replace(0.0, np.nan)) * math.sqrt(
            result.periods_per_year
        )
        return sharpe.dropna()

    def _store_tracking(self, strategy_id: str, record: EvaluationRecord) -> None:
        """Persist a tracking snapshot onto the promoted-strategy row."""
        with self._session() as session:
            row = session.get(PromotedStrategyRow, strategy_id)
            if row is None:  # pragma: no cover - checked by the caller
                raise KeyError(f"unknown strategy {strategy_id!r}")
            row.tracking = record.as_dict()
            row.tracked_at = record.updated_at


def _record_from_strategy(strategy: dict[str, Any]) -> EvaluationRecord:
    """Build an :class:`EvaluationRecord` from a stored promotion (+ tracking)."""
    promoted_at = datetime.fromisoformat(strategy["promoted_at"])
    tracking = strategy.get("tracking") or {}
    baseline = strategy.get("baseline") or {}
    updated = tracking.get("updated_at")
    return EvaluationRecord(
        strategy_id=strategy["id"],
        name=strategy["name"],
        promoted_on=promoted_at.date(),
        stage=strategy["stage"],
        status=strategy["status"],
        days_live=int(tracking.get("days_live", max(0, (utcnow().date() - promoted_at.date()).days))),
        observations=int(tracking.get("observations", 0)),
        live_return=tracking.get("live_return"),
        live_cagr=tracking.get("live_cagr"),
        live_sharpe=tracking.get("live_sharpe"),
        live_max_drawdown=tracking.get("live_max_drawdown"),
        expected_return=tracking.get("expected_return", baseline.get("total_return")),
        expected_cagr=tracking.get("expected_cagr", baseline.get("cagr")),
        expected_sharpe=tracking.get("expected_sharpe", baseline.get("sharpe")),
        benchmark_return=tracking.get("benchmark_return"),
        tracking_error=tracking.get("tracking_error"),
        decay_score=tracking.get("decay_score"),
        in_band=tracking.get("in_band"),
        band=tracking.get("band"),
        updated_at=datetime.fromisoformat(updated) if updated else None,
        notes=list(tracking.get("notes") or []),
    )


_engine: EvaluationEngine | None = None


def get_evaluation_engine() -> EvaluationEngine:
    """Return the process-wide evaluation engine, creating it on first use."""
    global _engine
    if _engine is None:
        _engine = EvaluationEngine()
    return _engine


def reset_evaluation_engine(engine: EvaluationEngine | None = None) -> EvaluationEngine:
    """Replace the process-wide engine. Intended for tests."""
    global _engine
    _engine = engine if engine is not None else EvaluationEngine()
    return _engine


__all__ = [
    "EVIDENCE_ROWS",
    "NO_ANALYSIS",
    "NO_LIVE_BOOK",
    "EvaluationEngine",
    "EvaluationRecord",
    "get_evaluation_engine",
    "reset_evaluation_engine",
]
