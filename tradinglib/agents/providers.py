"""LLM provider abstraction (``docs/SPEC.md`` decision D3).

Two real providers: Anthropic through ``claude-agent-sdk`` and OpenAI through
``openai``. The Anthropic provider may authenticate either with
``ANTHROPIC_API_KEY`` or with the local Claude Code CLI session; which one is
available is verified at runtime and reported honestly - never assumed.

Three things live here beyond the provider classes themselves:

* a **maintained price table** (:data:`PRICE_TABLE`) that turns token usage into
  dollars. Every entry carries the date it was taken from the vendor's public
  price list, and every price can be overridden from
  ``<data_dir>/model_prices.json`` without editing code. A model that is not in
  the table has *no* price - its cost is reported as unknown rather than
  invented;
* a :class:`SpendLedger` that accumulates real spend per run and per calendar
  window and compares it with the configured :class:`~tradinglib.config.Budgets`.
  :meth:`SpendLedger.check` raises :class:`BudgetExceeded`, which is what stops
  a pipeline stage from starting once the budget is gone;
* a :class:`ProviderRegistry` that reports, per provider, whether it is
  genuinely usable and which models it offers - the payload behind
  ``GET /api/agents/models``.

The tool-calling contract differs between the two vendors and the difference is
explicit rather than hidden: :attr:`LLMProvider.executes_tools` says whether the
provider runs the tool loop itself (the Claude Agent SDK does) or returns
pending tool calls for the caller to execute (OpenAI, and the test stub).
"""

from __future__ import annotations

import abc
import asyncio
import contextlib
import json
import logging
import os
import shutil
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Literal

from tradinglib.config import Budgets, Settings, get_settings

logger = logging.getLogger(__name__)

#: Result of one tool invocation, handed back to the model.
ToolExecutor = Callable[[str, dict[str, Any]], Awaitable["ToolResult"]]


def _utcnow() -> datetime:
    """Return the current UTC time as an aware datetime."""
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# value objects
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ModelPrice:
    """Published price of one model, in US dollars per million tokens."""

    input_per_mtok: float
    output_per_mtok: float
    cache_read_per_mtok: float | None = None
    cache_write_per_mtok: float | None = None
    context_window: int | None = None
    max_output_tokens: int | None = None
    as_of: str = "unknown"
    source: str = "table"

    def cost(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> float:
        """Return the dollar cost of a token count at this price.

        Cache-read and cache-write tokens fall back to the plain input rate when
        the vendor's discounted rates are not recorded, which over-states rather
        than under-states the cost - the safe direction for a budget.
        """
        cache_read_rate = (
            self.cache_read_per_mtok
            if self.cache_read_per_mtok is not None
            else self.input_per_mtok
        )
        cache_write_rate = (
            self.cache_write_per_mtok
            if self.cache_write_per_mtok is not None
            else self.input_per_mtok
        )
        total = (
            input_tokens * self.input_per_mtok
            + output_tokens * self.output_per_mtok
            + cache_read_tokens * cache_read_rate
            + cache_write_tokens * cache_write_rate
        )
        return total / 1_000_000.0

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of the price."""
        return {
            "input_per_mtok": self.input_per_mtok,
            "output_per_mtok": self.output_per_mtok,
            "cache_read_per_mtok": self.cache_read_per_mtok,
            "cache_write_per_mtok": self.cache_write_per_mtok,
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "as_of": self.as_of,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class ModelInfo:
    """A model offered by a configured provider."""

    id: str
    provider: str
    label: str
    input_cost_per_mtok: float | None = None
    output_cost_per_mtok: float | None = None
    context_window: int | None = None
    max_output_tokens: int | None = None
    price_source: str = "unknown"
    price_as_of: str | None = None
    note: str | None = None

    @property
    def priced(self) -> bool:
        """Whether a dollar cost can be computed for this model."""
        return self.input_cost_per_mtok is not None and self.output_cost_per_mtok is not None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping for the model picker."""
        return {
            "id": self.id,
            "provider": self.provider,
            "label": self.label,
            "input_cost_per_mtok": self.input_cost_per_mtok,
            "output_cost_per_mtok": self.output_cost_per_mtok,
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "priced": self.priced,
            "price_source": self.price_source,
            "price_as_of": self.price_as_of,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class Usage:
    """Token accounting for one completion."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    cost_known: bool = True

    def __add__(self, other: Usage) -> Usage:
        """Sum two usage records."""
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
            cost_known=self.cost_known and other.cost_known,
        )

    @property
    def total_tokens(self) -> int:
        """Every token billed for this completion."""
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of the usage."""
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "cost_known": self.cost_known,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> Usage:
        """Rebuild usage from :meth:`as_dict` output."""
        data = dict(payload or {})
        return cls(
            input_tokens=int(data.get("input_tokens", 0) or 0),
            output_tokens=int(data.get("output_tokens", 0) or 0),
            cache_read_tokens=int(data.get("cache_read_tokens", 0) or 0),
            cache_write_tokens=int(data.get("cache_write_tokens", 0) or 0),
            cost_usd=float(data.get("cost_usd", 0.0) or 0.0),
            cost_known=bool(data.get("cost_known", True)),
        )


@dataclass(slots=True)
class ToolSpec:
    """A library capability exposed to the model as a callable tool."""

    name: str
    description: str
    input_schema: dict[str, Any]

    def as_openai_tool(self) -> dict[str, Any]:
        """Render the spec in OpenAI's ``tools`` format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


@dataclass(slots=True)
class ToolCall:
    """A tool invocation requested by the model."""

    id: str
    name: str
    arguments: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of the call."""
        return {"id": self.id, "name": self.name, "arguments": self.arguments}


@dataclass(slots=True)
class ToolResult:
    """The outcome of executing one tool call."""

    content: str
    is_error: bool = False
    data: Any = None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of the result."""
        return {"content": self.content, "is_error": self.is_error}


@dataclass(slots=True)
class Completion:
    """One model response."""

    text: str
    usage: Usage
    model: str
    provider: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of the completion."""
        return {
            "text": self.text,
            "model": self.model,
            "provider": self.provider,
            "usage": self.usage.as_dict(),
            "tool_calls": [call.as_dict() for call in self.tool_calls],
            "tool_results": list(self.tool_results),
            "stop_reason": self.stop_reason,
        }


@dataclass(frozen=True, slots=True)
class ProviderCapability:
    """Honest capability report for one LLM provider."""

    provider: str
    available: bool
    auth_mode: str
    detail: str
    models: list[ModelInfo] = field(default_factory=list)
    verified: bool = False

    def as_dict(self, *, include_models: bool = True) -> dict[str, Any]:
        """Return a JSON-serialisable mapping for ``/api/agents/config``."""
        payload: dict[str, Any] = {
            "provider": self.provider,
            "available": self.available,
            "auth_mode": self.auth_mode,
            "detail": self.detail,
            "verified": self.verified,
            "model_count": len(self.models),
        }
        if include_models:
            payload["models"] = [model.as_dict() for model in self.models]
        return payload


class ProviderError(RuntimeError):
    """A provider call failed."""


class ProviderNotAvailable(ProviderError):
    """A provider was asked to work without usable credentials."""


class BudgetExceeded(RuntimeError):
    """Raised when starting work would exceed a configured spend budget.

    Attributes:
        scope: which budget was hit (``per_run``, ``daily``, ``monthly`` or
            ``stage``).
        used: dollars already spent inside that scope.
        budget: the configured ceiling.
        projected: the additional cost that would have breached it.
    """

    def __init__(
        self, scope: str, used: float, budget: float, projected: float = 0.0
    ) -> None:
        self.scope = scope
        self.used = used
        self.budget = budget
        self.projected = projected
        super().__init__(
            f"{scope} budget exceeded: ${used:.4f} used"
            + (f" + ${projected:.4f} projected" if projected else "")
            + f" of ${budget:.2f}"
        )

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of the breach."""
        return {
            "error": "budget_exceeded",
            "scope": self.scope,
            "used": round(self.used, 6),
            "projected": round(self.projected, 6),
            "budget": self.budget,
        }


# --------------------------------------------------------------------------
# price table
# --------------------------------------------------------------------------
#: Date the Anthropic list prices below were taken from Anthropic's published
#: model table. Prices change; this is why the override file exists.
ANTHROPIC_PRICES_AS_OF: Final[str] = "2026-06-24"

#: Date the OpenAI list prices below were taken from OpenAI's public pricing
#: page. Models absent from this table are priced ``None`` (unknown) rather
#: than guessed.
OPENAI_PRICES_AS_OF: Final[str] = "2026-05-01"

ANTHROPIC_PRICES: Final[dict[str, ModelPrice]] = {
    "claude-fable-5": ModelPrice(10.0, 50.0, context_window=1_000_000, max_output_tokens=128_000, as_of=ANTHROPIC_PRICES_AS_OF),
    "claude-opus-5": ModelPrice(5.0, 25.0, context_window=1_000_000, max_output_tokens=128_000, as_of=ANTHROPIC_PRICES_AS_OF),
    "claude-opus-4-8": ModelPrice(5.0, 25.0, context_window=1_000_000, max_output_tokens=128_000, as_of=ANTHROPIC_PRICES_AS_OF),
    "claude-opus-4-7": ModelPrice(5.0, 25.0, context_window=1_000_000, max_output_tokens=128_000, as_of=ANTHROPIC_PRICES_AS_OF),
    "claude-opus-4-6": ModelPrice(5.0, 25.0, context_window=1_000_000, max_output_tokens=128_000, as_of=ANTHROPIC_PRICES_AS_OF),
    "claude-sonnet-5": ModelPrice(2.0, 10.0, context_window=1_000_000, max_output_tokens=128_000, as_of=ANTHROPIC_PRICES_AS_OF),
    "claude-sonnet-4-6": ModelPrice(3.0, 15.0, context_window=1_000_000, max_output_tokens=128_000, as_of=ANTHROPIC_PRICES_AS_OF),
    "claude-haiku-4-5": ModelPrice(1.0, 5.0, context_window=200_000, max_output_tokens=64_000, as_of=ANTHROPIC_PRICES_AS_OF),
}

OPENAI_PRICES: Final[dict[str, ModelPrice]] = {
    "gpt-4.1": ModelPrice(2.0, 8.0, cache_read_per_mtok=0.5, context_window=1_047_576, max_output_tokens=32_768, as_of=OPENAI_PRICES_AS_OF),
    "gpt-4.1-mini": ModelPrice(0.4, 1.6, cache_read_per_mtok=0.1, context_window=1_047_576, max_output_tokens=32_768, as_of=OPENAI_PRICES_AS_OF),
    "gpt-4.1-nano": ModelPrice(0.1, 0.4, cache_read_per_mtok=0.025, context_window=1_047_576, max_output_tokens=32_768, as_of=OPENAI_PRICES_AS_OF),
    "gpt-4o": ModelPrice(2.5, 10.0, cache_read_per_mtok=1.25, context_window=128_000, max_output_tokens=16_384, as_of=OPENAI_PRICES_AS_OF),
    "gpt-4o-mini": ModelPrice(0.15, 0.6, cache_read_per_mtok=0.075, context_window=128_000, max_output_tokens=16_384, as_of=OPENAI_PRICES_AS_OF),
    "o3": ModelPrice(2.0, 8.0, cache_read_per_mtok=0.5, context_window=200_000, max_output_tokens=100_000, as_of=OPENAI_PRICES_AS_OF),
    "o3-mini": ModelPrice(1.1, 4.4, cache_read_per_mtok=0.55, context_window=200_000, max_output_tokens=100_000, as_of=OPENAI_PRICES_AS_OF),
    "o4-mini": ModelPrice(1.1, 4.4, cache_read_per_mtok=0.275, context_window=200_000, max_output_tokens=100_000, as_of=OPENAI_PRICES_AS_OF),
}

#: The deterministic test stub is priced so that cost accounting can be
#: exercised without a vendor. It is not a real product and is labelled as such
#: everywhere it appears.
MOCK_PRICES: Final[dict[str, ModelPrice]] = {
    "mock-small": ModelPrice(1.0, 5.0, context_window=32_000, max_output_tokens=4_096, as_of="n/a", source="mock"),
    "mock-large": ModelPrice(10.0, 50.0, context_window=128_000, max_output_tokens=8_192, as_of="n/a", source="mock"),
}

#: The full built-in table, keyed ``"<provider>/<model>"``.
PRICE_TABLE: Final[dict[str, ModelPrice]] = {
    **{f"anthropic/{model}": price for model, price in ANTHROPIC_PRICES.items()},
    **{f"openai/{model}": price for model, price in OPENAI_PRICES.items()},
    **{f"mock/{model}": price for model, price in MOCK_PRICES.items()},
}

#: File the user may drop next to the database to correct or extend prices.
PRICE_OVERRIDE_FILENAME: Final[str] = "model_prices.json"

_override_cache: tuple[float, dict[str, ModelPrice]] | None = None


def _price_override_path(settings: Settings | None = None) -> Path:
    """Return the path of the optional price-override file."""
    return (settings or get_settings()).data_dir / PRICE_OVERRIDE_FILENAME


def load_price_overrides(settings: Settings | None = None) -> dict[str, ModelPrice]:
    """Load ``<data_dir>/model_prices.json`` if it exists.

    The file maps ``"<provider>/<model>"`` (or a bare model id) to an object with
    at least ``input_per_mtok`` and ``output_per_mtok``. It is re-read whenever
    its modification time changes, so correcting a price does not need a
    restart. A malformed file is logged and ignored - never silently applied.
    """
    global _override_cache
    path = _price_override_path(settings)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        _override_cache = None
        return {}
    if _override_cache is not None and _override_cache[0] == mtime:
        return _override_cache[1]
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("price override file %s could not be read; ignoring it", path, exc_info=True)
        return {}
    overrides: dict[str, ModelPrice] = {}
    for key, value in (raw or {}).items():
        if not isinstance(value, dict):
            continue
        try:
            overrides[str(key)] = ModelPrice(
                input_per_mtok=float(value["input_per_mtok"]),
                output_per_mtok=float(value["output_per_mtok"]),
                cache_read_per_mtok=(
                    None if value.get("cache_read_per_mtok") is None
                    else float(value["cache_read_per_mtok"])
                ),
                cache_write_per_mtok=(
                    None if value.get("cache_write_per_mtok") is None
                    else float(value["cache_write_per_mtok"])
                ),
                context_window=(
                    None if value.get("context_window") is None else int(value["context_window"])
                ),
                max_output_tokens=(
                    None if value.get("max_output_tokens") is None
                    else int(value["max_output_tokens"])
                ),
                as_of=str(value.get("as_of") or "override"),
                source="override",
            )
        except (KeyError, TypeError, ValueError):
            logger.warning("price override for %r is malformed; ignoring that entry", key)
    _override_cache = (mtime, overrides)
    return overrides


def lookup_price(provider: str, model: str, *, settings: Settings | None = None) -> ModelPrice | None:
    """Return the price of ``model``, or ``None`` when it is genuinely unknown.

    Resolution order: an override keyed ``provider/model``, an override keyed by
    the bare model id, the built-in table keyed ``provider/model``, then the
    built-in table with vendor date suffixes stripped (``claude-opus-5-20260101``
    is priced as ``claude-opus-5``).
    """
    overrides = load_price_overrides(settings)
    for key in (f"{provider}/{model}", model):
        if key in overrides:
            return overrides[key]
    full = f"{provider}/{model}"
    if full in PRICE_TABLE:
        return PRICE_TABLE[full]
    # Vendor snapshot ids ("<model>-20260101") price as their base model.
    parts = model.split("-")
    while len(parts) > 1:
        parts.pop()
        candidate = f"{provider}/{'-'.join(parts)}"
        if candidate in PRICE_TABLE:
            return PRICE_TABLE[candidate]
    return None


def price_usage(
    provider: str,
    model: str,
    usage: Usage,
    *,
    settings: Settings | None = None,
) -> Usage:
    """Return ``usage`` with its dollar cost filled in from the price table.

    A usage record that already carries a vendor-reported cost (the Claude Agent
    SDK reports one) is returned unchanged. When no price is known the cost is
    left at zero and ``cost_known`` is ``False`` - which the budget check treats
    as "cannot be enforced", not as "free".
    """
    if usage.cost_usd:
        return usage
    price = lookup_price(provider, model, settings=settings)
    if price is None:
        return Usage(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            cost_usd=0.0,
            cost_known=False,
        )
    cost = price.cost(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
    )
    return Usage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        cost_usd=cost,
        cost_known=True,
    )


# --------------------------------------------------------------------------
# spend ledger
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SpendEntry:
    """One recorded charge."""

    ts: datetime
    run_id: str | None
    stage_id: str | None
    provider: str
    model: str
    usage: Usage

    @property
    def cost_usd(self) -> float:
        """Dollars charged by this entry."""
        return self.usage.cost_usd


class SpendLedger:
    """Accumulates real LLM spend and enforces the configured budgets.

    The ledger is in-memory but is *seeded* from persistence: the pipeline
    runner passes a ``loader`` that reads the cost column of every stored run,
    so daily and monthly budgets survive a restart. Entries recorded on a model
    whose price is unknown are kept (the tokens are real) but flagged, and
    :meth:`snapshot` reports how many such entries there were rather than
    pretending the spend was zero.
    """

    def __init__(
        self,
        budgets: Budgets | None = None,
        *,
        loader: Callable[[], Iterable[SpendEntry]] | None = None,
    ) -> None:
        """Create the ledger.

        Args:
            budgets: spend ceilings; defaults to the configured ones.
            loader: callable returning already-persisted entries, used to seed
                and to refresh the ledger.
        """
        self._budgets = budgets
        self._loader = loader
        self._entries: list[SpendEntry] = []
        self._loaded = False

    @property
    def budgets(self) -> Budgets:
        """The spend ceilings this ledger enforces."""
        return self._budgets if self._budgets is not None else get_settings().budgets

    def set_budgets(self, budgets: Budgets) -> None:
        """Replace the enforced budgets (the Configuration tab writes these)."""
        self._budgets = budgets

    # --- loading ----------------------------------------------------------
    def reload(self) -> None:
        """Re-read persisted entries from the loader, replacing the cache."""
        if self._loader is None:
            self._loaded = True
            return
        self._entries = [entry for entry in self._loader()]
        self._loaded = True

    def _ensure_loaded(self) -> None:
        """Seed from the loader on first use."""
        if not self._loaded:
            self.reload()

    # --- recording --------------------------------------------------------
    def record(
        self,
        *,
        provider: str,
        model: str,
        usage: Usage,
        run_id: str | None = None,
        stage_id: str | None = None,
        ts: datetime | None = None,
    ) -> SpendEntry:
        """Record one charge and return the stored entry."""
        self._ensure_loaded()
        entry = SpendEntry(
            ts=ts or _utcnow(),
            run_id=run_id,
            stage_id=stage_id,
            provider=provider,
            model=model,
            usage=usage,
        )
        self._entries.append(entry)
        return entry

    # --- reads ------------------------------------------------------------
    def entries(self) -> list[SpendEntry]:
        """Every recorded entry, oldest first."""
        self._ensure_loaded()
        return list(self._entries)

    def run_total(self, run_id: str) -> float:
        """Dollars spent by one run."""
        self._ensure_loaded()
        return sum(e.cost_usd for e in self._entries if e.run_id == run_id)

    def window_total(self, window: Literal["day", "month", "all"] = "day") -> float:
        """Dollars spent inside a UTC calendar window."""
        self._ensure_loaded()
        now = _utcnow()
        total = 0.0
        for entry in self._entries:
            if window == "day" and entry.ts.date() != now.date():
                continue
            if window == "month" and (entry.ts.year, entry.ts.month) != (now.year, now.month):
                continue
            total += entry.cost_usd
        return total

    def unpriced_count(self) -> int:
        """Number of recorded charges whose dollar cost could not be computed."""
        self._ensure_loaded()
        return sum(1 for entry in self._entries if not entry.usage.cost_known)

    def snapshot(self, *, run_id: str | None = None) -> dict[str, Any]:
        """Return the datastrip's SPEND payload - all real figures."""
        budgets = self.budgets
        payload: dict[str, Any] = {
            "used": round(self.window_total("day"), 6),
            "budget": budgets.daily_usd,
            "window": "day",
            "daily": {"used": round(self.window_total("day"), 6), "budget": budgets.daily_usd},
            "monthly": {
                "used": round(self.window_total("month"), 6),
                "budget": budgets.monthly_usd,
            },
            "per_run_budget": budgets.per_run_usd,
            "total": round(self.window_total("all"), 6),
            "entries": len(self.entries()),
            "unpriced_entries": self.unpriced_count(),
        }
        if run_id is not None:
            payload["run"] = {
                "id": run_id,
                "used": round(self.run_total(run_id), 6),
                "budget": budgets.per_run_usd,
            }
        return payload

    # --- enforcement ------------------------------------------------------
    def check(
        self,
        *,
        run_id: str | None = None,
        projected_usd: float = 0.0,
        stage_budget_usd: float | None = None,
        stage_used_usd: float = 0.0,
    ) -> None:
        """Raise :class:`BudgetExceeded` when the next unit of work cannot run.

        Called before every stage starts, with ``projected_usd`` set to the
        stage's own estimate. The daily and monthly ceilings are checked first
        because they protect the account, then the per-run ceiling, then any
        per-stage cap configured on the agent.
        """
        budgets = self.budgets
        daily = self.window_total("day")
        if budgets.daily_usd > 0 and daily + projected_usd > budgets.daily_usd:
            raise BudgetExceeded("daily", daily, budgets.daily_usd, projected_usd)
        monthly = self.window_total("month")
        if budgets.monthly_usd > 0 and monthly + projected_usd > budgets.monthly_usd:
            raise BudgetExceeded("monthly", monthly, budgets.monthly_usd, projected_usd)
        if run_id is not None and budgets.per_run_usd > 0:
            spent = self.run_total(run_id)
            if spent + projected_usd > budgets.per_run_usd:
                raise BudgetExceeded("per_run", spent, budgets.per_run_usd, projected_usd)
        if stage_budget_usd is not None and stage_budget_usd > 0:
            if stage_used_usd + projected_usd > stage_budget_usd:
                raise BudgetExceeded("stage", stage_used_usd, stage_budget_usd, projected_usd)


# --------------------------------------------------------------------------
# provider interface
# --------------------------------------------------------------------------
class LLMProvider(abc.ABC):
    """Interface every LLM provider implements."""

    #: Stable provider identifier (``"anthropic"``, ``"openai"``, ``"mock"``).
    name: str = "provider"

    #: Whether the provider runs the tool loop itself. The Claude Agent SDK
    #: does (it executes in-process MCP tools and returns the final answer);
    #: OpenAI and the test stub return pending calls for the caller to run.
    executes_tools: bool = False

    @abc.abstractmethod
    async def capability(self, *, probe: bool = False) -> ProviderCapability:
        """Verify at runtime whether this provider can actually be used.

        Args:
            probe: perform a real (billable, minimal) request to prove the
                credentials work, instead of only inspecting configuration.
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def models(self) -> list[ModelInfo]:
        """List models available under the current credentials."""
        raise NotImplementedError

    @abc.abstractmethod
    async def complete(
        self,
        *,
        model: str,
        system: str | None,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec] | None = None,
        tool_executor: ToolExecutor | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
        budget_usd: float | None = None,
        timeout: float | None = None,
    ) -> Completion:
        """Run one completion, returning text, tool calls and real usage."""
        raise NotImplementedError

    def estimate_cost(self, model: str, usage: Usage) -> float:
        """Return the dollar cost of ``usage`` on ``model`` (0.0 when unpriced)."""
        return price_usage(self.name, model, usage).cost_usd

    def price(self, model: str) -> ModelPrice | None:
        """Return the price entry backing :meth:`estimate_cost`."""
        return lookup_price(self.name, model)

    async def aclose(self) -> None:
        """Release provider resources. Safe to call more than once."""
        return None


def render_messages(messages: Sequence[Mapping[str, Any]], *, system: str | None = None) -> str:
    """Flatten a message list into one prompt string.

    The Claude Agent SDK takes a prompt rather than a message array, so a
    multi-turn conversation is rendered with explicit role markers. Tool results
    the caller already executed are rendered as ``TOOL RESULT`` blocks so the
    model can see what came back.
    """
    lines: list[str] = []
    if system:
        lines.append(f"[SYSTEM]\n{system}")
    for message in messages:
        role = str(message.get("role", "user")).upper()
        content = message.get("content", "")
        if isinstance(content, list):
            rendered_parts: list[str] = []
            for part in content:
                if isinstance(part, Mapping):
                    if part.get("type") == "tool_result":
                        rendered_parts.append(
                            f"TOOL RESULT ({part.get('name', part.get('tool_use_id', '?'))}):\n"
                            f"{part.get('content', '')}"
                        )
                    else:
                        rendered_parts.append(str(part.get("text", "")))
                else:
                    rendered_parts.append(str(part))
            text = "\n".join(p for p in rendered_parts if p)
        else:
            text = str(content)
        lines.append(f"[{role}]\n{text}")
    return "\n\n".join(lines).strip()


# --------------------------------------------------------------------------
# Anthropic
# --------------------------------------------------------------------------
#: Prompt used by the (optional) live credential probe. Deliberately tiny.
_PROBE_PROMPT: Final[str] = "Reply with the single word: ok"


#: Turn ceiling for one agent-loop stage. A research stage typically resolves
#: a universe, reads several company sections, may run a backtest and then
#: writes its output contract; each of those is a turn, and hitting the ceiling
#: aborts the stage (and with it the run). Cost is bounded by the run budget,
#: not by this number, so it is set to a value a real stage can finish inside.
DEFAULT_AGENT_MAX_TURNS: Final[int] = 40


def _log_cli_stderr(line: str) -> None:
    """Forward one line of the Claude Code CLI's stderr into the server log."""
    text = line.rstrip()
    if text:
        logger.warning("claude cli: %s", text)


def _agent_max_turns() -> int:
    """Return the configured agent-loop turn ceiling."""
    raw = os.environ.get("TRADINGLIB_AGENT_MAX_TURNS", "").strip()
    if not raw:
        return DEFAULT_AGENT_MAX_TURNS
    try:
        value = int(raw)
    except ValueError:
        logger.warning("TRADINGLIB_AGENT_MAX_TURNS=%r is not an integer; using the default", raw)
        return DEFAULT_AGENT_MAX_TURNS
    return max(1, value)


class AnthropicProvider(LLMProvider):
    """Anthropic provider backed by ``claude-agent-sdk``.

    The SDK drives the Claude Code agent loop, so tools handed to
    :meth:`complete` are registered as an in-process MCP server and executed by
    the SDK itself; :attr:`executes_tools` is therefore ``True`` and the
    completion returns the tool calls that actually ran together with their
    results. Built-in Claude Code tools (file access, bash, web) are switched
    off - an TradingLib agent may only reach the library through TradingLib's own tools.
    """

    name = "anthropic"
    executes_tools = True

    #: Name of the in-process MCP server the library tools are published under.
    MCP_SERVER = "tradinglib"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        settings: Settings | None = None,
        max_turns: int | None = None,
    ) -> None:
        """Create the provider. Without a key, CLI auth is probed at runtime.

        Args:
            api_key: explicit key; ``None`` reads the configured one.
            settings: settings used for keys and per-token pricing.
            max_turns: agent-loop turn ceiling. ``None`` reads
                ``TRADINGLIB_AGENT_MAX_TURNS`` and otherwise uses
                :data:`DEFAULT_AGENT_MAX_TURNS` - a pipeline stage that reads
                several company sections and then writes its contract needs far
                more than a chat turn budget, and running out of turns kills the
                whole run.
        """
        self._settings = settings
        self.api_key = api_key if api_key is not None else (settings or get_settings()).anthropic_api_key
        self.max_turns = _agent_max_turns() if max_turns is None else int(max_turns)
        self._live_models: list[ModelInfo] | None = None

    # --- capability -------------------------------------------------------
    @staticmethod
    def sdk_available() -> tuple[bool, str]:
        """Return whether ``claude_agent_sdk`` can be imported, and why not."""
        try:
            import claude_agent_sdk  # noqa: F401
        except Exception as exc:  # noqa: BLE001 - any import failure is "unusable"
            return False, f"claude-agent-sdk is not importable: {exc}"
        return True, "claude-agent-sdk is installed"

    @staticmethod
    def cli_path() -> str | None:
        """Return the path of a local Claude Code CLI, if one is installed.

        Mirrors the search the SDK's subprocess transport performs: a CLI
        bundled inside the wheel, then ``claude`` on ``PATH``, then the
        well-known install locations.
        """
        try:
            import claude_agent_sdk

            bundled = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
            if bundled.is_file():
                return str(bundled)
        except Exception:  # noqa: BLE001 - fall through to the PATH search
            logger.debug("bundled CLI probe failed", exc_info=True)
        found = shutil.which("claude")
        if found:
            return found
        for candidate in (
            Path.home() / ".npm-global/bin/claude",
            Path("/usr/local/bin/claude"),
            Path.home() / ".local/bin/claude",
            Path.home() / "node_modules/.bin/claude",
            Path.home() / ".yarn/bin/claude",
            Path.home() / ".claude/local/claude",
        ):
            if candidate.is_file():
                return str(candidate)
        return None

    async def capability(self, *, probe: bool = False) -> ProviderCapability:
        """Probe API-key auth, then local Claude Code CLI auth; report what works."""
        installed, detail = self.sdk_available()
        if not installed:
            return ProviderCapability(self.name, False, "none", detail)
        models = await self.models()
        cli = self.cli_path()
        if self.api_key:
            auth_mode = "api_key"
            message = "ANTHROPIC_API_KEY is configured"
            if cli is None:
                message += "; no local Claude Code CLI was found, which the SDK needs to spawn"
                return ProviderCapability(self.name, False, auth_mode, message, models)
        elif cli is not None:
            auth_mode = "claude_cli"
            message = (
                f"no ANTHROPIC_API_KEY; using the local Claude Code CLI at {cli}. "
                "Whether that CLI is logged in is only proven by a real request."
            )
        else:
            return ProviderCapability(
                self.name,
                False,
                "none",
                "no ANTHROPIC_API_KEY and no local Claude Code CLI was found",
                models,
            )
        if not probe:
            return ProviderCapability(self.name, True, auth_mode, message, models)
        try:
            completion = await self.complete(
                model=self.default_model(),
                system="Answer with one word.",
                messages=[{"role": "user", "content": _PROBE_PROMPT}],
                max_tokens=16,
            )
        except Exception as exc:  # noqa: BLE001 - a probe failure is a capability answer
            return ProviderCapability(
                self.name,
                False,
                auth_mode,
                f"live probe failed: {type(exc).__name__}: {exc}",
                models,
                verified=False,
            )
        return ProviderCapability(
            self.name,
            True,
            auth_mode,
            f"{message} - live probe answered {completion.text.strip()[:40]!r} "
            f"({completion.usage.total_tokens} tokens, ${completion.usage.cost_usd:.6f})",
            models,
            verified=True,
        )

    def default_model(self) -> str:
        """The model used when a stage does not name one."""
        return os.environ.get("TRADINGLIB_ANTHROPIC_MODEL", "claude-sonnet-4-6")

    # --- models -----------------------------------------------------------
    async def models(self) -> list[ModelInfo]:
        """List Anthropic models usable with the detected auth mode.

        With an API key the live Models API is asked first; without one (CLI
        auth) there is no list endpoint, so the maintained price table is the
        catalogue. Either way every entry carries a real price.
        """
        if self._live_models is not None:
            return list(self._live_models)
        live = await self._fetch_live_models() if self.api_key else None
        if live:
            self._live_models = live
            return list(live)
        catalogue = [
            ModelInfo(
                id=model,
                provider=self.name,
                label=_anthropic_label(model),
                input_cost_per_mtok=price.input_per_mtok,
                output_cost_per_mtok=price.output_per_mtok,
                context_window=price.context_window,
                max_output_tokens=price.max_output_tokens,
                price_source=price.source,
                price_as_of=price.as_of,
            )
            for model, price in ANTHROPIC_PRICES.items()
        ]
        return catalogue

    async def _fetch_live_models(self) -> list[ModelInfo] | None:
        """Ask the Anthropic Models API which models this key can use."""
        try:
            import httpx
        except Exception:  # noqa: BLE001 - httpx is a hard dependency, but be safe
            return None
        base = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
        headers = {"x-api-key": self.api_key or "", "anthropic-version": "2023-06-01"}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(f"{base}/v1/models?limit=100", headers=headers)
            if response.status_code != 200:
                logger.info(
                    "anthropic models API answered %s; using the maintained catalogue",
                    response.status_code,
                )
                return None
            payload = response.json()
        except Exception as exc:  # noqa: BLE001 - offline is a normal state here
            logger.info("anthropic models API unreachable (%s); using the catalogue", exc)
            return None
        out: list[ModelInfo] = []
        for item in payload.get("data", []):
            model_id = str(item.get("id", "")).strip()
            if not model_id:
                continue
            price = lookup_price(self.name, model_id)
            out.append(
                ModelInfo(
                    id=model_id,
                    provider=self.name,
                    label=str(item.get("display_name") or _anthropic_label(model_id)),
                    input_cost_per_mtok=price.input_per_mtok if price else None,
                    output_cost_per_mtok=price.output_per_mtok if price else None,
                    context_window=(
                        int(item["max_input_tokens"])
                        if item.get("max_input_tokens")
                        else (price.context_window if price else None)
                    ),
                    max_output_tokens=(
                        int(item["max_tokens"])
                        if item.get("max_tokens")
                        else (price.max_output_tokens if price else None)
                    ),
                    price_source=price.source if price else "unknown",
                    price_as_of=price.as_of if price else None,
                    note=None if price else "no price on file; cost cannot be computed",
                )
            )
        return out or None

    # --- completion -------------------------------------------------------
    async def complete(
        self,
        *,
        model: str,
        system: str | None,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec] | None = None,
        tool_executor: ToolExecutor | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
        budget_usd: float | None = None,
        timeout: float | None = None,
    ) -> Completion:
        """Run a completion through the Claude Agent SDK.

        ``temperature`` and ``max_tokens`` are accepted for interface parity;
        the Agent SDK does not expose them (the CLI owns sampling), so they are
        not silently pretended to have been applied - they are simply unused
        and recorded in ``raw`` so a caller can see that.
        """
        installed, detail = self.sdk_available()
        if not installed:
            raise ProviderNotAvailable(detail)
        if self.api_key is None and self.cli_path() is None:
            raise ProviderNotAvailable(
                "anthropic is not configured: no ANTHROPIC_API_KEY and no local Claude Code CLI"
            )
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ResultMessage,
            TextBlock,
            ToolUseBlock,
            create_sdk_mcp_server,
            query,
        )

        specs = list(tools or [])
        sdk_tools = (
            [self._sdk_tool(spec, tool_executor) for spec in specs]
            if specs and tool_executor is not None
            else []
        )
        allowed = [f"mcp__{self.MCP_SERVER}__{spec.name}" for spec in specs] if sdk_tools else []
        mcp_servers: dict[str, Any] = (
            {self.MCP_SERVER: create_sdk_mcp_server(name=self.MCP_SERVER, tools=sdk_tools)}
            if sdk_tools
            else {}
        )
        # `options.env` is an OVERLAY, not the child's whole environment: the
        # SDK already inherits os.environ and then deliberately strips
        # CLAUDECODE and forces CLAUDE_CODE_ENTRYPOINT=sdk-py, so that a CLI it
        # spawns does not believe it is running inside an enclosing Claude Code
        # session. Copying os.environ in here put those variables straight back
        # and the child CLI died with SIGSEGV (exit -11) whenever the server had
        # itself been started from a Claude Code terminal. Only what we mean to
        # add belongs here.
        env: dict[str, str] = {}
        if self.api_key:
            env["ANTHROPIC_API_KEY"] = self.api_key
        options = ClaudeAgentOptions(
            model=model,
            system_prompt=system,
            # No built-in Claude Code tools: an TradingLib agent reaches the world
            # only through the TradingLib tools registered above.
            tools=[],
            allowed_tools=allowed,
            mcp_servers=mcp_servers,
            permission_mode="dontAsk",
            setting_sources=[],
            max_turns=self.max_turns,
            max_budget_usd=budget_usd,
            env=env,
            # Keep the CLI's own diagnostics: when the subprocess dies the SDK
            # only reports the exit code, and without this the reason is lost.
            stderr=_log_cli_stderr,
        )

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        usage = Usage()
        stop_reason: str | None = None
        raw: dict[str, Any] = {
            "max_tokens_ignored": max_tokens,
            "temperature_ignored": temperature,
        }

        async def _drain() -> None:
            nonlocal usage, stop_reason
            async for message in query(prompt=render_messages(messages), options=options):
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            text_parts.append(block.text)
                        elif isinstance(block, ToolUseBlock):
                            tool_calls.append(
                                ToolCall(
                                    id=str(block.id),
                                    name=_strip_mcp_prefix(str(block.name), self.MCP_SERVER),
                                    arguments=dict(block.input or {}),
                                )
                            )
                elif isinstance(message, ResultMessage):
                    usage = _usage_from_result(message)
                    stop_reason = message.stop_reason or message.subtype
                    raw["session_id"] = message.session_id
                    raw["num_turns"] = message.num_turns
                    raw["duration_ms"] = message.duration_ms
                    if message.is_error:
                        raw["error"] = message.result or "the SDK reported an error result"

        try:
            if timeout:
                await asyncio.wait_for(_drain(), timeout=timeout)
            else:
                await _drain()
        except TimeoutError as exc:
            raise ProviderError(f"anthropic completion timed out after {timeout}s") from exc
        except Exception as exc:  # noqa: BLE001 - SDK/transport errors are provider errors
            raise ProviderError(f"{type(exc).__name__}: {exc}") from exc

        return Completion(
            text="\n".join(part for part in text_parts if part).strip(),
            usage=price_usage(self.name, model, usage, settings=self._settings),
            model=model,
            provider=self.name,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            raw=raw,
        )

    def _sdk_tool(self, spec: ToolSpec, executor: ToolExecutor | None) -> Any:
        """Wrap one :class:`ToolSpec` as an in-process SDK MCP tool."""
        from claude_agent_sdk import tool as sdk_tool

        async def handler(args: dict[str, Any]) -> dict[str, Any]:
            """Execute the library tool and return its MCP content payload."""
            if executor is None:  # pragma: no cover - guarded by the caller
                return {
                    "content": [{"type": "text", "text": "no tool executor is bound"}],
                    "is_error": True,
                }
            result = await executor(spec.name, dict(args or {}))
            return {
                "content": [{"type": "text", "text": result.content}],
                "is_error": result.is_error,
            }

        return sdk_tool(spec.name, spec.description, spec.input_schema)(handler)


def _anthropic_label(model_id: str) -> str:
    """Turn a model id into the label the picker shows."""
    return model_id.replace("claude-", "Claude ").replace("-", " ").title()


def _strip_mcp_prefix(name: str, server: str) -> str:
    """Return the bare tool name from an ``mcp__<server>__<tool>`` name."""
    prefix = f"mcp__{server}__"
    return name[len(prefix):] if name.startswith(prefix) else name


def _usage_from_result(message: Any) -> Usage:
    """Build :class:`Usage` from the SDK's ``ResultMessage``.

    The SDK reports its own dollar cost (``total_cost_usd``); when it is present
    it wins over the price table because it is what the vendor actually billed.
    """
    raw = message.usage or {}
    input_tokens = int(raw.get("input_tokens", 0) or 0)
    output_tokens = int(raw.get("output_tokens", 0) or 0)
    cache_read = int(raw.get("cache_read_input_tokens", 0) or 0)
    cache_write = int(raw.get("cache_creation_input_tokens", 0) or 0)
    model_usage = getattr(message, "model_usage", None) or {}
    if not (input_tokens or output_tokens) and model_usage:
        for entry in model_usage.values():
            input_tokens += int(entry.get("inputTokens", 0) or 0)
            output_tokens += int(entry.get("outputTokens", 0) or 0)
            cache_read += int(entry.get("cacheReadInputTokens", 0) or 0)
            cache_write += int(entry.get("cacheCreationInputTokens", 0) or 0)
    cost = float(message.total_cost_usd or 0.0)
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        cost_usd=cost,
        cost_known=True,
    )


# --------------------------------------------------------------------------
# OpenAI
# --------------------------------------------------------------------------
#: Model id prefixes the picker offers; other ids the account can see (audio,
#: embeddings, images, moderation) are not chat models and are filtered out.
OPENAI_CHAT_PREFIXES: Final[tuple[str, ...]] = ("gpt-", "o1", "o3", "o4", "chatgpt-")

#: Ids matching a chat prefix that are nonetheless not chat completions models.
OPENAI_EXCLUDED_MARKERS: Final[tuple[str, ...]] = (
    "audio",
    "realtime",
    "transcribe",
    "tts",
    "image",
    "search-preview",
    "instruct",
    "embedding",
    "moderation",
)


class OpenAIProvider(LLMProvider):
    """OpenAI provider backed by the ``openai`` package."""

    name = "openai"
    executes_tools = False

    def __init__(self, *, api_key: str | None = None, settings: Settings | None = None) -> None:
        """Create the provider; without a key it reports itself unavailable."""
        self._settings = settings
        self.api_key = api_key if api_key is not None else (settings or get_settings()).openai_api_key
        self._client: Any | None = None
        self._models_cache: list[ModelInfo] | None = None

    def _get_client(self) -> Any:
        """Return the async OpenAI client, creating it on first use."""
        if self.api_key is None:
            raise ProviderNotAvailable("openai is not configured: no OPENAI_API_KEY")
        if self._client is None:
            try:
                from openai import AsyncOpenAI
            except Exception as exc:  # noqa: BLE001
                raise ProviderNotAvailable(f"the openai package is not importable: {exc}") from exc
            self._client = AsyncOpenAI(api_key=self.api_key)
        return self._client

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.close()
            self._client = None

    async def capability(self, *, probe: bool = False) -> ProviderCapability:
        """Report whether an OpenAI key is configured and the API answers."""
        if self.api_key is None:
            return ProviderCapability(
                self.name, False, "none", "OPENAI_API_KEY is not configured", []
            )
        try:
            models = await self.models()
        except ProviderError as exc:
            return ProviderCapability(self.name, False, "api_key", str(exc), [])
        if not models:
            return ProviderCapability(
                self.name,
                False,
                "api_key",
                "the configured key returned no chat models",
                [],
            )
        capability = ProviderCapability(
            self.name,
            True,
            "api_key",
            f"OPENAI_API_KEY is configured; {len(models)} chat models are visible",
            models,
            verified=True,
        )
        if not probe:
            return capability
        try:
            completion = await self.complete(
                model=models[0].id,
                system="Answer with one word.",
                messages=[{"role": "user", "content": _PROBE_PROMPT}],
                max_tokens=16,
            )
        except Exception as exc:  # noqa: BLE001 - a probe failure is a capability answer
            return ProviderCapability(
                self.name, False, "api_key", f"live probe failed: {exc}", models
            )
        return ProviderCapability(
            self.name,
            True,
            "api_key",
            f"live probe answered {completion.text.strip()[:40]!r}",
            models,
            verified=True,
        )

    async def models(self) -> list[ModelInfo]:
        """List models the configured key can access.

        The list is genuinely the account's - ``GET /v1/models`` - filtered to
        chat-completions models. Prices come from the maintained table; a model
        the table does not know is offered with ``priced: false`` and a note,
        never with a made-up rate.
        """
        if self._models_cache is not None:
            return list(self._models_cache)
        client = self._get_client()
        try:
            listing = await client.models.list()
        except Exception as exc:  # noqa: BLE001 - vendor/network failure
            raise ProviderError(f"listing OpenAI models failed: {exc}") from exc
        out: list[ModelInfo] = []
        for item in listing.data:
            model_id = str(getattr(item, "id", "")).strip()
            if not model_id or not model_id.startswith(OPENAI_CHAT_PREFIXES):
                continue
            if any(marker in model_id for marker in OPENAI_EXCLUDED_MARKERS):
                continue
            price = lookup_price(self.name, model_id, settings=self._settings)
            out.append(
                ModelInfo(
                    id=model_id,
                    provider=self.name,
                    label=model_id,
                    input_cost_per_mtok=price.input_per_mtok if price else None,
                    output_cost_per_mtok=price.output_per_mtok if price else None,
                    context_window=price.context_window if price else None,
                    max_output_tokens=price.max_output_tokens if price else None,
                    price_source=price.source if price else "unknown",
                    price_as_of=price.as_of if price else None,
                    note=None if price else "no price on file; cost cannot be computed",
                )
            )
        out.sort(key=lambda model: model.id)
        self._models_cache = out
        return list(out)

    async def complete(
        self,
        *,
        model: str,
        system: str | None,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec] | None = None,
        tool_executor: ToolExecutor | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
        budget_usd: float | None = None,
        timeout: float | None = None,
    ) -> Completion:
        """Run a completion through the OpenAI Chat Completions API.

        Tool calls are returned to the caller (``executes_tools`` is ``False``);
        the pipeline runner executes them and calls back with the results.
        """
        client = self._get_client()
        payload: list[dict[str, Any]] = []
        if system:
            payload.append({"role": "system", "content": system})
        payload.extend(_openai_messages(messages))
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": payload,
            "max_completion_tokens": max_tokens,
        }
        if tools:
            kwargs["tools"] = [spec.as_openai_tool() for spec in tools]
            kwargs["tool_choice"] = "auto"
        if temperature:
            kwargs["temperature"] = temperature
        if timeout:
            kwargs["timeout"] = timeout
        try:
            response = await client.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 - vendor/network failure
            raise ProviderError(f"{type(exc).__name__}: {exc}") from exc

        choice = response.choices[0]
        message = choice.message
        calls: list[ToolCall] = []
        for call in getattr(message, "tool_calls", None) or []:
            function = getattr(call, "function", None)
            if function is None:
                continue
            try:
                arguments = json.loads(function.arguments or "{}")
            except ValueError:
                arguments = {"__raw_arguments": function.arguments}
            calls.append(
                ToolCall(
                    id=str(call.id), name=str(function.name), arguments=dict(arguments)
                )
            )
        raw_usage = getattr(response, "usage", None)
        cached = 0
        if raw_usage is not None:
            details = getattr(raw_usage, "prompt_tokens_details", None)
            cached = int(getattr(details, "cached_tokens", 0) or 0) if details else 0
        usage = Usage(
            input_tokens=max(int(getattr(raw_usage, "prompt_tokens", 0) or 0) - cached, 0),
            output_tokens=int(getattr(raw_usage, "completion_tokens", 0) or 0),
            cache_read_tokens=cached,
        )
        return Completion(
            text=message.content or "",
            usage=price_usage(self.name, model, usage, settings=self._settings),
            model=str(getattr(response, "model", model)),
            provider=self.name,
            tool_calls=calls,
            stop_reason=str(choice.finish_reason) if choice.finish_reason else None,
            raw={"id": getattr(response, "id", None)},
        )


def _openai_messages(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Convert TradingLib's message list into OpenAI's chat format.

    TradingLib carries tool results as ``{"role": "tool", "tool_call_id": ..., ...}``
    entries and assistant tool calls as ``{"role": "assistant", "tool_calls":
    [...]}``; both map one-to-one onto the OpenAI shape.
    """
    out: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role", "user"))
        if role == "tool":
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": str(message.get("tool_call_id", "")),
                    "content": str(message.get("content", "")),
                }
            )
            continue
        entry: dict[str, Any] = {"role": role, "content": message.get("content") or ""}
        calls = message.get("tool_calls")
        if calls:
            entry["tool_calls"] = [
                {
                    "id": call["id"],
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": json.dumps(call.get("arguments") or {}),
                    },
                }
                for call in calls
            ]
        out.append(entry)
    return out


# --------------------------------------------------------------------------
# deterministic test stub
# --------------------------------------------------------------------------
@dataclass(slots=True)
class MockTurn:
    """One scripted response from :class:`MockProvider`."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = "end_turn"
    #: Extra output tokens to charge, on top of the deterministic count.
    extra_output_tokens: int = 0


@dataclass(slots=True)
class MockRequest:
    """What the stub was asked for, so a responder can react to it."""

    model: str
    system: str | None
    messages: list[dict[str, Any]]
    tools: list[ToolSpec]
    call_index: int


class MockProvider(LLMProvider):
    """Deterministic, offline LLM stub. **Tests only - never a real model.**

    It exists so the runner's loop, contracts, budgets and cost arithmetic can
    be exercised without a vendor. Its usage figures are computed from the real
    prompt and response text with a fixed 4-characters-per-token rule, so cost
    accounting does real arithmetic on reproducible inputs. It is registered in
    the provider registry only when explicitly enabled, and always reports
    itself as the stub it is.
    """

    name = "mock"
    executes_tools = False

    #: Characters per token in the stub's deterministic tokenizer.
    CHARS_PER_TOKEN: Final[int] = 4

    def __init__(
        self,
        *,
        turns: Sequence[MockTurn] | None = None,
        responder: Callable[[MockRequest], MockTurn] | None = None,
        default_text: str = "mock completion",
    ) -> None:
        """Create the stub.

        Args:
            turns: scripted responses, consumed in order.
            responder: called for every request when the script is exhausted;
                lets a test react to tool results.
            default_text: answer used when neither a script nor a responder
                produced one.
        """
        self._turns: deque[MockTurn] = deque(turns or [])
        self._responder = responder
        self._default_text = default_text
        #: Every request the stub received, for assertions.
        self.requests: list[MockRequest] = []

    async def capability(self, *, probe: bool = False) -> ProviderCapability:
        """Report the stub honestly: available, but not a real provider."""
        return ProviderCapability(
            self.name,
            True,
            "none",
            "deterministic offline stub used by the test suite - not a real LLM",
            await self.models(),
            verified=True,
        )

    async def models(self) -> list[ModelInfo]:
        """Return the stub's two priced pseudo-models."""
        return [
            ModelInfo(
                id=model,
                provider=self.name,
                label=f"Mock {model.split('-')[-1]} (test stub)",
                input_cost_per_mtok=price.input_per_mtok,
                output_cost_per_mtok=price.output_per_mtok,
                context_window=price.context_window,
                max_output_tokens=price.max_output_tokens,
                price_source="mock",
                price_as_of=price.as_of,
                note="deterministic test stub; not a real model",
            )
            for model, price in MOCK_PRICES.items()
        ]

    def tokens(self, text: str) -> int:
        """Deterministic token count for a piece of text."""
        return max(1, (len(text) + self.CHARS_PER_TOKEN - 1) // self.CHARS_PER_TOKEN)

    async def complete(
        self,
        *,
        model: str,
        system: str | None,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec] | None = None,
        tool_executor: ToolExecutor | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
        budget_usd: float | None = None,
        timeout: float | None = None,
    ) -> Completion:
        """Return the next scripted response with deterministic usage."""
        request = MockRequest(
            model=model,
            system=system,
            messages=[dict(message) for message in messages],
            tools=list(tools or []),
            call_index=len(self.requests),
        )
        self.requests.append(request)
        if self._turns:
            turn = self._turns.popleft()
        elif self._responder is not None:
            turn = self._responder(request)
        else:
            turn = MockTurn(text=self._default_text)
        prompt = render_messages(messages, system=system)
        output_text = turn.text + "".join(
            json.dumps(call.arguments, sort_keys=True) for call in turn.tool_calls
        )
        usage = Usage(
            input_tokens=self.tokens(prompt),
            output_tokens=self.tokens(output_text) + turn.extra_output_tokens,
        )
        calls = [
            ToolCall(id=call.id or f"mock-call-{index}", name=call.name, arguments=call.arguments)
            for index, call in enumerate(turn.tool_calls)
        ]
        return Completion(
            text=turn.text,
            usage=price_usage(self.name, model, usage),
            model=model,
            provider=self.name,
            tool_calls=calls,
            stop_reason="tool_use" if calls else turn.stop_reason,
            raw={"stub": True},
        )


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------
def _env_flag(name: str, default: bool = False) -> bool:
    """Read a boolean environment flag (``1/true/yes/on`` are true)."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class ProviderRegistry:
    """Resolves provider instances and aggregates the model picker's contents.

    Capability probes are cached for :attr:`cache_ttl` seconds because they can
    spawn a CLI or call a vendor; ``refresh=True`` forces a re-probe (the
    Configuration tab's "re-check" does that).
    """

    #: Seconds a capability report is reused before it is re-computed.
    cache_ttl: float = 60.0

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        providers: Mapping[str, LLMProvider] | None = None,
        include_mock: bool | None = None,
    ) -> None:
        """Create the registry.

        Args:
            settings: settings the providers read their keys from.
            providers: explicit provider instances (tests inject the stub).
            include_mock: register the deterministic stub. Defaults to the
                ``TRADINGLIB_ENABLE_MOCK_PROVIDER`` flag, which is off in a real
                server.
        """
        self._settings = settings
        self._providers: dict[str, LLMProvider] = dict(providers or {})
        if not self._providers:
            self._providers = {
                "anthropic": AnthropicProvider(settings=settings),
                "openai": OpenAIProvider(settings=settings),
            }
            enable_mock = (
                _env_flag("TRADINGLIB_ENABLE_MOCK_PROVIDER") if include_mock is None else include_mock
            )
            if enable_mock:
                self._providers["mock"] = MockProvider()
        self._cache: dict[str, tuple[float, ProviderCapability]] = {}

    # --- registration -----------------------------------------------------
    def register(self, provider: LLMProvider) -> LLMProvider:
        """Add or replace a provider instance and drop its cached capability."""
        self._providers[provider.name] = provider
        self._cache.pop(provider.name, None)
        return provider

    def names(self) -> list[str]:
        """Registered provider names, in a stable order."""
        return sorted(self._providers)

    def get(self, provider: str) -> LLMProvider:
        """Return a provider by name.

        Raises:
            KeyError: when no such provider is registered.
        """
        try:
            return self._providers[provider]
        except KeyError as exc:
            raise KeyError(f"unknown provider {provider!r}") from exc

    def has(self, provider: str) -> bool:
        """Whether a provider with this name is registered."""
        return provider in self._providers

    # --- capability -------------------------------------------------------
    async def capability(self, provider: str, *, refresh: bool = False) -> ProviderCapability:
        """Return one provider's capability report, cached briefly."""
        instance = self.get(provider)
        cached = self._cache.get(provider)
        if cached is not None and not refresh and (time.monotonic() - cached[0]) < self.cache_ttl:
            return cached[1]
        try:
            report = await instance.capability()
        except Exception as exc:  # noqa: BLE001 - an unavailable provider is data
            report = ProviderCapability(
                provider, False, "unknown", f"{type(exc).__name__}: {exc}", []
            )
        self._cache[provider] = (time.monotonic(), report)
        return report

    async def capabilities(self, *, refresh: bool = False) -> list[ProviderCapability]:
        """Probe every provider and report honestly what is usable."""
        return [await self.capability(name, refresh=refresh) for name in self.names()]

    async def available_models(self, *, refresh: bool = False) -> list[ModelInfo]:
        """Return every model from providers that verified as available."""
        models: list[ModelInfo] = []
        for report in await self.capabilities(refresh=refresh):
            if report.available:
                models.extend(report.models)
        return models

    async def models_payload(self, *, refresh: bool = False) -> dict[str, Any]:
        """Return the payload behind ``GET /api/agents/models``."""
        reports = await self.capabilities(refresh=refresh)
        models = [model for report in reports if report.available for model in report.models]
        return {
            "providers": [report.as_dict(include_models=False) for report in reports],
            "models": [model.as_dict() for model in models],
            "default": self.default_model(reports),
            "prices": {
                "anthropic_as_of": ANTHROPIC_PRICES_AS_OF,
                "openai_as_of": OPENAI_PRICES_AS_OF,
                "override_file": str(_price_override_path(self._settings)),
                "overrides": len(load_price_overrides(self._settings)),
            },
            "generated_at": _utcnow().isoformat(),
        }

    @staticmethod
    def default_model(reports: Sequence[ProviderCapability]) -> dict[str, str] | None:
        """Pick a sensible default model from what is actually available."""
        for report in reports:
            if report.available and report.models:
                return {"provider": report.provider, "model": report.models[0].id}
        return None

    async def resolve(self, provider: str | None, model: str | None) -> tuple[LLMProvider, str]:
        """Return the provider instance and model id a stage should use.

        Raises:
            KeyError: when the named provider is not registered.
            ProviderNotAvailable: when it is registered but not usable.
            ValueError: when no model can be determined.
        """
        if provider:
            instance = self.get(provider)
        else:
            reports = await self.capabilities()
            available = [report for report in reports if report.available]
            if not available:
                raise ProviderNotAvailable(
                    "no LLM provider is configured; set ANTHROPIC_API_KEY or OPENAI_API_KEY, "
                    "or log in to the local Claude Code CLI"
                )
            instance = self.get(available[0].provider)
        report = await self.capability(instance.name)
        if not report.available:
            raise ProviderNotAvailable(f"{instance.name} is not usable: {report.detail}")
        if model:
            return instance, model
        models = await instance.models()
        if not models:
            raise ValueError(f"{instance.name} exposes no models to run with")
        return instance, models[0].id

    async def aclose(self) -> None:
        """Close every registered provider."""
        for provider in self._providers.values():
            with contextlib.suppress(Exception):
                await provider.aclose()


_registry: ProviderRegistry | None = None


def get_provider_registry() -> ProviderRegistry:
    """Return the process-wide provider registry, creating it on first use."""
    global _registry
    if _registry is None:
        _registry = ProviderRegistry()
    return _registry


def reset_provider_registry(registry: ProviderRegistry | None = None) -> ProviderRegistry:
    """Install (or forget) the process-wide provider registry. Returns it."""
    global _registry
    _registry = registry if registry is not None else ProviderRegistry()
    return _registry


def new_tool_call_id() -> str:
    """Return a unique identifier for a tool call the runner synthesises."""
    return f"call-{uuid.uuid4().hex[:12]}"


__all__ = [
    "ANTHROPIC_PRICES",
    "ANTHROPIC_PRICES_AS_OF",
    "AnthropicProvider",
    "BudgetExceeded",
    "Completion",
    "LLMProvider",
    "MOCK_PRICES",
    "MockProvider",
    "MockRequest",
    "MockTurn",
    "ModelInfo",
    "ModelPrice",
    "OPENAI_PRICES",
    "OPENAI_PRICES_AS_OF",
    "OpenAIProvider",
    "PRICE_TABLE",
    "ProviderCapability",
    "ProviderError",
    "ProviderNotAvailable",
    "ProviderRegistry",
    "SpendEntry",
    "SpendLedger",
    "ToolCall",
    "ToolExecutor",
    "ToolResult",
    "ToolSpec",
    "Usage",
    "get_provider_registry",
    "load_price_overrides",
    "lookup_price",
    "new_tool_call_id",
    "price_usage",
    "render_messages",
    "reset_provider_registry",
]
