"""ADV-participation capacity estimate for a candidate book.

The question the Evaluation scorecard's ``Capacity est.`` row answers is: how
much money could this book hold before its own trading would move the prices it
is measured on? The estimate here is deliberately the simplest defensible one -
an *ADV participation* bound:

    capacity = min over held names of (participation x ADV_usd / target weight)

Read it as: if the strategy is willing to be ``participation`` of a name's
average daily dollar volume, and that name is ``weight`` of the book, then the
book cannot be larger than ``participation x ADV / weight`` without breaking the
participation limit in that name. The *smallest* such number binds, because the
book has to fit all of its names at once.

What this is NOT: it is not a market-impact model. It says nothing about how
much the strategy would pay in slippage at that size, and it assumes the
strategy can spread a rebalance over a full session at a constant participation
rate. Both assumptions are stated on the returned estimate (``assumption``) and
are surfaced in the scorecard cell, because a capacity number without its
participation assumption is meaningless.

Every input is measured: ADV comes from the volume actually stored in the
time-series store, weights from the backtest's own trade ledger. When a held
name has no stored volume history the estimate is refused - a bound computed
over a subset of the book is not a bound, since the missing name could be the
binding one.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from tradinglib.data.base import BarInterval
from tradinglib.data.store import TimeSeriesStore, get_store

logger = logging.getLogger(__name__)

#: Share of a name's average daily dollar volume the book is willing to be.
DEFAULT_PARTICIPATION: float = 0.05

#: Sessions averaged into the ADV figure (about one calendar quarter).
DEFAULT_ADV_WINDOW: int = 63

#: Fewest sessions with a positive traded value before an ADV is trusted.
MIN_ADV_SESSIONS: int = 20


@dataclass(slots=True)
class NameCapacity:
    """The capacity one held name allows, and the ADV it was derived from."""

    symbol: str
    weight: float
    adv_usd: float | None
    sessions: int
    capacity_usd: float | None
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping for the API layer."""
        return {
            "symbol": self.symbol,
            "weight": self.weight,
            "adv_usd": self.adv_usd,
            "sessions": self.sessions,
            "capacity_usd": self.capacity_usd,
            "reason": self.reason,
        }


@dataclass(slots=True)
class CapacityEstimate:
    """The book-level capacity bound, or an honest refusal to state one."""

    capacity_usd: float | None
    participation: float
    window: int
    assumption: str
    binding_symbol: str | None = None
    names: list[NameCapacity] = field(default_factory=list)
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping for the API layer."""
        return {
            "capacity_usd": self.capacity_usd,
            "participation": self.participation,
            "window": self.window,
            "assumption": self.assumption,
            "binding_symbol": self.binding_symbol,
            "names": [name.as_dict() for name in self.names],
            "reason": self.reason,
        }


def assumption_text(participation: float, window: int) -> str:
    """Return the sentence every capacity figure must be read with."""
    return (
        f"ADV participation bound: the book trades at most {participation:.0%} of each "
        f"name's {window}-session average daily dollar volume, spread over a full "
        "session. No market-impact or slippage model is applied, so this is a size "
        "ceiling, not a cost estimate."
    )


def holdings_from_trade_log(trade_log: Mapping[str, Any] | None) -> dict[str, float]:
    """Return ``symbol -> target weight`` for the positions a run ends holding.

    The backtest's stored trade ledger marks a trade ``open`` when the book is
    still holding it on the final bar, and records the mean absolute weight the
    position was held at. Those open trades *are* the book at the end of the
    run, which is the book whose capacity the scorecard is asking about.
    """
    rows: Sequence[Mapping[str, Any]] = (trade_log or {}).get("rows") or []
    holdings: dict[str, float] = {}
    for row in rows:
        if not row.get("open"):
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        weight = abs(float(row.get("weight") or 0.0))
        if not symbol or weight <= 0.0:
            continue
        holdings[symbol] = holdings.get(symbol, 0.0) + weight
    return holdings


class CapacityEngine:
    """Estimates how large a book can be before it fights its own liquidity."""

    def __init__(self, *, store: TimeSeriesStore | None = None) -> None:
        """Create the engine.

        Args:
            store: time-series store the volume history is read from; defaults
                to the process-wide one.
        """
        self._store = store

    @property
    def store(self) -> TimeSeriesStore:
        """The time-series store ADV is measured from."""
        if self._store is None:
            self._store = get_store()
        return self._store

    def average_dollar_volume(
        self,
        symbol: str,
        *,
        window: int = DEFAULT_ADV_WINDOW,
        interval: BarInterval = BarInterval.DAY_1,
        asof: datetime | None = None,
    ) -> tuple[float | None, int]:
        """Return ``(ADV in USD, sessions used)`` for one symbol.

        ``None`` means the store does not hold enough volume history to state an
        ADV; the session count says how much it does hold.
        """
        frame = self.store.read_bars(symbol, interval=interval, end=asof)
        if frame.empty or "volume" not in frame.columns:
            return None, 0
        traded = (
            pd.to_numeric(frame["close"], errors="coerce")
            * pd.to_numeric(frame["volume"], errors="coerce")
        ).dropna()
        traded = traded[traded > 0.0]
        if traded.empty:
            return None, 0
        recent = traded.tail(max(1, window))
        if recent.size < MIN_ADV_SESSIONS:
            return None, int(recent.size)
        return float(np.mean(recent.to_numpy(dtype="float64"))), int(recent.size)

    def estimate(
        self,
        holdings: Mapping[str, float],
        *,
        participation: float = DEFAULT_PARTICIPATION,
        window: int = DEFAULT_ADV_WINDOW,
        interval: BarInterval = BarInterval.DAY_1,
        asof: datetime | None = None,
    ) -> CapacityEstimate:
        """Return the ADV-participation capacity of a weighted book.

        Args:
            holdings: ``symbol -> target weight`` (weights are absolute; they
                need not sum to one, and a gross-levered book legitimately sums
                above it).
            participation: share of each name's ADV the book may be.
            window: sessions averaged into each name's ADV.
            interval: bar interval the volume is read at.
            asof: measure the ADV as of this moment (defaults to the newest
                stored bar).

        Raises:
            ValueError: when ``participation`` is not in ``(0, 1]``.
        """
        if not 0.0 < participation <= 1.0:
            raise ValueError(f"participation must be in (0, 1]: {participation!r}")
        assumption = assumption_text(participation, window)
        cleaned = {
            str(symbol).strip().upper(): abs(float(weight))
            for symbol, weight in holdings.items()
            if str(symbol).strip() and abs(float(weight)) > 0.0
        }
        if not cleaned:
            return CapacityEstimate(
                capacity_usd=None,
                participation=participation,
                window=window,
                assumption=assumption,
                reason="the run holds no position at its final bar, so there is no book to size",
            )

        names: list[NameCapacity] = []
        for symbol in sorted(cleaned):
            weight = cleaned[symbol]
            adv, sessions = self.average_dollar_volume(
                symbol, window=window, interval=interval, asof=asof
            )
            if adv is None:
                names.append(
                    NameCapacity(
                        symbol=symbol,
                        weight=weight,
                        adv_usd=None,
                        sessions=sessions,
                        capacity_usd=None,
                        reason=(
                            f"{sessions} stored session(s) with volume; "
                            f"{MIN_ADV_SESSIONS} needed for an ADV"
                        ),
                    )
                )
                continue
            names.append(
                NameCapacity(
                    symbol=symbol,
                    weight=weight,
                    adv_usd=adv,
                    sessions=sessions,
                    capacity_usd=participation * adv / weight,
                )
            )

        missing = [name.symbol for name in names if name.capacity_usd is None]
        if missing:
            # A bound computed over part of the book is not a bound: the name
            # with no volume history could be the one that binds.
            return CapacityEstimate(
                capacity_usd=None,
                participation=participation,
                window=window,
                assumption=assumption,
                names=names,
                reason=(
                    "no usable volume history for "
                    + ", ".join(missing[:4])
                    + (f" (+{len(missing) - 4} more)" if len(missing) > 4 else "")
                    + " — a capacity bound over part of the book would not be a bound"
                ),
            )

        binding = min(names, key=lambda name: name.capacity_usd or float("inf"))
        return CapacityEstimate(
            capacity_usd=binding.capacity_usd,
            participation=participation,
            window=window,
            assumption=assumption,
            binding_symbol=binding.symbol,
            names=names,
        )


_engine: CapacityEngine | None = None


def get_capacity_engine() -> CapacityEngine:
    """Return the process-wide capacity engine, creating it on first use."""
    global _engine
    if _engine is None:
        _engine = CapacityEngine()
    return _engine


def reset_capacity_engine(engine: CapacityEngine | None = None) -> CapacityEngine:
    """Replace the process-wide capacity engine. Intended for tests."""
    global _engine
    _engine = engine if engine is not None else CapacityEngine()
    return _engine


__all__ = [
    "DEFAULT_ADV_WINDOW",
    "DEFAULT_PARTICIPATION",
    "MIN_ADV_SESSIONS",
    "CapacityEngine",
    "CapacityEstimate",
    "NameCapacity",
    "assumption_text",
    "get_capacity_engine",
    "holdings_from_trade_log",
    "reset_capacity_engine",
]
