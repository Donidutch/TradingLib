"""Execution layer: portfolio accounting, paper broker and live-broker adapters.

Safety rule from ``docs/SPEC.md`` decision D5: agents, development code and tests
never place live orders. Brokers default to paper/sandbox endpoints and a live
adapter must be constructed with an explicit, deliberate opt-in.

Layout:

* :mod:`tradinglib.trade.portfolio` - the shared vocabulary (:class:`Order`,
  :class:`Fill`, :class:`Position`, :class:`Portfolio`), the average-cost P&L
  arithmetic, the SQLite :class:`PortfolioStore` and the :class:`Broker`
  interface every adapter implements;
* :mod:`tradinglib.trade.paper` - :class:`PaperBroker`, simulating fills against
  live quotes with configurable slippage and full cash accounting;
* :mod:`tradinglib.trade.alpaca_broker` - :class:`AlpacaBroker` over ``alpaca-py``,
  paper endpoints by default;
* :mod:`tradinglib.trade.ibkr_broker` - :class:`IBKRBroker` over ``ib_async``,
  paper TWS/Gateway ports by default.

The three adapters are interchangeable: they all satisfy :class:`Broker`, so
``/api/trade`` (and any strategy code) can switch venue without changing shape.
"""

from __future__ import annotations

from tradinglib.trade.alpaca_broker import (
    LIVE_OPT_IN_ENV,
    AlpacaBroker,
    live_trading_opted_in,
)
from tradinglib.trade.ibkr_broker import LIVE_PORTS, PAPER_PORTS, IBKRBroker
from tradinglib.trade.paper import PaperBroker
from tradinglib.trade.portfolio import (
    Account,
    Broker,
    Fill,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Portfolio,
    PortfolioStore,
    Position,
    TimeInForce,
)

__all__: list[str] = [
    "LIVE_OPT_IN_ENV",
    "LIVE_PORTS",
    "PAPER_PORTS",
    "Account",
    "AlpacaBroker",
    "Broker",
    "Fill",
    "IBKRBroker",
    "Order",
    "OrderSide",
    "OrderStatus",
    "OrderType",
    "PaperBroker",
    "Portfolio",
    "PortfolioStore",
    "Position",
    "TimeInForce",
    "live_trading_opted_in",
]
