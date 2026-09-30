"""Backtester correctness.

Every expectation in this module is written out as arithmetic, not copied from
a previous run: the point is that the engine reproduces numbers a human can
derive with a calculator from the timing convention documented in
``tradinglib.research.backtest``.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from tradinglib.research.backtest import (
    BacktestConfig,
    Backtester,
    CostModel,
    ExpressionError,
    Rebalance,
    SignalKind,
    SignalSpec,
    Weighting,
    build_factor_panels,
    compile_expression,
    compute_stats,
    infer_periods_per_year,
    max_drawdown,
)

# --------------------------------------------------------------------------
# fixtures: two hand-written price paths
# --------------------------------------------------------------------------
AAA = [100.0, 102.0, 101.0, 104.0, 103.0, 106.0]
BBB = [50.0, 49.5, 50.5, 50.0, 51.0, 50.5]


@pytest.fixture()
def prices() -> pd.DataFrame:
    """Six business days of two hand-written price paths."""
    index = pd.date_range("2026-01-05", periods=6, freq="B", tz="UTC")
    return pd.DataFrame({"AAA": AAA, "BBB": BBB}, index=index)


def _config(**overrides: object) -> BacktestConfig:
    """A two-symbol, equal-weight, daily-rebalanced configuration."""
    defaults: dict[str, object] = {
        "name": "hand-computed",
        "symbols": ["AAA", "BBB"],
        "signal": SignalSpec(
            kind=SignalKind.STATIC, symbols=["AAA", "BBB"], weighting=Weighting.EQUAL
        ),
        "rebalance": Rebalance.DAILY,
        "benchmark": None,
        "costs": CostModel(commission_bps=0.0, slippage_bps=0.0),
        "oos_fraction": None,
        "initial_capital": 100.0,
        "periods_per_year": 252,
    }
    defaults.update(overrides)
    return BacktestConfig(**defaults)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# the accounting
# --------------------------------------------------------------------------
def test_costless_equal_weight_returns_are_hand_computable(prices: pd.DataFrame) -> None:
    """Each bar's return is the weighted average of that bar's asset returns."""
    result = Backtester().run(_config(), prices)

    expected = [
        0.5 * (102.0 / 100.0 - 1) + 0.5 * (49.5 / 50.0 - 1),
        0.5 * (101.0 / 102.0 - 1) + 0.5 * (50.5 / 49.5 - 1),
        0.5 * (104.0 / 101.0 - 1) + 0.5 * (50.0 / 50.5 - 1),
        0.5 * (103.0 / 104.0 - 1) + 0.5 * (51.0 / 50.0 - 1),
        0.5 * (106.0 / 103.0 - 1) + 0.5 * (50.5 / 51.0 - 1),
    ]
    assert result.returns.tolist() == pytest.approx(expected, rel=1e-12)
    # The curve starts at the initial capital: bar 0 only opens the position.
    assert result.equity_curve.size == 6
    assert result.equity_curve.iloc[0] == pytest.approx(100.0, rel=1e-12)
    assert result.equity_curve.iloc[-1] == pytest.approx(
        100.0 * math.prod(1 + r for r in expected), rel=1e-12
    )
    assert result.stats.total_return == pytest.approx(
        math.prod(1 + r for r in expected) - 1, rel=1e-12
    )


def test_costs_are_charged_on_the_bar_after_the_trade(prices: pd.DataFrame) -> None:
    """Entry costs hit bar 1; each rebalance's cost hits the following bar."""
    costs = CostModel(commission_bps=4.0, slippage_bps=6.0)  # 10 bps one way
    rate = 0.001
    result = Backtester().run(_config(costs=costs), prices)

    gross_1 = 0.5 * (102.0 / 100.0 - 1) + 0.5 * (49.5 / 50.0 - 1)
    # Bar 0 opened a fully invested book: traded notional 1.0.
    assert result.returns.iloc[0] == pytest.approx(gross_1 - 1.0 * rate, rel=1e-12)

    # After bar 1 the weights have drifted; the rebalance trades the difference.
    drifted_aaa = 0.5 * (102.0 / 100.0) / (1 + gross_1)
    drifted_bbb = 0.5 * (49.5 / 50.0) / (1 + gross_1)
    traded_1 = abs(0.5 - drifted_aaa) + abs(0.5 - drifted_bbb)
    assert result.turnover.iloc[1] == pytest.approx(traded_1, rel=1e-12)

    gross_2 = 0.5 * (101.0 / 102.0 - 1) + 0.5 * (50.5 / 49.5 - 1)
    assert result.returns.iloc[1] == pytest.approx(gross_2 - traded_1 * rate, rel=1e-12)

    # Annualised turnover is the mean traded notional times the period count.
    assert result.stats.turnover == pytest.approx(
        float(result.turnover.mean()) * 252, rel=1e-12
    )


def test_no_trade_is_executed_on_the_final_bar(prices: pd.DataFrame) -> None:
    """The last bar has no following bar, so it never pays a cost it cannot earn."""
    result = Backtester().run(_config(costs=CostModel(10.0, 10.0)), prices)
    # turnover is indexed by the bar the cost lands on; the final trade is absent.
    assert result.turnover.size == result.returns.size == 5
    assert result.trade_counts.iloc[0] == 2  # the entry, both names


def test_weights_drift_between_rebalances() -> None:
    """A monthly book is untouched inside a month and drifts with its holdings."""
    index = pd.date_range("2026-01-01", periods=45, freq="B", tz="UTC")
    frame = pd.DataFrame(
        {
            "AAA": 100.0 * (1.01 ** np.arange(45)),
            "BBB": 100.0 * (0.99 ** np.arange(45)),
        },
        index=index,
    )
    result = Backtester().run(_config(rebalance=Rebalance.MONTHLY), frame)

    traded = result.turnover
    # 45 business days from 1 January span three months, so the only trades are
    # the entry and the first bar of February and of March. Costs are recorded
    # against the bar after the trade, hence the shifted stamps.
    non_zero = traded[traded > 1e-12]
    assert non_zero.size == 3
    assert non_zero.iloc[0] == pytest.approx(1.0, rel=1e-12)
    assert [str(ts.date()) for ts in non_zero.index] == ["2026-01-02", "2026-02-03", "2026-03-03"]

    # Inside January the winner's weight grows without any trading.
    january = result.positions.loc[result.positions.index < pd.Timestamp("2026-02-01", tz="UTC")]
    assert january["AAA"].iloc[0] == pytest.approx(0.5, rel=1e-12)
    assert january["AAA"].iloc[-1] > 0.55
    assert january["AAA"].iloc[-1] + january["BBB"].iloc[-1] == pytest.approx(1.0, rel=1e-12)


# --------------------------------------------------------------------------
# statistics
# --------------------------------------------------------------------------
def test_compute_stats_matches_the_textbook_formulas() -> None:
    """Sharpe, volatility, drawdown and hit rate from a four-return series."""
    values = [0.01, -0.005, 0.02, -0.01]
    index = pd.date_range("2026-03-02", periods=4, freq="B", tz="UTC")
    returns = pd.Series(values, index=index)

    stats = compute_stats(returns, periods_per_year=252)

    mean = sum(values) / 4
    variance = sum((v - mean) ** 2 for v in values) / 3  # ddof=1
    std = math.sqrt(variance)
    assert stats.periods == 4
    assert stats.years == pytest.approx(4 / 252, rel=1e-12)
    assert stats.total_return == pytest.approx(math.prod(1 + v for v in values) - 1, rel=1e-12)
    assert stats.volatility == pytest.approx(std * math.sqrt(252), rel=1e-12)
    assert stats.sharpe == pytest.approx(mean / std * math.sqrt(252), rel=1e-12)
    assert stats.hit_rate == pytest.approx(0.5, rel=1e-12)
    assert stats.cagr == pytest.approx(
        (1 + stats.total_return) ** (252 / 4) - 1, rel=1e-12
    )

    downside = math.sqrt(sum(min(v, 0.0) ** 2 for v in values) / 4)
    assert stats.sortino == pytest.approx(mean / downside * math.sqrt(252), rel=1e-12)


def test_max_drawdown_is_the_worst_peak_to_trough() -> None:
    """A +10% then -20% then +5% path draws down exactly 20%."""
    returns = pd.Series(
        [0.10, -0.20, 0.05], index=pd.date_range("2026-03-02", periods=3, freq="B", tz="UTC")
    )
    assert max_drawdown(returns) == pytest.approx(-0.20, rel=1e-12)


def test_benchmark_relative_stats_are_exact_when_the_book_is_the_benchmark() -> None:
    """Holding the benchmark gives beta 1, zero alpha, zero excess and zero IR."""
    index = pd.date_range("2026-01-05", periods=8, freq="B", tz="UTC")
    path = pd.Series([100.0, 101.0, 100.5, 102.0, 103.5, 103.0, 104.0, 105.0], index=index)
    frame = pd.DataFrame({"AAA": path})
    config = _config(
        symbols=["AAA"],
        signal=SignalSpec(kind=SignalKind.STATIC, symbols=["AAA"], weighting=Weighting.EQUAL),
        benchmark="AAA",
    )
    result = Backtester().run(config, frame, benchmark=path)

    assert result.stats.beta_vs_benchmark == pytest.approx(1.0, rel=1e-9)
    assert result.stats.alpha_vs_benchmark == pytest.approx(0.0, abs=1e-12)
    assert result.stats.excess_return == pytest.approx(0.0, abs=1e-12)
    assert result.stats.information_ratio == pytest.approx(0.0, abs=1e-12)
    assert result.stats.benchmark_return == pytest.approx(105.0 / 100.0 - 1, rel=1e-12)


def test_out_of_sample_split_partitions_the_return_series(prices: pd.DataFrame) -> None:
    """The split is a partition: both halves together are the whole run."""
    result = Backtester().run(_config(), prices)
    assert result.out_of_sample is None  # oos_fraction was disabled

    in_sample, out_of_sample = Backtester().split_oos(result, result.returns.index[3].date())
    assert in_sample.periods == 3
    assert out_of_sample.periods == 2
    assert in_sample.periods + out_of_sample.periods == result.stats.periods
    assert (1 + in_sample.total_return) * (1 + out_of_sample.total_return) == pytest.approx(
        1 + result.stats.total_return, rel=1e-12
    )


def test_oos_fraction_holds_out_the_tail() -> None:
    """A 30% hold-out puts the last 30% of observations out of sample."""
    index = pd.date_range("2026-01-05", periods=21, freq="B", tz="UTC")
    frame = pd.DataFrame({"AAA": 100.0 * (1.002 ** np.arange(21))}, index=index)
    config = _config(
        symbols=["AAA"],
        signal=SignalSpec(kind=SignalKind.STATIC, symbols=["AAA"], weighting=Weighting.EQUAL),
        oos_fraction=0.3,
    )
    result = Backtester().run(config, frame)
    assert result.in_sample is not None and result.out_of_sample is not None
    assert result.in_sample.periods + result.out_of_sample.periods == result.stats.periods
    assert result.out_of_sample.periods == pytest.approx(6, abs=1)


@pytest.mark.parametrize(
    ("freq", "expected"),
    [("B", 252), ("D", 252), ("W", 52), ("MS", 12), ("QS", 4)],
)
def test_periods_per_year_is_inferred_from_bar_spacing(freq: str, expected: int) -> None:
    """Daily, weekly, monthly and quarterly indexes annualise differently."""
    index = pd.date_range("2026-01-01", periods=24, freq=freq, tz="UTC")
    assert infer_periods_per_year(index) == expected


# --------------------------------------------------------------------------
# signals and weighting
# --------------------------------------------------------------------------
def test_momentum_signal_ranks_and_caps_positions() -> None:
    """Top-N selection keeps only the strongest signals, equally weighted."""
    index = pd.date_range("2026-01-01", periods=40, freq="B", tz="UTC")
    frame = pd.DataFrame(
        {
            "UP": 100.0 * (1.01 ** np.arange(40)),
            "FLAT": np.full(40, 100.0),
            "DOWN": 100.0 * (0.99 ** np.arange(40)),
        },
        index=index,
    )
    spec = SignalSpec(
        kind=SignalKind.MOMENTUM,
        lookback=20,
        skip=1,
        top_n=1,
        weighting=Weighting.EQUAL,
    )
    engine = Backtester()
    signals = engine.build_signals(spec, frame)
    config = _config(symbols=list(frame.columns), signal=spec)
    weights = engine.build_positions(signals, spec, config, prices=frame)

    last = weights.iloc[-1]
    assert last["UP"] == pytest.approx(1.0, rel=1e-12)
    assert last["FLAT"] == 0.0
    assert last["DOWN"] == 0.0
    # 20-day momentum skipping one day is exactly that ratio of prices.
    expected_signal = frame["UP"].shift(1) / frame["UP"].shift(20) - 1
    assert signals["UP"].iloc[-1] == pytest.approx(float(expected_signal.iloc[-1]), rel=1e-12)


def test_signal_weighting_is_proportional_and_long_only_drops_negatives() -> None:
    """Signal weighting allocates in proportion; negatives cannot be held long."""
    index = pd.date_range("2026-01-05", periods=3, freq="B", tz="UTC")
    frame = pd.DataFrame(
        {"A": [10.0, 10.0, 10.0], "B": [10.0, 10.0, 10.0], "C": [10.0, 10.0, 10.0]}, index=index
    )
    signals = pd.DataFrame({"A": [3.0] * 3, "B": [1.0] * 3, "C": [-5.0] * 3}, index=index)
    spec = SignalSpec(kind=SignalKind.EXPRESSION, expression="close", weighting=Weighting.SIGNAL)
    config = _config(symbols=["A", "B", "C"], signal=spec)

    weights = Backtester().build_positions(signals, spec, config, prices=frame)
    assert weights.iloc[0]["A"] == pytest.approx(0.75, rel=1e-12)
    assert weights.iloc[0]["B"] == pytest.approx(0.25, rel=1e-12)
    assert weights.iloc[0]["C"] == 0.0


def test_a_signal_that_never_selects_anything_fails_loudly(prices: pd.DataFrame) -> None:
    """An impossible filter raises instead of returning a flat, meaningless curve."""
    spec = SignalSpec(
        kind=SignalKind.EXPRESSION,
        expression="close",
        filter_expression="close > 1000000",
        weighting=Weighting.EQUAL,
    )
    with pytest.raises(ValueError, match="never selects"):
        Backtester().run(_config(signal=spec), prices)


def test_unadjusted_corporate_actions_are_flagged() -> None:
    """A 90% single-bar drop is reported as a probable split, not traded blindly."""
    index = pd.date_range("2026-01-05", periods=6, freq="B", tz="UTC")
    frame = pd.DataFrame(
        {"AAA": [100.0, 101.0, 10.0, 10.1, 10.2, 10.3], "BBB": BBB}, index=index
    )
    result = Backtester().run(_config(), frame)
    assert any("corporate action" in warning for warning in result.warnings)


# --------------------------------------------------------------------------
# the expression engine
# --------------------------------------------------------------------------
def test_factor_panels_are_derived_from_prices() -> None:
    """``mom_12_1`` is the documented skip-a-month twelve-month return."""
    index = pd.date_range("2024-01-01", periods=300, freq="B", tz="UTC")
    frame = pd.DataFrame({"AAA": 100.0 * (1.001 ** np.arange(300))}, index=index)
    panels = build_factor_panels(frame)
    expected = frame["AAA"].shift(21) / frame["AAA"].shift(252) - 1
    assert panels["mom_12_1"]["AAA"].iloc[-1] == pytest.approx(
        float(expected.iloc[-1]), rel=1e-12
    )
    assert set(panels) >= {"close", "ret_1d", "vol_20d", "rsi_14", "ma_ratio_50_200"}


def test_expressions_combine_factors_cross_sectionally() -> None:
    """``rank`` is a per-bar percentile, so the best name ranks 1.0."""
    index = pd.date_range("2026-01-05", periods=2, freq="B", tz="UTC")
    namespace = {
        "a": pd.DataFrame({"X": [1.0, 1.0], "Y": [2.0, 2.0], "Z": [3.0, 3.0]}, index=index)
    }
    ranked = compile_expression("rank(a)", namespace)
    assert ranked.iloc[0].tolist() == pytest.approx([1 / 3, 2 / 3, 1.0], rel=1e-12)

    combined = compile_expression("2 * a + 1", namespace)
    assert combined.iloc[0].tolist() == pytest.approx([3.0, 5.0, 7.0], rel=1e-12)

    masked = compile_expression("where(a > 1.5, a, 0)", namespace)
    assert masked.iloc[0].tolist() == pytest.approx([0.0, 2.0, 3.0], rel=1e-12)


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('true')",
        "close.__class__",
        "open('/etc/passwd')",
        "lambda: 1",
        "[c for c in close]",
        "unknown_factor * 2",
    ],
)
def test_expressions_reject_anything_outside_the_whitelist(expression: str) -> None:
    """Nothing but arithmetic and the named functions may execute."""
    index = pd.date_range("2026-01-05", periods=2, freq="B", tz="UTC")
    namespace = {"close": pd.DataFrame({"X": [1.0, 2.0]}, index=index)}
    with pytest.raises(ExpressionError):
        compile_expression(expression, namespace)
