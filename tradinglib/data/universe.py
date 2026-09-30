"""Universes and watchlists, with point-in-time membership snapshots.

Three built-in universes ship with TradingLib:

``us-quality-50``
    A fixed, documented 50-name US quality definition (see
    :data:`US_QUALITY_50_RULE`). It is an editorial definition curated at
    :data:`US_QUALITY_50_AS_OF`, not a screen recomputed from vendor
    fundamentals - rule-recomputed universes belong in
    ``tradinglib.research.screen``, which can save its output as a user universe.

``sp500`` / ``nasdaq100``
    Real index constituents fetched from the Wikipedia constituent tables and
    cached to ``<data_dir>/universes/<id>.json``. :meth:`UniverseRegistry.refresh_builtin`
    re-fetches them; when the network is unavailable the cached list is used and
    the staleness is reported honestly (``fetched_at``), never silently faked.

Everything is persisted through :mod:`tradinglib.db` (SQLite): universes, their
members, dated membership snapshots, watchlists and the active-universe
selection. Snapshots are what make backtests point-in-time: a snapshot taken on
a date is replayed verbatim for that date, so names that later left the index
(or were delisted) still appear in the membership used for that period.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import httpx
from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    delete,
    func,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from tradinglib.data.base import ProviderUnavailable, to_utc, utcnow
from tradinglib.db import Base, ensure_columns, get_engine, get_session_factory

logger = logging.getLogger(__name__)

#: Identifiers of the universes TradingLib ships with.
BUILTIN_UNIVERSES: Final[tuple[str, ...]] = ("us-quality-50", "sp500", "nasdaq100")

#: Date at which the ``us-quality-50`` definition below was curated.
US_QUALITY_50_AS_OF: Final[date] = date(2026, 6, 30)

#: The documented selection rule behind :data:`US_QUALITY_50`.
US_QUALITY_50_RULE: Final[str] = (
    "US Quality 50 - 50 US-listed large caps selected at 2026-06-30 on a "
    "quality-compounder profile: an S&P 500 constituent with a long record of "
    "positive free cash flow, high returns on invested capital, conservative "
    "balance-sheet leverage and ample trading liquidity. Sector exposure is "
    "capped at 8 names per GICS sector; utilities, energy and real estate are "
    "excluded by construction because their capital intensity and structural "
    "leverage do not fit the profile. The list is equal-weighted (no vendor "
    "weights are stored) and is a fixed editorial definition: it is NOT "
    "recomputed from vendor fundamentals at runtime."
)

#: ``(symbol, company name, GICS sector)`` for every US Quality 50 constituent.
US_QUALITY_50: Final[tuple[tuple[str, str, str], ...]] = (
    # Information Technology (8)
    ("MSFT", "Microsoft Corporation", "Information Technology"),
    ("AAPL", "Apple Inc.", "Information Technology"),
    ("NVDA", "NVIDIA Corporation", "Information Technology"),
    ("AVGO", "Broadcom Inc.", "Information Technology"),
    ("ADBE", "Adobe Inc.", "Information Technology"),
    ("TXN", "Texas Instruments Incorporated", "Information Technology"),
    ("CSCO", "Cisco Systems, Inc.", "Information Technology"),
    ("INTU", "Intuit Inc.", "Information Technology"),
    # Communication Services (3)
    ("GOOGL", "Alphabet Inc.", "Communication Services"),
    ("META", "Meta Platforms, Inc.", "Communication Services"),
    ("NFLX", "Netflix, Inc.", "Communication Services"),
    # Consumer Discretionary (6)
    ("HD", "The Home Depot, Inc.", "Consumer Discretionary"),
    ("NKE", "NIKE, Inc.", "Consumer Discretionary"),
    ("TJX", "The TJX Companies, Inc.", "Consumer Discretionary"),
    ("ORLY", "O'Reilly Automotive, Inc.", "Consumer Discretionary"),
    ("ROST", "Ross Stores, Inc.", "Consumer Discretionary"),
    ("LULU", "Lululemon Athletica Inc.", "Consumer Discretionary"),
    # Consumer Staples (6)
    ("PG", "The Procter & Gamble Company", "Consumer Staples"),
    ("KO", "The Coca-Cola Company", "Consumer Staples"),
    ("PEP", "PepsiCo, Inc.", "Consumer Staples"),
    ("COST", "Costco Wholesale Corporation", "Consumer Staples"),
    ("CL", "Colgate-Palmolive Company", "Consumer Staples"),
    ("MNST", "Monster Beverage Corporation", "Consumer Staples"),
    # Health Care (8)
    ("JNJ", "Johnson & Johnson", "Health Care"),
    ("LLY", "Eli Lilly and Company", "Health Care"),
    ("ISRG", "Intuitive Surgical, Inc.", "Health Care"),
    ("REGN", "Regeneron Pharmaceuticals, Inc.", "Health Care"),
    ("VRTX", "Vertex Pharmaceuticals Incorporated", "Health Care"),
    ("ZTS", "Zoetis Inc.", "Health Care"),
    ("IDXX", "IDEXX Laboratories, Inc.", "Health Care"),
    ("MCK", "McKesson Corporation", "Health Care"),
    # Financials (7)
    ("V", "Visa Inc.", "Financials"),
    ("MA", "Mastercard Incorporated", "Financials"),
    ("SPGI", "S&P Global Inc.", "Financials"),
    ("MCO", "Moody's Corporation", "Financials"),
    ("BLK", "BlackRock, Inc.", "Financials"),
    ("CME", "CME Group Inc.", "Financials"),
    ("PGR", "The Progressive Corporation", "Financials"),
    # Industrials (8)
    ("HON", "Honeywell International Inc.", "Industrials"),
    ("UNP", "Union Pacific Corporation", "Industrials"),
    ("ITW", "Illinois Tool Works Inc.", "Industrials"),
    ("ADP", "Automatic Data Processing, Inc.", "Industrials"),
    ("PAYX", "Paychex, Inc.", "Industrials"),
    ("CTAS", "Cintas Corporation", "Industrials"),
    ("FAST", "Fastenal Company", "Industrials"),
    ("GWW", "W.W. Grainger, Inc.", "Industrials"),
    # Materials (4)
    ("LIN", "Linde plc", "Materials"),
    ("SHW", "The Sherwin-Williams Company", "Materials"),
    ("ECL", "Ecolab Inc.", "Materials"),
    ("NUE", "Nucor Corporation", "Materials"),
)

#: Default HTTP timeout for constituent fetches, in seconds.
FETCH_TIMEOUT: Final[float] = 30.0

#: How old a cached constituent list may be before a refresh re-fetches it.
DEFAULT_MAX_CACHE_AGE: Final[timedelta] = timedelta(days=1)

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_CLASS_SHARE_RE = re.compile(r"^([A-Z0-9]{1,5})\.([A-Z]{1,2})$")
#: A ticker, optionally prefixed with ``^`` for an index (``^VIX``, ``^GSPC``).
#: Index symbols are real instruments the price providers serve and the Overview's
#: market-context panel needs; rejecting them is what left the VIX row empty.
_SYMBOL_RE = re.compile(r"^\^?[A-Z0-9][A-Z0-9.\-]{0,14}$")


def normalize_symbol(raw: str) -> str:
    """Return the canonical TradingLib ticker for ``raw``.

    Tickers are upper-cased and trimmed, and US class-share suffixes are
    written with a hyphen (``BRK.B`` -> ``BRK-B``) because that is the form the
    price providers (yfinance, Alpaca) and therefore the Parquet store use. The
    vendor's own spelling is kept alongside in
    :attr:`UniverseMember.source_symbol` so a lookup can be traced back.

    Raises:
        ValueError: when ``raw`` is empty or is not a plausible ticker.
    """
    cleaned = str(raw).replace(" ", " ").strip().upper()
    if not cleaned:
        raise ValueError("symbol must not be empty")
    match = _CLASS_SHARE_RE.match(cleaned)
    if match:
        cleaned = f"{match.group(1)}-{match.group(2)}"
    if not _SYMBOL_RE.match(cleaned):
        raise ValueError(f"not a valid ticker: {raw!r}")
    return cleaned


def _slugify(value: str, *, fallback: str = "universe") -> str:
    """Return a URL/id-safe slug for ``value``."""
    slug = _SLUG_RE.sub("-", value.strip().lower()).strip("-")
    return (slug or fallback)[:48]


def _as_date(value: Any) -> date | None:
    """Coerce a stored value to a :class:`datetime.date`, or ``None``."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _aware(value: datetime | None) -> datetime | None:
    """Coerce a datetime read back from SQLite to aware UTC."""
    return None if value is None else to_utc(value)


# --------------------------------------------------------------------------
# models
# --------------------------------------------------------------------------
@dataclass(slots=True)
class UniverseMember:
    """One constituent of a universe at a point in time."""

    symbol: str
    name: str | None = None
    sector: str | None = None
    weight: float | None = None
    added_on: date | None = None
    removed_on: date | None = None
    source_symbol: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of the member."""
        return {
            "symbol": self.symbol,
            "name": self.name,
            "sector": self.sector,
            "weight": self.weight,
            "added_on": self.added_on.isoformat() if self.added_on else None,
            "removed_on": self.removed_on.isoformat() if self.removed_on else None,
            "source_symbol": self.source_symbol,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> UniverseMember:
        """Rebuild a member from :meth:`as_dict` output."""
        return cls(
            symbol=normalize_symbol(payload["symbol"]),
            name=payload.get("name"),
            sector=payload.get("sector"),
            weight=payload.get("weight"),
            added_on=_as_date(payload.get("added_on")),
            removed_on=_as_date(payload.get("removed_on")),
            source_symbol=payload.get("source_symbol"),
        )

    def active_on(self, as_of: date) -> bool:
        """Whether the member was in the universe on ``as_of``.

        A member with no ``added_on`` is treated as present from the beginning
        of time: the vendor did not tell us when it joined, and inventing a
        date would silently bias point-in-time membership.
        """
        if self.added_on is not None and self.added_on > as_of:
            return False
        return not (self.removed_on is not None and self.removed_on <= as_of)


@dataclass(slots=True)
class Universe:
    """A named set of symbols with optional point-in-time history."""

    id: str
    name: str
    description: str | None = None
    builtin: bool = False
    members: list[UniverseMember] = field(default_factory=list)
    updated_at: datetime | None = None
    created_at: datetime | None = None
    source: str = "user"
    rule: str | None = None
    active: bool = False
    members_fetched_at: datetime | None = None

    def symbols(self) -> list[str]:
        """Return current member symbols in stored order."""
        return [m.symbol for m in self.members if m.removed_on is None]

    def as_dict(self, *, include_members: bool = True) -> dict[str, Any]:
        """Return a JSON-serialisable mapping for the API layer."""
        payload: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "builtin": self.builtin,
            "source": self.source,
            "rule": self.rule,
            "active": self.active,
            "symbol_count": len(self.symbols()),
            "member_count": len(self.members),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "members_fetched_at": (
                self.members_fetched_at.isoformat() if self.members_fetched_at else None
            ),
        }
        if include_members:
            payload["members"] = [m.as_dict() for m in self.members]
        return payload


@dataclass(slots=True)
class Watchlist:
    """A user watchlist (the Overview page's watchlist panel)."""

    id: str
    name: str
    symbols: list[str] = field(default_factory=list)
    is_default: bool = False
    #: Whether this watchlist is the active scope (see :class:`WatchlistRow`).
    active: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of the watchlist."""
        return {
            "id": self.id,
            "name": self.name,
            "symbols": list(self.symbols),
            "symbol_count": len(self.symbols),
            "is_default": self.is_default,
            "active": self.active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


# --------------------------------------------------------------------------
# ORM rows
# --------------------------------------------------------------------------
class UniverseRow(Base):
    """SQLite row for one universe."""

    __tablename__ = "universes"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text(), default=None)
    builtin: Mapped[bool] = mapped_column(Boolean(), default=False, nullable=False)
    source: Mapped[str] = mapped_column(String(64), default="user", nullable=False)
    rule: Mapped[str | None] = mapped_column(Text(), default=None)
    active: Mapped[bool] = mapped_column(Boolean(), default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    members_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(), default=None)


class UniverseMemberRow(Base):
    """SQLite row for one universe constituent."""

    __tablename__ = "universe_members"
    __table_args__ = (UniqueConstraint("universe_id", "symbol", name="uq_universe_member"),)

    id: Mapped[int] = mapped_column(Integer(), primary_key=True, autoincrement=True)
    universe_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("universes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str | None] = mapped_column(String(256), default=None)
    sector: Mapped[str | None] = mapped_column(String(64), default=None)
    weight: Mapped[float | None] = mapped_column(Float(), default=None)
    added_on: Mapped[date | None] = mapped_column(Date(), default=None)
    removed_on: Mapped[date | None] = mapped_column(Date(), default=None)
    source_symbol: Mapped[str | None] = mapped_column(String(16), default=None)
    position: Mapped[int] = mapped_column(Integer(), default=0, nullable=False)


class UniverseSnapshotRow(Base):
    """A frozen, dated copy of a universe's membership."""

    __tablename__ = "universe_snapshots"
    __table_args__ = (UniqueConstraint("universe_id", "as_of", name="uq_universe_snapshot"),)

    id: Mapped[int] = mapped_column(Integer(), primary_key=True, autoincrement=True)
    universe_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("universes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    as_of: Mapped[date] = mapped_column(Date(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    source: Mapped[str] = mapped_column(String(64), default="manual", nullable=False)
    note: Mapped[str | None] = mapped_column(Text(), default=None)
    #: The construction rule in force when the snapshot was frozen. Stored on
    #: the snapshot rather than read from the universe, so a rule that is later
    #: edited cannot retroactively re-explain a change it did not cause.
    rule: Mapped[str | None] = mapped_column(Text(), default=None)
    members: Mapped[list[dict[str, Any]]] = mapped_column(JSON(), nullable=False)


class WatchlistRow(Base):
    """SQLite row for one watchlist."""

    __tablename__ = "watchlists"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    symbols: Mapped[list[str]] = mapped_column(JSON(), nullable=False, default=list)
    is_default: Mapped[bool] = mapped_column(Boolean(), default=False, nullable=False)
    #: Whether this watchlist is the *active scope* - what the topbar selector
    #: points at. Exactly one row across `universes` and `watchlists` may carry
    #: it; :meth:`UniverseRegistry.set_active_scope` is what enforces that.
    active: Mapped[bool] = mapped_column(Boolean(), default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)


_OWNED_TABLES = (
    UniverseRow.__table__,
    UniverseMemberRow.__table__,
    UniverseSnapshotRow.__table__,
    WatchlistRow.__table__,
)


def _active_symbols(members: Sequence[dict[str, Any]] | None) -> set[str]:
    """Symbols that were live in a stored snapshot (``removed_on`` unset)."""
    out: set[str] = set()
    for member in members or []:
        if member.get("removed_on"):
            continue
        symbol = normalize_symbol(str(member.get("symbol") or ""))
        if symbol:
            out.add(symbol)
    return out


async def detect_corporate_actions(
    added: Sequence[str],
    dropped: Sequence[str],
    *,
    edgar: Any | None = None,
) -> list[dict[str, Any]]:
    """Pair symbols that entered and left as the same SEC registrant.

    A ticker that disappears while another appears for the *same* CIK is a
    corporate action rather than a membership decision: a ticker change, a
    share-class consolidation (the reference's GOOG/GOOGL case) or a
    re-listing. Resolution uses SEC EDGAR's public ticker→CIK index, so this
    works with no API key and reports nothing at all when EDGAR is unreachable —
    an unverified pairing is never asserted.

    Args:
        added: symbols that entered the universe.
        dropped: symbols that left it.
        edgar: EDGAR provider to resolve with; built on demand when omitted.

    Returns:
        One mapping per detected pairing, with ``from``, ``to``, ``cik`` and a
        human-readable ``detail``.
    """
    if not added or not dropped:
        return []
    provider = edgar
    if provider is None:
        from tradinglib.data.providers import build_provider

        provider = build_provider("edgar")

    async def cik_of(symbol: str) -> tuple[str, str | None]:
        try:
            return symbol, await provider.resolve_cik(symbol)
        except Exception:  # noqa: BLE001 - an unresolvable ticker is simply not paired
            logger.debug("corporate actions: could not resolve %s", symbol, exc_info=True)
            return symbol, None

    resolved = await asyncio.gather(
        *(cik_of(symbol) for symbol in list(added) + list(dropped))
    )
    by_symbol = dict(resolved)
    dropped_by_cik: dict[str, list[str]] = {}
    for symbol in dropped:
        cik = by_symbol.get(symbol)
        if cik:
            dropped_by_cik.setdefault(cik, []).append(symbol)

    actions: list[dict[str, Any]] = []
    for symbol in added:
        cik = by_symbol.get(symbol)
        if not cik:
            continue
        partners = dropped_by_cik.get(cik)
        if not partners:
            continue
        previous = partners.pop(0)
        actions.append(
            {
                "from": previous,
                "to": symbol,
                "cik": cik,
                "detail": f"{previous}/{symbol} are the same SEC registrant (CIK {cik})",
            }
        )
    return actions


def ensure_schema() -> None:
    """Create the universe/watchlist tables if they do not exist yet, then migrate."""
    Base.metadata.create_all(get_engine(), tables=list(_OWNED_TABLES))
    for table in _OWNED_TABLES:
        ensure_columns(table)


# --------------------------------------------------------------------------
# constituent sources
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ConstituentSource:
    """Where a built-in index universe gets its real constituent list."""

    universe_id: str
    name: str
    url: str
    table_id: str
    symbol_column: str
    name_column: str
    sector_column: str | None = None
    added_column: str | None = None
    description: str = ""


#: The two index universes TradingLib fetches. Wikipedia's constituent tables are
#: maintained continuously and are the source the SPEC names for this job.
CONSTITUENT_SOURCES: Final[dict[str, ConstituentSource]] = {
    "sp500": ConstituentSource(
        universe_id="sp500",
        name="S&P 500",
        url="https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
        table_id="constituents",
        symbol_column="Symbol",
        name_column="Security",
        sector_column="GICS Sector",
        added_column="Date added",
        description="S&P 500 index constituents (Wikipedia constituent table).",
    ),
    "nasdaq100": ConstituentSource(
        universe_id="nasdaq100",
        name="Nasdaq-100",
        url="https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies",
        table_id="constituents",
        symbol_column="Ticker",
        name_column="Company",
        sector_column="ICB Industry",
        description="Nasdaq-100 index constituents (Wikipedia constituent table).",
    ),
}


def _default_user_agent() -> str:
    """Return a contactable User-Agent, as Wikipedia's API policy requires."""
    from tradinglib.config import get_settings

    return f"TradingLib/0.1 constituent-fetch ({get_settings().edgar_user_agent})"


def _cell_text(cell: Any) -> str:
    """Return the visible text of a table cell, without footnote markers."""
    text = cell.text_content()
    text = text.replace(" ", " ")
    text = re.sub(r"\[\w+\]", "", text)
    return text.strip()


def parse_wikipedia_table(html: str, table_id: str) -> list[dict[str, str]]:
    """Parse a Wikipedia table into a list of ``{header: cell}`` mappings.

    Args:
        html: the page source.
        table_id: the ``id`` attribute of the table (Wikipedia's constituent
            tables carry ``id="constituents"``).

    Raises:
        ValueError: when the table or its header row cannot be found - a layout
            change must fail loudly rather than yield an empty universe.
    """
    import lxml.html  # imported lazily: only the fetch path needs the parser

    doc = lxml.html.fromstring(html)
    tables = doc.xpath(f"//table[@id={table_id!r}]")
    if not tables:
        raise ValueError(f"no table with id={table_id!r} in the fetched page")
    table = tables[0]
    rows = table.xpath(".//tr")
    if not rows:
        raise ValueError(f"table id={table_id!r} has no rows")
    headers = [_cell_text(th) for th in rows[0].xpath("./th")]
    if not headers:
        raise ValueError(f"table id={table_id!r} has no header row")

    out: list[dict[str, str]] = []
    for row in rows[1:]:
        cells = row.xpath("./td")
        if not cells:
            continue
        values = [_cell_text(td) for td in cells]
        record = {
            header: values[index] for index, header in enumerate(headers) if index < len(values)
        }
        if record:
            out.append(record)
    return out


def _column(record: dict[str, str], wanted: str) -> str | None:
    """Return a column value, tolerating small header variations."""
    if wanted in record:
        return record[wanted]
    target = wanted.lower().replace(" ", "")
    for key, value in record.items():
        if key.lower().replace(" ", "").startswith(target):
            return value
    return None


def constituents_from_records(
    source: ConstituentSource, records: Sequence[dict[str, str]]
) -> list[UniverseMember]:
    """Map parsed table rows to :class:`UniverseMember` objects.

    Rows whose ticker cannot be normalised are skipped with a warning rather
    than guessed at; the caller sees the resulting count and can tell.
    """
    members: list[UniverseMember] = []
    seen: set[str] = set()
    for record in records:
        raw_symbol = _column(record, source.symbol_column)
        if not raw_symbol:
            continue
        try:
            symbol = normalize_symbol(raw_symbol)
        except ValueError:
            logger.warning("skipping unparsable ticker %r from %s", raw_symbol, source.url)
            continue
        if symbol in seen:
            continue
        seen.add(symbol)
        sector = _column(record, source.sector_column) if source.sector_column else None
        added = _column(record, source.added_column) if source.added_column else None
        members.append(
            UniverseMember(
                symbol=symbol,
                name=(_column(record, source.name_column) or None),
                sector=sector or None,
                added_on=_as_date(added),
                source_symbol=raw_symbol.strip() or None,
            )
        )
    return members


async def fetch_constituents(
    source: ConstituentSource,
    *,
    client: httpx.AsyncClient | None = None,
    user_agent: str | None = None,
    timeout: float = FETCH_TIMEOUT,
) -> list[UniverseMember]:
    """Fetch and parse the real constituent list for ``source``.

    Raises:
        ProviderUnavailable: when the page cannot be fetched or parsed.
    """
    headers = {"User-Agent": user_agent or _default_user_agent()}
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=timeout, follow_redirects=True)
    try:
        response = await http.get(source.url, headers=headers)
        response.raise_for_status()
        html = response.text
    except httpx.HTTPError as exc:
        raise ProviderUnavailable(
            source.universe_id, f"could not fetch {source.url}: {exc}"
        ) from exc
    finally:
        if owns_client:
            await http.aclose()

    try:
        records = await asyncio.to_thread(parse_wikipedia_table, html, source.table_id)
    except ValueError as exc:
        raise ProviderUnavailable(source.universe_id, str(exc)) from exc

    members = constituents_from_records(source, records)
    if not members:
        raise ProviderUnavailable(
            source.universe_id, f"constituent table at {source.url} yielded no usable rows"
        )
    return members


@dataclass(frozen=True, slots=True)
class CachedConstituents:
    """A constituent list read back from the on-disk cache."""

    universe_id: str
    fetched_at: datetime
    source_url: str
    members: list[UniverseMember]

    @property
    def age(self) -> timedelta:
        """How long ago the cached list was fetched."""
        return utcnow() - self.fetched_at


class ConstituentCache:
    """JSON cache of fetched index constituents under ``<data_dir>/universes``.

    This is deliberately separate from :mod:`tradinglib.data.cache`: constituent
    lists are small, long-lived and must remain readable (and hand-editable)
    when the network is down, so they are stored as plain JSON.
    """

    def __init__(self, directory: Path | None = None) -> None:
        if directory is None:
            from tradinglib.config import get_settings

            directory = get_settings().data_dir / "universes"
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def path(self, universe_id: str) -> Path:
        """Return the cache file path for ``universe_id``."""
        return self.directory / f"{_slugify(universe_id)}.json"

    def load(self, universe_id: str) -> CachedConstituents | None:
        """Return the cached constituents, or ``None`` when absent/unreadable."""
        path = self.path(universe_id)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            members = [UniverseMember.from_dict(item) for item in payload["members"]]
            fetched_at = to_utc(datetime.fromisoformat(payload["fetched_at"]))
        except Exception:  # noqa: BLE001 - a damaged cache file is simply a miss
            logger.warning("discarding unreadable constituent cache %s", path, exc_info=True)
            return None
        return CachedConstituents(
            universe_id=universe_id,
            fetched_at=fetched_at,
            source_url=str(payload.get("source_url", "")),
            members=members,
        )

    def save(
        self, universe_id: str, members: Sequence[UniverseMember], *, source_url: str
    ) -> CachedConstituents:
        """Persist a fetched constituent list and return the cache record."""
        fetched_at = utcnow()
        payload = {
            "universe": universe_id,
            "fetched_at": fetched_at.isoformat(),
            "source_url": source_url,
            "members": [m.as_dict() for m in members],
        }
        path = self.path(universe_id)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
        return CachedConstituents(
            universe_id=universe_id,
            fetched_at=fetched_at,
            source_url=source_url,
            members=list(members),
        )


def us_quality_50_members() -> list[UniverseMember]:
    """Return the US Quality 50 constituents as universe members."""
    return [
        UniverseMember(
            symbol=symbol, name=name, sector=sector, added_on=US_QUALITY_50_AS_OF
        )
        for symbol, name, sector in US_QUALITY_50
    ]


# --------------------------------------------------------------------------
# registries
# --------------------------------------------------------------------------
class UniverseRegistry:
    """CRUD and point-in-time access for universes."""

    def __init__(
        self,
        session_factory: sessionmaker[Session] | None = None,
        *,
        cache: ConstituentCache | None = None,
        user_agent: str | None = None,
    ) -> None:
        """Create the registry.

        Args:
            session_factory: SQLAlchemy session factory; defaults to the
                process-wide one from :mod:`tradinglib.db`.
            cache: constituent cache; defaults to ``<data_dir>/universes``.
            user_agent: User-Agent used for constituent fetches.
        """
        self._session_factory = session_factory
        self._cache = cache
        self._user_agent = user_agent
        self._watchlists: WatchlistRegistry | None = None
        ensure_schema()

    # --- plumbing ---------------------------------------------------------
    @contextmanager
    def _session(self) -> Iterator[Session]:
        """Transactional session scope bound to this registry."""
        factory = self._session_factory or get_session_factory()
        session = factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @property
    def watchlists(self) -> WatchlistRegistry:
        """The watchlist registry used to resolve ``watchlist:`` references."""
        if self._watchlists is None:
            self._watchlists = WatchlistRegistry(self._session_factory)
        return self._watchlists

    @property
    def cache(self) -> ConstituentCache:
        """The constituent cache, created on first use."""
        if self._cache is None:
            self._cache = ConstituentCache()
        return self._cache

    @staticmethod
    def _member_from_row(row: UniverseMemberRow) -> UniverseMember:
        """Map a member row to the dataclass."""
        return UniverseMember(
            symbol=row.symbol,
            name=row.name,
            sector=row.sector,
            weight=row.weight,
            added_on=row.added_on,
            removed_on=row.removed_on,
            source_symbol=row.source_symbol,
        )

    def _load(self, session: Session, universe_id: str, *, with_members: bool = True) -> Universe:
        """Load one universe (and optionally its members) inside a session."""
        row = session.get(UniverseRow, universe_id)
        if row is None:
            raise KeyError(f"unknown universe {universe_id!r}")
        members: list[UniverseMember] = []
        if with_members:
            member_rows = session.scalars(
                select(UniverseMemberRow)
                .where(UniverseMemberRow.universe_id == universe_id)
                .order_by(UniverseMemberRow.position, UniverseMemberRow.symbol)
            ).all()
            members = [self._member_from_row(r) for r in member_rows]
        return Universe(
            id=row.id,
            name=row.name,
            description=row.description,
            builtin=row.builtin,
            members=members,
            updated_at=_aware(row.updated_at),
            created_at=_aware(row.created_at),
            source=row.source,
            rule=row.rule,
            active=row.active,
            members_fetched_at=_aware(row.members_fetched_at),
        )

    @staticmethod
    def _normalise_members(members: Iterable[UniverseMember | str | dict[str, Any]]) -> list[
        UniverseMember
    ]:
        """Coerce symbols/dicts/members into deduplicated members."""
        out: list[UniverseMember] = []
        seen: set[str] = set()
        for item in members:
            if isinstance(item, UniverseMember):
                member = UniverseMember(
                    symbol=normalize_symbol(item.symbol),
                    name=item.name,
                    sector=item.sector,
                    weight=item.weight,
                    added_on=item.added_on,
                    removed_on=item.removed_on,
                    source_symbol=item.source_symbol,
                )
            elif isinstance(item, dict):
                member = UniverseMember.from_dict(item)
            else:
                member = UniverseMember(symbol=normalize_symbol(str(item)))
            if member.symbol in seen:
                continue
            seen.add(member.symbol)
            out.append(member)
        return out

    def _replace_members(
        self, session: Session, universe_id: str, members: Sequence[UniverseMember]
    ) -> None:
        """Replace the stored membership of a universe."""
        session.execute(
            delete(UniverseMemberRow).where(UniverseMemberRow.universe_id == universe_id)
        )
        for position, member in enumerate(members):
            session.add(
                UniverseMemberRow(
                    universe_id=universe_id,
                    symbol=member.symbol,
                    name=member.name,
                    sector=member.sector,
                    weight=member.weight,
                    added_on=member.added_on,
                    removed_on=member.removed_on,
                    source_symbol=member.source_symbol,
                    position=position,
                )
            )

    # --- built-ins --------------------------------------------------------
    def ensure_builtins(self) -> list[Universe]:
        """Create the built-in universe rows if they are missing.

        ``us-quality-50`` is fully materialised from its static definition.
        ``sp500``/``nasdaq100`` are materialised from the on-disk constituent
        cache when one exists; otherwise the row is created empty and reports
        ``member_count == 0`` until :meth:`refresh_builtin` fetches the real
        list. Nothing is invented to fill the gap.
        """
        now = utcnow()
        with self._session() as session:
            if session.get(UniverseRow, "us-quality-50") is None:
                session.add(
                    UniverseRow(
                        id="us-quality-50",
                        name="US Quality 50",
                        description=(
                            "50 US large-cap quality compounders, curated "
                            f"{US_QUALITY_50_AS_OF.isoformat()}."
                        ),
                        builtin=True,
                        source="builtin-static",
                        rule=US_QUALITY_50_RULE,
                        created_at=now,
                        updated_at=now,
                        members_fetched_at=now,
                    )
                )
                self._replace_members(session, "us-quality-50", us_quality_50_members())

            for source in CONSTITUENT_SOURCES.values():
                if session.get(UniverseRow, source.universe_id) is not None:
                    continue
                cached = self.cache.load(source.universe_id)
                session.add(
                    UniverseRow(
                        id=source.universe_id,
                        name=source.name,
                        description=source.description,
                        builtin=True,
                        source=f"wikipedia:{source.table_id}",
                        rule=f"Index constituents as published at {source.url}",
                        created_at=now,
                        updated_at=now,
                        members_fetched_at=cached.fetched_at if cached else None,
                    )
                )
                if cached is not None:
                    self._replace_members(session, source.universe_id, cached.members)

            has_active = session.scalar(
                select(UniverseRow.id).where(UniverseRow.active.is_(True)).limit(1)
            )
            if has_active is None:
                from tradinglib.config import get_settings

                preferred = get_settings().default_universe
                target = session.get(UniverseRow, preferred) or session.get(
                    UniverseRow, "us-quality-50"
                )
                if target is not None:
                    target.active = True

        return self.list_universes(include_members=False)

    async def refresh_builtin(
        self,
        universe_id: str,
        *,
        force: bool = False,
        max_age: timedelta = DEFAULT_MAX_CACHE_AGE,
        client: httpx.AsyncClient | None = None,
    ) -> Universe:
        """Rebuild a built-in universe from its real upstream constituent source.

        ``us-quality-50`` is re-materialised from its static definition (no
        network involved). The index universes are fetched from Wikipedia; a
        cached list younger than ``max_age`` is reused unless ``force`` is set,
        and if the fetch fails while a cache exists the cached list is used and
        the failure is logged - the stored ``members_fetched_at`` then keeps
        telling the truth about how old the membership is.

        Raises:
            KeyError: when ``universe_id`` is not a built-in.
            ProviderUnavailable: when the fetch fails and no cache exists.
        """
        self.ensure_builtins()
        if universe_id == "us-quality-50":
            with self._session() as session:
                row = session.get(UniverseRow, "us-quality-50")
                if row is None:  # pragma: no cover - ensure_builtins just created it
                    raise KeyError("unknown universe 'us-quality-50'")
                self._replace_members(session, "us-quality-50", us_quality_50_members())
                row.rule = US_QUALITY_50_RULE
                row.updated_at = utcnow()
                row.members_fetched_at = utcnow()
            return self.get("us-quality-50")

        source = CONSTITUENT_SOURCES.get(universe_id)
        if source is None:
            raise KeyError(f"{universe_id!r} is not a built-in universe")

        cached = self.cache.load(universe_id)
        fresh_enough = cached is not None and cached.age <= max_age
        if cached is not None and fresh_enough and not force:
            members, fetched_at = cached.members, cached.fetched_at
        else:
            try:
                fetched = await fetch_constituents(
                    source, client=client, user_agent=self._user_agent
                )
            except ProviderUnavailable:
                if cached is None:
                    raise
                logger.warning(
                    "constituent fetch for %s failed; using cache from %s",
                    universe_id,
                    cached.fetched_at.isoformat(),
                    exc_info=True,
                )
                members, fetched_at = cached.members, cached.fetched_at
            else:
                record = self.cache.save(universe_id, fetched, source_url=source.url)
                members, fetched_at = record.members, record.fetched_at

        with self._session() as session:
            row = session.get(UniverseRow, universe_id)
            if row is None:  # pragma: no cover - ensure_builtins just created it
                raise KeyError(f"unknown universe {universe_id!r}")
            self._replace_members(session, universe_id, members)
            row.updated_at = utcnow()
            row.members_fetched_at = fetched_at
        return self.get(universe_id)

    def unpopulated_builtins(self) -> list[str]:
        """Return the built-in universes that currently hold no members.

        ``us-quality-50`` is materialised from a static list and is never in
        here; the index universes are, until their constituent list has been
        fetched at least once.
        """
        empty: list[str] = []
        with self._session() as session:
            for universe_id in BUILTIN_UNIVERSES:
                row = session.get(UniverseRow, universe_id)
                if row is None:
                    continue
                count = session.scalar(
                    select(func.count())
                    .select_from(UniverseMemberRow)
                    .where(UniverseMemberRow.universe_id == universe_id)
                )
                if not count:
                    empty.append(universe_id)
        return empty

    async def seed_unpopulated_builtins(
        self, *, client: httpx.AsyncClient | None = None
    ) -> dict[str, str]:
        """Fetch the constituent list of any built-in that has no members yet.

        A universe the selector offers but that resolves to nothing reads as a
        broken app rather than as an unfetched list, so the first start pulls
        the real constituents once. Already-populated universes are left alone
        (this is not a refresh), and a vendor failure is reported per universe
        instead of aborting startup - the universe simply stays empty and the
        Market & Data page's "Refresh members" action remains the way to fix it.

        Args:
            client: HTTP client to fetch with; one is opened and closed here
                when it is not supplied.

        Returns:
            ``{universe_id: "<n> members" | "error: ..."}`` for the universes
            that were empty; an empty mapping when there was nothing to do.
        """
        targets = self.unpopulated_builtins()
        if not targets:
            return {}
        out: dict[str, str] = {}
        owned = client is None
        http = client or httpx.AsyncClient(timeout=FETCH_TIMEOUT, follow_redirects=True)
        try:
            for universe_id in targets:
                try:
                    universe = await self.refresh_builtin(universe_id, client=http)
                except Exception as exc:  # noqa: BLE001 - reported, never fatal
                    logger.warning(
                        "could not seed built-in universe %s", universe_id, exc_info=True
                    )
                    out[universe_id] = f"error: {exc}"
                else:
                    out[universe_id] = f"{len(universe.members)} members"
                    logger.info(
                        "seeded built-in universe %s with %d members",
                        universe_id,
                        len(universe.members),
                    )
        finally:
            if owned:
                await http.aclose()
        return out

    async def refresh_all_builtins(
        self, *, force: bool = False, max_age: timedelta = DEFAULT_MAX_CACHE_AGE
    ) -> dict[str, str]:
        """Refresh every built-in, reporting per-universe outcomes honestly.

        Returns a mapping ``universe_id -> "<n> members"`` or ``"error: ..."``;
        one failing vendor never aborts the others.
        """
        out: dict[str, str] = {}
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT, follow_redirects=True) as client:
            for universe_id in BUILTIN_UNIVERSES:
                try:
                    universe = await self.refresh_builtin(
                        universe_id, force=force, max_age=max_age, client=client
                    )
                except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                    logger.warning("refresh of %s failed", universe_id, exc_info=True)
                    out[universe_id] = f"error: {exc}"
                else:
                    out[universe_id] = f"{len(universe.members)} members"
        return out

    # --- CRUD -------------------------------------------------------------
    def list_universes(self, *, include_members: bool = True) -> list[Universe]:
        """Return every universe, built-ins first, then alphabetically."""
        with self._session() as session:
            ids = list(
                session.scalars(
                    select(UniverseRow.id).order_by(
                        UniverseRow.builtin.desc(), UniverseRow.name
                    )
                ).all()
            )
            return [self._load(session, uid, with_members=include_members) for uid in ids]

    def get(self, universe_id: str) -> Universe:
        """Return one universe by id.

        Raises:
            KeyError: when the id is unknown.
        """
        with self._session() as session:
            return self._load(session, universe_id)

    def exists(self, universe_id: str) -> bool:
        """Whether a universe with this id is stored."""
        with self._session() as session:
            return session.get(UniverseRow, universe_id) is not None

    def create(
        self,
        name: str,
        symbols: Sequence[str] | Sequence[UniverseMember] | Sequence[dict[str, Any]],
        *,
        description: str | None = None,
        universe_id: str | None = None,
        source: str = "user",
        rule: str | None = None,
    ) -> Universe:
        """Create a user universe from an explicit symbol list.

        Raises:
            ValueError: when the name is empty or the id is already taken.
        """
        label = name.strip()
        if not label:
            raise ValueError("universe name must not be empty")
        members = self._normalise_members(symbols)
        now = utcnow()
        with self._session() as session:
            new_id = universe_id or self._unique_id(session, _slugify(label))
            if session.get(UniverseRow, new_id) is not None:
                raise ValueError(f"universe {new_id!r} already exists")
            session.add(
                UniverseRow(
                    id=new_id,
                    name=label,
                    description=description,
                    builtin=False,
                    source=source,
                    rule=rule,
                    created_at=now,
                    updated_at=now,
                    members_fetched_at=now,
                )
            )
            self._replace_members(session, new_id, members)
        return self.get(new_id)

    @staticmethod
    def _unique_id(session: Session, base: str) -> str:
        """Return ``base`` or ``base-2``/``base-3``... if it is taken."""
        candidate = base
        suffix = 2
        while session.get(UniverseRow, candidate) is not None:
            candidate = f"{base}-{suffix}"
            suffix += 1
        return candidate

    def update(self, universe_id: str, **changes: Any) -> Universe:
        """Update a user universe's name, description or membership.

        Accepted keys: ``name``, ``description``, ``rule``, ``symbols`` and
        ``members``.

        Raises:
            KeyError: when the universe is unknown.
            ValueError: on a built-in universe or an unknown key.
        """
        allowed = {"name", "description", "rule", "symbols", "members"}
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unknown universe fields: {sorted(unknown)}")
        with self._session() as session:
            row = session.get(UniverseRow, universe_id)
            if row is None:
                raise KeyError(f"unknown universe {universe_id!r}")
            if row.builtin:
                raise ValueError(
                    f"{universe_id!r} is built-in; use refresh_builtin() to update it"
                )
            if "name" in changes:
                label = str(changes["name"]).strip()
                if not label:
                    raise ValueError("universe name must not be empty")
                row.name = label
            if "description" in changes:
                row.description = changes["description"]
            if "rule" in changes:
                row.rule = changes["rule"]
            payload = changes.get("members", changes.get("symbols"))
            if payload is not None:
                members = self._normalise_members(payload)
                self._replace_members(session, universe_id, members)
                row.members_fetched_at = utcnow()
            row.updated_at = utcnow()
        return self.get(universe_id)

    def delete(self, universe_id: str) -> None:
        """Delete a user universe. Built-ins cannot be deleted.

        Raises:
            KeyError: when the universe is unknown.
            ValueError: when it is built-in.
        """
        with self._session() as session:
            row = session.get(UniverseRow, universe_id)
            if row is None:
                raise KeyError(f"unknown universe {universe_id!r}")
            if row.builtin:
                raise ValueError(f"built-in universe {universe_id!r} cannot be deleted")
            session.execute(
                delete(UniverseMemberRow).where(UniverseMemberRow.universe_id == universe_id)
            )
            session.execute(
                delete(UniverseSnapshotRow).where(UniverseSnapshotRow.universe_id == universe_id)
            )
            session.delete(row)

    # --- membership -------------------------------------------------------
    def membership(self, universe_id: str, *, as_of: date | None = None) -> dict[str, Any]:
        """Resolve membership, point-in-time when ``as_of`` is given.

        Resolution order for a dated request:

        1. the newest snapshot taken on or before ``as_of`` (exact PIT truth);
        2. otherwise the current membership filtered by each member's
           ``added_on``/``removed_on`` window.

        The returned mapping names which of the two was used (``basis``) so the
        UI can show whether a date is genuinely point-in-time or reconstructed.
        """
        with self._session() as session:
            universe = self._load(session, universe_id)
            if as_of is None:
                return {
                    "universe": universe_id,
                    "as_of": None,
                    "basis": "current",
                    "snapshot_as_of": None,
                    "members": [m.as_dict() for m in universe.members],
                }
            snapshot = session.scalars(
                select(UniverseSnapshotRow)
                .where(
                    UniverseSnapshotRow.universe_id == universe_id,
                    UniverseSnapshotRow.as_of <= as_of,
                )
                .order_by(UniverseSnapshotRow.as_of.desc())
                .limit(1)
            ).first()
            if snapshot is not None:
                members = [UniverseMember.from_dict(item) for item in snapshot.members]
                return {
                    "universe": universe_id,
                    "as_of": as_of.isoformat(),
                    "basis": "snapshot",
                    "snapshot_as_of": snapshot.as_of.isoformat(),
                    "members": [m.as_dict() for m in members],
                }
            members = [m for m in universe.members if m.active_on(as_of)]
            return {
                "universe": universe_id,
                "as_of": as_of.isoformat(),
                "basis": "membership-dates",
                "snapshot_as_of": None,
                "members": [m.as_dict() for m in members],
            }

    def members(self, universe_id: str, *, as_of: date | None = None) -> list[UniverseMember]:
        """Return membership, point-in-time when ``as_of`` is given."""
        payload = self.membership(universe_id, as_of=as_of)
        return [UniverseMember.from_dict(item) for item in payload["members"]]

    def symbols(self, universe_id: str, *, as_of: date | None = None) -> list[str]:
        """Return member symbols, point-in-time when ``as_of`` is given."""
        return [m.symbol for m in self.members(universe_id, as_of=as_of)]

    def resolve(self, ref: str, *, as_of: date | None = None) -> list[str]:
        """Resolve a scope reference to a concrete symbol list.

        Accepted forms:

        * ``"universe:sp500"`` / ``"watchlist:my-list"`` - explicit kind;
        * ``"sp500"`` - a universe id, then a watchlist id;
        * ``"AAPL,MSFT"`` - an ad-hoc comma separated symbol list.

        Raises:
            KeyError: when nothing matches the reference.
        """
        text = ref.strip()
        if not text:
            raise KeyError("empty universe reference")
        kind, _, rest = text.partition(":")
        kind = kind.strip().lower()
        rest = rest.strip()
        if kind == "universe" and rest:
            return self.symbols(rest, as_of=as_of)
        if kind == "watchlist" and rest:
            return self.watchlists.get(rest).symbols
        if self.exists(text):
            return self.symbols(text, as_of=as_of)
        try:
            return self.watchlists.get(text).symbols
        except KeyError:
            pass
        if "," in text or _SYMBOL_RE.match(text.upper()):
            symbols: list[str] = []
            for part in text.split(","):
                part = part.strip()
                if part:
                    symbols.append(normalize_symbol(part))
            if symbols:
                return symbols
        raise KeyError(f"cannot resolve universe reference {ref!r}")

    # --- snapshots --------------------------------------------------------
    def snapshot(
        self,
        universe_id: str,
        *,
        as_of: date | None = None,
        source: str = "manual",
        note: str | None = None,
    ) -> dict[str, Any]:
        """Freeze current membership as a dated snapshot and return its record.

        Re-snapshotting the same date replaces that day's record, so a daily
        scheduler can run repeatedly without piling up duplicates.
        """
        stamp = as_of or utcnow().date()
        with self._session() as session:
            universe = self._load(session, universe_id)
            payload = [m.as_dict() for m in universe.members]
            existing = session.scalars(
                select(UniverseSnapshotRow).where(
                    UniverseSnapshotRow.universe_id == universe_id,
                    UniverseSnapshotRow.as_of == stamp,
                )
            ).first()
            now = utcnow()
            if existing is None:
                existing = UniverseSnapshotRow(
                    universe_id=universe_id,
                    as_of=stamp,
                    created_at=now,
                    source=source,
                    note=note,
                    rule=universe.rule,
                    members=payload,
                )
                session.add(existing)
            else:
                existing.created_at = now
                existing.source = source
                existing.note = note
                existing.rule = universe.rule
                existing.members = payload
            session.flush()
            return {
                "universe": universe_id,
                "as_of": stamp.isoformat(),
                "created_at": now.isoformat(),
                "source": source,
                "note": note,
                "rule": universe.rule,
                "symbol_count": len(payload),
            }

    def snapshots(self, universe_id: str) -> list[dict[str, Any]]:
        """List stored snapshots for a universe, newest first."""
        with self._session() as session:
            rows = session.scalars(
                select(UniverseSnapshotRow)
                .where(UniverseSnapshotRow.universe_id == universe_id)
                .order_by(UniverseSnapshotRow.as_of.desc())
            ).all()
            return [
                {
                    "universe": row.universe_id,
                    "as_of": row.as_of.isoformat(),
                    "created_at": to_utc(row.created_at).isoformat(),
                    "source": row.source,
                    "note": row.note,
                    "rule": row.rule,
                    "symbol_count": len(row.members),
                }
                for row in rows
            ]

    def change_log(self, universe_id: str, *, limit: int = 6) -> list[dict[str, Any]]:
        """Return the add/drop diff between consecutive snapshots, newest first.

        Each entry names the symbols that entered and left between one snapshot
        and the next, and attributes the change to the construction rule that
        was in force at the later snapshot (stored on the snapshot itself) and
        to the provenance recorded for that snapshot. The oldest snapshot has no
        predecessor, so it is reported as the baseline rather than as an
        all-names addition.

        Args:
            universe_id: universe to report on.
            limit: how many diffs to return.

        Returns:
            One mapping per transition, each with ``as_of``, ``previous_as_of``,
            ``added``, ``dropped``, ``rule``, ``rule_changed``, ``source`` and
            ``symbol_count``.
        """
        with self._session() as session:
            rows = list(
                session.scalars(
                    select(UniverseSnapshotRow)
                    .where(UniverseSnapshotRow.universe_id == universe_id)
                    .order_by(UniverseSnapshotRow.as_of)
                ).all()
            )
            frames = [
                (
                    row.as_of,
                    row.source,
                    row.rule,
                    row.note,
                    _active_symbols(row.members),
                )
                for row in rows
            ]

        entries: list[dict[str, Any]] = []
        for index in range(1, len(frames)):
            as_of, source, rule, note, symbols = frames[index]
            prev_as_of, _prev_source, prev_rule, _prev_note, prev_symbols = frames[index - 1]
            added = sorted(symbols - prev_symbols)
            dropped = sorted(prev_symbols - symbols)
            entries.append(
                {
                    "as_of": as_of.isoformat(),
                    "previous_as_of": prev_as_of.isoformat(),
                    "added": added,
                    "dropped": dropped,
                    "rule": rule,
                    # A rule edit between two snapshots is itself an explanation
                    # for the diff, and the UI says so instead of blaming data.
                    "rule_changed": rule != prev_rule,
                    "previous_rule": prev_rule,
                    "source": source,
                    "note": note,
                    "symbol_count": len(symbols),
                    "net": len(symbols) - len(prev_symbols),
                }
            )
        if frames:
            first_as_of, first_source, first_rule, first_note, first_symbols = frames[0]
            # Oldest-first while building; the reverse below puts the baseline
            # at the bottom of a newest-first list, where it belongs.
            entries.insert(
                0,
                {
                    "as_of": first_as_of.isoformat(),
                    "previous_as_of": None,
                    "added": [],
                    "dropped": [],
                    "rule": first_rule,
                    "rule_changed": False,
                    "previous_rule": None,
                    "source": first_source,
                    "note": first_note,
                    "symbol_count": len(first_symbols),
                    "net": 0,
                    "baseline": True,
                },
            )
        entries.reverse()
        return entries[:limit] if limit and limit > 0 else entries

    def rule_revisions(self, universe_id: str) -> list[dict[str, Any]]:
        """Return the distinct construction rules the snapshots were taken under.

        This is what makes "rule def v3" a real version number: it counts the
        rule texts actually recorded in the history, oldest first.
        """
        with self._session() as session:
            rows = list(
                session.scalars(
                    select(UniverseSnapshotRow)
                    .where(UniverseSnapshotRow.universe_id == universe_id)
                    .order_by(UniverseSnapshotRow.as_of)
                ).all()
            )
        revisions: list[dict[str, Any]] = []
        for row in rows:
            if revisions and revisions[-1]["rule"] == row.rule:
                revisions[-1]["until"] = row.as_of.isoformat()
                continue
            revisions.append(
                {
                    "version": len(revisions) + 1,
                    "rule": row.rule,
                    "since": row.as_of.isoformat(),
                    "until": row.as_of.isoformat(),
                }
            )
        return revisions

    def snapshot_dates(self, universe_id: str) -> list[date]:
        """Return the dates a universe has snapshots for, oldest first."""
        with self._session() as session:
            return list(
                session.scalars(
                    select(UniverseSnapshotRow.as_of)
                    .where(UniverseSnapshotRow.universe_id == universe_id)
                    .order_by(UniverseSnapshotRow.as_of)
                ).all()
            )

    def delete_snapshot(self, universe_id: str, as_of: date) -> bool:
        """Delete one snapshot. Returns whether a row was removed."""
        with self._session() as session:
            result = session.execute(
                delete(UniverseSnapshotRow).where(
                    UniverseSnapshotRow.universe_id == universe_id,
                    UniverseSnapshotRow.as_of == as_of,
                )
            )
            return bool(result.rowcount)

    # --- active universe --------------------------------------------------
    def set_active(self, universe_id: str) -> Universe:
        """Make ``universe_id`` the active scope.

        Any active watchlist is deselected: the topbar selector points at one
        collection, so exactly one row across both tables carries the flag.

        Raises:
            KeyError: when the universe is unknown.
        """
        with self._session() as session:
            row = session.get(UniverseRow, universe_id)
            if row is None:
                raise KeyError(f"unknown universe {universe_id!r}")
            for other in session.scalars(
                select(UniverseRow).where(UniverseRow.active.is_(True))
            ).all():
                other.active = False
            for listed in session.scalars(
                select(WatchlistRow).where(WatchlistRow.active.is_(True))
            ).all():
                listed.active = False
            row.active = True
        return self.get(universe_id)

    def set_active_watchlist(self, watchlist_id: str) -> Watchlist:
        """Make a watchlist the active scope, deselecting any active universe.

        The reference's topbar offers watchlists beside the universes
        ("Watchlist: Semis"), and the selection has to mean the same thing for
        both: it is what the tape streams and what an unscoped screen runs over.

        Raises:
            KeyError: when the watchlist is unknown.
        """
        with self._session() as session:
            row = session.get(WatchlistRow, watchlist_id)
            if row is None:
                raise KeyError(f"unknown watchlist {watchlist_id!r}")
            for other in session.scalars(
                select(WatchlistRow).where(WatchlistRow.active.is_(True))
            ).all():
                other.active = False
            for universe in session.scalars(
                select(UniverseRow).where(UniverseRow.active.is_(True))
            ).all():
                universe.active = False
            row.active = True
        return self.watchlists.get(watchlist_id)

    def set_active_scope(self, ref: str) -> dict[str, Any]:
        """Select the active scope from a universe id or a watchlist reference.

        Accepts the same reference forms as :meth:`resolve` for the two kinds
        that can be *selected*: ``"sp500"``, ``"universe:sp500"``,
        ``"watchlist:primary-research"``. An ad-hoc symbol list is not a
        collection and cannot be made active.

        Raises:
            KeyError: when the reference names nothing selectable.
        """
        text = str(ref or "").strip()
        if not text:
            raise KeyError("empty scope reference")
        kind, _, rest = text.partition(":")
        kind = kind.strip().lower()
        rest = rest.strip()
        if kind == "universe" and rest:
            self.set_active(rest)
            return self.active_scope()
        if kind == "watchlist" and rest:
            self.set_active_watchlist(rest)
            return self.active_scope()
        if self.exists(text):
            self.set_active(text)
            return self.active_scope()
        self.set_active_watchlist(text)
        return self.active_scope()

    def active_universe_id(self) -> str | None:
        """Return the active *universe* id, or ``None``.

        ``None`` when a watchlist is the active scope: that is the honest
        answer for a caller that specifically needs a universe. Callers that
        want "whatever is selected" want :meth:`active_scope`.
        """
        with self._session() as session:
            return session.scalar(
                select(UniverseRow.id).where(UniverseRow.active.is_(True)).limit(1)
            )

    def active_watchlist_id(self) -> str | None:
        """Return the active watchlist id, or ``None`` when none is selected."""
        with self._session() as session:
            return session.scalar(
                select(WatchlistRow.id).where(WatchlistRow.active.is_(True)).limit(1)
            )

    def get_active(self) -> Universe | None:
        """Return the active universe, or ``None`` when none is set."""
        universe_id = self.active_universe_id()
        return None if universe_id is None else self.get(universe_id)

    def active_scope(self) -> dict[str, Any]:
        """Return the selected collection, whichever kind it is.

        Returns:
            ``{"kind": "universe"|"watchlist"|None, "id", "name", "ref",
            "symbols", "symbol_count"}``. ``kind`` is ``None`` when nothing has
            been selected yet, which the UI reports rather than defaulting.
        """
        universe_id = self.active_universe_id()
        if universe_id is not None:
            universe = self.get(universe_id)
            symbols = universe.symbols()
            return {
                "kind": "universe",
                "id": universe.id,
                "name": universe.name,
                "ref": f"universe:{universe.id}",
                "symbols": symbols,
                "symbol_count": len(symbols),
            }
        watchlist_id = self.active_watchlist_id()
        if watchlist_id is not None:
            try:
                watchlist = self.watchlists.get(watchlist_id)
            except KeyError:
                logger.warning("active watchlist %s no longer exists", watchlist_id)
            else:
                return {
                    "kind": "watchlist",
                    "id": watchlist.id,
                    "name": watchlist.name,
                    "ref": f"watchlist:{watchlist.id}",
                    "symbols": list(watchlist.symbols),
                    "symbol_count": len(watchlist.symbols),
                }
        return {
            "kind": None,
            "id": None,
            "name": None,
            "ref": None,
            "symbols": [],
            "symbol_count": 0,
        }


class WatchlistRegistry:
    """CRUD for user watchlists (the Overview page's watchlist panel)."""

    def __init__(self, session_factory: sessionmaker[Session] | None = None) -> None:
        self._session_factory = session_factory
        ensure_schema()

    @contextmanager
    def _session(self) -> Iterator[Session]:
        """Transactional session scope bound to this registry."""
        factory = self._session_factory or get_session_factory()
        session = factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @staticmethod
    def _to_dataclass(row: WatchlistRow) -> Watchlist:
        """Map a watchlist row to the dataclass."""
        return Watchlist(
            id=row.id,
            name=row.name,
            symbols=list(row.symbols or []),
            is_default=row.is_default,
            active=bool(row.active),
            created_at=_aware(row.created_at),
            updated_at=_aware(row.updated_at),
        )

    @staticmethod
    def _clean_symbols(symbols: Iterable[str]) -> list[str]:
        """Normalise and deduplicate a symbol list, preserving order."""
        out: list[str] = []
        for raw in symbols:
            symbol = normalize_symbol(raw)
            if symbol not in out:
                out.append(symbol)
        return out

    def list_watchlists(self) -> list[Watchlist]:
        """Return every watchlist, the default one first."""
        with self._session() as session:
            rows = session.scalars(
                select(WatchlistRow).order_by(WatchlistRow.is_default.desc(), WatchlistRow.name)
            ).all()
            return [self._to_dataclass(row) for row in rows]

    def get(self, watchlist_id: str) -> Watchlist:
        """Return one watchlist.

        Raises:
            KeyError: when the id is unknown.
        """
        with self._session() as session:
            row = session.get(WatchlistRow, watchlist_id)
            if row is None:
                raise KeyError(f"unknown watchlist {watchlist_id!r}")
            return self._to_dataclass(row)

    def create(
        self,
        name: str,
        symbols: Sequence[str] = (),
        *,
        watchlist_id: str | None = None,
        is_default: bool = False,
    ) -> Watchlist:
        """Create a watchlist.

        Raises:
            ValueError: when the name is empty or the id is taken.
        """
        label = name.strip()
        if not label:
            raise ValueError("watchlist name must not be empty")
        cleaned = self._clean_symbols(symbols)
        now = utcnow()
        with self._session() as session:
            base = watchlist_id or _slugify(label, fallback="watchlist")
            new_id = base
            suffix = 2
            while session.get(WatchlistRow, new_id) is not None:
                if watchlist_id is not None:
                    raise ValueError(f"watchlist {watchlist_id!r} already exists")
                new_id = f"{base}-{suffix}"
                suffix += 1
            first = session.scalar(select(WatchlistRow.id).limit(1)) is None
            row = WatchlistRow(
                id=new_id,
                name=label,
                symbols=cleaned,
                is_default=is_default or first,
                created_at=now,
                updated_at=now,
            )
            if row.is_default:
                for other in session.scalars(
                    select(WatchlistRow).where(WatchlistRow.is_default.is_(True))
                ).all():
                    other.is_default = False
            session.add(row)
        return self.get(new_id)

    def update(self, watchlist_id: str, **changes: Any) -> Watchlist:
        """Rename a watchlist or replace its symbols.

        Accepted keys: ``name``, ``symbols``, ``is_default``.

        Raises:
            KeyError: when the watchlist is unknown.
            ValueError: on an unknown key or an empty name.
        """
        allowed = {"name", "symbols", "is_default"}
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unknown watchlist fields: {sorted(unknown)}")
        with self._session() as session:
            row = session.get(WatchlistRow, watchlist_id)
            if row is None:
                raise KeyError(f"unknown watchlist {watchlist_id!r}")
            if "name" in changes:
                label = str(changes["name"]).strip()
                if not label:
                    raise ValueError("watchlist name must not be empty")
                row.name = label
            if "symbols" in changes:
                row.symbols = self._clean_symbols(changes["symbols"])
            if changes.get("is_default"):
                for other in session.scalars(
                    select(WatchlistRow).where(WatchlistRow.is_default.is_(True))
                ).all():
                    other.is_default = False
                row.is_default = True
            elif "is_default" in changes and not changes["is_default"]:
                row.is_default = False
            row.updated_at = utcnow()
        return self.get(watchlist_id)

    def delete(self, watchlist_id: str) -> None:
        """Delete a watchlist.

        Raises:
            KeyError: when the watchlist is unknown.
        """
        with self._session() as session:
            row = session.get(WatchlistRow, watchlist_id)
            if row is None:
                raise KeyError(f"unknown watchlist {watchlist_id!r}")
            was_default = row.is_default
            session.delete(row)
            if was_default:
                session.flush()
                replacement = session.scalars(
                    select(WatchlistRow).order_by(WatchlistRow.name).limit(1)
                ).first()
                if replacement is not None:
                    replacement.is_default = True

    def add_symbol(self, watchlist_id: str, symbol: str) -> Watchlist:
        """Append one symbol to a watchlist (no-op when already present)."""
        with self._session() as session:
            row = session.get(WatchlistRow, watchlist_id)
            if row is None:
                raise KeyError(f"unknown watchlist {watchlist_id!r}")
            symbols = list(row.symbols or [])
            cleaned = normalize_symbol(symbol)
            if cleaned not in symbols:
                symbols.append(cleaned)
                row.symbols = symbols
                row.updated_at = utcnow()
        return self.get(watchlist_id)

    def remove_symbol(self, watchlist_id: str, symbol: str) -> Watchlist:
        """Remove one symbol from a watchlist (no-op when absent)."""
        with self._session() as session:
            row = session.get(WatchlistRow, watchlist_id)
            if row is None:
                raise KeyError(f"unknown watchlist {watchlist_id!r}")
            cleaned = normalize_symbol(symbol)
            symbols = [s for s in (row.symbols or []) if s != cleaned]
            if len(symbols) != len(row.symbols or []):
                row.symbols = symbols
                row.updated_at = utcnow()
        return self.get(watchlist_id)

    def default_watchlist(self) -> Watchlist | None:
        """Return the default watchlist, or ``None`` when none exists."""
        with self._session() as session:
            row = session.scalars(
                select(WatchlistRow).where(WatchlistRow.is_default.is_(True)).limit(1)
            ).first()
            return None if row is None else self._to_dataclass(row)


# --------------------------------------------------------------------------
# market breadth
# --------------------------------------------------------------------------
#: Symbols may only contain these characters; anything else is dropped before a
#: symbol reaches the DuckDB ``IN`` list built by :func:`moving_average_breadth`.
_SYMBOL_SAFE: Final[re.Pattern[str]] = re.compile(r"^[A-Z0-9.\-]{1,12}$")

#: Fewest priced constituents needed before a breadth reading is published.
#: Below this the sample says more about the store's coverage than the market.
MIN_BREADTH_COVERAGE: Final[int] = 20


def moving_average_breadth(
    store: Any,
    symbols: Sequence[str],
    *,
    window: int = 200,
    interval: str = "1d",
) -> dict[str, Any]:
    """Return how many of ``symbols`` close above their own ``window``-bar mean.

    This is index *breadth* - the share of constituents in an uptrend - not the
    index against its own moving average. A constituent only counts when the
    store holds at least ``window`` closes for it; everything else is reported
    as uncovered rather than quietly assumed to be participating.

    Args:
        store: a :class:`~tradinglib.data.store.TimeSeriesStore`.
        symbols: the constituents to measure.
        window: length of the simple moving average, in bars.
        interval: bar interval to read.

    Returns:
        A mapping with ``percent`` (``None`` when coverage is too thin),
        ``above``/``below``/``covered``/``requested``, the uncovered symbols and
        the newest bar timestamp the calculation saw.
    """
    wanted = [s for s in {str(sym).strip().upper() for sym in symbols} if _SYMBOL_SAFE.match(s)]
    result: dict[str, Any] = {
        "window": window,
        "interval": interval,
        "requested": len(wanted),
        "covered": 0,
        "above": 0,
        "below": 0,
        "percent": None,
        "as_of": None,
        "uncovered": [],
        "reason": None,
    }
    if not wanted:
        result["reason"] = "the universe resolved to no symbols"
        return result
    quoted = ", ".join("'" + symbol.replace("'", "''") + "'" for symbol in wanted)
    frame = store.query(
        "SELECT symbol, ts, close FROM bars "
        f"WHERE interval = $interval AND symbol IN ({quoted}) "
        "ORDER BY symbol, ts",
        interval=str(interval),
    )
    above = 0
    below = 0
    latest: datetime | None = None
    covered: set[str] = set()
    if not frame.empty:
        for symbol, rows in frame.groupby("symbol", sort=False):
            closes = rows["close"].astype("float64").dropna()
            if len(closes) < window:
                continue
            tail = closes.iloc[-window:]
            average = float(tail.mean())
            last = float(closes.iloc[-1])
            covered.add(str(symbol))
            if last >= average:
                above += 1
            else:
                below += 1
            stamp = rows["ts"].iloc[-1]
            moment = to_utc(stamp.to_pydatetime() if hasattr(stamp, "to_pydatetime") else stamp)
            if latest is None or moment > latest:
                latest = moment
    result["covered"] = len(covered)
    result["above"] = above
    result["below"] = below
    result["uncovered"] = sorted(set(wanted) - covered)
    result["as_of"] = latest.isoformat() if latest else None
    if len(covered) < MIN_BREADTH_COVERAGE:
        result["reason"] = (
            f"only {len(covered)} of {len(wanted)} constituents have {window} stored "
            f"daily closes; at least {MIN_BREADTH_COVERAGE} are needed for a breadth reading"
        )
        return result
    result["percent"] = above / len(covered)
    return result


_universe_registry: UniverseRegistry | None = None
_watchlist_registry: WatchlistRegistry | None = None


def get_universe_registry() -> UniverseRegistry:
    """Return the process-wide universe registry, creating it on first use."""
    global _universe_registry
    if _universe_registry is None:
        _universe_registry = UniverseRegistry()
    return _universe_registry


def get_watchlist_registry() -> WatchlistRegistry:
    """Return the process-wide watchlist registry, creating it on first use."""
    global _watchlist_registry
    if _watchlist_registry is None:
        _watchlist_registry = WatchlistRegistry()
    return _watchlist_registry


def reset_registries() -> None:
    """Forget the process-wide registries. Intended for tests and reconfiguration."""
    global _universe_registry, _watchlist_registry
    _universe_registry = None
    _watchlist_registry = None


__all__ = [
    "BUILTIN_UNIVERSES",
    "CONSTITUENT_SOURCES",
    "CachedConstituents",
    "ConstituentCache",
    "ConstituentSource",
    "US_QUALITY_50",
    "US_QUALITY_50_AS_OF",
    "US_QUALITY_50_RULE",
    "Universe",
    "UniverseMember",
    "UniverseMemberRow",
    "UniverseRegistry",
    "UniverseRow",
    "UniverseSnapshotRow",
    "Watchlist",
    "WatchlistRegistry",
    "WatchlistRow",
    "constituents_from_records",
    "ensure_schema",
    "fetch_constituents",
    "get_universe_registry",
    "get_watchlist_registry",
    "normalize_symbol",
    "parse_wikipedia_table",
    "reset_registries",
    "us_quality_50_members",
]
