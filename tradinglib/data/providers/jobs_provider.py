"""Job postings from public applicant-tracking-system job boards.

Keyless, like EDGAR and FRED: Greenhouse, Lever and Ashby all publish the
open-roles feed behind every company career page as an unauthenticated JSON
endpoint, and every one of them stamps each posting with the date it was first
published. That is what makes a hiring-momentum reading possible from a single
request - no vendor account, no scraping, no stored history required.

What this provider does *not* do is guess. A company whose careers page is not
hosted on one of these three boards has no reading here, and the payload says
so; a board is only accepted when its own ``company_name``/slug agrees with the
company we asked for, so one issuer's postings are never reported as another's.

Coverage in practice: Greenhouse and Ashby host a large share of US technology
issuers; most mega-cap incumbents run their own careers stack and are therefore
uncovered. :meth:`JobPostingsProvider.get_postings` reports which board served
a reading (or why none did) so the UI can be honest about it.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Final

import httpx

from tradinglib.data.base import (
    DataProvider,
    ProviderRateLimited,
    ProviderStatus,
    ProviderUnavailable,
    to_utc,
    utcnow,
)

logger = logging.getLogger(__name__)

#: Greenhouse's public board API. ``content=true`` is required: the compact
#: response omits ``first_published``, which is the whole point of the call.
GREENHOUSE_URL: Final[str] = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"

#: Lever's public postings API. ``createdAt`` is epoch milliseconds.
LEVER_URL: Final[str] = "https://api.lever.co/v0/postings/{slug}?mode=json"

#: Ashby's public job-board API. ``publishedAt`` is an ISO-8601 timestamp.
ASHBY_URL: Final[str] = "https://api.ashbyhq.com/posting-api/job-board/{slug}"

#: Board id -> (URL template, human label), probed in this order.
BOARDS: Final[tuple[tuple[str, str, str], ...]] = (
    ("greenhouse", GREENHOUSE_URL, "Greenhouse"),
    ("ashby", ASHBY_URL, "Ashby"),
    ("lever", LEVER_URL, "Lever"),
)

#: Slug used to prove the Greenhouse endpoint is reachable in ``status()``.
PROBE_SLUG: Final[str] = "greenhouse"

#: Window each side of the comparison, in days. The tile reads "90D".
WINDOW_DAYS: Final[int] = 90

#: Corporate suffixes stripped when deriving a board slug from a company name.
_SUFFIXES: Final[frozenset[str]] = frozenset(
    {
        "inc",
        "inc.",
        "incorporated",
        "corp",
        "corp.",
        "corporation",
        "co",
        "co.",
        "company",
        "ltd",
        "ltd.",
        "limited",
        "plc",
        "llc",
        "lp",
        "nv",
        "sa",
        "ag",
        "se",
        "holdings",
        "holding",
        "group",
        "technologies",
        "technology",
        "the",
        "class",
    }
)

#: Characters dropped from a slug candidate. Hyphens survive because board
#: slugs use them ("palo-alto-networks"); they are trimmed at the edges.
_SLUG_RE: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9-]+")

#: Characters dropped when comparing two company names for equivalence.
_LETTERS_RE: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]+")

#: User-Agent sent to the board APIs, identifying the client honestly.
USER_AGENT: Final[str] = f"TradingLib research (python-httpx/{httpx.__version__})"


@dataclass(slots=True)
class JobPostings:
    """A hiring-momentum reading derived from one public job board.

    Attributes:
        symbol: the ticker the reading was requested for.
        board: board id that served it (``greenhouse``/``ashby``/``lever``), or
            ``None`` when no board was found.
        board_label: the board's display name, or ``None``.
        slug: the board slug that matched, or ``None``.
        open_roles: currently listed postings on that board.
        opened_recent: postings first published inside the trailing window.
        opened_prior: postings first published in the window before that and
            still listed.
        change_pct: percentage change between the two windows, or ``None`` when
            the prior window is empty (a change from zero has no percentage).
        as_of: when the board was read.
        reason: why there is no reading, when there is none.
    """

    symbol: str
    board: str | None = None
    board_label: str | None = None
    slug: str | None = None
    open_roles: int | None = None
    opened_recent: int | None = None
    opened_prior: int | None = None
    change_pct: float | None = None
    as_of: datetime | None = None
    reason: str | None = None
    #: Slugs that were probed and answered "no such board".
    tried: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of the reading."""
        return {
            "symbol": self.symbol,
            "board": self.board,
            "board_label": self.board_label,
            "slug": self.slug,
            "open_roles": self.open_roles,
            "opened_recent": self.opened_recent,
            "opened_prior": self.opened_prior,
            "change_pct": self.change_pct,
            "window_days": WINDOW_DAYS,
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "reason": self.reason,
            "tried": list(self.tried),
        }


def slug_candidates(name: str | None, symbol: str) -> list[str]:
    """Return board-slug candidates for a company, most likely first.

    "Datadog, Inc." yields ``["datadog"]``; "Grab Holdings Limited" yields
    ``["grab", "grabholdings"]``; a company with no known name falls back to
    the lower-cased ticker. Candidates are de-duplicated in order.
    """
    out: list[str] = []

    def add(value: str) -> None:
        """Append a normalised candidate if it is new and non-trivial."""
        token = _SLUG_RE.sub("", value.lower()).strip("-")
        if len(token) >= 2 and token not in out:
            out.append(token)

    text = (name or "").strip()
    if text:
        words = [w.strip(",.") for w in re.split(r"[\s,]+", text) if w.strip(",.")]
        core = [w for w in words if w.lower().strip(".") not in _SUFFIXES]
        if core:
            add(core[0])
            if len(core) > 1:
                add("".join(core))
                add("-".join(core))
        add("".join(words))
    add(symbol)
    return out


def _parse_moment(value: Any) -> datetime | None:
    """Parse a board's "first published" stamp into aware UTC, or ``None``."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # Lever reports epoch milliseconds.
        seconds = float(value) / 1000.0 if float(value) > 1e11 else float(value)
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return to_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        return None


def _company_matches(claimed: str | None, wanted: str | None, slug: str) -> bool:
    """Whether a board's own company name plausibly names the company we asked for.

    Compared on letters only, in both directions, so "Datadog" matches
    "Datadog, Inc." and "Grab" matches "Grab Holdings". A board that does not
    state a company name is accepted on the slug alone - the slug had to be
    derived from the company name to be probed at all.
    """
    if not claimed:
        return True
    left = _LETTERS_RE.sub("", claimed.lower())
    right = _LETTERS_RE.sub("", (wanted or "").lower())
    bare = _LETTERS_RE.sub("", slug)
    if not left:
        return True
    if right and (left.startswith(right[:6]) or right.startswith(left[:6])):
        return True
    return left.startswith(bare[:6]) or bare.startswith(left[:6])


class JobPostingsProvider(DataProvider):
    """Hiring momentum from public Greenhouse / Ashby / Lever job boards."""

    name = "jobs"

    def __init__(self, *, timeout: float = 30.0) -> None:
        """Create the provider.

        Args:
            timeout: connect/read timeout for the board APIs. Greenhouse's
                ``content=true`` payload runs to several megabytes for a large
                employer, so this is deliberately generous.
        """
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()
        self._last_success: datetime | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Return the shared HTTP client, creating it on first use."""
        async with self._client_lock:
            if self._client is None or self._client.is_closed:
                self._client = httpx.AsyncClient(
                    timeout=self.timeout,
                    follow_redirects=True,
                    headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                )
            return self._client

    async def close(self) -> None:
        """Close the HTTP client."""
        async with self._client_lock:
            if self._client is not None and not self._client.is_closed:
                await self._client.aclose()
            self._client = None

    async def status(self) -> ProviderStatus:
        """Probe Greenhouse's own board to report real reachability."""
        client = await self._get_client()
        started = utcnow()
        try:
            response = await client.get(GREENHOUSE_URL.format(slug=PROBE_SLUG))
        except httpx.HTTPError as exc:
            return ProviderStatus(
                name=self.name,
                configured=True,
                reachable=False,
                detail=f"{type(exc).__name__}: {exc}",
            )
        latency_ms = (utcnow() - started).total_seconds() * 1000.0
        ok = response.status_code == 200
        if ok:
            self._last_success = utcnow()
        return ProviderStatus(
            name=self.name,
            configured=True,
            reachable=ok,
            latency_ms=latency_ms,
            last_success=self._last_success,
            detail=(
                "public ATS boards: Greenhouse, Ashby, Lever (keyless)"
                if ok
                else f"Greenhouse answered HTTP {response.status_code}"
            ),
        )

    # --- board access ------------------------------------------------------
    async def _read_board(self, board: str, url: str, slug: str) -> list[dict[str, Any]] | None:
        """Return a board's postings, or ``None`` when it has no such slug.

        Raises:
            ProviderRateLimited: when the board throttles the request.
            ProviderUnavailable: on a transport failure or an unusable body.
        """
        client = await self._get_client()
        try:
            response = await client.get(url.format(slug=slug))
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(self.name, f"{board}: {type(exc).__name__}: {exc}") from exc
        if response.status_code in (404, 410):
            return None
        if response.status_code == 429:
            raise ProviderRateLimited(self.name, f"{board} throttled the board request")
        if response.status_code >= 400:
            raise ProviderUnavailable(
                self.name, f"{board} answered HTTP {response.status_code} for {slug!r}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderUnavailable(self.name, f"{board} returned a non-JSON body") from exc
        self._last_success = utcnow()
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            jobs = payload.get("jobs")
            if isinstance(jobs, list):
                return [item for item in jobs if isinstance(item, dict)]
        return None

    @staticmethod
    def _published(board: str, posting: dict[str, Any]) -> datetime | None:
        """Return when a posting was first published, per that board's schema."""
        if board == "greenhouse":
            return _parse_moment(posting.get("first_published") or posting.get("updated_at"))
        if board == "ashby":
            return _parse_moment(posting.get("publishedAt"))
        return _parse_moment(posting.get("createdAt"))

    @staticmethod
    def _listed(board: str, posting: dict[str, Any]) -> bool:
        """Whether a posting is actually listed (Ashby publishes hidden ones)."""
        if board == "ashby":
            return bool(posting.get("isListed", True))
        return True

    @staticmethod
    def _claimed_company(board: str, postings: list[dict[str, Any]]) -> str | None:
        """The company name the board states, when it states one."""
        if board != "greenhouse":
            return None
        for posting in postings:
            claimed = posting.get("company_name")
            if isinstance(claimed, str) and claimed.strip():
                return claimed.strip()
        return None

    async def get_postings(
        self,
        symbol: str,
        *,
        company_name: str | None = None,
        slug: str | None = None,
        now: datetime | None = None,
    ) -> JobPostings:
        """Return the hiring-momentum reading for one company.

        Every derived candidate slug is probed against every board until one
        answers. A miss is not an error: the returned :class:`JobPostings`
        carries ``reason`` and the slugs that were tried.

        Args:
            symbol: the ticker, used for the fallback slug and for the label.
            company_name: the issuer's name, from which slugs are derived.
            slug: an exact board slug. When given it is the only candidate, so
                an ingestion source can name a board directly instead of
                relying on the name-to-slug derivation.
            now: evaluation moment; defaults to the current UTC time.

        Raises:
            ProviderRateLimited: when a board throttles the request.
            ProviderUnavailable: when a board is reachable but unusable.
        """
        moment = to_utc(now) if now is not None else utcnow()
        ticker = symbol.strip().upper()
        candidates = [slug.strip().lower()] if slug else slug_candidates(company_name, ticker)
        tried: list[str] = []
        for slug in candidates:
            for board, url, label in BOARDS:
                tried.append(f"{board}:{slug}")
                postings = await self._read_board(board, url, slug)
                if postings is None or not postings:
                    continue
                claimed = self._claimed_company(board, postings)
                if not _company_matches(claimed, company_name, slug):
                    logger.info(
                        "jobs: %s board %r is %r, not %r - rejected",
                        board,
                        slug,
                        claimed,
                        company_name,
                    )
                    continue
                return self._summarise(
                    ticker, board, label, slug, postings, moment=moment, tried=tried
                )
        return JobPostings(
            symbol=ticker,
            as_of=moment,
            tried=tried,
            reason=(
                "no public Greenhouse, Ashby or Lever job board is published for "
                f"{company_name or ticker}"
            ),
        )

    def _summarise(
        self,
        symbol: str,
        board: str,
        label: str,
        slug: str,
        postings: list[dict[str, Any]],
        *,
        moment: datetime,
        tried: list[str],
    ) -> JobPostings:
        """Count the two windows and turn them into a percentage change."""
        recent_cutoff = moment - timedelta(days=WINDOW_DAYS)
        prior_cutoff = moment - timedelta(days=2 * WINDOW_DAYS)
        listed = [p for p in postings if self._listed(board, p)]
        recent = 0
        prior = 0
        dated = 0
        for posting in listed:
            published = self._published(board, posting)
            if published is None:
                continue
            dated += 1
            if published >= recent_cutoff:
                recent += 1
            elif published >= prior_cutoff:
                prior += 1
        if not dated:
            return JobPostings(
                symbol=symbol,
                board=board,
                board_label=label,
                slug=slug,
                open_roles=len(listed),
                as_of=moment,
                tried=tried,
                reason=f"{label} board {slug!r} publishes no posting dates",
            )
        change = (recent / prior - 1.0) * 100.0 if prior else None
        return JobPostings(
            symbol=symbol,
            board=board,
            board_label=label,
            slug=slug,
            open_roles=len(listed),
            opened_recent=recent,
            opened_prior=prior,
            change_pct=change,
            as_of=moment,
            tried=tried,
            reason=(
                None
                if change is not None
                else f"no role was opened in the {WINDOW_DAYS} days before the trailing window"
            ),
        )


__all__ = [
    "ASHBY_URL",
    "BOARDS",
    "GREENHOUSE_URL",
    "LEVER_URL",
    "WINDOW_DAYS",
    "JobPostings",
    "JobPostingsProvider",
    "slug_candidates",
]
