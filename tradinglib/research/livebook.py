"""Correlation of a research candidate against the live (paper) book.

The Evaluation scorecard asks "how correlated is this candidate to what we are
already running?", because a candidate whose returns are a copy of the live book
adds risk without adding diversification, however good its own Sharpe is.

Answering it needs a *return series for the live book*, and TradingLib does not
store one - it stores what actually happened: fills and positions in
:mod:`tradinglib.trade.portfolio`. So the book's daily equity is reconstructed here
from those primitives:

* cash starts at the account's opening balance and moves with every fill
  (``-side x qty x price - fee``);
* the position in each name is the running signed quantity of its fills;
* each session's equity is that cash plus the marked value of those positions,
  using the daily closes already in the time-series store;
* the daily return series is the percentage change of that equity curve.

The module is strictly **read-only** with respect to :mod:`tradinglib.trade`: it
loads accounts, fills and positions and never writes, submits or mutates
anything.

The refusals matter as much as the number. A book with no fills, a book whose
names have no stored prices, or an overlap with the candidate shorter than
:data:`DEFAULT_MIN_OVERLAP` sessions all yield ``None`` with a reason - never
``0.0``, which would read as "uncorrelated" when the truth is "unknown".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from tradinglib.data.base import BarInterval
from tradinglib.data.store import TimeSeriesStore, get_store
from tradinglib.trade.portfolio import PortfolioStore

logger = logging.getLogger(__name__)

#: Overlapping sessions required before a correlation is reported at all.
DEFAULT_MIN_OVERLAP: int = 20

#: Fills loaded per account. Far above any paper book this app produces.
FILL_LIMIT: int = 100_000

#: Prefix every refusal carries, so the scorecard cell can render "— no live book".
NO_BOOK: str = "no live book"


def _utc_day(value: Any) -> pd.Timestamp:
    """Return ``value`` as a UTC-midnight timestamp (naive input is read as UTC)."""
    stamp = pd.Timestamp(value)
    stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
    return stamp.normalize()


def _utc_days(index: Any) -> pd.DatetimeIndex:
    """Return ``index`` as UTC-midnight timestamps (naive input is read as UTC)."""
    stamps = pd.DatetimeIndex(index)
    stamps = stamps.tz_localize("UTC") if stamps.tz is None else stamps.tz_convert("UTC")
    return stamps.normalize()


def _naive_days(index: Any) -> pd.DatetimeIndex:
    """Return ``index`` as tz-naive midnight timestamps, for joining two series."""
    stamps = pd.DatetimeIndex(index)
    if stamps.tz is not None:
        stamps = stamps.tz_convert("UTC").tz_localize(None)
    return stamps.normalize()


@dataclass(slots=True)
class LiveBook:
    """The reconstructed daily equity and returns of one traded account."""

    account_id: str | None
    equity: pd.Series = field(default_factory=lambda: pd.Series(dtype="float64"))
    returns: pd.Series = field(default_factory=lambda: pd.Series(dtype="float64"))
    symbols: list[str] = field(default_factory=list)
    fills: int = 0
    reason: str | None = None

    @property
    def usable(self) -> bool:
        """Whether a return series was actually reconstructed."""
        return self.reason is None and self.returns.size > 0

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable summary (without the series itself)."""
        return {
            "account_id": self.account_id,
            "days": int(self.returns.size),
            "symbols": list(self.symbols),
            "fills": self.fills,
            "start": self.returns.index[0].date().isoformat() if self.returns.size else None,
            "end": self.returns.index[-1].date().isoformat() if self.returns.size else None,
            "reason": self.reason,
        }


class LiveBookEngine:
    """Reconstructs the paper book's return series and correlates against it."""

    def __init__(
        self,
        *,
        store: TimeSeriesStore | None = None,
        portfolio: PortfolioStore | None = None,
    ) -> None:
        """Create the engine.

        Args:
            store: time-series store the daily marks are read from.
            portfolio: read-only handle on the trade store; defaults to a new
                :class:`~tradinglib.trade.portfolio.PortfolioStore` over the app DB.
        """
        self._store = store
        self._portfolio = portfolio

    @property
    def store(self) -> TimeSeriesStore:
        """The time-series store positions are marked with."""
        if self._store is None:
            self._store = get_store()
        return self._store

    @property
    def portfolio(self) -> PortfolioStore:
        """The trade store fills and accounts are read from (never written)."""
        if self._portfolio is None:
            self._portfolio = PortfolioStore()
        return self._portfolio

    def default_account_id(self) -> str | None:
        """Return the account the scorecard means by "the live book".

        The first paper account, else the first account of any broker, else
        ``None`` when nothing has ever traded.
        """
        try:
            accounts = self.portfolio.list_accounts()
        except Exception:  # noqa: BLE001 - a missing trade table is simply "no book"
            logger.debug("could not list trading accounts", exc_info=True)
            return None
        if not accounts:
            return None
        for account in accounts:
            if account.broker == "paper":
                return account.id
        return accounts[0].id

    def daily_returns(
        self,
        account_id: str | None = None,
        *,
        interval: BarInterval = BarInterval.DAY_1,
    ) -> LiveBook:
        """Reconstruct one account's daily equity curve and return series."""
        resolved = account_id or self.default_account_id()
        if resolved is None:
            return LiveBook(
                account_id=None,
                reason=f"{NO_BOOK}: no trading account has been opened",
            )
        try:
            account = self.portfolio.get_account(resolved)
        except KeyError:
            return LiveBook(
                account_id=resolved,
                reason=f"{NO_BOOK}: account {resolved!r} does not exist",
            )
        fills = sorted(
            self.portfolio.list_fills(resolved, limit=FILL_LIMIT), key=lambda f: f.ts
        )
        if not fills:
            return LiveBook(
                account_id=resolved,
                reason=f"{NO_BOOK}: the {resolved} account has no fills",
            )

        symbols = sorted({fill.symbol.upper() for fill in fills})
        first = _utc_day(fills[0].ts)

        closes: dict[str, pd.Series] = {}
        missing: list[str] = []
        for symbol in symbols:
            frame = self.store.read_bars(symbol, interval=interval)
            if frame.empty:
                missing.append(symbol)
                continue
            series = pd.to_numeric(frame["close"], errors="coerce").dropna()
            series.index = _utc_days(series.index)
            closes[symbol] = series[~series.index.duplicated(keep="last")]
        if missing:
            # Marking part of the book and calling the result "the book" would
            # be an invented number, so say which name cannot be marked.
            return LiveBook(
                account_id=resolved,
                symbols=symbols,
                fills=len(fills),
                reason=(
                    f"{NO_BOOK}: no stored {interval} bars for "
                    + ", ".join(missing[:4])
                    + (f" (+{len(missing) - 4} more)" if len(missing) > 4 else "")
                ),
            )

        panel = pd.DataFrame(closes).sort_index()
        panel = panel[panel.index >= first]
        if panel.shape[0] < 2:
            return LiveBook(
                account_id=resolved,
                symbols=symbols,
                fills=len(fills),
                reason=(
                    f"{NO_BOOK}: only {panel.shape[0]} stored session(s) since the first fill"
                ),
            )
        panel = panel.ffill()

        index = panel.index
        quantities = pd.DataFrame(0.0, index=index, columns=panel.columns)
        cash = pd.Series(0.0, index=index, dtype="float64")
        for fill in fills:
            stamp = _utc_day(fill.ts)
            # A fill lands on the first stored session at or after its timestamp;
            # one that post-dates every stored bar cannot be marked yet.
            position = int(index.searchsorted(stamp, side="left"))
            if position >= index.size:
                continue
            signed = fill.side.sign * abs(fill.qty)
            quantities.iloc[position, quantities.columns.get_loc(fill.symbol.upper())] += signed
            cash.iloc[position] += -signed * fill.price - fill.fee

        held = quantities.cumsum()
        balance = float(account.starting_cash) + cash.cumsum()
        equity = (held * panel).sum(axis=1) + balance
        equity.name = "equity"
        returns = equity.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
        returns.name = "live_book"
        if returns.empty:
            return LiveBook(
                account_id=resolved,
                symbols=symbols,
                fills=len(fills),
                reason=f"{NO_BOOK}: the reconstructed equity curve has no usable daily change",
            )
        return LiveBook(
            account_id=resolved,
            equity=equity,
            returns=returns,
            symbols=symbols,
            fills=len(fills),
        )

    def correlate(
        self,
        candidate: pd.Series,
        *,
        account_id: str | None = None,
        min_overlap: int = DEFAULT_MIN_OVERLAP,
        book: LiveBook | None = None,
    ) -> dict[str, Any]:
        """Return the Pearson correlation of ``candidate`` with the live book.

        Args:
            candidate: the candidate's daily return series (indexed by date).
            account_id: which account is "the live book"; the default paper
                account when omitted.
            min_overlap: sessions both series must share before a correlation is
                reported.
            book: a already-reconstructed book, so a scorecard with several
                candidates rebuilds it once rather than per candidate.

        Returns:
            ``correlation`` (``None`` when it cannot be measured), the
            ``overlap_days`` behind it, and a ``reason`` whenever it is ``None``.
        """
        resolved_book = book if book is not None else self.daily_returns(account_id)
        payload: dict[str, Any] = {
            "correlation": None,
            "overlap_days": 0,
            "min_overlap": min_overlap,
            "account_id": resolved_book.account_id,
            "reason": resolved_book.reason,
        }
        if not resolved_book.usable:
            return payload

        if candidate is None or getattr(candidate, "empty", True):
            payload["reason"] = "the candidate has no stored daily return series"
            return payload

        left = pd.Series(
            np.asarray(candidate.to_numpy(), dtype="float64"),
            index=_naive_days(candidate.index),
        ).dropna()
        left = left[~left.index.duplicated(keep="last")]
        right = pd.Series(
            resolved_book.returns.to_numpy(dtype="float64"),
            index=_naive_days(resolved_book.returns.index),
        )
        joined = pd.concat([left, right], axis=1, join="inner").dropna()
        overlap = int(joined.shape[0])
        payload["overlap_days"] = overlap
        if overlap < min_overlap:
            payload["reason"] = (
                f"{NO_BOOK}: only {overlap} overlapping session(s) with the candidate; "
                f"{min_overlap} needed"
            )
            return payload

        first = joined.iloc[:, 0].to_numpy(dtype="float64")
        second = joined.iloc[:, 1].to_numpy(dtype="float64")
        if float(np.std(first)) == 0.0 or float(np.std(second)) == 0.0:
            payload["reason"] = (
                "one of the two return series never varies over the overlap, "
                "so a correlation is undefined"
            )
            return payload
        payload["correlation"] = float(np.corrcoef(first, second)[0, 1])
        payload["reason"] = None
        payload["start"] = joined.index[0].date().isoformat()
        payload["end"] = joined.index[-1].date().isoformat()
        return payload


def returns_from_curve(curve: dict[str, Any] | None) -> tuple[pd.Series, str | None]:
    """Turn a stored equity-curve payload into a daily return series.

    Args:
        curve: the ``equity_curve`` payload persisted with a backtest run.

    Returns:
        ``(series, reason)``. The series is empty whenever ``reason`` is set - a
        thinned curve is refused rather than resampled, because the returns
        between kept points are not daily returns.
    """
    if not curve:
        return pd.Series(dtype="float64"), "the run stored no equity curve"
    if curve.get("sampled"):
        return (
            pd.Series(dtype="float64"),
            "the stored equity curve is thinned, so it has no daily returns to correlate",
        )
    dates = curve.get("dates") or []
    equity = curve.get("equity") or []
    if len(dates) != len(equity) or len(equity) < 2:
        return pd.Series(dtype="float64"), "the stored equity curve is too short"
    series = pd.Series([float(value) for value in equity], index=_naive_days(dates))
    returns = series.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
    if returns.empty:
        return pd.Series(dtype="float64"), "the stored equity curve has no usable daily change"
    return returns, None


_engine: LiveBookEngine | None = None


def get_livebook_engine() -> LiveBookEngine:
    """Return the process-wide live-book engine, creating it on first use."""
    global _engine
    if _engine is None:
        _engine = LiveBookEngine()
    return _engine


def reset_livebook_engine(engine: LiveBookEngine | None = None) -> LiveBookEngine:
    """Replace the process-wide live-book engine. Intended for tests."""
    global _engine
    _engine = engine if engine is not None else LiveBookEngine()
    return _engine


__all__ = [
    "DEFAULT_MIN_OVERLAP",
    "NO_BOOK",
    "LiveBook",
    "LiveBookEngine",
    "get_livebook_engine",
    "reset_livebook_engine",
    "returns_from_curve",
]
