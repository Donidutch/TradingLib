"""Screener tests: the rule engine on synthetic data and the funnel end to end.

The rule engine is pure, so it is tested against a hand-built metric frame with
known values. The funnel is tested against a stub metric builder so the staged
counts, the persisted runs, the turnover statistic and the backtest hand-off are
exercised without pricing a real universe.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd
import pytest

from tradinglib.config import Settings
from tradinglib.data.universe import UniverseRegistry
from tradinglib.db import init_db
from tradinglib.research import BacktestRequest, register_backtest_queue
from tradinglib.research.screen.funnel import FunnelEvaluator, ScreenRegistry
from tradinglib.research.screen.rules import (
    METRICS,
    MetricBuilder,
    MetricFrame,
    Operator,
    Rule,
    RuleEngine,
    RuleError,
    Scope,
    Screen,
    add_scores,
    frame_records,
)

UTC = timezone.utc


# ---------------------------------------------------------------------------
# synthetic metric frame
# ---------------------------------------------------------------------------
def synthetic_frame() -> pd.DataFrame:
    """Return a small metric panel with hand-chosen values.

    ``NOGRO`` deliberately has no revenue growth, so "a NaN never passes" can
    be asserted; sectors are unbalanced so sector-scoped percentiles differ
    from overall ones.
    """
    rows = [
        # symbol, sector, mkt_cap,  rev_growth, fcf_margin, gross_margin, roic, ndebt, mom_12_1
        ("AAA", "Tech", 900e9, 40.0, 30.0, 60.0, 35.0, -0.5, 50.0),
        ("BBB", "Tech", 300e9, 22.0, 18.0, 55.0, 28.0, 0.4, 30.0),
        ("CCC", "Tech", 12e9, 18.0, 9.0, 41.0, 14.0, 1.2, 12.0),
        ("DDD", "Tech", 1.2e9, 25.0, 12.0, 48.0, 19.0, 0.9, 41.0),
        ("EEE", "Health", 250e9, 16.0, 21.0, 70.0, 24.0, 1.8, 8.0),
        ("FFF", "Health", 40e9, 9.0, 6.0, 62.0, 11.0, 2.6, -4.0),
        ("GGG", "Fin", 80e9, 12.0, 4.0, 35.0, 9.0, 3.1, 22.0),
        ("NOGRO", "Fin", 20e9, np.nan, 15.0, 44.0, 16.0, 0.2, 5.0),
    ]
    frame = pd.DataFrame(
        rows,
        columns=[
            "symbol",
            "sector",
            "mkt_cap",
            "rev_growth",
            "fcf_margin",
            "gross_margin",
            "roic",
            "net_debt_ebitda",
            "mom_12_1",
        ],
    ).set_index("symbol")
    frame["symbol"] = frame.index
    frame["country"] = [
        "United States",
        "United States",
        "Singapore",
        "United States",
        "United States",
        "Germany",
        "United States",
        "United States",
    ]
    return add_scores(frame)


@pytest.fixture()
def frame() -> pd.DataFrame:
    """The synthetic metric panel."""
    return synthetic_frame()


@pytest.fixture()
def engine() -> RuleEngine:
    """A rule engine."""
    return RuleEngine()


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------
def test_unknown_metric_is_rejected(engine: RuleEngine) -> None:
    """A rule may only reference a metric the engine actually computes."""
    with pytest.raises(RuleError, match="unknown metric"):
        engine.validate(Rule(field="vibes", operator=Operator.GT, value=1))


def test_non_numeric_threshold_is_rejected(engine: RuleEngine) -> None:
    """A numeric comparison needs a number."""
    with pytest.raises(RuleError, match="numeric value"):
        engine.validate(Rule(field="mkt_cap", operator=Operator.GT, value="big"))


def test_percentile_bounds_are_enforced(engine: RuleEngine) -> None:
    """A percentile cut outside (0, 100] is meaningless."""
    with pytest.raises(RuleError, match=r"\(0, 100\]"):
        engine.validate(Rule(field="roic", operator=Operator.TOP_PCTL, value=0))
    engine.validate(Rule(field="roic", operator=Operator.TOP_PCTL, value=30))


def test_categorical_metrics_reject_ordering(engine: RuleEngine) -> None:
    """A sector cannot be greater than another sector."""
    with pytest.raises(RuleError, match="categorical"):
        engine.validate(Rule(field="sector", operator=Operator.GT, value="Tech"))
    with pytest.raises(RuleError, match="categorical"):
        engine.validate(Rule(field="sector", operator=Operator.TOP_PCTL, value=10))


def test_between_requires_an_ordered_range(engine: RuleEngine) -> None:
    """``between`` needs low <= high."""
    with pytest.raises(RuleError, match="low <= high"):
        engine.validate(
            Rule(field="mkt_cap", operator=Operator.BETWEEN, value=10, value2=1)
        )


def test_any_category_is_a_real_unconstrained_state() -> None:
    """The UI's "Any" choice filters nothing and is valid."""
    rule = Rule(field="country", operator=Operator.IN, value="Any")
    assert rule.unconstrained
    RuleEngine().validate(rule)


# ---------------------------------------------------------------------------
# row-wise evaluation
# ---------------------------------------------------------------------------
def test_threshold_filters_and_nan_never_passes(engine: RuleEngine, frame: pd.DataFrame) -> None:
    """A company that does not report the metric cannot satisfy the rule."""
    rule = Rule(field="rev_growth", operator=Operator.GT, value=15)
    survivors = engine.apply_rule(frame, rule)
    assert set(survivors.index) == {"AAA", "BBB", "CCC", "DDD", "EEE"}
    assert "NOGRO" not in survivors.index

    inverted = engine.apply_rule(frame, Rule(field="rev_growth", operator=Operator.LT, value=15))
    assert "NOGRO" not in inverted.index


def test_between_and_category_rules(engine: RuleEngine, frame: pd.DataFrame) -> None:
    """Range and membership rules select what they say they select."""
    ranged = engine.apply_rule(
        frame, Rule(field="mkt_cap", operator=Operator.BETWEEN, value=10e9, value2=100e9)
    )
    assert set(ranged.index) == {"CCC", "FFF", "GGG", "NOGRO"}

    us_only = engine.apply_rule(
        frame, Rule(field="country", operator=Operator.IN, value=["United States"])
    )
    assert "CCC" not in us_only.index and "FFF" not in us_only.index

    anything = engine.apply_rule(
        frame, Rule(field="country", operator=Operator.IN, value="Any")
    )
    assert len(anything) == len(frame)


def test_disabled_rules_are_skipped(engine: RuleEngine, frame: pd.DataFrame) -> None:
    """An unticked rule does not filter."""
    rule = Rule(field="mkt_cap", operator=Operator.GT, value=1e12, enabled=False)
    assert len(engine.apply_rule(frame, rule)) == len(frame)


def test_top_percentile_overall_keeps_the_ceiling(engine: RuleEngine, frame: pd.DataFrame) -> None:
    """``top_pctl`` keeps ceil(n * pct) of the rows it is given."""
    rule = Rule(field="roic", operator=Operator.TOP_PCTL, value=25)
    survivors = engine.apply_rule(frame, rule)
    # 8 rows, 25% -> ceil(2.0) = 2 names, the two highest ROIC.
    assert list(survivors.index) == ["AAA", "BBB"]


def test_top_percentile_within_sector(engine: RuleEngine, frame: pd.DataFrame) -> None:
    """Sector scope ranks inside each sector, so small sectors keep a name."""
    rule = Rule(field="roic", operator=Operator.TOP_PCTL, value=50, scope=Scope.SECTOR)
    survivors = engine.apply_rule(frame, rule)
    # Tech keeps 2 of 4, Health 1 of 2, Fin 1 of 2 (NOGRO out-earns GGG).
    assert set(survivors.index) == {"AAA", "BBB", "EEE", "NOGRO"}
    # Incoming order is preserved.
    assert list(survivors.index) == ["AAA", "BBB", "EEE", "NOGRO"]


def test_bottom_percentile_selects_the_low_end(engine: RuleEngine, frame: pd.DataFrame) -> None:
    """``bottom_pctl`` is the mirror image, for metrics where low is good."""
    rule = Rule(field="net_debt_ebitda", operator=Operator.BOTTOM_PCTL, value=25)
    survivors = engine.apply_rule(frame, rule)
    assert list(survivors.index) == ["AAA", "NOGRO"]


def test_percentile_rules_depend_on_funnel_position(
    engine: RuleEngine, frame: pd.DataFrame
) -> None:
    """The same percentile rule keeps different names after an earlier filter."""
    percentile_rule = Rule(field="roic", operator=Operator.TOP_PCTL, value=50)
    alone = engine.apply_rule(frame, percentile_rule)
    after_filter = engine.apply_rule(
        engine.apply_rule(frame, Rule(field="mkt_cap", operator=Operator.LT, value=100e9)),
        percentile_rule,
    )
    assert set(alone.index) != set(after_filter.index)


# ---------------------------------------------------------------------------
# expressions
# ---------------------------------------------------------------------------
def test_expression_evaluates_boolean_logic(engine: RuleEngine, frame: pd.DataFrame) -> None:
    """A compiled expression returns a mask over the frame."""
    compiled = engine.compile_expression("rev_growth > 15 and fcf_margin > 10")
    mask = compiled(frame)
    assert set(frame[mask].index) == {"AAA", "BBB", "DDD", "EEE"}


def test_expression_supports_arithmetic_and_membership(
    engine: RuleEngine, frame: pd.DataFrame
) -> None:
    """Arithmetic and ``in`` are allowed; both are pure."""
    mask = engine.compile_expression("gross_margin - fcf_margin > 40")(frame)
    assert "EEE" in frame[mask].index
    mask = engine.compile_expression("sector in ['Health', 'Fin']")(frame)
    assert set(frame[mask].index) == {"EEE", "FFF", "GGG", "NOGRO"}


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('ls')",
        "open('/etc/passwd').read()",
        "mkt_cap.__class__",
        "[x for x in range(3)]",
        "lambda: 1",
        "unknown_metric > 3",
        "rev_growth >",
    ],
)
def test_expression_rejects_anything_outside_the_whitelist(
    engine: RuleEngine, expression: str
) -> None:
    """Nothing but comparisons over known metrics may be evaluated."""
    with pytest.raises(RuleError):
        engine.compile_expression(expression)


def test_expression_reports_metrics_missing_from_the_frame(engine: RuleEngine) -> None:
    """A frame without the metric is an error, not a silent false."""
    compiled = engine.compile_expression("roe > 10")
    with pytest.raises(RuleError, match="absent from the frame"):
        compiled(pd.DataFrame({"symbol": ["AAA"]}).set_index("symbol"))


# ---------------------------------------------------------------------------
# screens
# ---------------------------------------------------------------------------
def build_screen(**changes: Any) -> Screen:
    """Return the reference screen: cap, growth, FCF margin, ROIC percentile."""
    payload: dict[str, Any] = {
        "id": "quality-momentum",
        "name": "quality-momentum",
        "universe_id": "test-universe",
        "rules": [
            {"field": "mkt_cap", "operator": ">", "value": 2e9},
            {"field": "rev_growth", "operator": ">", "value": 15},
            {"field": "fcf_margin", "operator": ">", "value": 5},
            {"field": "country", "operator": "in", "value": "Any"},
            {"field": "roic", "operator": "top_pctl", "value": 60, "scope": "sector"},
        ],
        "rank_by": "composite",
    }
    payload.update(changes)
    return Screen.from_dict(payload)


def test_evaluate_marks_cumulative_survival(engine: RuleEngine, frame: pd.DataFrame) -> None:
    """Rule columns are cumulative so they agree with the funnel counts."""
    screen = build_screen()
    evaluated = engine.evaluate(screen, frame)
    assert evaluated["rule_0"].sum() == 7  # DDD is below $2B
    assert evaluated["rule_1"].sum() == 4  # NOGRO/FFF/GGG then fail growth
    assert int(evaluated["passes"].sum()) == engine.count(screen, frame)
    # A row that failed rule 1 cannot be True in a later rule column.
    assert not bool(evaluated.loc["NOGRO", "rule_2"])


def test_survivors_are_ranked(engine: RuleEngine, frame: pd.DataFrame) -> None:
    """Results come back ordered by the screen's rank metric."""
    screen = build_screen(rank_by="mkt_cap")
    survivors = engine.survivors(screen, frame)
    caps = list(survivors["mkt_cap"])
    assert caps == sorted(caps, reverse=True)

    cheap_first = engine.survivors(build_screen(rank_by="net_debt_ebitda"), frame)
    leverage = list(cheap_first["net_debt_ebitda"])
    assert leverage == sorted(leverage)


def test_rank_by_must_be_rankable(engine: RuleEngine, frame: pd.DataFrame) -> None:
    """Ranking by a categorical metric is refused."""
    with pytest.raises(RuleError, match="cannot rank by"):
        engine.validate_screen(build_screen(rank_by="sector"))


def test_rule_describe_uses_metric_units() -> None:
    """The rule list renders values in the metric's own unit."""
    assert Rule(field="mkt_cap", operator=Operator.GT, value=2e9).describe() == (
        "Market cap > $2.0B"
    )
    assert Rule(field="rev_growth", operator=Operator.GT, value=15).describe() == (
        "Revenue growth > 15%"
    )
    assert Rule(
        field="roic", operator=Operator.TOP_PCTL, value=30, scope=Scope.SECTOR
    ).describe() == "ROIC top 30% within sector"


def test_query_rendering_matches_the_rules() -> None:
    """The "View as query" text is generated from the rules the engine runs."""
    from datetime import date

    query = build_screen().to_query(as_of=date(2026, 8, 28))
    lines = query.splitlines()
    assert lines[0] == 'screen(universe="test-universe", as_of="2026-08-28")'
    # The "Any" country rule constrains nothing, so it is not written out.
    assert not any("country" in line for line in lines)
    assert '  .filter(pctl(roic, by="sector") > 0.40)' in lines
    assert lines[-1] == '  .rank_by("composite")'


def test_rule_round_trips_through_its_payload() -> None:
    """A rule survives serialisation to the API shape and back."""
    rule = Rule(field="roic", operator=Operator.TOP_PCTL, value=30, scope=Scope.SECTOR)
    payload = rule.as_dict()
    assert payload["description"] == "ROIC top 30% within sector"
    assert Rule.from_dict(payload) == rule


# ---------------------------------------------------------------------------
# scores and record rendering
# ---------------------------------------------------------------------------
def test_scores_are_percentile_composites(frame: pd.DataFrame) -> None:
    """Quality and composite are bounded, ordered and require two inputs."""
    assert frame["quality"].between(0, 100).all()
    assert frame.loc["AAA", "quality"] > frame.loc["FFF", "quality"]
    assert frame.loc["AAA", "composite"] > frame.loc["GGG", "composite"]

    sparse = pd.DataFrame(
        {"symbol": ["X", "Y"], "gross_margin": [50.0, 30.0]}
    ).set_index("symbol")
    scored = add_scores(sparse)
    assert scored["quality"].isna().all()


def test_frame_records_render_nan_as_null(frame: pd.DataFrame) -> None:
    """JSON records carry ``None``, never NaN."""
    records = frame_records(frame, columns=("symbol", "rev_growth", "sector"))
    nogro = next(row for row in records if row["symbol"] == "NOGRO")
    assert nogro["rev_growth"] is None
    assert nogro["sector"] == "Fin"


# ---------------------------------------------------------------------------
# price metrics
# ---------------------------------------------------------------------------
def test_price_metrics_from_synthetic_bars() -> None:
    """Momentum, ADV and volatility are computed from the bars given."""
    sessions = 300
    index = pd.date_range("2025-01-01", periods=sessions, freq="D", tz=UTC)
    closes = pd.Series([100.0 * (1.001**day) for day in range(sessions)], index=index)
    volume = pd.Series([1_000.0] * sessions, index=index)
    metrics = MetricBuilder.price_metrics(pd.DataFrame({"close": closes, "volume": volume}))

    last = closes.iloc[-1]
    assert metrics["price"] == pytest.approx(last)
    assert metrics["mom_1m"] == pytest.approx((last / closes.iloc[-22] - 1) * 100)
    assert metrics["mom_12_1"] == pytest.approx(
        (closes.iloc[-22] / closes.iloc[-253] - 1) * 100
    )
    assert metrics["adv_shares"] == pytest.approx(1_000.0)
    assert metrics["adv_usd"] == pytest.approx(
        float((closes * volume).tail(63).mean())
    )
    # A perfectly smooth series has (almost) no volatility.
    assert metrics["vol_3m"] == pytest.approx(0.0, abs=1e-9)


def test_price_metrics_leave_uncovered_windows_null() -> None:
    """A short history yields NaN rather than a shorter-window substitute."""
    index = pd.date_range("2026-01-01", periods=30, freq="D", tz=UTC)
    frame = pd.DataFrame({"close": np.linspace(10, 20, 30), "volume": [500.0] * 30}, index=index)
    metrics = MetricBuilder.price_metrics(frame)
    assert not np.isnan(metrics["mom_1m"])
    assert np.isnan(metrics["mom_12m"])
    assert np.isnan(metrics["mom_12_1"])


# ---------------------------------------------------------------------------
# the funnel, end to end against a stub metric builder
# ---------------------------------------------------------------------------
class StubBuilder:
    """Metric builder stand-in returning a fixed panel (tests only)."""

    def __init__(self, frame: pd.DataFrame) -> None:
        """Store the panel this builder always returns."""
        self.frame = frame
        self.calls = 0

    async def build(
        self,
        symbols: Any,
        *,
        as_of: Any = None,
        sectors: Any = None,
        names: Any = None,
    ) -> MetricFrame:
        """Return the fixed panel, restricted to the requested symbols."""
        self.calls += 1
        wanted = [s for s in symbols if s in self.frame.index]
        return MetricFrame(
            frame=self.frame.loc[wanted],
            as_of=as_of,
            built_at=datetime.now(UTC),
            coverage={"requested": len(list(symbols)), "evaluated": len(wanted)},
            missing={},
        )


@pytest.fixture()
def universe(settings: Settings) -> UniverseRegistry:
    """A registry holding a universe with the synthetic frame's symbols."""
    init_db()
    registry = UniverseRegistry()
    registry.create(
        "Test Universe",
        [
            {"symbol": symbol, "sector": sector}
            for symbol, sector in synthetic_frame()["sector"].items()
        ],
        universe_id="test-universe",
    )
    registry.watchlists.create("Live book", ["AAA", "EEE", "ZZZ"], is_default=True)
    return registry


@pytest.fixture()
def evaluator(universe: UniverseRegistry, frame: pd.DataFrame) -> FunnelEvaluator:
    """A funnel evaluator over the stub builder and the temp database."""
    return FunnelEvaluator(
        registry=ScreenRegistry(),
        universes=universe,
        builder=StubBuilder(frame),  # type: ignore[arg-type]
    )


async def test_screen_crud_round_trip(evaluator: FunnelEvaluator) -> None:
    """Screens persist, list, update and delete."""
    registry = evaluator.registry
    screen = registry.create(
        "Quality momentum v2",
        "test-universe",
        rules=[{"field": "mkt_cap", "operator": ">", "value": 2e9}],
        description="cap floor",
    )
    assert screen.id == "quality-momentum-v2"
    assert registry.get(screen.id).name == "Quality momentum v2"
    assert [s.id for s in registry.list_screens()] == [screen.id]

    updated = registry.update(
        screen.id, rules=[{"field": "rev_growth", "operator": ">", "value": 20}]
    )
    assert updated.rules[0].field == "rev_growth"

    registry.delete(screen.id)
    with pytest.raises(KeyError):
        registry.get(screen.id)


async def test_invalid_rules_are_refused_before_they_are_stored(
    evaluator: FunnelEvaluator,
) -> None:
    """A screen with a bad rule never reaches the database."""
    with pytest.raises(RuleError):
        evaluator.registry.create(
            "broken", "test-universe", rules=[{"field": "nope", "operator": ">", "value": 1}]
        )
    assert evaluator.registry.list_screens() == []


async def test_run_produces_a_real_funnel_and_profile(evaluator: FunnelEvaluator) -> None:
    """Every stage count is the number of names that actually survived it."""
    screen = evaluator.registry.create(
        "quality-momentum",
        "test-universe",
        rules=build_screen().as_dict()["rules"],
    )
    run = await evaluator.run(screen)

    assert run.base_count == 8
    assert [stage.input_count for stage in run.stages][0] == 8
    for previous, stage in zip(run.stages, run.stages[1:], strict=False):
        assert stage.input_count == previous.output_count
    assert run.stages[-1].output_count == run.pass_count
    assert run.pass_count == len(run.survivors) > 0

    stats = run.stats
    assert stats["count"] == run.pass_count
    assert sum(bucket["count"] for bucket in stats["sector_mix"]) == run.pass_count
    assert stats["median_market_cap"] is not None
    assert stats["turnover"] is None  # first run: nothing to compare against
    # No trading account holds anything yet, so the watchlist stands in for the
    # book - and the payload says so instead of reporting a bare zero.
    overlap = stats["live_book_overlap"]
    assert overlap["source"] == "watchlist:live-book"
    assert overlap["kind"] == "watchlist"
    assert "no trading account" in overlap["reason"]
    assert "AAA" in overlap["symbols"]

    stored = evaluator.registry.get_run(run.id)
    assert stored["pass_count"] == run.pass_count
    assert stored["symbols"] == run.symbols


async def test_live_book_overlap_reads_the_real_trading_book(
    evaluator: FunnelEvaluator,
) -> None:
    """Once an account holds something, the overlap is measured against it."""
    from tradinglib.trade.portfolio import Portfolio, Position, PortfolioStore

    store = PortfolioStore(evaluator.registry.session_factory)
    store.ensure_account("paper-1", broker="paper", starting_cash=100_000.0)
    store.save_state(
        Portfolio(
            account_id="paper-1",
            cash=50_000.0,
            positions={
                "AAA": Position(symbol="AAA", qty=100.0, avg_price=10.0),
                "ZZZ": Position(symbol="ZZZ", qty=40.0, avg_price=25.0),
                # A flat row is history, not a holding: it must not count.
                "BBB": Position(symbol="BBB", qty=0.0, avg_price=0.0),
            },
        )
    )

    overlap = evaluator.live_book_overlap(["AAA", "BBB", "CCC"])
    assert overlap["source"] == "accounts:paper-1"
    assert overlap["kind"] == "paper"
    assert overlap["symbols"] == ["AAA"]
    assert overlap["count"] == 1
    assert overlap["book_size"] == 2  # AAA and ZZZ; the flat BBB is excluded
    assert overlap["share"] == pytest.approx(1 / 3)
    assert overlap["reason"] is None

    # A screen proposing only names already held is exactly what the panel is
    # there to reveal.
    assert evaluator.live_book_overlap(["AAA", "ZZZ"])["share"] == 1.0
    assert evaluator.live_book_overlap([])["count"] == 0


async def test_turnover_is_measured_against_the_previous_run(
    evaluator: FunnelEvaluator, frame: pd.DataFrame
) -> None:
    """The second run compares its survivors with the stored first run."""
    screen = evaluator.registry.create(
        "growth", "test-universe", rules=[{"field": "rev_growth", "operator": ">", "value": 20}]
    )
    first = await evaluator.run(screen)
    assert set(first.symbols) == {"AAA", "BBB", "DDD"}

    # The world moves: CCC accelerates, BBB slows.
    moved = frame.copy()
    moved.loc["CCC", "rev_growth"] = 33.0
    moved.loc["BBB", "rev_growth"] = 5.0
    evaluator._builder = StubBuilder(moved)  # type: ignore[assignment]
    second = await evaluator.run(screen, refresh=True)

    turnover = second.stats["turnover"]
    assert turnover is not None
    assert turnover["compared_with_run"] == first.id
    assert turnover["entered"] == ["CCC"]
    assert turnover["exited"] == ["BBB"]
    assert turnover["retained_count"] == 2


async def test_preview_count_matches_the_run(evaluator: FunnelEvaluator) -> None:
    """The live badge and the executed screen agree."""
    screen = build_screen()
    count = await evaluator.preview_count(screen)
    run = await evaluator.run(screen, persist=False)
    assert count == run.pass_count


async def test_queue_backtest_without_a_lab_runner_is_honest(
    evaluator: FunnelEvaluator,
) -> None:
    """With no lab registered the request is stored and says pending_runner."""
    register_backtest_queue(None)
    screen = evaluator.registry.create(
        "growth", "test-universe", rules=[{"field": "rev_growth", "operator": ">", "value": 20}]
    )
    with pytest.raises(ValueError, match="no stored run"):
        await evaluator.queue_backtest(screen.id)

    run = await evaluator.run(screen)
    record = await evaluator.queue_backtest(screen.id)
    assert record["status"] == "pending_runner"
    assert record["backtest_id"] is None
    assert "no Strategy Lab backtest runner" in record["detail"]
    assert record["request"]["symbols"] == run.symbols
    assert evaluator.registry.list_backtest_requests(screen.id)[0]["id"] == record["id"]


async def test_queue_backtest_hands_off_to_a_registered_lab(
    evaluator: FunnelEvaluator,
) -> None:
    """A registered queue receives the request and its id is recorded."""
    seen: list[BacktestRequest] = []

    class RecordingQueue:
        """Stand-in for the Strategy Lab's backtest queue."""

        async def queue_backtest(self, request: BacktestRequest) -> dict[str, Any]:
            """Record the request and return a lab-shaped record."""
            seen.append(request)
            return {"id": "bt-1001", "status": "queued", "detail": "accepted by the lab"}

    register_backtest_queue(RecordingQueue())
    try:
        screen = evaluator.registry.create(
            "growth",
            "test-universe",
            rules=[{"field": "rev_growth", "operator": ">", "value": 20}],
        )
        await evaluator.run(screen)
        record = await evaluator.queue_backtest(screen.id, benchmark="QQQ")
    finally:
        register_backtest_queue(None)

    assert record["status"] == "queued"
    assert record["backtest_id"] == "bt-1001"
    assert seen and seen[0].benchmark == "QQQ"
    assert seen[0].symbols == ["AAA", "BBB", "DDD"]
    assert seen[0].screen_id == screen.id


async def test_run_publishes_a_notification(
    evaluator: FunnelEvaluator, bus: Any, frame: pd.DataFrame
) -> None:
    """A completed run is announced on the notifications channel."""
    received: list[dict[str, Any]] = []
    bus.subscribe("notifications", lambda event: received.append(event.payload))
    evaluator._bus = bus
    await evaluator.run(build_screen(), persist=False)
    assert received and received[0]["event"] == "screen_run"
    assert received[0]["message"].startswith("Screen executed")


def test_every_result_column_is_a_known_metric() -> None:
    """The result table cannot ship a column the metric catalogue lacks."""
    from tradinglib.research.screen.funnel import RESULT_COLUMNS

    assert set(RESULT_COLUMNS) <= set(METRICS)


def test_metric_frame_reports_coverage() -> None:
    """The coverage report is JSON-safe and states what was evaluated."""
    panel = MetricFrame(
        frame=synthetic_frame(),
        as_of=None,
        built_at=datetime.now(UTC) - timedelta(seconds=1),
        coverage={"requested": 8, "evaluated": 8},
        missing={"ZZZ": "no data"},
    )
    payload = panel.as_dict()
    assert payload["rows"] == 8
    assert payload["missing"] == {"ZZZ": "no data"}
