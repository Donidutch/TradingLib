"""yfinance-backed provider: quotes, bars, fundamentals, news, symbol search.

yfinance is keyless, so this provider is the always-available baseline. Its
calls are blocking and therefore dispatched to a thread executor
(:func:`asyncio.to_thread`), as ``docs/SPEC.md`` section 4 requires.

Vendor failures are translated into the :class:`~tradinglib.data.base.ProviderError`
hierarchy so callers can distinguish "unknown symbol" from "Yahoo throttled us"
from "Yahoo genuinely has no rows for this range". Nothing is ever substituted
for missing data.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable, Iterable
from datetime import date, datetime, timezone
from typing import Any, Final, TypeVar

import pandas as pd
import yfinance as yf
from yfinance import exceptions as yf_exceptions

from tradinglib.data.base import (
    Bar,
    BarInterval,
    DataProvider,
    Fundamentals,
    NewsItem,
    ProviderRateLimited,
    ProviderStatus,
    ProviderUnavailable,
    Quote,
    SymbolNotFound,
    to_utc,
    utcnow,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: Symbol used for the reachability probe in :meth:`YFinanceProvider.status`.
PROBE_SYMBOL: Final[str] = "SPY"

#: Default look-back period per interval when the caller gives no start date.
#: Yahoo enforces its own intraday retention limits; these stay inside them.
DEFAULT_PERIOD: Final[dict[BarInterval, str]] = {
    BarInterval.MIN_1: "7d",
    BarInterval.MIN_5: "60d",
    BarInterval.MIN_15: "60d",
    BarInterval.MIN_30: "60d",
    BarInterval.HOUR_1: "2y",
    BarInterval.DAY_1: "10y",
    BarInterval.WEEK_1: "max",
    BarInterval.MONTH_1: "max",
}

#: Maximum number of symbols fetched concurrently in :meth:`get_quotes`.
MAX_CONCURRENCY: Final[int] = 8


def _clean_float(value: Any) -> float | None:
    """Return ``value`` as a float, or ``None`` for missing/NaN/inf input."""
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def _clean_int(value: Any) -> int | None:
    """Return ``value`` as an int, or ``None`` for missing/NaN input."""
    number = _clean_float(value)
    return None if number is None else int(number)


def _clean_str(value: Any) -> str | None:
    """Return ``value`` as a non-empty string, or ``None``."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_datetime(value: Any) -> datetime | None:
    """Coerce a Yahoo timestamp (epoch, ISO string, Timestamp) to aware UTC."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if math.isnan(value):
            return None
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, pd.Timestamp):
        # Checked before ``datetime`` because Timestamp subclasses it; the
        # store and the models want plain datetimes.
        if pd.isna(value):
            return None
        return to_utc(value.to_pydatetime())
    if isinstance(value, datetime):
        return to_utc(value)
    if isinstance(value, str):
        try:
            return to_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def _as_date(value: Any) -> date | None:
    """Coerce a Yahoo timestamp to a plain date."""
    moment = _as_datetime(value)
    return moment.date() if moment else None


def _frame_records(frame: Any, *, index_name: str = "period") -> list[dict[str, Any]]:
    """Convert a yfinance dataframe into JSON-safe records.

    The index (a period, a date or an analyst label) is emitted as
    ``index_name`` so the API layer never has to guess what the rows mean.
    """
    if frame is None or not isinstance(frame, pd.DataFrame) or frame.empty:
        return []
    records: list[dict[str, Any]] = []
    for index_value, row in frame.iterrows():
        record: dict[str, Any] = {index_name: _stringify_label(index_value)}
        for column, value in row.items():
            record[str(column)] = _json_scalar(value)
        records.append(record)
    return records


def _statement_records(frame: Any) -> list[dict[str, Any]]:
    """Convert a statement frame (metrics x periods) into per-period records."""
    if frame is None or not isinstance(frame, pd.DataFrame) or frame.empty:
        return []
    records: list[dict[str, Any]] = []
    for column in frame.columns:
        record: dict[str, Any] = {"period": _stringify_label(column)}
        series = frame[column]
        for metric, value in series.items():
            record[str(metric)] = _json_scalar(value)
        records.append(record)
    records.sort(key=lambda item: str(item["period"]), reverse=True)
    return records


def _stringify_label(value: Any) -> str:
    """Render a dataframe index/column label as a stable string."""
    if isinstance(value, pd.Timestamp):
        moment = value.to_pydatetime()
        if moment.tzinfo is None and moment.time() == datetime.min.time():
            return moment.date().isoformat()
        return to_utc(moment).isoformat()
    if isinstance(value, datetime):
        return to_utc(value).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _json_scalar(value: Any) -> Any:
    """Render a dataframe cell as a JSON-safe scalar."""
    if value is None:
        return None
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return _stringify_label(value)
    if isinstance(value, (int,)) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return _clean_float(value)
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    number = _clean_float(value)
    if number is not None and not isinstance(value, str):
        return number
    return _clean_str(value)


class YFinanceProvider(DataProvider):
    """Keyless market-data provider backed by the ``yfinance`` package."""

    name = "yfinance"

    def __init__(self, *, timeout: float = 20.0) -> None:
        """Create the provider.

        Args:
            timeout: per-request timeout, in seconds, handed to yfinance.
        """
        self.timeout = timeout
        self._last_success: datetime | None = None
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

    # --- plumbing ---------------------------------------------------------
    @staticmethod
    def _ticker(symbol: str) -> yf.Ticker:
        """Return a yfinance ticker handle for ``symbol``."""
        return yf.Ticker(symbol.upper())

    async def _call(self, symbol: str, fn: Callable[[], T]) -> T:
        """Run a blocking yfinance call in a thread, mapping vendor errors."""
        try:
            result = await asyncio.to_thread(fn)
        except yf_exceptions.YFRateLimitError as exc:
            raise ProviderRateLimited(self.name, f"Yahoo rate limited the request: {exc}") from exc
        except yf_exceptions.YFTickerMissingError as exc:
            raise SymbolNotFound(self.name, str(exc)) from exc
        except yf_exceptions.YFException as exc:
            raise ProviderUnavailable(self.name, f"{symbol}: {exc}") from exc
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ProviderUnavailable(
                self.name, f"{symbol}: {type(exc).__name__}: {exc}"
            ) from exc
        self._last_success = utcnow()
        return result

    # --- status -----------------------------------------------------------
    async def status(self) -> ProviderStatus:
        """Report reachability by issuing one cheap live request."""
        started = utcnow()
        try:
            price = await self._call(
                PROBE_SYMBOL, lambda: self._ticker(PROBE_SYMBOL).fast_info["lastPrice"]
            )
        except ProviderRateLimited as exc:
            return ProviderStatus(
                name=self.name,
                configured=True,
                reachable=False,
                last_success=self._last_success,
                detail=f"rate limited: {exc.message}",
            )
        except ProviderUnavailable as exc:
            return ProviderStatus(
                name=self.name,
                configured=True,
                reachable=False,
                last_success=self._last_success,
                detail=exc.message,
            )
        latency_ms = (utcnow() - started).total_seconds() * 1000.0
        return ProviderStatus(
            name=self.name,
            configured=True,
            reachable=True,
            latency_ms=latency_ms,
            last_success=self._last_success,
            detail=f"{PROBE_SYMBOL} last price {price}",
        )

    # --- quotes -----------------------------------------------------------
    def _quote_sync(self, symbol: str) -> Quote:
        """Blocking quote fetch (runs in a worker thread)."""
        ticker = self._ticker(symbol)
        fast = ticker.fast_info
        price = _clean_float(fast.get("lastPrice"))
        if price is None:
            raise SymbolNotFound(self.name, f"{symbol}: Yahoo returned no last price")
        metadata = ticker.history_metadata or {}
        ts = _as_datetime(metadata.get("regularMarketTime")) or utcnow()
        return Quote(
            symbol=symbol.upper(),
            price=price,
            ts=ts,
            volume=_clean_int(fast.get("lastVolume")),
            prev_close=_clean_float(fast.get("previousClose"))
            or _clean_float(fast.get("regularMarketPreviousClose")),
            source=self.name,
        )

    async def get_quote(self, symbol: str) -> Quote:
        """Return the latest yfinance quote for ``symbol``.

        Yahoo's free feed carries no bid/ask on this endpoint, so those fields
        stay ``None`` rather than being filled with the last trade price.
        """
        return await self._call(symbol, lambda: self._quote_sync(symbol))

    async def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """Fetch quotes for ``symbols`` concurrently.

        Symbols Yahoo cannot serve are omitted from the result (and logged);
        a partial tape is honest, an invented one is not.
        """

        async def one(symbol: str) -> tuple[str, Quote] | None:
            async with self._semaphore:
                try:
                    return symbol.upper(), await self.get_quote(symbol)
                except ProviderRateLimited:
                    raise
                except ProviderUnavailable as exc:
                    logger.info("yfinance quote unavailable for %s: %s", symbol, exc.message)
                    return None
                except SymbolNotFound as exc:
                    logger.info("yfinance has no quote for %s: %s", symbol, exc.message)
                    return None

        results = await asyncio.gather(*(one(s) for s in symbols))
        return {symbol: quote for item in results if item for symbol, quote in [item]}

    # --- bars -------------------------------------------------------------
    def _bars_sync(
        self,
        symbol: str,
        start: datetime | None,
        end: datetime | None,
        interval: BarInterval,
        limit: int | None,
    ) -> list[Bar]:
        """Blocking OHLCV fetch (runs in a worker thread)."""
        kwargs: dict[str, Any] = {
            "interval": str(interval),
            "auto_adjust": False,
            "actions": False,
            "timeout": self.timeout,
        }
        if start is not None:
            kwargs["start"] = to_utc(start)
            if end is not None:
                kwargs["end"] = to_utc(end)
        elif end is not None:
            kwargs["end"] = to_utc(end)
            kwargs["period"] = DEFAULT_PERIOD[interval]
        else:
            kwargs["period"] = DEFAULT_PERIOD[interval]

        frame = self._ticker(symbol).history(**kwargs)
        if frame is None or frame.empty:
            return []
        if limit is not None and limit > 0:
            frame = frame.tail(limit)

        bars: list[Bar] = []
        for index_value, row in frame.iterrows():
            ts = _as_datetime(index_value)
            close = _clean_float(row.get("Close"))
            if ts is None or close is None:
                continue
            open_ = _clean_float(row.get("Open"))
            high = _clean_float(row.get("High"))
            low = _clean_float(row.get("Low"))
            bars.append(
                Bar(
                    symbol=symbol.upper(),
                    ts=ts,
                    open=close if open_ is None else open_,
                    high=close if high is None else high,
                    low=close if low is None else low,
                    close=close,
                    volume=_clean_float(row.get("Volume")) or 0.0,
                    interval=interval,
                    source=self.name,
                )
            )
        return bars

    async def get_bars(
        self,
        symbol: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        interval: BarInterval = BarInterval.DAY_1,
        limit: int | None = None,
    ) -> list[Bar]:
        """Return OHLCV history from ``Ticker.history``.

        Prices are *not* auto-adjusted, so ``close`` is the raw close; splits
        and dividends are available separately from the vendor if a caller
        needs an adjusted series.
        """
        return await self._call(
            symbol, lambda: self._bars_sync(symbol, start, end, interval, limit)
        )

    # --- fundamentals ------------------------------------------------------
    def _fundamentals_sync(self, symbol: str) -> Fundamentals:
        """Blocking fundamentals fetch (runs in a worker thread)."""
        info = self._ticker(symbol).info or {}
        if not any(
            info.get(key) is not None for key in ("longName", "shortName", "marketCap", "quoteType")
        ):
            raise SymbolNotFound(self.name, f"{symbol}: Yahoo returned no company profile")
        return Fundamentals(
            symbol=symbol.upper(),
            as_of=_as_date(info.get("mostRecentQuarter")),
            currency=_clean_str(info.get("financialCurrency") or info.get("currency")),
            name=_clean_str(info.get("longName") or info.get("shortName")),
            sector=_clean_str(info.get("sector")),
            industry=_clean_str(info.get("industry")),
            market_cap=_clean_float(info.get("marketCap")),
            enterprise_value=_clean_float(info.get("enterpriseValue")),
            shares_outstanding=_clean_float(info.get("sharesOutstanding")),
            revenue_ttm=_clean_float(info.get("totalRevenue")),
            gross_profit_ttm=_clean_float(info.get("grossProfits")),
            ebitda_ttm=_clean_float(info.get("ebitda")),
            net_income_ttm=_clean_float(info.get("netIncomeToCommon")),
            free_cash_flow_ttm=_clean_float(info.get("freeCashflow")),
            total_debt=_clean_float(info.get("totalDebt")),
            cash_and_equivalents=_clean_float(info.get("totalCash")),
            extras={
                key: _json_scalar(info.get(key))
                for key in (
                    "trailingPE",
                    "forwardPE",
                    "priceToBook",
                    "priceToSalesTrailing12Months",
                    "profitMargins",
                    "operatingMargins",
                    "grossMargins",
                    "returnOnEquity",
                    "returnOnAssets",
                    "revenueGrowth",
                    "earningsGrowth",
                    "beta",
                    "dividendYield",
                    "payoutRatio",
                    "fiftyTwoWeekHigh",
                    "fiftyTwoWeekLow",
                    "averageVolume",
                    "exchange",
                    "quoteType",
                    "website",
                    "longBusinessSummary",
                    "fullTimeEmployees",
                    "country",
                )
                if info.get(key) is not None
            },
            source=self.name,
            fetched_at=utcnow(),
        )

    async def get_fundamentals(self, symbol: str) -> Fundamentals:
        """Compose a :class:`Fundamentals` snapshot from yfinance statements."""
        return await self._call(symbol, lambda: self._fundamentals_sync(symbol))

    def _statements_sync(self, symbol: str) -> dict[str, Any]:
        """Blocking statement fetch (runs in a worker thread)."""
        ticker = self._ticker(symbol)
        payload: dict[str, Any] = {
            "symbol": symbol.upper(),
            "source": self.name,
            "fetched_at": utcnow().isoformat(),
            "annual": {
                "income_statement": _statement_records(ticker.income_stmt),
                "balance_sheet": _statement_records(ticker.balance_sheet),
                "cash_flow": _statement_records(ticker.cash_flow),
            },
            "quarterly": {
                "income_statement": _statement_records(ticker.quarterly_income_stmt),
                "balance_sheet": _statement_records(ticker.quarterly_balance_sheet),
                "cash_flow": _statement_records(ticker.quarterly_cash_flow),
            },
        }
        return payload

    async def get_financial_statements(self, symbol: str) -> dict[str, Any]:
        """Return income statement, balance sheet and cash-flow frames as records.

        Shape: ``{"annual": {"income_statement": [{"period": ..., <metric>: ...}]}}``
        with the same keys under ``"quarterly"``. Statements Yahoo does not
        publish come back as empty lists, never as fabricated rows.
        """
        return await self._call(symbol, lambda: self._statements_sync(symbol))

    def _estimates_sync(self, symbol: str) -> dict[str, Any]:
        """Blocking analyst-estimate fetch (runs in a worker thread)."""
        ticker = self._ticker(symbol)

        def safe(getter: Callable[[], Any], label: str) -> Any:
            """Call one estimate accessor, tolerating vendor gaps."""
            try:
                return getter()
            except Exception as exc:  # noqa: BLE001 - one gap must not kill the tab
                logger.info("yfinance %s unavailable for %s: %s", label, symbol, exc)
                return None

        targets = safe(lambda: ticker.analyst_price_targets, "price targets")
        return {
            "symbol": symbol.upper(),
            "source": self.name,
            "fetched_at": utcnow().isoformat(),
            "earnings_estimate": _frame_records(
                safe(lambda: ticker.earnings_estimate, "earnings estimate")
            ),
            "revenue_estimate": _frame_records(
                safe(lambda: ticker.revenue_estimate, "revenue estimate")
            ),
            "eps_trend": _frame_records(safe(lambda: ticker.eps_trend, "eps trend")),
            "eps_revisions": _frame_records(safe(lambda: ticker.eps_revisions, "eps revisions")),
            "growth_estimates": _frame_records(
                safe(lambda: ticker.growth_estimates, "growth estimates")
            ),
            "recommendations": _frame_records(
                safe(lambda: ticker.recommendations, "recommendations"), index_name="row"
            ),
            "upgrades_downgrades": _frame_records(
                safe(lambda: ticker.upgrades_downgrades, "upgrades/downgrades"), index_name="date"
            )[:50],
            "earnings_dates": _frame_records(
                safe(lambda: ticker.earnings_dates, "earnings dates"), index_name="date"
            ),
            "price_targets": {k: _json_scalar(v) for k, v in targets.items()}
            if isinstance(targets, dict)
            else None,
        }

    async def get_estimates(self, symbol: str) -> dict[str, Any]:
        """Return analyst estimates, recommendations and earnings dates."""
        return await self._call(symbol, lambda: self._estimates_sync(symbol))

    # --- news -------------------------------------------------------------
    def _news_sync(self, symbol: str, limit: int) -> list[NewsItem]:
        """Blocking news fetch (runs in a worker thread)."""
        raw = self._ticker(symbol).get_news(count=max(1, limit)) or []
        items: list[NewsItem] = []
        for entry in raw:
            content = entry.get("content") if isinstance(entry, dict) else None
            if not isinstance(content, dict):
                continue
            headline = _clean_str(content.get("title"))
            if headline is None:
                continue
            url_block = content.get("canonicalUrl") or content.get("clickThroughUrl") or {}
            url = _clean_str(url_block.get("url") if isinstance(url_block, dict) else url_block)
            published = _as_datetime(content.get("pubDate") or content.get("displayTime"))
            provider = content.get("provider")
            items.append(
                NewsItem(
                    symbol=symbol.upper(),
                    headline=headline,
                    url=url or "",
                    published_at=published or utcnow(),
                    publisher=_clean_str(provider.get("displayName"))
                    if isinstance(provider, dict)
                    else None,
                    summary=_clean_str(content.get("summary")),
                    sentiment=None,
                    source=self.name,
                )
            )
            if len(items) >= limit:
                break
        items.sort(key=lambda item: item.published_at, reverse=True)
        return items

    async def get_news(self, symbol: str, *, limit: int = 25) -> list[NewsItem]:
        """Return recent yfinance news items for ``symbol`` (sentiment unscored)."""
        return await self._call(symbol, lambda: self._news_sync(symbol, limit))

    # --- options ----------------------------------------------------------
    def _option_iv_sync(self, symbol: str, target_days: int) -> dict[str, Any]:
        """Blocking 30-day at-the-money implied volatility (runs in a thread).

        Yahoo publishes a per-contract ``impliedVolatility`` on every option
        chain. This picks the two listed expiries that bracket ``target_days``,
        takes the call and put closest to the money on each, averages them, and
        interpolates linearly in time between the two expiries. That is the
        standard cheap ATM-IV construction; it is *not* a variance-swap VIX-style
        integral, and the returned payload says so in ``method`` so nobody
        mistakes it for one.

        Raises:
            ProviderUnavailable: when the symbol lists no options at all.
        """
        ticker = self._ticker(symbol)
        expiries = list(ticker.options or [])
        if not expiries:
            raise ProviderUnavailable(self.name, f"{symbol.upper()} lists no option expiries")
        today = datetime.now(timezone.utc).date()
        dated: list[tuple[int, str]] = []
        for text in expiries:
            try:
                days = (date.fromisoformat(str(text)) - today).days
            except ValueError:
                continue
            if days > 0:
                dated.append((days, str(text)))
        if not dated:
            raise ProviderUnavailable(
                self.name, f"{symbol.upper()} lists only expired option contracts"
            )
        dated.sort()
        spot = self._quote_sync(symbol).price
        if spot is None or not math.isfinite(spot) or spot <= 0:
            raise ProviderUnavailable(
                self.name, f"{symbol.upper()} has no usable spot price to centre the chain on"
            )

        below = [item for item in dated if item[0] <= target_days]
        above = [item for item in dated if item[0] > target_days]
        picks = [below[-1]] if below else []
        if above:
            picks.append(above[0])
        if not picks:
            picks = [dated[-1]]

        samples: list[tuple[int, float, int]] = []
        for days, expiry in picks:
            iv, contracts = self._atm_iv(ticker, expiry, spot)
            if iv is not None:
                samples.append((days, iv, contracts))
        if not samples:
            raise ProviderUnavailable(
                self.name,
                f"{symbol.upper()} option chains carry no implied volatility near the money",
            )
        if len(samples) == 1 or samples[0][0] == samples[-1][0]:
            value = samples[0][1]
        else:
            (d0, iv0, _), (d1, iv1, _) = samples[0], samples[-1]
            weight = (target_days - d0) / (d1 - d0)
            weight = min(1.0, max(0.0, weight))
            value = iv0 + weight * (iv1 - iv0)
        return {
            "symbol": symbol.upper(),
            "as_of": datetime.now(timezone.utc).isoformat(),
            "target_days": int(target_days),
            "implied_volatility": round(float(value), 6),
            "spot": float(spot),
            "expiries_used": [
                {"expiry": expiry, "days": days, "iv": iv, "contracts": contracts}
                for (days, iv, contracts), (_, expiry) in zip(samples, picks, strict=False)
            ],
            "listed_expiries": len(dated),
            "method": (
                "at-the-money call/put average per expiry, interpolated linearly in "
                "calendar days; not a variance-swap (VIX-style) integral"
            ),
            "source": self.name,
        }

    @staticmethod
    def _atm_iv(ticker: yf.Ticker, expiry: str, spot: float) -> tuple[float | None, int]:
        """Return the at-the-money implied volatility of one expiry."""
        chain = ticker.option_chain(expiry)
        values: list[float] = []
        contracts = 0
        for frame in (chain.calls, chain.puts):
            if frame is None or frame.empty or "impliedVolatility" not in frame:
                continue
            usable = frame[["strike", "impliedVolatility"]].dropna()
            usable = usable[(usable["impliedVolatility"] > 0.0) & (usable["impliedVolatility"] < 5.0)]
            if usable.empty:
                continue
            contracts += int(len(usable))
            nearest = (usable["strike"] - spot).abs().idxmin()
            values.append(float(usable.loc[nearest, "impliedVolatility"]))
        if not values:
            return None, contracts
        return sum(values) / len(values), contracts

    async def get_option_iv(self, symbol: str, *, target_days: int = 30) -> dict[str, Any]:
        """Return the at-the-money implied volatility of ``symbol``.

        Args:
            symbol: underlying ticker.
            target_days: tenor to interpolate to, in calendar days.
        """
        return await self._call(symbol, lambda: self._option_iv_sync(symbol, target_days))

    # --- search -----------------------------------------------------------
    def _search_sync(self, query: str, limit: int) -> list[dict[str, Any]]:
        """Blocking symbol search (runs in a worker thread)."""
        search = yf.Search(
            query,
            max_results=max(1, limit),
            news_count=0,
            lists_count=0,
            enable_fuzzy_query=True,
            timeout=self.timeout,
            raise_errors=True,
        )
        quotes: Iterable[dict[str, Any]] = search.quotes or []
        out: list[dict[str, Any]] = []
        for quote in quotes:
            symbol = _clean_str(quote.get("symbol"))
            if symbol is None:
                continue
            out.append(
                {
                    "symbol": symbol,
                    "name": _clean_str(quote.get("longname") or quote.get("shortname")),
                    "exchange": _clean_str(quote.get("exchDisp") or quote.get("exchange")),
                    "type": _clean_str(quote.get("quoteType") or quote.get("typeDisp")),
                    "sector": _clean_str(quote.get("sectorDisp") or quote.get("sector")),
                    "industry": _clean_str(quote.get("industryDisp") or quote.get("industry")),
                    "score": _clean_float(quote.get("score")),
                    "source": self.name,
                }
            )
            if len(out) >= limit:
                break
        return out

    async def search(self, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
        """Resolve a free-text query through the Yahoo symbol-search endpoint."""
        text = query.strip()
        if not text:
            return []
        return await self._call(text, lambda: self._search_sync(text, limit))
