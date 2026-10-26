"""Strategy Lab lifecycle: hypotheses, experiments, gates, promotion, evaluation.

The price history used here is synthetic but deterministic (a seeded generator
writes real bars into a temporary Parquet store), so the engines run their real
code paths - DuckDB reads, the backtester, scipy's tests - without touching a
vendor.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from tradinglib.config import Settings
from tradinglib.data.base import Bar, BarInterval
from tradinglib.data.store import TimeSeriesStore, reset_store
from tradinglib.events import EventBus
from tradinglib.research.backtest import (
    BacktestConfig,
    BacktestEngine,
    Backtester,
    BacktestStatus,
    CostModel,
    Rebalance,
    SignalKind,
    SignalSpec,
    Weighting,
)
from tradinglib.research.analyses import AnalysisEngine, AnalysisKind
from tradinglib.research.evaluation import EvaluationEngine
from tradinglib.research.experiments import (
    AcceptanceCriteria,
    ExperimentEngine,
    ExperimentStage,
    ExperimentStatus,
    information_coefficient,
)
from tradinglib.research.hypotheses import (
    HypothesisRegistry,
    HypothesisStatus,
)
from tradinglib.research.validation import (
    GateStatus,
    GateThresholds,
    PromotedStrategyRow,
    ValidationEngine,
    bootstrap_confidence,
    deflated_sharpe,
    evaluate_gates,
    excess_return_t_test,
    variance_ratio_test,
)

def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


#: Names whose synthetic path trends up, and names whose path drifts down.
WINNERS = ("MOMA", "MOMB", "MOMC")
LOSERS = ("LAGA", "LAGB", "LAGC")
BENCHMARK = "SPY"

#: Length of the synthetic history, in business days (~4.4 years).
BARS = 1_120


def _paths(end: date) -> dict[str, pd.Series]:
    """Build the deterministic synthetic price paths written into the store."""
    index = pd.bdate_range(end=pd.Timestamp(end), periods=BARS, tz="UTC")
    rng = np.random.default_rng(20260828)
    spec = {
        **{symbol: (0.00100, 0.0060) for symbol in WINNERS},
        **{symbol: (-0.00100, 0.0110) for symbol in LOSERS},
        BENCHMARK: (0.00030, 0.0070),
    }
    out: dict[str, pd.Series] = {}
    for symbol, (mu, sigma) in spec.items():
        shocks = rng.normal(mu, sigma, size=BARS)
        out[symbol] = pd.Series(100.0 * np.exp(np.cumsum(shocks)), index=index, name=symbol)
    return out


def _seed(store: TimeSeriesStore, end: date) -> dict[str, pd.Series]:
    """Write the synthetic paths into the store as real daily bars."""
    paths = _paths(end)
    for symbol, series in paths.items():
        bars = [
            Bar(
                symbol=symbol,
                ts=ts.to_pydatetime(),
                open=float(value),
                high=float(value) * 1.004,
                low=float(value) * 0.996,
                close=float(value),
                volume=1_000_000.0,
                interval=BarInterval.DAY_1,
                source="synthetic-test",
            )
            for ts, value in series.items()
        ]
        store.write_bars(bars)
    return paths


@pytest.fixture()
def lab(settings: Settings, bus: EventBus) -> Iterator[SimpleNamespace]:
    """A fully wired Strategy Lab over a temporary store and database."""
    store = reset_store(TimeSeriesStore(settings.ts_dir))
    # The UTC date, not the local one: the lookahead scan compares bar stamps
    # with UTC now, and between local and UTC midnight the local date's bar
    # would count as future data.
    end = _utc_today()
    paths = _seed(store, end)
    backtests = BacktestEngine(store=store, bus=bus)
    analyses = AnalysisEngine(backtests=backtests, bus=bus)
    experiments = ExperimentEngine(bus=bus, backtests=backtests)
    hypotheses = HypothesisRegistry(bus=bus)
    validation = ValidationEngine(bus=bus, backtests=backtests, experiments=experiments)
    evaluation = EvaluationEngine(
        bus=bus,
        backtests=backtests,
        experiments=experiments,
        validation=validation,
        hypotheses=hypotheses,
    )
    yield SimpleNamespace(
        store=store,
        paths=paths,
        end=end,
        backtests=backtests,
        analyses=analyses,
        experiments=experiments,
        hypotheses=hypotheses,
        validation=validation,
        evaluation=evaluation,
    )
    reset_store()


def _momentum_config(name: str = "momentum-3") -> BacktestConfig:
    """A monthly-rebalanced, top-3 momentum book over the synthetic universe."""
    return BacktestConfig(
        name=name,
        symbols=[*WINNERS, *LOSERS],
        signal=SignalSpec(
            kind=SignalKind.MOMENTUM,
            lookback=252,
            skip=21,
            top_n=3,
            weighting=Weighting.EQUAL,
        ),
        benchmark=BENCHMARK,
        rebalance=Rebalance.MONTHLY,
        costs=CostModel(commission_bps=2.0, slippage_bps=4.0),
        oos_fraction=0.3,
    )


def _losing_config() -> BacktestConfig:
    """A book that holds the drifting-down names: the gates must reject it."""
    return BacktestConfig(
        name="hold-the-losers",
        symbols=list(LOSERS),
        signal=SignalSpec(
            kind=SignalKind.STATIC, symbols=list(LOSERS), weighting=Weighting.EQUAL
        ),
        benchmark=BENCHMARK,
        rebalance=Rebalance.MONTHLY,
        costs=CostModel(commission_bps=2.0, slippage_bps=4.0),
        oos_fraction=0.3,
    )


# --------------------------------------------------------------------------
# hypotheses
# --------------------------------------------------------------------------
def test_hypothesis_requires_a_falsifier(lab: SimpleNamespace) -> None:
    """A statement nothing could disprove is not a hypothesis."""
    with pytest.raises(ValueError, match="falsifier"):
        lab.hypotheses.create("Momentum works", "Winners keep winning", "  ")


def test_hypothesis_crud_and_evidence(lab: SimpleNamespace) -> None:
    """Create, read, update, link evidence and delete."""
    registry: HypothesisRegistry = lab.hypotheses
    created = registry.create(
        "Momentum persists in the synthetic universe",
        "Twelve-month momentum skipping a month keeps ranking future returns.",
        "OOS Sharpe falls below 0.8 once costs exceed 12bps",
        owner="ana",
        tags=["momentum", "momentum", " "],
        origin="manual",
    )
    assert created.id == "H-1"
    assert created.status is HypothesisStatus.DRAFT
    assert created.tags == ["momentum"]

    link = registry.add_evidence(created.id, "paper", "Jegadeesh & Titman (1993)", label="seminal")
    assert link.id is not None
    assert len(registry.evidence(created.id)) == 1

    updated = registry.update(created.id, owner="marco", rationale="carried over from v3")
    assert updated.owner == "marco"
    assert updated.rationale == "carried over from v3"

    assert [h.id for h in registry.list(status=HypothesisStatus.DRAFT)] == [created.id]
    assert registry.list(query="synthetic")[0].id == created.id
    assert registry.summary()["by_status"]["draft"] == 1

    registry.remove_evidence(created.id, link.id)
    assert registry.evidence(created.id) == []

    registry.delete(created.id)
    with pytest.raises(KeyError):
        registry.get(created.id)


def test_hypothesis_status_lifecycle(lab: SimpleNamespace) -> None:
    """draft → testing → validated is legal; draft → validated is not."""
    registry: HypothesisRegistry = lab.hypotheses
    hypothesis = registry.create("H", "statement", "falsifier")

    with pytest.raises(ValueError, match="cannot move"):
        registry.set_status(hypothesis.id, HypothesisStatus.VALIDATED)

    registry.set_status(hypothesis.id, HypothesisStatus.TESTING, note="experiments queued")
    validated = registry.set_status(hypothesis.id, HypothesisStatus.VALIDATED, actor="ana")
    assert validated.status is HypothesisStatus.VALIDATED
    # Creation plus the two transitions are all recorded.
    assert [entry["to"] for entry in validated.status_history] == [
        "draft",
        "testing",
        "validated",
    ]
    assert validated.status_history[-1]["actor"] == "ana"

    rejected = registry.set_status(hypothesis.id, HypothesisStatus.REJECTED, note="falsified")
    assert rejected.status is HypothesisStatus.REJECTED
    with pytest.raises(ValueError):
        registry.set_status(hypothesis.id, HypothesisStatus.VALIDATED)


# --------------------------------------------------------------------------
# experiments
# --------------------------------------------------------------------------
async def test_experiment_run_produces_real_metrics_and_logs(
    lab: SimpleNamespace, bus: EventBus
) -> None:
    """A run reads the store, backtests, measures IC and persists everything."""
    events: list[dict] = []
    bus.subscribe("lab", lambda event: events.append(event.payload))

    hypothesis = lab.hypotheses.create(
        "Momentum persists", "12-1 momentum ranks future returns", "IC is not positive"
    )
    experiment = lab.experiments.create(
        "momentum-top3",
        _momentum_config(),
        hypothesis_id=hypothesis.id,
        criteria=AcceptanceCriteria(min_oos_sharpe=0.8, max_drawdown=0.25, min_trades=30),
    )
    run = await lab.experiments.run(experiment.id)

    assert run.status is ExperimentStatus.COMPLETE, run.error
    assert run.backtest_id is not None
    assert run.results["passed_gates"] is True
    assert run.results["stats"]["periods"] > 500
    assert run.results["out_of_sample"]["sharpe"] > 0.8
    assert run.results["ic"]["observations"] > 10
    assert run.results["ic"]["mean_ic"] > 0.0

    # The log is ordered, persisted and readable back out.
    log = lab.experiments.run_log(run.id)
    assert log[0]["message"].startswith("experiment momentum-top3 started")
    assert any("loaded" in line["message"] for line in log)
    assert any("PASS" in line["message"] for line in log)

    # Both trial tables carry a row, which is what /api/status counts.
    assert lab.experiments.count_runs() == 1
    assert lab.backtests.count() == 1
    record = lab.backtests.get(run.backtest_id)
    assert record.status is BacktestStatus.COMPLETE
    assert record.equity_curve is not None
    assert len(record.equity_curve["dates"]) == len(record.equity_curve["equity"])
    assert record.equity_curve["equity"][0] == pytest.approx(record.config.initial_capital)

    phases = {(event["kind"], event["phase"]) for event in events}
    assert ("experiment", "started") in phases
    assert ("experiment", "complete") in phases
    assert ("backtest", "complete") in phases


async def test_experiment_failure_is_recorded_not_swallowed(lab: SimpleNamespace) -> None:
    """A run against symbols with no stored history fails honestly."""
    config = BacktestConfig(
        name="no-data",
        symbols=["NOPE1", "NOPE2"],
        signal=SignalSpec(
            kind=SignalKind.STATIC, symbols=["NOPE1", "NOPE2"], weighting=Weighting.EQUAL
        ),
        benchmark=None,
    )
    experiment = lab.experiments.create("missing-data", config)
    run = await lab.experiments.run(experiment.id)

    assert run.status is ExperimentStatus.FAILED
    assert "no stored price history" in (run.error or "")
    assert run.log[-1]["level"] == "error"


def test_information_coefficient_detects_a_perfect_ranker() -> None:
    """A signal equal to the forward return has an IC of exactly 1."""
    index = pd.bdate_range("2026-01-01", periods=60, tz="UTC")
    rng = np.random.default_rng(3)
    prices = pd.DataFrame(
        {f"S{i}": 100.0 * np.exp(np.cumsum(rng.normal(0, 0.01, 60))) for i in range(8)},
        index=index,
    )
    horizon = 5
    perfect = prices.shift(-horizon) / prices - 1.0
    outcome = information_coefficient(perfect, prices, horizon=horizon)
    assert outcome["observations"] > 5
    assert outcome["mean_ic"] == pytest.approx(1.0, rel=1e-12)
    assert outcome["hit_rate"] == 1.0

    # A signal with no cross-sectional variation yields nothing, honestly.
    flat = pd.DataFrame(1.0, index=prices.index, columns=prices.columns)
    empty = information_coefficient(flat, prices, horizon=horizon)
    assert empty["observations"] == 0
    assert empty["mean_ic"] is None


# --------------------------------------------------------------------------
# validation gates, on crafted results
# --------------------------------------------------------------------------
def _run(lab: SimpleNamespace, config: BacktestConfig, *, cost_multiplier: float = 1.0):
    """Run a configuration synchronously against the seeded store."""
    scaled = BacktestConfig.from_dict(config.as_dict())
    scaled.costs = config.costs.scaled(cost_multiplier)
    return lab.backtests.run_sync(scaled)


def test_gates_pass_on_a_result_that_deserves_it(lab: SimpleNamespace) -> None:
    """Every gate passes for the winning book, and each reports its own value."""
    base = _run(lab, _momentum_config())
    stressed = _run(lab, _momentum_config(), cost_multiplier=2.0)
    gates, tests = evaluate_gates(base, stressed=stressed, trials=1)

    by_id = {gate.id: gate for gate in gates}
    assert set(by_id) == {
        "min_oos_sharpe",
        "max_drawdown",
        "min_trades",
        "min_history",
        "cost_sensitivity",
        "benchmark_outperformance",
        "excess_return_t_test",
        "deflated_sharpe",
        "cpcv_pbo",
        "regime_robustness",
        "lookahead_scan",
        "artifact_bundle",
    }
    # CPCV PBO measures overfitting from *selecting* between candidates. This
    # run has no siblings, so no selection happened and the gate passes with the
    # reason stated rather than pretending a suite was run.
    assert by_id["cpcv_pbo"].status is GateStatus.PASS
    assert "only one trial" in (by_id["cpcv_pbo"].detail or "")
    assert by_id["lookahead_scan"].status is GateStatus.PASS
    assert by_id["artifact_bundle"].status is GateStatus.PASS
    assert all(gate.status is GateStatus.PASS for gate in gates), [
        (gate.id, gate.status, gate.observed, gate.detail) for gate in gates
    ]
    assert by_id["min_oos_sharpe"].observed == pytest.approx(
        base.out_of_sample.sharpe, rel=1e-12
    )
    assert by_id["max_drawdown"].observed == pytest.approx(abs(base.stats.max_drawdown))
    assert by_id["min_trades"].observed == float(base.stats.trades)
    assert tests["t_test"]["basis"] == "excess_over_benchmark"
    assert tests["cost_sensitivity"]["stressed_costs_bps"] == pytest.approx(12.0)


def test_gates_fail_on_a_losing_book(lab: SimpleNamespace) -> None:
    """A book that loses to its benchmark is rejected, gate by gate."""
    base = _run(lab, _losing_config())
    stressed = _run(lab, _losing_config(), cost_multiplier=2.0)
    gates, _ = evaluate_gates(base, stressed=stressed, trials=12)
    by_id = {gate.id: gate for gate in gates}

    assert by_id["min_oos_sharpe"].status is GateStatus.FAIL
    assert by_id["benchmark_outperformance"].status is GateStatus.FAIL
    assert by_id["excess_return_t_test"].status is GateStatus.FAIL
    assert by_id["min_history"].status is GateStatus.PASS  # the data is there
    # A negative base Sharpe makes "retention" meaningless rather than a pass.
    assert by_id["cost_sensitivity"].status is GateStatus.PENDING
    assert "not positive" in (by_id["cost_sensitivity"].detail or "")


def test_gate_thresholds_are_honoured(lab: SimpleNamespace) -> None:
    """Raising a threshold above the observed value flips that gate to fail."""
    base = _run(lab, _momentum_config())
    strict = GateThresholds(min_oos_sharpe=base.out_of_sample.sharpe + 1.0)
    gates, _ = evaluate_gates(base, trials=1, thresholds=strict)
    by_id = {gate.id: gate for gate in gates}
    assert by_id["min_oos_sharpe"].status is GateStatus.FAIL
    # Without the stressed re-run the cost gate is pending, never a silent pass.
    assert by_id["cost_sensitivity"].status is GateStatus.PENDING


def test_history_gate_is_hard_and_blocks_short_runs() -> None:
    """Ten bars cannot satisfy a two-year history requirement."""
    index = pd.bdate_range("2026-01-05", periods=10, tz="UTC")
    frame = pd.DataFrame({"AAA": 100.0 * (1.002 ** np.arange(10))}, index=index)
    config = BacktestConfig(
        name="tiny",
        symbols=["AAA"],
        signal=SignalSpec(kind=SignalKind.STATIC, symbols=["AAA"], weighting=Weighting.EQUAL),
        benchmark=None,
        rebalance=Rebalance.DAILY,
        costs=CostModel(0.0, 0.0),
        oos_fraction=None,
    )
    result = Backtester().run(config, frame)
    gates, _ = evaluate_gates(result, trials=1)
    history = next(gate for gate in gates if gate.id == "min_history")
    assert history.hard is True
    assert history.status is GateStatus.FAIL
    assert history.observed == pytest.approx(9 / 252, rel=1e-12)


# --------------------------------------------------------------------------
# statistical helpers
# --------------------------------------------------------------------------
def test_t_test_on_excess_returns() -> None:
    """A constant positive edge is significant; a symmetric one is not."""
    index = pd.bdate_range("2026-01-01", periods=200, tz="UTC")
    rng = np.random.default_rng(11)
    benchmark = pd.Series(rng.normal(0.0002, 0.008, 200), index=index)
    edge = rng.normal(0.0015, 0.002, 200)
    strong = benchmark + edge
    outcome = excess_return_t_test(strong, benchmark)
    assert outcome["basis"] == "excess_over_benchmark"
    assert outcome["p_value"] is not None
    assert outcome["p_value"] < 1e-6
    assert outcome["mean"] == pytest.approx(float(edge.mean()), rel=1e-9)

    noisy = pd.Series(rng.normal(0.0, 0.01, 200), index=index)
    assert excess_return_t_test(noisy, None)["basis"] == "absolute_return"

    short = pd.Series([0.01, 0.02], index=index[:2])
    assert excess_return_t_test(short, None)["p_value"] is None


def test_deflated_sharpe_falls_as_trials_rise() -> None:
    """The same track record is worth less after a hundred attempts."""
    index = pd.bdate_range("2026-01-01", periods=500, tz="UTC")
    rng = np.random.default_rng(5)
    returns = pd.Series(rng.normal(0.0008, 0.008, 500), index=index)
    one = deflated_sharpe(returns, trials=1, trial_sharpe_std=0.05)
    many = deflated_sharpe(returns, trials=200, trial_sharpe_std=0.05)
    assert 0.0 <= many["deflated_sharpe"] <= one["deflated_sharpe"] <= 1.0
    assert many["expected_max_sharpe"] > one["expected_max_sharpe"] == 0.0
    assert one["annualised_sharpe"] == pytest.approx(
        one["sharpe"] * math.sqrt(252), rel=1e-12
    )
    # Too short to deflate is reported, not guessed.
    assert deflated_sharpe(returns.iloc[:3], trials=5)["deflated_sharpe"] is None


def test_bootstrap_is_seeded_and_brackets_the_observation() -> None:
    """The band is reproducible and contains the observed Sharpe."""
    index = pd.bdate_range("2026-01-01", periods=400, tz="UTC")
    rng = np.random.default_rng(9)
    returns = pd.Series(rng.normal(0.0006, 0.009, 400), index=index)
    first = bootstrap_confidence(returns, iterations=200)
    second = bootstrap_confidence(returns, iterations=200)
    assert first == second
    assert first["p05"] <= first["observed"] <= first["p95"]
    with pytest.raises(ValueError, match="unknown metric"):
        bootstrap_confidence(returns, metric="omega")


def test_variance_ratio_detects_a_trending_series() -> None:
    """A random walk is not rejected; a strongly trending series is."""
    index = pd.bdate_range("2026-01-01", periods=400, tz="UTC")
    rng = np.random.default_rng(17)
    walk = pd.Series(rng.normal(0.0, 0.01, 400), index=index)
    assert variance_ratio_test(walk, q=5)["p_value"] > 0.05

    # An AR(1) with positive persistence: today's move partly repeats tomorrow.
    shocks = rng.normal(0.0, 0.01, 400)
    persistent = np.zeros(400)
    for step in range(1, 400):
        persistent[step] = 0.4 * persistent[step - 1] + shocks[step]
    trending = variance_ratio_test(pd.Series(persistent, index=index), q=5)
    assert trending["variance_ratio"] > 1.0
    assert trending["p_value"] < 0.05
    assert trending["interpretation"] == "trending"
    assert variance_ratio_test(walk.iloc[:5], q=5)["variance_ratio"] is None


# --------------------------------------------------------------------------
# promotion
# --------------------------------------------------------------------------
async def test_promotion_requires_the_gates(lab: SimpleNamespace) -> None:
    """A losing backtest cannot be promoted, and hard gates cannot be waived."""
    record = await lab.backtests.run(_losing_config())
    report = await lab.validation.evaluate(record.id)
    assert report.passed is False
    assert report.verdict in {"fail", "conditional"}

    with pytest.raises(PermissionError, match="blocked by"):
        await lab.validation.promote(record.id, actor="ana")

    tiny = BacktestConfig(
        name="too-short",
        symbols=list(WINNERS),
        signal=SignalSpec(
            kind=SignalKind.STATIC, symbols=list(WINNERS), weighting=Weighting.EQUAL
        ),
        benchmark=BENCHMARK,
        rebalance=Rebalance.MONTHLY,
        start=_utc_today() - timedelta(days=200),
        oos_fraction=0.3,
    )
    short_record = await lab.backtests.run(tiny)
    short_report = await lab.validation.evaluate(short_record.id)
    failing = {gate.id for gate in short_report.failed_gates}
    assert "min_history" in failing
    with pytest.raises(PermissionError, match="cannot be overridden"):
        await lab.validation.promote(
            short_record.id, overrides=sorted(failing), comment="ship it", actor="ana"
        )


async def test_promotion_records_the_strategy_and_advances_the_funnel(
    lab: SimpleNamespace, bus: EventBus
) -> None:
    """A passing run promotes: strategy stored, stage advanced, hypothesis validated."""
    events: list[dict] = []
    bus.subscribe("lab", lambda event: events.append(event.payload))

    hypothesis = lab.hypotheses.create("Momentum", "12-1 momentum works", "IC turns negative")
    experiment = lab.experiments.create(
        "momentum-top3", _momentum_config(), hypothesis_id=hypothesis.id
    )
    run = await lab.experiments.run(experiment.id)
    assert run.backtest_id is not None

    # First signature: recorded, but nothing is promoted on one key alone.
    first = await lab.validation.promote(
        run.backtest_id, comment="paper trade it", actor="ana", stage=ExperimentStage.PAPER
    )
    assert first.promoted is False
    assert first.promotion_pending is True
    assert [entry["actor"] for entry in first.approvals] == ["ana"]
    assert lab.validation.promoted() == []
    with pytest.raises(PermissionError, match="already signed"):
        await lab.validation.promote(
            run.backtest_id, actor="ana", stage=ExperimentStage.PAPER
        )
    with pytest.raises(PermissionError, match="named approver"):
        await lab.validation.promote(run.backtest_id, stage=ExperimentStage.PAPER)

    report = await lab.validation.promote(
        run.backtest_id, comment="paper trade it", actor="marco", stage=ExperimentStage.PAPER
    )
    assert report.promoted is True
    assert report.passed is True
    assert [entry["actor"] for entry in report.approvals] == ["ana", "marco"]

    promoted = lab.validation.promoted()
    assert len(promoted) == 1
    assert promoted[0]["backtest_id"] == run.backtest_id
    assert promoted[0]["stage"] == "paper"
    assert promoted[0]["promoted_by"] == "ana + marco"
    assert promoted[0]["baseline"]["sharpe"] == report.gates[0].observed

    assert lab.experiments.get(experiment.id).stage is ExperimentStage.PAPER
    assert lab.hypotheses.get(hypothesis.id).status is HypothesisStatus.VALIDATED
    assert any(event.get("kind") == "promotion" for event in events)

    # The report survives a round trip through SQLite unchanged.
    stored = lab.validation.get_report(report.id)
    assert stored.verdict == "pass"
    assert stored.thresholds.min_oos_sharpe == report.thresholds.min_oos_sharpe
    assert len(stored.gates) == len(report.gates)


async def test_promotion_needs_two_distinct_approvers(lab: SimpleNamespace) -> None:
    """The two-key rule: one signature parks the promotion, two release it."""
    record = await lab.backtests.run(_momentum_config())

    idle = lab.validation.promotion_state(record.id)
    assert idle["signed"] == []
    assert idle["outstanding"] == 2
    assert idle["label"] == "needs 2 approvals"

    await lab.validation.promote(record.id, comment="looks sound", actor="ana")
    parked = lab.validation.promotion_state(record.id)
    assert parked["signed"] == ["ana"]
    assert parked["outstanding"] == 1
    assert parked["label"] == "needs 2nd approval"
    assert parked["complete"] is False
    assert lab.validation.promoted() == []
    assert [state["backtest_id"] for state in lab.validation.pending_promotions()] == [
        record.id
    ]

    # A withdrawn signature really is withdrawn.
    assert lab.validation.withdraw_promotion_approval(record.id, "ana") == 1
    assert lab.validation.promotion_state(record.id)["signed"] == []

    await lab.validation.promote(record.id, comment="looks sound", actor="ana")
    report = await lab.validation.promote(record.id, comment="second pair", actor="marco")
    assert report.promoted is True
    assert lab.validation.promotion_state(record.id)["complete"] is True
    assert lab.validation.pending_promotions() == []
    # A completed signature is history and cannot be taken back.
    assert lab.validation.withdraw_promotion_approval(record.id, "ana") == 0


async def test_monte_carlo_cone_and_tearsheet(lab: SimpleNamespace) -> None:
    """Resampling a finished run yields a real cone, and the PDF export renders."""
    record = await lab.backtests.run(_momentum_config())

    analysis = await lab.analyses.run_monte_carlo(
        record.config, paths=300, shown=25, backtest_id=record.id
    )
    cone = analysis.cells[0]["cone"]
    assert analysis.summary["paths"] == 300
    assert cone["paths"] == 300
    assert cone["shown"] == 25
    assert len(cone["curves"]) == 25
    assert len(cone["dates"]) == len(cone["bands"]["p50"]) == len(cone["realised"])
    # The percentile bands are ordered at every point in time.
    for low, mid, high in zip(
        cone["bands"]["p5"], cone["bands"]["p50"], cone["bands"]["p95"], strict=True
    ):
        assert low <= mid <= high
    assert 0.0 <= cone["sharpe"]["prob_positive"] <= 1.0
    assert 0.0 <= cone["terminal"]["realised_percentile"] <= 100.0

    # The cone is persisted against the backtest, which is what lets
    # GET /api/lab/backtests/{id}/fan answer `reused: true` instead of
    # recomputing thousands of resamples on every tearsheet open.
    stored = lab.analyses.list_analyses(
        kind=AnalysisKind.MONTE_CARLO, backtest_id=record.id
    )
    assert [item.id for item in stored] == [analysis.id]
    assert int(stored[0].summary["paths"]) == 300

    # The resample is seeded, so a recomputation is the same cone rather than a
    # different one that happens to look similar.
    again = await lab.analyses.run_monte_carlo(
        record.config, paths=300, shown=25, backtest_id=record.id
    )
    assert again.cells[0]["cone"]["bands"]["p50"] == cone["bands"]["p50"]

    body, filename = lab.backtests.tearsheet(record.id)
    assert filename.endswith(".pdf")
    assert body.startswith(b"%PDF-1.4")
    assert body.rstrip().endswith(b"%%EOF")
    assert b"TradingLib backtest tearsheet" in body

    queued = lab.backtests.queue(_momentum_config())
    with pytest.raises(ValueError, match="only a completed run"):
        lab.backtests.tearsheet(queued.id)


async def test_override_needs_a_note(lab: SimpleNamespace) -> None:
    """Waiving a soft gate is allowed, but only with a written justification."""
    record = await lab.backtests.run(_momentum_config())
    strict = GateThresholds(min_oos_sharpe=99.0)
    engine = ValidationEngine(
        bus=lab.validation.bus,
        backtests=lab.backtests,
        experiments=lab.experiments,
        thresholds=strict,
    )
    with pytest.raises(PermissionError, match="requires a note"):
        await engine.promote(record.id, overrides=["min_oos_sharpe"], actor="ana")
    pending = await engine.promote(
        record.id,
        overrides=["min_oos_sharpe"],
        comment="threshold under review",
        actor="ana",
    )
    assert pending.promoted is False
    report = await engine.promote(
        record.id,
        overrides=["min_oos_sharpe"],
        comment="threshold under review",
        actor="marco",
    )
    assert report.overrides == ["min_oos_sharpe"]
    assert report.promoted is True

    state = engine.promotion_state(record.id)
    assert state["complete"] is True
    assert state["signed"] == ["ana", "marco"]


# --------------------------------------------------------------------------
# evaluation
# --------------------------------------------------------------------------
async def test_evaluation_aggregates_the_real_ledger(lab: SimpleNamespace) -> None:
    """Candidates, funnel, gate outcomes and the summary all count real rows."""
    hypothesis = lab.hypotheses.create("Momentum", "12-1 momentum works", "IC turns negative")
    winner = lab.experiments.create(
        "momentum-top3", _momentum_config(), hypothesis_id=hypothesis.id
    )
    loser = lab.experiments.create("hold-losers", _losing_config(), hypothesis_id=hypothesis.id)
    winning_run = await lab.experiments.run(winner.id)
    losing_run = await lab.experiments.run(loser.id)
    await lab.validation.evaluate(winning_run.backtest_id)
    await lab.validation.evaluate(losing_run.backtest_id)

    candidates = lab.evaluation.candidates()
    assert [row["name"] for row in candidates] == ["momentum-top3", "hold-losers"]
    assert candidates[0]["gates_passed"] == candidates[0]["gates_total"]
    assert candidates[1]["gates_passed"] < candidates[1]["gates_total"]
    assert candidates[0]["verdict"] == "pass"

    scorecard = lab.evaluation.scorecard()
    assert [item["name"] for item in scorecard["candidates"]] == [
        "momentum-top3",
        "hold-losers",
    ]
    assert {row["key"] for row in scorecard["rows"]} >= {"oos_sharpe", "max_drawdown"}

    outcomes = lab.evaluation.gate_outcomes()
    assert outcomes["reports"] == 2
    sharpe_gate = next(item for item in outcomes["by_gate"] if item["gate"] == "min_oos_sharpe")
    assert sharpe_gate["pass"] == 1
    assert sharpe_gate["fail"] == 1
    assert sharpe_gate["pass_rate"] == pytest.approx(0.5)
    assert outcomes["timeline"][0]["verdict"] in {"pass", "fail", "conditional"}

    summary = lab.evaluation.summary()
    assert summary["experiments"]["total"] == 2
    assert summary["runs"]["experiment_runs"] == 2
    assert summary["runs"]["backtest_runs"] == 2
    assert summary["validation"]["reports"] == 2
    assert summary["validation"]["gate_pass_rate"] == pytest.approx(0.5)
    assert summary["hypotheses"]["total"] == 1

    funnel = lab.evaluation.funnel()
    assert funnel["total"] == 2
    assert dict((row["stage"], row["count"]) for row in funnel["stages"])["research"] == 2


async def test_promoted_strategy_tracking_measures_the_live_window(
    lab: SimpleNamespace,
) -> None:
    """Back-dating a promotion makes the window after it genuinely out-of-sample."""
    record = await lab.backtests.run(_momentum_config())
    await lab.validation.promote(record.id, comment="paper", actor="ana")
    await lab.validation.promote(record.id, comment="paper", actor="marco")
    strategy_id = lab.validation.promoted()[0]["id"]

    # Move the promotion back so there is real post-promotion history to score.
    backdated = datetime.now(timezone.utc) - timedelta(days=200)
    with lab.evaluation._session() as session:  # noqa: SLF001 - test setup
        row = session.get(PromotedStrategyRow, strategy_id)
        row.promoted_at = backdated

    tracked = await lab.evaluation.update(strategy_id)
    assert tracked.observations > 50
    assert tracked.live_sharpe is not None
    assert tracked.expected_sharpe is not None
    assert tracked.decay_score == pytest.approx(
        (tracked.expected_sharpe - tracked.live_sharpe) / tracked.expected_sharpe, rel=1e-12
    )
    assert tracked.band is not None and tracked.in_band is not None
    assert tracked.tracking_error is not None

    # The snapshot is persisted and read back by the list view.
    stored = lab.evaluation.get(strategy_id)
    assert stored.live_sharpe == pytest.approx(tracked.live_sharpe)
    assert [item.strategy_id for item in lab.evaluation.list()] == [strategy_id]

    decay = await lab.evaluation.decay(strategy_id, window=63)
    assert not decay.empty
    assert decay.notna().all()


# --------------------------------------------------------------------------
# the persistence surface the API layer drives
# --------------------------------------------------------------------------
async def test_queued_backtest_runs_under_its_own_identifier(lab: SimpleNamespace) -> None:
    """The screener's "queue backtest" path: queue, run, read results, delete."""
    config = _momentum_config("queued-from-screen")
    queued = lab.backtests.queue(config, screen_id="sc-047")
    assert queued.status is BacktestStatus.QUEUED
    assert queued.screen_id == "sc-047"
    assert [record.id for record in lab.backtests.list_runs(status=BacktestStatus.QUEUED)] == [
        queued.id
    ]

    finished = await lab.backtests.run_queued(queued.id)
    assert finished.id == queued.id  # the identifier the toast showed still resolves
    assert finished.status is BacktestStatus.COMPLETE
    assert finished.source == "screen"

    results = lab.backtests.results(queued.id)
    assert results["equity_curve"]["dates"]
    assert results["stats"]["periods"] == finished.stats["periods"]
    assert results["config"]["signal"]["kind"] == "momentum"

    lab.backtests.delete(queued.id)
    with pytest.raises(KeyError):
        lab.backtests.get(queued.id)


def test_backtest_config_round_trips_through_json(lab: SimpleNamespace) -> None:
    """A persisted specification rebuilds into exactly the same configuration."""
    config = _momentum_config()
    rebuilt = BacktestConfig.from_dict(config.as_dict())
    assert rebuilt.as_dict() == config.as_dict()
    assert rebuilt.signal.kind is SignalKind.MOMENTUM
    assert rebuilt.costs.total_bps == pytest.approx(6.0)
    assert rebuilt.effective_top_n() == 3


def test_externally_supplied_signals_are_backtested_as_given(lab: SimpleNamespace) -> None:
    """Screen survivors can be handed straight to the backtester as a signal panel."""
    from tradinglib.research.backtest import equal_weight_signals

    prices, volumes, benchmark, warnings = lab.backtests.prepare(_momentum_config())
    survivors = equal_weight_signals(prices, ["MOMA", "MOMB"])
    config = BacktestConfig(
        name="screen survivors",
        symbols=[*WINNERS, *LOSERS],
        signal=SignalSpec(kind=SignalKind.STATIC, symbols=["MOMA", "MOMB"]),
        benchmark=BENCHMARK,
        rebalance=Rebalance.MONTHLY,
        oos_fraction=0.3,
    )
    result = Backtester().run(config, prices, signals=survivors, benchmark=benchmark)
    held = result.positions.iloc[-1]
    assert held["MOMA"] > 0 and held["MOMB"] > 0
    assert held[list(LOSERS)].sum() == pytest.approx(0.0)
    assert result.stats.periods == prices.shape[0] - 1


async def test_hypothesis_card_counts_come_from_the_run_tables(lab: SimpleNamespace) -> None:
    """"8 experiments · 2 passed" is counted, never stored as a guess."""
    hypothesis = lab.hypotheses.create("Momentum", "12-1 momentum works", "IC turns negative")
    winner = lab.experiments.create(
        "momentum-top3", _momentum_config(), hypothesis_id=hypothesis.id
    )
    loser = lab.experiments.create("hold-losers", _losing_config(), hypothesis_id=hypothesis.id)
    await lab.experiments.run(winner.id)
    await lab.experiments.run(loser.id)

    stats = lab.hypotheses.stats(hypothesis.id)
    assert stats == {
        "hypothesis_id": hypothesis.id,
        "experiments": 2,
        "runs": 2,
        "complete_runs": 2,
        "passed_runs": 1,
        "failed_runs": 1,
        "trials": 2,
    }


def test_creating_an_experiment_against_an_unknown_hypothesis_fails(
    lab: SimpleNamespace,
) -> None:
    """An experiment cannot dangle off a hypothesis that does not exist."""
    with pytest.raises(KeyError, match="unknown hypothesis"):
        lab.experiments.create("orphan", _momentum_config(), hypothesis_id="H-999")


async def test_the_lab_serves_the_screener_backtest_queue(lab: SimpleNamespace) -> None:
    """A screen's survivors become a real, running lab backtest."""
    from tradinglib.research import BacktestRequest, get_backtest_queue
    from tradinglib.research.backtest import LabBacktestQueue, install_backtest_queue

    # Importing the lab is what registers it; other tests swap the global out,
    # so re-install rather than depend on collection order.
    installed = install_backtest_queue()
    assert get_backtest_queue() is installed
    assert isinstance(installed, LabBacktestQueue)

    queue = LabBacktestQueue(engine=lab.backtests)
    request = BacktestRequest(
        name="screen: synthetic-winners",
        symbols=[*WINNERS, "moma"],  # duplicates and case are normalised
        screen_id="sc-001",
        screen_run_id="sr-001",
        benchmark=BENCHMARK,
        rebalance="monthly",
        start=_utc_today() - timedelta(days=900),
    )
    config = queue.build_config(request)
    assert config.symbols == list(WINNERS)
    assert config.signal.kind is SignalKind.STATIC
    assert config.params["screen_run_id"] == "sr-001"

    response = await queue.queue_backtest(request)
    assert response["status"] in {"queued", "running", "complete"}
    assert response["id"].startswith("bt-")
    assert "progress streams" in response["detail"]

    await queue.drain()
    finished = lab.backtests.get(response["id"])
    assert finished.status is BacktestStatus.COMPLETE
    assert finished.screen_id == "sc-001"
    assert finished.source == "screener"
    assert finished.stats["periods"] > 100

    with pytest.raises(ValueError, match="at least one symbol"):
        queue.build_config(BacktestRequest(name="empty", symbols=[]))
    with pytest.raises(ValueError, match="unknown rebalance"):
        queue.build_config(
            BacktestRequest(name="bad", symbols=["MOMA"], rebalance="fortnightly")
        )
