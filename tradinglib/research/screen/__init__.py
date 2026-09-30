"""Screener: rule engine and funnel evaluation.

* :mod:`tradinglib.research.screen.rules` - the metric catalogue (:data:`METRICS`),
  the :class:`Rule`/:class:`Screen` models, the ``ast``-validated expression
  engine and the :class:`MetricBuilder` that turns a universe into a metric
  panel from the Parquet store and the data providers.
* :mod:`tradinglib.research.screen.funnel` - staged evaluation with per-rule
  survivor counts, the result-profile statistics, persisted screens/runs and
  the hand-off that queues a real backtest in the Strategy Lab.

Nothing is re-exported here: both modules pull in pandas and SQLAlchemy, and a
caller that wants only the metric catalogue should not pay for the ORM.
"""

from __future__ import annotations

__all__: list[str] = []
