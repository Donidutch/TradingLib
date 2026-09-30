"""The scorecard's evidence rows: capacity, live-book correlation, joins.

Everything here runs against a real temporary Parquet store and a real SQLite
application database - the capacity figures come from stored volume, the live
book is rebuilt from stored fills, and the joined rows come from analyses that
were actually run. The refusal paths are tested as carefully as the numbers:
"no live book" and "no volume history" must produce ``None`` with a reason, never
a plausible-looking zero.
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
from tradinglib.research.analyses import (
    AnalysisEngine,
    AnalysisKind,
    ParameterAxis,
    SweepSpec,
    WalkForwardSpec,
)
from tradinglib.research.backtest import (
    BacktestConfig,
    BacktestEngine,
    CostModel,
    Rebalance,
    SignalKind,
    SignalSpec,
    Weighting,
)
from tradinglib.research.capacity import (
    DEFAULT_PARTICIPATION,
    MIN_ADV_SESSIONS,
    CapacityEngine,
    holdings_from_trade_log,
)
from tradinglib.research.evaluation import NO_ANALYSIS, EvaluationEngine
from tradinglib.research.experiments import ExperimentEngine
from tradinglib.research.hypotheses import HypothesisRegistry
from tradinglib.research.livebook import (
    DEFAULT_MIN_OVERLAP,
    NO_BOOK,
    LiveBookEngine,
    returns_from_curve,
)
from tradinglib.research.validation import ValidationEngine
from tradinglib.trade.portfolio import Fill, Order, OrderSide, OrderStatus, PortfolioStore

UTC = timezone.utc

#: The book the live-book tests trade.
BOOK_SYMBOLS = ("BOOKA", "BOOKB")

#: Names the backtests trade: the first two trend up, the last two drift down.
WINNERS = ("EVMOMA", "EVMOMB")
LOSERS = ("EVLAGA", "EVLAGB")
BENCHMARK = "EVSPY"

#: Length of the synthetic history, in business days.
BARS = 900

#: Dollar volume per name, chosen so the *smallest* one binds the capacity.
VOLUMES: dict[str, float] = {
    "EVMOMA": 4_000_000.0,
    "EVMOMB": 1_000_000.0,
    "EVLAGA": 9_000_000.0,
    "EVLAGB": 9_000_000.0,
    BENCHMARK: 20_000_000.0,
    "BOOKA": 5_000_000.0,
    "BOOKB": 5_000_000.0,
}


def _paths(end: date, symbols: dict[str, tuple[float, float]]) -> dict[str, pd.Series]:
    """Build deterministic synthetic price paths."""
    index = pd.bdate_range(end=pd.Timestamp(end), periods=BARS, tz="UTC")
    rng = np.random.default_rng(20260902)
    return {
        symbol: pd.Series(
            100.0 * np.exp(np.cumsum(rng.normal(mu, sigma, size=BARS))), index=index, name=symbol
        )
        for symbol, (mu, sigma) in symbols.items()
    }


def _write(store: TimeSeriesStore, paths: dict[str, pd.Series]) -> None:
    """Write the paths into the store as real daily bars, with real volume."""
    for symbol, series in paths.items():
        shares = VOLUMES.get(symbol, 1_000_000.0)
        store.write_bars(
            [
                Bar(
                    symbol=symbol,
                    ts=ts.to_pydatetime(),
                    open=float(value),
                    high=float(value) * 1.004,
                    low=float(value) * 0.996,
                    close=float(value),
                    # Constant share volume, so ADV(usd) is close*shares and the
                    # expected capacity can be computed independently below.
                    volume=shares,
                    interval=BarInterval.DAY_1,
                    source="synthetic-test",
                )
                for ts, value in series.items()
            ]
        )


@pytest.fixture()
def lab(settings: Settings, bus: EventBus) -> Iterator[SimpleNamespace]:
    """A store, backtest ledger, analysis ledger and evaluation engine."""
    store = reset_store(TimeSeriesStore(settings.ts_dir))
    end = date.today()
    spec = {
        **{symbol: (0.0012, 0.0060) for symbol in WINNERS},
        **{symbol: (-0.0012, 0.0110) for symbol in LOSERS},
        BENCHMARK: (0.0003, 0.0070),
        **{symbol: (0.0005, 0.0090) for symbol in BOOK_SYMBOLS},
    }
    paths = _paths(end, spec)
    _write(store, paths)
    backtests = BacktestEngine(store=store, bus=bus)
    analyses = AnalysisEngine(backtests=backtests, bus=bus)
    experiments = ExperimentEngine(bus=bus, backtests=backtests)
    hypotheses = HypothesisRegistry(bus=bus)
    validation = ValidationEngine(bus=bus, backtests=backtests, experiments=experiments)
    portfolio = PortfolioStore()
    livebook = LiveBookEngine(store=store, portfolio=portfolio)
    capacity = CapacityEngine(store=store)
    evaluation = EvaluationEngine(
        bus=bus,
        backtests=backtests,
        experiments=experiments,
        validation=validation,
        hypotheses=hypotheses,
        analyses=analyses,
        livebook=livebook,
        capacity=capacity,
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
        livebook=livebook,
        capacity=capacity,
        portfolio=portfolio,
    )
    reset_store()


def _config(name: str = "evidence-momentum") -> BacktestConfig:
    """A monthly-rebalanced momentum book over the synthetic universe."""
    return BacktestConfig(
        name=name,
        symbols=[*WINNERS, *LOSERS],
        signal=SignalSpec(
            kind=SignalKind.MOMENTUM, lookback=126, skip=21, top_n=2, weighting=Weighting.EQUAL
        ),
        benchmark=BENCHMARK,
        rebalance=Rebalance.MONTHLY,
        costs=CostModel(commission_bps=2.0, slippage_bps=4.0),
        oos_fraction=0.3,
    )


def _seed_book(
    portfolio: PortfolioStore, paths: dict[str, pd.Series], *, days_ago: int = 200
) -> str:
    """Open a paper account and fill it with two real positions.

    Returns the account id. Prices come from the same stored series the book is
    later marked with, so the reconstruction is measured against real numbers.
    """
    account = portfolio.ensure_account("paper", starting_cash=1_000_000.0)
    index = paths[BOOK_SYMBOLS[0]].index
    when = index[max(0, len(index) - days_ago)].to_pydatetime()
    for number, symbol in enumerate(BOOK_SYMBOLS):
        order = Order(
            id=f"ord-{number}",
            symbol=symbol,
            side=OrderSide.BUY,
            qty=1_000.0,
            status=OrderStatus.FILLED,
            filled_qty=1_000.0,
            submitted_at=when,
            account_id=account.id,
        )
        portfolio.save_order(order)
        portfolio.save_fill(
            account.id,
            Fill(
                id=f"fill-{number}",
                order_id=order.id,
                symbol=symbol,
                side=OrderSide.BUY,
                qty=1_000.0,
                price=float(paths[symbol].loc[index[max(0, len(index) - days_ago)]]),
                ts=when,
                fee=1.0,
            ),
        )
    return account.id


# ---------------------------------------------------------------------------
# capacity
# ---------------------------------------------------------------------------
def test_capacity_is_bound_by_the_least_liquid_name(lab: SimpleNamespace) -> None:
    """The bound is min(participation x ADV / weight), and it names what binds."""
    holdings = {"EVMOMA": 0.5, "EVMOMB": 0.5}
    estimate = lab.capacity.estimate(holdings)

    assert estimate.capacity_usd is not None
    assert estimate.reason is None
    # EVMOMB trades a quarter of EVMOMA's shares at a similar price, so it binds.
    assert estimate.binding_symbol == "EVMOMB"

    per_name = {name.symbol: name for name in estimate.names}
    expected = DEFAULT_PARTICIPATION * per_name["EVMOMB"].adv_usd / 0.5
    assert estimate.capacity_usd == pytest.approx(expected)
    assert estimate.capacity_usd < per_name["EVMOMA"].capacity_usd

    # The ADV is the stored volume, not an assumption.
    closes = lab.paths["EVMOMB"].tail(63)
    assert per_name["EVMOMB"].adv_usd == pytest.approx(
        float((closes * VOLUMES["EVMOMB"]).mean())
    )

    # Halving the participation halves the book it allows.
    tighter = lab.capacity.estimate(holdings, participation=DEFAULT_PARTICIPATION / 2)
    assert tighter.capacity_usd == pytest.approx(estimate.capacity_usd / 2)

    with pytest.raises(ValueError, match="participation"):
        lab.capacity.estimate(holdings, participation=0.0)


def test_capacity_states_the_assumption_it_was_computed_under(lab: SimpleNamespace) -> None:
    """A capacity number without its participation assumption is meaningless."""
    estimate = lab.capacity.estimate({"EVMOMA": 1.0})
    assert "5%" in estimate.assumption
    assert "63-session" in estimate.assumption
    assert "no market-impact" in estimate.assumption.lower()
    payload = estimate.as_dict()
    assert payload["assumption"] == estimate.assumption
    assert payload["participation"] == DEFAULT_PARTICIPATION


def test_capacity_refuses_when_the_volume_history_is_missing(lab: SimpleNamespace) -> None:
    """A bound over part of the book is not a bound, and is not reported as one."""
    unknown = lab.capacity.estimate({"EVMOMA": 0.5, "NOSUCH": 0.5})
    assert unknown.capacity_usd is None
    assert "NOSUCH" in unknown.reason
    assert "would not be a bound" in unknown.reason

    # A name with some, but not enough, history is refused for the same reason.
    thin_days = pd.bdate_range(end=pd.Timestamp(lab.end), periods=MIN_ADV_SESSIONS - 5, tz="UTC")
    lab.store.write_bars(
        [
            Bar(
                symbol="THIN",
                ts=ts.to_pydatetime(),
                open=10.0,
                high=10.1,
                low=9.9,
                close=10.0,
                volume=100_000.0,
                interval=BarInterval.DAY_1,
                source="synthetic-test",
            )
            for ts in thin_days
        ]
    )
    thin = lab.capacity.estimate({"THIN": 1.0})
    assert thin.capacity_usd is None
    assert f"{MIN_ADV_SESSIONS} needed" in thin.names[0].reason

    # An empty book is refused too, rather than reported as unlimited.
    empty = lab.capacity.estimate({})
    assert empty.capacity_usd is None
    assert "no book to size" in empty.reason


def test_holdings_come_from_the_runs_open_trades() -> None:
    """Only the positions the run ends holding define the book to be sized."""
    trade_log = {
        "rows": [
            {"symbol": "aaa", "weight": 0.4, "open": True},
            {"symbol": "BBB", "weight": -0.3, "open": True},
            {"symbol": "CCC", "weight": 0.9, "open": False},
            {"symbol": "", "weight": 0.5, "open": True},
        ]
    }
    assert holdings_from_trade_log(trade_log) == {"AAA": 0.4, "BBB": 0.3}
    assert holdings_from_trade_log(None) == {}
    assert holdings_from_trade_log({"rows": []}) == {}


# ---------------------------------------------------------------------------
# live-book correlation
# ---------------------------------------------------------------------------
def test_live_book_returns_are_rebuilt_from_fills_and_marks(lab: SimpleNamespace) -> None:
    """Equity = cash after the fills + the marked positions, day by day."""
    account_id = _seed_book(lab.portfolio, lab.paths)
    book = lab.livebook.daily_returns()

    assert book.reason is None
    assert book.usable is True
    assert book.account_id == account_id
    assert sorted(book.symbols) == sorted(BOOK_SYMBOLS)
    assert book.returns.size >= DEFAULT_MIN_OVERLAP

    # Reconstruct the last day independently: 1,000 shares of each name, plus
    # the cash left after paying for them.
    last = book.equity.index[-1]
    spent = 0.0
    for symbol in BOOK_SYMBOLS:
        entry_index = lab.paths[symbol].index[max(0, len(lab.paths[symbol]) - 200)]
        spent += 1_000.0 * float(lab.paths[symbol].loc[entry_index]) + 1.0
    marked = sum(1_000.0 * float(lab.paths[symbol].loc[last]) for symbol in BOOK_SYMBOLS)
    assert float(book.equity.loc[last]) == pytest.approx(1_000_000.0 - spent + marked)


def test_a_book_correlated_with_itself_is_one(lab: SimpleNamespace) -> None:
    """The correlation is a real Pearson coefficient over the shared sessions."""
    _seed_book(lab.portfolio, lab.paths)
    book = lab.livebook.daily_returns()

    same = lab.livebook.correlate(book.returns, book=book)
    assert same["correlation"] == pytest.approx(1.0)
    assert same["reason"] is None
    assert same["overlap_days"] == book.returns.size

    inverted = lab.livebook.correlate(-book.returns, book=book)
    assert inverted["correlation"] == pytest.approx(-1.0)

    # An independent series is close to zero *and says how much it overlapped*,
    # which is what makes the zero meaningful rather than a default.
    rng = np.random.default_rng(7)
    noise = pd.Series(rng.normal(0.0, 0.01, size=book.returns.size), index=book.returns.index)
    independent = lab.livebook.correlate(noise, book=book)
    assert abs(independent["correlation"]) < 0.4
    assert independent["overlap_days"] == book.returns.size


def test_an_empty_book_is_not_a_zero_correlation(lab: SimpleNamespace) -> None:
    """With nothing traded, the answer is "unknown", never 0.0."""
    book = lab.livebook.daily_returns()
    assert book.usable is False
    assert book.reason is not None and book.reason.startswith(NO_BOOK)

    series = pd.Series(
        np.linspace(0.001, 0.002, 40),
        index=pd.bdate_range(end=pd.Timestamp(lab.end), periods=40),
    )
    result = lab.livebook.correlate(series, book=book)
    assert result["correlation"] is None
    assert result["reason"].startswith(NO_BOOK)
    assert result["overlap_days"] == 0

    # An account that exists but has never filled anything is equally honest.
    lab.portfolio.ensure_account("paper", starting_cash=50_000.0)
    empty = lab.livebook.daily_returns()
    assert empty.usable is False
    assert "has no fills" in empty.reason


def test_correlation_needs_a_meaningful_overlap(lab: SimpleNamespace) -> None:
    """Fewer than twenty shared sessions is not enough to report a number."""
    _seed_book(lab.portfolio, lab.paths)
    book = lab.livebook.daily_returns()
    short = book.returns.tail(DEFAULT_MIN_OVERLAP - 1)

    result = lab.livebook.correlate(short, book=book)
    assert result["correlation"] is None
    assert result["overlap_days"] == DEFAULT_MIN_OVERLAP - 1
    assert result["reason"].startswith(NO_BOOK)
    assert f"{DEFAULT_MIN_OVERLAP} needed" in result["reason"]

    # One more session, and the number is reported.
    enough = lab.livebook.correlate(book.returns.tail(DEFAULT_MIN_OVERLAP), book=book)
    assert enough["correlation"] == pytest.approx(1.0)


def test_an_unmarkable_position_refuses_rather_than_marking_half_the_book(
    lab: SimpleNamespace,
) -> None:
    """A name with no stored prices makes the whole book unmeasurable, and says so."""
    _seed_book(lab.portfolio, lab.paths)
    order = Order(
        id="ord-ghost",
        symbol="GHOST",
        side=OrderSide.BUY,
        qty=10.0,
        status=OrderStatus.FILLED,
        filled_qty=10.0,
        submitted_at=datetime.now(UTC) - timedelta(days=5),
        account_id="paper",
    )
    lab.portfolio.save_order(order)
    lab.portfolio.save_fill(
        "paper",
        Fill(
            id="fill-ghost",
            order_id="ord-ghost",
            symbol="GHOST",
            side=OrderSide.BUY,
            qty=10.0,
            price=42.0,
            ts=order.submitted_at,
        ),
    )
    book = lab.livebook.daily_returns()
    assert book.usable is False
    assert "GHOST" in book.reason


def test_returns_from_curve_refuses_a_thinned_curve() -> None:
    """A sampled equity curve has no daily returns, and none are invented."""
    dates = [f"2026-01-{day:02d}T00:00:00+00:00" for day in range(1, 6)]
    series, reason = returns_from_curve(
        {"dates": dates, "equity": [100.0, 101.0, 102.0, 101.5, 103.0], "sampled": False}
    )
    assert reason is None
    assert series.size == 4
    assert series.iloc[0] == pytest.approx(0.01)

    thinned, why = returns_from_curve(
        {"dates": dates, "equity": [100.0, 101.0, 102.0, 101.5, 103.0], "sampled": True}
    )
    assert thinned.empty
    assert "thinned" in why

    empty, missing = returns_from_curve(None)
    assert empty.empty
    assert "no equity curve" in missing


# ---------------------------------------------------------------------------
# the scorecard joins
# ---------------------------------------------------------------------------
async def test_scorecard_joins_the_recorded_analyses(lab: SimpleNamespace) -> None:
    """Sweep / MC / WFA cells come from analyses that were really run."""
    hypothesis = lab.hypotheses.create(
        "Momentum", "12-1 momentum works", "the IC turns negative"
    )
    experiment = lab.experiments.create("evidence", _config(), hypothesis_id=hypothesis.id)
    run = await lab.experiments.run(experiment.id)
    backtest_id = run.backtest_id
    config = lab.backtests.get(backtest_id).config

    before = lab.evaluation.scorecard()
    joined = {row["key"]: row for row in before["rows"]}
    for key in ("sweep_stability", "mc_prob_sharpe_above_one", "wfa_positive_folds"):
        assert joined[key]["values"][0] is None
        assert joined[key]["notes"][0] == NO_ANALYSIS

    await lab.analyses.run_sweep(
        config,
        SweepSpec(axes=[ParameterAxis(path="signal.lookback", values=[63, 126, 189])]),
        backtest_id=backtest_id,
    )
    await lab.analyses.run_monte_carlo(config, paths=200, shown=10, backtest_id=backtest_id)
    await lab.analyses.run_walk_forward(
        config, WalkForwardSpec(folds=3, train_fraction=0.6), backtest_id=backtest_id
    )

    scorecard = lab.evaluation.scorecard()
    rows = {row["key"]: row for row in scorecard["rows"]}
    assert [row["label"] for row in scorecard["rows"]][-6:] == [
        "Sweep stability",
        "MC: P(Sharpe>1)",
        "MC: p95 max DD",
        "WFA positive folds",
        "Corr. to live book",
        "Capacity est.",
    ]

    sweep_summary = lab.analyses.list_analyses(
        kind=AnalysisKind.SWEEP, backtest_id=backtest_id
    )[0].summary
    stability = rows["sweep_stability"]
    assert stability["values"][0] == pytest.approx(
        sweep_summary["median_metric"] / sweep_summary["best_metric"]
    )
    assert stability["displays"][0] in {"IN REGION ✓", "PEAK ONLY"}
    assert "cell(s) keeps" in stability["notes"][0]

    mc_summary = lab.analyses.list_analyses(
        kind=AnalysisKind.MONTE_CARLO, backtest_id=backtest_id
    )[0].summary
    assert rows["mc_prob_sharpe_above_one"]["values"][0] == pytest.approx(
        mc_summary["prob_above_one"]
    )
    assert rows["mc_p95_max_drawdown"]["values"][0] == pytest.approx(
        mc_summary["p95_max_drawdown"]
    )
    assert "200 block-bootstrap resamples" in rows["mc_prob_sharpe_above_one"]["notes"][0]

    wfa_summary = lab.analyses.list_analyses(
        kind=AnalysisKind.WALK_FORWARD, backtest_id=backtest_id
    )[0].summary
    folds = rows["wfa_positive_folds"]
    assert folds["displays"][0] == (
        f"{int(wfa_summary['positive_folds'])} / {int(wfa_summary['scored'])}"
    )
    assert folds["values"][0] == pytest.approx(
        wfa_summary["positive_folds"] / wfa_summary["scored"]
    )

    # The capacity cell is a real figure, with its assumption attached.
    capacity = rows["capacity_usd"]
    assert capacity["format"] == "usd"
    assert capacity["values"][0] is not None
    assert math.isfinite(capacity["values"][0])
    assert "ADV participation bound" in capacity["notes"][0]

    # And nothing has been traded, so the correlation is empty *with a reason*.
    correlation = rows["live_book_correlation"]
    assert correlation["values"][0] is None
    assert correlation["notes"][0].startswith(NO_BOOK)
    assert scorecard["live_book"]["reason"].startswith(NO_BOOK)


async def test_scorecard_correlates_against_a_real_book(lab: SimpleNamespace) -> None:
    """With a traded book, the correlation row carries a measured number."""
    _seed_book(lab.portfolio, lab.paths, days_ago=400)
    hypothesis = lab.hypotheses.create("Momentum", "momentum works", "the IC turns negative")
    experiment = lab.experiments.create("correlated", _config(), hypothesis_id=hypothesis.id)
    await lab.experiments.run(experiment.id)

    scorecard = lab.evaluation.scorecard()
    row = {item["key"]: item for item in scorecard["rows"]}["live_book_correlation"]
    assert row["values"][0] is not None, row["notes"][0]
    assert -1.0 <= row["values"][0] <= 1.0
    assert "overlapping session" in row["notes"][0]
    assert scorecard["live_book"]["reason"] is None
    assert scorecard["live_book"]["days"] > DEFAULT_MIN_OVERLAP


async def test_scorecard_survives_a_candidate_with_no_evidence(lab: SimpleNamespace) -> None:
    """Every empty cell states a reason; none of them is a zero."""
    hypothesis = lab.hypotheses.create("Momentum", "momentum works", "the IC turns negative")
    experiment = lab.experiments.create("bare", _config(), hypothesis_id=hypothesis.id)
    await lab.experiments.run(experiment.id)

    scorecard = lab.evaluation.scorecard()
    for row in scorecard["rows"]:
        for index, value in enumerate(row["values"]):
            if value is None:
                assert row["notes"][index], f"{row['label']} is empty without a reason"
