"""Research engines: company pages, screening, hypotheses, backtests, validation.

Everything here consumes :mod:`tradinglib.data` and never calls a vendor directly.

This module also carries the one contract shared between the screener and the
Strategy Lab: :class:`BacktestQueue`. The screener's "Backtest this screen"
action must create a *real* lab backtest record, but the screener must not
import the lab's engine (they are built independently and the lab owns its own
models). The lab registers its queue implementation once with
:func:`register_backtest_queue`; until it does,
:meth:`tradinglib.research.screen.funnel.FunnelEvaluator.queue_backtest` still
persists the request and reports honestly that no runner has picked it up yet -
it never pretends a backtest was started.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "BacktestQueue",
    "BacktestRequest",
    "get_backtest_queue",
    "register_backtest_queue",
]


@dataclass(slots=True)
class BacktestRequest:
    """A request to backtest a concrete set of symbols.

    The screener fills this in from a completed screen run; the Strategy Lab
    turns it into one of its own backtest records. Every field is data the
    requester genuinely has - nothing is defaulted to a made-up window.
    """

    #: Human-readable name for the backtest record ("screen: quality-mom-v2").
    name: str
    #: Symbols that passed the screen, in rank order.
    symbols: list[str]
    #: Universe the screen ran against.
    universe_id: str | None = None
    #: Screen that produced the request, when there was one.
    screen_id: str | None = None
    #: Screen run that produced the symbol list, when there was one.
    screen_run_id: str | None = None
    #: Backtest window; ``None`` means "the lab's configured default".
    start: date | None = None
    end: date | None = None
    benchmark: str = "SPY"
    rebalance: str = "monthly"
    initial_capital: float = 1_000_000.0
    #: Where the request came from ("screener", "agent", ...).
    origin: str = "screener"
    #: Free-form extras forwarded verbatim to the lab.
    params: dict[str, Any] = field(default_factory=dict)
    requested_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of the request."""
        return {
            "name": self.name,
            "symbols": list(self.symbols),
            "symbol_count": len(self.symbols),
            "universe_id": self.universe_id,
            "screen_id": self.screen_id,
            "screen_run_id": self.screen_run_id,
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "benchmark": self.benchmark,
            "rebalance": self.rebalance,
            "initial_capital": self.initial_capital,
            "origin": self.origin,
            "params": dict(self.params),
            "requested_at": self.requested_at.isoformat(),
        }


@runtime_checkable
class BacktestQueue(Protocol):
    """What the Strategy Lab must provide for "queue a backtest" to work.

    The returned mapping is passed through to the API response unchanged. It
    must at least carry ``id`` (the lab's backtest record id) and ``status``.
    """

    async def queue_backtest(self, request: BacktestRequest) -> dict[str, Any]:
        """Create a backtest record for ``request`` and return it."""
        ...


_backtest_queue: BacktestQueue | None = None


def register_backtest_queue(queue: BacktestQueue | None) -> None:
    """Register (or clear, with ``None``) the process-wide backtest queue."""
    global _backtest_queue
    _backtest_queue = queue


def get_backtest_queue() -> BacktestQueue | None:
    """Return the registered backtest queue, or ``None`` when the lab is absent."""
    return _backtest_queue
