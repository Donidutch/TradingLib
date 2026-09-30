"""Tests for the intel engines added in the fix round.

Four small engines feed panels that previously rendered em-dashes:

* :mod:`tradinglib.data.macro` - FRED levels behind the Overview's *FRED macro* tab;
* :func:`tradinglib.data.universe.moving_average_breadth` - the constituent breadth
  behind "S&P above 200D";
* :mod:`tradinglib.research.memory` - the *Research memory* narrative rows;
* :mod:`tradinglib.agents.inbox` - the five-kind attention queue.

Every test here is deterministic and offline: the vendors are replaced with
stubs, so what is being asserted is the *derivation*, never the vendor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pandas as pd
import pytest

from tradinglib.agents.inbox import INBOX_TYPES, InboxComposer, InboxSources
from tradinglib.data.base import Series
from tradinglib.data.macro import MacroService, MacroSeriesSpec, _transform, _value_near
from tradinglib.data.universe import MIN_BREADTH_COVERAGE, moving_average_breadth
from tradinglib.research.company import interest_rank_composite, interest_year_over_year


def _utc(year: int, month: int, day: int) -> datetime:
    """A UTC midnight timestamp."""
    return datetime(year, month, day, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# market breadth
# ---------------------------------------------------------------------------
class _FakeStore:
    """A store stub whose ``query`` returns a fixed bars frame."""

    def __init__(self, frame: pd.DataFrame) -> None:
        self.frame = frame
        self.sql: str | None = None

    def query(self, sql: str, **params: Any) -> pd.DataFrame:
        """Record the SQL and return the canned frame."""
        self.sql = sql
        return self.frame


def _bars(symbol: str, closes: list[float]) -> pd.DataFrame:
    """Build a bars frame for one symbol with consecutive daily timestamps."""
    start = _utc(2026, 1, 1)
    return pd.DataFrame(
        {
            "symbol": [symbol] * len(closes),
            "ts": [pd.Timestamp(start + timedelta(days=i)) for i in range(len(closes))],
            "close": closes,
        }
    )


def test_breadth_counts_only_symbols_with_a_full_window() -> None:
    """A symbol without ``window`` closes is excluded, not assumed to be above."""
    window = 5
    frames = [
        # Two clear uptrends: the last close is above the 5-bar mean.
        _bars("AAA", [1.0, 2.0, 3.0, 4.0, 9.0]),
        _bars("BBB", [1.0, 2.0, 3.0, 4.0, 8.0]),
        # One downtrend.
        _bars("CCC", [9.0, 8.0, 7.0, 6.0, 1.0]),
        # Too short to judge.
        _bars("DDD", [5.0, 5.0]),
    ]
    store = _FakeStore(pd.concat(frames, ignore_index=True))
    out = moving_average_breadth(
        store, ["AAA", "BBB", "CCC", "DDD", "EEE"], window=window
    )
    assert out["requested"] == 5
    assert out["covered"] == 3
    assert out["above"] == 2
    assert out["below"] == 1
    assert out["uncovered"] == ["DDD", "EEE"]
    # Coverage is below the publication floor, so no percentage is quoted.
    assert MIN_BREADTH_COVERAGE > 3
    assert out["percent"] is None
    assert "at least" in out["reason"]


def test_breadth_publishes_a_percentage_once_coverage_is_enough() -> None:
    """With enough covered names the share above the average is reported."""
    window = 3
    frames = []
    for index in range(MIN_BREADTH_COVERAGE + 4):
        rising = index % 2 == 0
        closes = [1.0, 2.0, 9.0] if rising else [9.0, 8.0, 1.0]
        frames.append(_bars(f"S{index:02d}", closes))
    store = _FakeStore(pd.concat(frames, ignore_index=True))
    symbols = [f"S{index:02d}" for index in range(MIN_BREADTH_COVERAGE + 4)]
    out = moving_average_breadth(store, symbols, window=window)
    assert out["covered"] == len(symbols)
    assert out["above"] + out["below"] == len(symbols)
    assert out["percent"] == pytest.approx(out["above"] / len(symbols))
    assert out["reason"] is None
    assert out["as_of"] is not None


def test_breadth_rejects_a_symbol_that_is_not_a_ticker() -> None:
    """Anything that is not ticker-shaped never reaches the SQL statement."""
    store = _FakeStore(pd.DataFrame(columns=["symbol", "ts", "close"]))
    out = moving_average_breadth(store, ["'; DROP TABLE bars; --"], window=3)
    assert out["requested"] == 0
    assert out["percent"] is None
    assert store.sql is None


# ---------------------------------------------------------------------------
# macro series
# ---------------------------------------------------------------------------
class _StubFred:
    """A FRED provider stub returning a fixed monthly series."""

    def __init__(self, points: list[tuple[datetime, float]]) -> None:
        self.points = points
        self.calls: list[str] = []
        self.closed = False

    async def get_series(
        self, series_id: str, *, start: date | None = None, end: date | None = None
    ) -> Series:
        """Return the canned series, recording the request."""
        self.calls.append(series_id)
        return Series(name=series_id, points=list(self.points), source="fred-stub")

    async def close(self) -> None:
        """Record that the provider was closed."""
        self.closed = True


class _MemoryStore:
    """A store stub that keeps written series in memory."""

    def __init__(self) -> None:
        self.written: dict[str, Series] = {}

    def read_series(
        self, namespace: str, name: str, *, start: date | None = None, end: date | None = None
    ) -> Series:
        """Return the stored series, or an empty one."""
        return self.written.get(name, Series(name=name, points=[]))

    def write_series(self, namespace: str, series: Series) -> int:
        """Record the series as written."""
        self.written[series.name] = series
        return len(series.points)


def _monthly(values: list[float], *, end: datetime) -> list[tuple[datetime, float]]:
    """Build a monthly series ending at ``end`` (oldest first)."""
    out: list[tuple[datetime, float]] = []
    for offset, value in enumerate(reversed(values)):
        out.append((end - timedelta(days=30 * offset), value))
    return sorted(out)


def test_yoy_transform_needs_a_year_of_history() -> None:
    """The YoY transform emits nothing until a 12-month-old base exists."""
    end = _utc(2026, 8, 1)
    short = _monthly([100.0, 101.0, 102.0], end=end)
    assert _transform(short, "yoy") == []
    assert _transform(short, "level") == short


def test_yoy_transform_is_a_percentage_change_on_the_year_ago_value() -> None:
    """A 5% rise over twelve months reads as +5.0."""
    end = _utc(2026, 8, 1)
    values = [100.0] * 12 + [105.0]
    points = _monthly(values, end=end)
    out = _transform(points, "yoy")
    assert out, "a 13-point monthly series must produce at least one YoY reading"
    assert out[-1][1] == pytest.approx(5.0, abs=1e-6)


def test_value_near_respects_its_tolerance() -> None:
    """An observation outside the tolerance window is not used as a base."""
    points = [(_utc(2025, 1, 1), 1.0), (_utc(2026, 1, 1), 2.0)]
    assert _value_near(points, _utc(2026, 1, 3)) == 2.0
    assert _value_near(points, _utc(2025, 7, 1), tolerance=timedelta(days=10)) is None


async def test_macro_reading_refreshes_from_the_vendor_and_persists_it() -> None:
    """An empty store is filled from FRED, and the level plus deltas are derived."""
    end = _utc(2026, 8, 1)
    provider = _StubFred(_monthly([4.0, 4.1, 4.2, 4.3], end=end))
    store = _MemoryStore()
    service = MacroService(store, provider_factory=lambda: provider)
    spec = MacroSeriesSpec(
        series_id="UNRATE",
        label="UNEMPLOYMENT",
        refresh_after=timedelta(days=1),
        stale_after=timedelta(days=36500),
    )
    reading = await service.reading(spec)
    assert provider.calls == ["UNRATE"]
    assert provider.closed is True
    assert reading.value == pytest.approx(4.3)
    assert reading.previous == pytest.approx(4.2)
    assert reading.change == pytest.approx(0.1)
    assert reading.origin == "fred"
    assert reading.stale is False
    assert reading.reason is None
    # What was fetched is written back so the next read is local.
    assert "UNRATE" in store.written


async def test_macro_reading_says_why_it_has_no_value() -> None:
    """A vendor that returns nothing produces a null reading with the reason."""
    provider = _StubFred([])
    service = MacroService(_MemoryStore(), provider_factory=lambda: provider)
    spec = MacroSeriesSpec(series_id="NOPE", label="NOPE")
    reading = await service.reading(spec)
    assert reading.value is None
    assert reading.stale is True
    assert "NOPE" in (reading.reason or "")


# ---------------------------------------------------------------------------
# alternative-data derivations
# ---------------------------------------------------------------------------
def test_interest_rank_composite_reports_a_rank_move() -> None:
    """A series that has fallen back gives a negative percentile-rank move."""
    end = _utc(2026, 8, 1)
    # Interest peaked more than 90 days ago and has decayed since, so the newest
    # observation sits low in its own five-year distribution.
    points = [(end - timedelta(weeks=i), float(20 + i * 2)) for i in range(40)]
    out = interest_rank_composite(sorted(points), keyword="NVIDIA", geo="US")
    assert out["source"] == "trends"
    assert out["rank"] is not None
    assert out["rank_90d_ago"] is not None
    assert out["value"] == pytest.approx(out["rank"] - out["rank_90d_ago"])
    assert out["value"] < 0
    assert "search-interest rank" in out["basis"]


def test_interest_rank_composite_refuses_a_short_series() -> None:
    """Too few observations produce a null with the reason, not a number."""
    out = interest_rank_composite(
        [(_utc(2026, 8, 1), 50.0)], keyword="NVIDIA", geo="US"
    )
    assert out["value"] is None
    assert out["source"] is None
    assert "observation" in out["reason"]


def test_interest_year_over_year_compares_matching_windows() -> None:
    """Twelve weeks at 110 against twelve weeks at 100 a year ago is +10%."""
    end = _utc(2026, 8, 1)
    points: list[tuple[datetime, float]] = []
    for week in range(60):
        moment = end - timedelta(weeks=week)
        points.append((moment, 110.0 if week < 12 else 100.0))
    out = interest_year_over_year(sorted(points), keyword="NVIDIA", geo="US")
    assert out["value"] == pytest.approx(0.10, abs=1e-6)
    assert out["weeks"] == 12
    assert "proxy for site traffic" in out["basis"]


def test_interest_year_over_year_needs_two_years_of_weeks() -> None:
    """Without a year-ago window the tile stays null and says why."""
    end = _utc(2026, 8, 1)
    points = [(end - timedelta(weeks=i), 100.0) for i in range(10)]
    out = interest_year_over_year(sorted(points), keyword="NVIDIA", geo="US")
    assert out["value"] is None
    assert "year-over-year" in out["reason"]


# ---------------------------------------------------------------------------
# the attention queue
# ---------------------------------------------------------------------------
@dataclass
class _StubApproval:
    """Minimal stand-in for an approval request."""

    id: str
    run_id: str
    stage_id: str = "approval"
    agent_id: str = "human-review"
    title: str = "Human review"
    summary: str = "decide"
    requested_at: datetime = field(default_factory=lambda: _utc(2026, 8, 1))
    context: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Return the fields the composer reads back."""
        return {"requested_at": self.requested_at.isoformat(), "age_seconds": 10.0}


class _StubGate:
    """An approval gate stub with one pending request."""

    def __init__(self, items: list[_StubApproval]) -> None:
        self.items = items

    def queue(self, *, limit: int = 50) -> list[_StubApproval]:
        """Return the pending queue."""
        return self.items[:limit]


@dataclass
class _StubGateResult:
    """A validation gate result."""

    name: str
    hard: bool = False
    passed: bool = False


@dataclass
class _StubReport:
    """A validation report stub."""

    id: str
    backtest_id: str
    hypothesis_id: str | None = None
    evaluated_at: datetime = field(default_factory=lambda: _utc(2026, 8, 1))
    promoted: bool = False
    score: float = 50.0
    verdict: str = "fail"
    gates: list[_StubGateResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """Whether every gate passed."""
        return bool(self.gates) and all(gate.passed for gate in self.gates)

    @property
    def failed_gates(self) -> list[_StubGateResult]:
        """The gates that did not pass."""
        return [gate for gate in self.gates if not gate.passed]


class _StubValidation:
    """A validation engine stub."""

    def __init__(self, reports: list[_StubReport]) -> None:
        self._reports = reports

    def reports(self, *, limit: int = 100) -> list[_StubReport]:
        """Return the recorded reports."""
        return self._reports[:limit]


@dataclass
class _StubIncident:
    """A data-quality incident stub."""

    id: str
    dataset: str
    check: str
    severity: str
    message: str
    opened_at: datetime = field(default_factory=lambda: _utc(2026, 8, 1))


class _StubQuality:
    """A quality engine stub."""

    def __init__(self, incidents: list[_StubIncident]) -> None:
        self._incidents = incidents

    def incidents(self, *, limit: int = 100) -> list[_StubIncident]:
        """Return the open incidents."""
        return self._incidents[:limit]


class _StubFindings:
    """A findings store stub that already returns inbox-shaped rows."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def inbox(self, *, limit: int = 25) -> list[dict[str, Any]]:
        """Return the unacknowledged findings feed."""
        return self.rows[:limit]


def test_inbox_merges_every_kind_and_orders_by_urgency() -> None:
    """All five reference kinds are produced, approvals first."""
    sources = InboxSources(
        approvals=_StubGate([_StubApproval(id="APV-1", run_id="RUN-1")]),
        validation=_StubValidation(
            [
                _StubReport(
                    id="val-1",
                    backtest_id="bt-1",
                    gates=[_StubGateResult("OOS Sharpe", hard=True, passed=False)],
                )
            ]
        ),
        quality=_StubQuality(
            [
                _StubIncident(
                    id="INC-1",
                    dataset="bars:AAPL:1d",
                    check="outliers",
                    severity="error",
                    message="3 outliers",
                )
            ]
        ),
        findings=_StubFindings(
            [
                {
                    "id": "FND-1",
                    "title": "A finding",
                    "reason": "because",
                    "urgency": "Normal",
                    "status": "new",
                    "symbol": "AAPL",
                    "route": "findings",
                    "context": {"findingId": "FND-1"},
                    "age_seconds": 60.0,
                }
            ]
        ),
    )
    payload = InboxComposer(sources).build(limit=20)
    kinds = [item["type"] for item in payload["items"]]
    assert kinds[0] == "Approval", "a blocked run outranks everything else"
    assert set(kinds) == {"Approval", "Validation", "Data", "Agent"}
    assert payload["types"] == list(INBOX_TYPES)
    assert payload["counts"]["Finding"] == 1
    # The experiment source was not supplied, so it is reported as unavailable
    # rather than silently contributing nothing.
    assert payload["sources_available"]["Experiment"] is False
    assert payload["sources_available"]["Approval"] is True


def test_inbox_survives_one_broken_registry() -> None:
    """A registry that raises is marked unavailable; the rest of the queue stands."""

    class _Broken:
        def queue(self, *, limit: int = 50) -> list[Any]:
            raise RuntimeError("the gate is down")

    sources = InboxSources(
        approvals=_Broken(),
        quality=_StubQuality(
            [
                _StubIncident(
                    id="INC-2",
                    dataset="bars:MSFT:1d",
                    check="staleness",
                    severity="warning",
                    message="2 days behind",
                )
            ]
        ),
    )
    payload = InboxComposer(sources).build()
    assert payload["sources_available"]["Approval"] is False
    assert payload["counts"]["Data"] == 1
    assert payload["items"][0]["type"] == "Data"


# ---------------------------------------------------------------------------
# research memory
# ---------------------------------------------------------------------------
@dataclass
class _StubLink:
    """One evidence link on a hypothesis."""

    kind: str
    ref: str
    supports: bool = True


@dataclass
class _StubHypothesis:
    """A hypothesis stub carrying only what the composer reads."""

    id: str
    title: str
    statement: str
    falsifier: str
    status: Any
    tags: list[str] = field(default_factory=list)
    rationale: str | None = None
    owner: str | None = None
    origin: str | None = None
    evidence: list[_StubLink] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: _utc(2026, 8, 1))
    updated_at: datetime = field(default_factory=lambda: _utc(2026, 8, 1))


class _StubHypotheses:
    """A hypothesis registry stub."""

    def __init__(self, items: list[_StubHypothesis]) -> None:
        self.items = items

    def list(self, *, include_evidence: bool = True) -> list[_StubHypothesis]:
        """Return every hypothesis."""
        return list(self.items)


class _StubFindingStore:
    """A findings store stub for the memory composer."""

    def __init__(self, items: list[Any]) -> None:
        self.items = items

    def list(self, *, symbol: str | None = None, limit: int = 0) -> list[Any]:
        """Return the findings naming ``symbol``."""
        if symbol is None:
            return list(self.items)
        return [item for item in self.items if symbol in item.symbols]


class _StubKnowledge:
    """A knowledge base stub with no entries."""

    def search(self, query: str, *, limit: int = 25) -> list[Any]:
        """Return no hits."""
        return []


def test_memory_picks_the_strongest_standing_hypothesis_as_the_thesis() -> None:
    """A validated hypothesis outranks a draft, and its falsifier is a question."""
    from tradinglib.research.hypotheses import HypothesisStatus
    from tradinglib.research.memory import ResearchMemoryComposer

    validated = _StubHypothesis(
        id="H-9",
        title="NVDA compounds",
        statement="NVDA earns above its cost of capital through the cycle.",
        falsifier="ROIC falls below WACC for four consecutive quarters.",
        status=HypothesisStatus.VALIDATED,
        tags=["NVDA"],
        evidence=[_StubLink("backtest", "bt-1", True)],
    )
    draft = _StubHypothesis(
        id="H-10",
        title="NVDA multiple compresses",
        statement="NVDA de-rates as competition arrives.",
        falsifier="Forward multiple holds above 30x for a year.",
        status=HypothesisStatus.DRAFT,
        tags=["NVDA"],
    )
    composer = ResearchMemoryComposer(
        hypotheses=_StubHypotheses([draft, validated]),
        findings=_StubFindingStore([]),
        knowledge=_StubKnowledge(),
    )
    memory = composer.compose("NVDA")
    assert memory.thesis.kind == "hypothesis"
    assert memory.thesis.text == validated.statement
    assert "H-9" in (memory.thesis.detail or "")
    # The unsettled hypothesis becomes the open question, phrased as its falsifier.
    questions = [item.text for item in memory.questions]
    assert draft.falsifier in questions
    assert validated.falsifier not in questions
    assert memory.counts["hypotheses"] == 2


def test_memory_is_explicitly_empty_when_nothing_names_the_symbol() -> None:
    """No recorded research produces a stated absence, never invented prose."""
    from tradinglib.research.memory import ResearchMemoryComposer

    composer = ResearchMemoryComposer(
        hypotheses=_StubHypotheses([]),
        findings=_StubFindingStore([]),
        knowledge=_StubKnowledge(),
    )
    memory = composer.compose("ZZZZ")
    assert memory.thesis.kind == "empty"
    assert "ZZZZ" in memory.thesis.text
    assert memory.questions == []
    assert memory.counts == {
        "hypotheses": 0,
        "findings": 0,
        "knowledge": 0,
        "evidence": 0,
    }
