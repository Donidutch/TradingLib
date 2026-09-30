"""Screener rule and expression engine.

A screen is an ordered list of :class:`Rule` objects applied as a funnel: each
rule receives the names that survived the previous one, which is what makes the
"3,214 -> 812 -> 47" pass funnel in the UI meaningful. Two kinds of rule exist:

* **row-wise rules** (``>``, ``<``, ``between``, ``in``, ...) test one row at a
  time and could be applied in any order;
* **cross-sectional rules** (``top_pctl`` / ``bottom_pctl``) rank the names
  *currently surviving* - optionally within their sector - and keep the best
  slice. Their result depends on where they sit in the funnel, so the engine
  applies rules strictly in order.

Free-form expressions (``rev_growth > 15 and gross_margin > 40``) are parsed
with :mod:`ast` and validated against the metric whitelist below; ``eval`` is
never called on user text. Expressions are row-wise only - a percentile cut has
no meaning without knowing which rows precede it.

Units are the units the UI types: percentages are percent numbers (``15`` means
15%), money is in the reporting currency (US dollars for the built-in
universes), multiples are plain ratios (``2.0`` means 2.0x). Metrics a company
does not report are ``NaN``, and **a NaN never passes a rule** - a screen that
demands a 15% growth rate must not admit a company whose growth is unknown.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Final

import numpy as np
import pandas as pd

from tradinglib.data.base import (
    BarInterval,
    DataProvider,
    Fundamentals,
    ProviderError,
    utcnow,
)
from tradinglib.data.cache import DataCache, get_cache, provider_key
from tradinglib.data.store import TimeSeriesStore, get_store
from tradinglib.research.company import derive_metrics, region_for

logger = logging.getLogger(__name__)

#: Trading sessions per year, used to annualise volatility.
SESSIONS_PER_YEAR: Final[int] = 252

#: Look-back windows (in sessions) of the price-derived metrics.
WINDOW_1M: Final[int] = 21
WINDOW_3M: Final[int] = 63
WINDOW_6M: Final[int] = 126
WINDOW_12M: Final[int] = 252

#: Calendar days of daily bars loaded to compute the momentum windows.
PRICE_LOOKBACK_DAYS: Final[int] = 460

#: Default ceiling on how many symbols one screen run will price and value.
DEFAULT_MAX_SYMBOLS: Final[int] = 250


class Operator(StrEnum):
    """Comparison operators available to a simple rule."""

    GT = ">"
    GTE = ">="
    LT = "<"
    LTE = "<="
    EQ = "=="
    NEQ = "!="
    BETWEEN = "between"
    IN = "in"
    NOT_IN = "not_in"
    TOP_PCTL = "top_pctl"
    BOTTOM_PCTL = "bottom_pctl"

    @property
    def cross_sectional(self) -> bool:
        """Whether the operator ranks the surviving set instead of one row."""
        return self in (Operator.TOP_PCTL, Operator.BOTTOM_PCTL)


class Scope(StrEnum):
    """Population a cross-sectional rule ranks within."""

    ALL = "all"
    SECTOR = "sector"


@dataclass(frozen=True, slots=True)
class MetricDef:
    """One screenable metric: what it means, its unit and where it comes from."""

    key: str
    label: str
    unit: str
    source: str
    description: str
    categorical: bool = False
    higher_is_better: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of the definition."""
        return {
            "key": self.key,
            "label": self.label,
            "unit": self.unit,
            "source": self.source,
            "description": self.description,
            "categorical": self.categorical,
            "higher_is_better": self.higher_is_better,
        }


def _metric(
    key: str,
    label: str,
    unit: str,
    source: str,
    description: str,
    *,
    categorical: bool = False,
    higher_is_better: bool | None = None,
) -> MetricDef:
    """Build a :class:`MetricDef` (keeps the table below readable)."""
    return MetricDef(
        key=key,
        label=label,
        unit=unit,
        source=source,
        description=description,
        categorical=categorical,
        higher_is_better=higher_is_better,
    )


#: Every metric a rule or expression may reference.
METRICS: Final[dict[str, MetricDef]] = {
    m.key: m
    for m in (
        _metric("symbol", "Symbol", "cat", "universe", "Ticker.", categorical=True),
        _metric("name", "Name", "cat", "provider", "Company name.", categorical=True),
        _metric("sector", "Sector", "cat", "provider", "Vendor sector.", categorical=True),
        _metric("industry", "Industry", "cat", "provider", "Vendor industry.", categorical=True),
        _metric(
            "country", "Country", "cat", "provider", "Country of domicile.", categorical=True
        ),
        _metric(
            "region",
            "Region",
            "cat",
            "derived",
            "Region derived from the country of domicile.",
            categorical=True,
        ),
        _metric("exchange", "Exchange", "cat", "provider", "Listing venue.", categorical=True),
        _metric("price", "Price", "usd", "prices", "Last stored close.", higher_is_better=None),
        _metric(
            "mkt_cap",
            "Market cap",
            "usd",
            "fundamentals",
            "Vendor market capitalisation, or shares outstanding x last price.",
            higher_is_better=True,
        ),
        _metric(
            "adv_usd",
            "ADV (60d, $)",
            "usd",
            "prices",
            "Mean close x volume over the last 63 sessions - the liquidity screen.",
            higher_is_better=True,
        ),
        _metric(
            "adv_shares",
            "ADV (60d, shares)",
            "num",
            "prices",
            "Mean daily share volume over the last 63 sessions.",
            higher_is_better=True,
        ),
        _metric(
            "rev_growth",
            "Revenue growth",
            "pct",
            "fundamentals",
            "Trailing-twelve-month revenue against the prior twelve months.",
            higher_is_better=True,
        ),
        _metric(
            "gross_margin",
            "Gross margin",
            "pct",
            "fundamentals",
            "Trailing gross profit over trailing revenue.",
            higher_is_better=True,
        ),
        _metric(
            "ebitda_margin",
            "EBITDA margin",
            "pct",
            "fundamentals",
            "Trailing EBITDA over trailing revenue.",
            higher_is_better=True,
        ),
        _metric(
            "fcf_margin",
            "FCF margin",
            "pct",
            "fundamentals",
            "Trailing free cash flow over trailing revenue.",
            higher_is_better=True,
        ),
        _metric(
            "net_margin",
            "Net margin",
            "pct",
            "fundamentals",
            "Trailing net income over trailing revenue.",
            higher_is_better=True,
        ),
        _metric(
            "roic",
            "ROIC",
            "pct",
            "fundamentals",
            "Trailing operating income over invested capital (no tax adjustment).",
            higher_is_better=True,
        ),
        _metric(
            "roe",
            "ROE",
            "pct",
            "fundamentals",
            "Trailing net income over shareholders' equity.",
            higher_is_better=True,
        ),
        _metric(
            "net_debt_ebitda",
            "Net debt / EBITDA",
            "x",
            "fundamentals",
            "(Total debt - cash) over trailing EBITDA; leverage screen.",
            higher_is_better=False,
        ),
        _metric(
            "ev_sales",
            "EV / Sales",
            "x",
            "fundamentals",
            "Enterprise value over trailing revenue.",
            higher_is_better=False,
        ),
        _metric(
            "ev_ebitda",
            "EV / EBITDA",
            "x",
            "fundamentals",
            "Enterprise value over trailing EBITDA.",
            higher_is_better=False,
        ),
        _metric(
            "pe", "P / E", "x", "fundamentals", "Market cap over trailing net income.",
            higher_is_better=False,
        ),
        _metric(
            "pb", "P / B", "x", "fundamentals", "Market cap over shareholders' equity.",
            higher_is_better=False,
        ),
        _metric(
            "fcf_yield",
            "FCF yield",
            "pct",
            "fundamentals",
            "Trailing free cash flow over market cap.",
            higher_is_better=True,
        ),
        _metric("mom_1m", "1M momentum", "pct", "prices", "Return over 21 sessions.", higher_is_better=True),
        _metric("mom_3m", "3M momentum", "pct", "prices", "Return over 63 sessions.", higher_is_better=True),
        _metric("mom_6m", "6M momentum", "pct", "prices", "Return over 126 sessions.", higher_is_better=True),
        _metric(
            "mom_12m", "12M momentum", "pct", "prices", "Return over 252 sessions.",
            higher_is_better=True,
        ),
        _metric(
            "mom_12_1",
            "12-1 momentum",
            "pct",
            "prices",
            "Return from 252 to 21 sessions ago: 12 months skipping the last one.",
            higher_is_better=True,
        ),
        _metric(
            "vol_3m",
            "3M volatility",
            "pct",
            "prices",
            "Annualised standard deviation of daily returns over 63 sessions.",
            higher_is_better=False,
        ),
        _metric(
            "quality",
            "Quality score",
            "num",
            "derived",
            (
                "0-100 composite: the mean cross-sectional percentile of gross margin, "
                "FCF margin, return on capital and (inverted) net-debt/EBITDA over the "
                "universe being screened. At least two components must be available."
            ),
            higher_is_better=True,
        ),
        _metric(
            "composite",
            "Composite score",
            "num",
            "derived",
            (
                "0-100 composite: the mean cross-sectional percentile of return on "
                "capital, 12-1 momentum and the quality score."
            ),
            higher_is_better=True,
        ),
        _metric("roic_pctl", "ROIC percentile", "num", "derived", "ROIC percentile in [0, 1]."),
        _metric(
            "mom_12_1_pctl", "12-1 momentum percentile", "num", "derived", "Momentum percentile."
        ),
        _metric("quality_pctl", "Quality percentile", "num", "derived", "Quality percentile."),
    )
}

#: Metrics that may be used to rank a result set.
RANKABLE: Final[tuple[str, ...]] = tuple(
    key for key, spec in METRICS.items() if not spec.categorical
)

#: Fields an expression may reference.
ALLOWED_FIELDS: Final[frozenset[str]] = frozenset(METRICS)


class RuleError(ValueError):
    """Raised when a rule or an expression is malformed."""


# ---------------------------------------------------------------------------
# rules and screens
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class Rule:
    """One screening criterion."""

    field: str
    operator: Operator
    value: Any = None
    value2: Any = None
    scope: Scope = Scope.ALL
    label: str | None = None
    enabled: bool = True
    source: str | None = None

    def __post_init__(self) -> None:
        """Coerce string inputs (API payloads) into the enums."""
        self.operator = Operator(self.operator)
        self.scope = Scope(self.scope)

    @property
    def unconstrained(self) -> bool:
        """Whether the rule filters nothing.

        The reference UI's category dropdown carries an explicit "Any" choice;
        it is a real state of the rule, not a missing value, so it is honoured
        here rather than being rejected as invalid.
        """
        if self.operator in (Operator.IN, Operator.NOT_IN):
            if self.value is None:
                return True
            if isinstance(self.value, str):
                return self.value.strip().lower() in {"", "any"}
            return not list(self.value)
        return False

    def describe(self) -> str:
        """Human-readable rendering used in the UI rule list."""
        spec = METRICS.get(self.field)
        name = spec.label if spec else self.field
        if self.operator.cross_sectional:
            direction = "top" if self.operator is Operator.TOP_PCTL else "bottom"
            where = " within sector" if self.scope is Scope.SECTOR else ""
            return f"{name} {direction} {_fmt_plain(self.value)}%{where}"
        if self.operator is Operator.BETWEEN:
            return f"{name} between {self._fmt(self.value)} and {self._fmt(self.value2)}"
        if self.operator in (Operator.IN, Operator.NOT_IN):
            if self.unconstrained:
                return f"{name} — any"
            values = self.value if isinstance(self.value, (list, tuple)) else [self.value]
            verb = "is" if self.operator is Operator.IN else "is not"
            return f"{name} {verb} {', '.join(str(v) for v in values)}"
        return f"{name} {self.operator.value} {self._fmt(self.value)}"

    def _fmt(self, value: Any) -> str:
        """Format a value in its metric's unit."""
        spec = METRICS.get(self.field)
        if spec is None or not isinstance(value, (int, float)) or isinstance(value, bool):
            return str(value)
        if spec.unit == "usd":
            return _fmt_usd(float(value))
        if spec.unit == "pct":
            return f"{_fmt_plain(value)}%"
        if spec.unit == "x":
            return f"{_fmt_plain(value)}x"
        return _fmt_plain(value)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of the rule."""
        return {
            "field": self.field,
            "operator": str(self.operator),
            "value": self.value,
            "value2": self.value2,
            "scope": str(self.scope),
            "label": self.label,
            "description": self.describe(),
            "enabled": self.enabled,
            "source": self.source,
            "unit": METRICS[self.field].unit if self.field in METRICS else None,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Rule:
        """Rebuild a rule from :meth:`as_dict` output (or an API payload)."""
        try:
            field_name = str(payload["field"])
            operator = Operator(str(payload.get("operator") or payload.get("op") or ">"))
        except (KeyError, ValueError) as exc:
            raise RuleError(f"invalid rule payload {payload!r}: {exc}") from exc
        scope_raw = str(payload.get("scope") or Scope.ALL)
        try:
            scope = Scope(scope_raw)
        except ValueError as exc:
            raise RuleError(f"unknown rule scope {scope_raw!r}") from exc
        return cls(
            field=field_name,
            operator=operator,
            value=payload.get("value"),
            value2=payload.get("value2"),
            scope=scope,
            label=payload.get("label"),
            enabled=bool(payload.get("enabled", True)),
            source=payload.get("source"),
        )


def _fmt_plain(value: Any) -> str:
    """Render a number without trailing zeros."""
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:g}"


def _fmt_usd(value: float) -> str:
    """Render a dollar amount the way the UI does ($2.0B, $850M)."""
    magnitude = abs(value)
    if magnitude >= 1e12:
        return f"${value / 1e12:.1f}T"
    if magnitude >= 1e9:
        return f"${value / 1e9:.1f}B"
    if magnitude >= 1e6:
        return f"${value / 1e6:.0f}M"
    return f"${value:,.0f}"


@dataclass(slots=True)
class Screen:
    """A saved screen: an ordered rule set over a universe."""

    id: str
    name: str
    universe_id: str
    rules: list[Rule] = field(default_factory=list)
    expression: str | None = None
    description: str | None = None
    rank_by: str = "composite"
    owner: str | None = None
    tags: list[str] = field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def active_rules(self) -> list[Rule]:
        """Enabled rules that actually constrain the result."""
        return [rule for rule in self.rules if rule.enabled and not rule.unconstrained]

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of the screen."""
        return {
            "id": self.id,
            "name": self.name,
            "universe_id": self.universe_id,
            "rules": [rule.as_dict() for rule in self.rules],
            "expression": self.expression,
            "description": self.description,
            "rank_by": self.rank_by,
            "owner": self.owner,
            "tags": list(self.tags),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def to_query(self, *, as_of: date | None = None) -> str:
        """Render the screen as its equivalent query text.

        This is the "View as query" panel's content. It is generated from the
        same rule objects the engine evaluates, so the text can never drift
        from what the screen actually does. Percentile rules are written as the
        percentile threshold they enforce (``top 30%`` -> ``pctl(...) > 0.70``).
        """
        lines = [
            f'screen(universe="{self.universe_id}"'
            + (f', as_of="{as_of.isoformat()}"' if as_of else "")
            + ")"
        ]
        for rule in self.rules:
            if not rule.enabled or rule.unconstrained:
                continue
            if rule.operator.cross_sectional:
                keep = _coerce_number(rule.value, rule.field) / 100.0
                by = ', by="sector"' if rule.scope is Scope.SECTOR else ""
                comparison = (
                    f"> {1 - keep:.2f}" if rule.operator is Operator.TOP_PCTL else f"< {keep:.2f}"
                )
                lines.append(f"  .filter(pctl({rule.field}{by}) {comparison})")
            elif rule.operator is Operator.BETWEEN:
                lines.append(
                    f"  .filter({rule.value} <= {rule.field} <= {rule.value2})"
                )
            elif rule.operator in (Operator.IN, Operator.NOT_IN):
                values = (
                    list(rule.value) if isinstance(rule.value, (list, tuple)) else [rule.value]
                )
                verb = "in" if rule.operator is Operator.IN else "not in"
                rendered = ", ".join(f'"{value}"' for value in values)
                lines.append(f"  .filter({rule.field} {verb} [{rendered}])")
            else:
                value = rule.value if not isinstance(rule.value, str) else f'"{rule.value}"'
                lines.append(f"  .filter({rule.field} {rule.operator.value} {value})")
        if self.expression:
            lines.append(f"  .filter({self.expression})")
        lines.append(f'  .rank_by("{self.rank_by}")')
        return "\n".join(lines)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Screen:
        """Rebuild a screen from :meth:`as_dict` output (or an API payload)."""
        return cls(
            id=str(payload["id"]),
            name=str(payload["name"]),
            universe_id=str(payload["universe_id"]),
            rules=[Rule.from_dict(r) for r in payload.get("rules") or []],
            expression=payload.get("expression"),
            description=payload.get("description"),
            rank_by=str(payload.get("rank_by") or "composite"),
            owner=payload.get("owner"),
            tags=list(payload.get("tags") or []),
        )


# ---------------------------------------------------------------------------
# expression compilation
# ---------------------------------------------------------------------------
_COMPARATORS: Final[dict[type[ast.cmpop], Callable[[Any, Any], Any]]] = {
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
}

_BINARY: Final[dict[type[ast.operator], Callable[[Any, Any], Any]]] = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.Mod: lambda a, b: a % b,
    ast.Pow: lambda a, b: a**b,
}


class CompiledExpression:
    """A validated expression, callable against a metric frame."""

    def __init__(self, source: str, tree: ast.Expression, names: frozenset[str]) -> None:
        """Store the parsed tree and the metric names it references."""
        self.source = source
        self.names = names
        self._tree = tree

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"CompiledExpression({self.source!r})"

    def __call__(self, frame: pd.DataFrame) -> pd.Series:
        """Evaluate the expression, returning a boolean mask over ``frame``."""
        missing = sorted(self.names - set(frame.columns))
        if missing:
            raise RuleError(f"expression references metrics absent from the frame: {missing}")
        result = self._eval(self._tree.body, frame)
        return _as_mask(result, frame.index)

    def _eval(self, node: ast.AST, frame: pd.DataFrame) -> Any:
        """Recursively evaluate a validated node against ``frame``."""
        if isinstance(node, ast.BoolOp):
            parts = [_as_mask(self._eval(v, frame), frame.index) for v in node.values]
            combined = parts[0]
            for part in parts[1:]:
                combined = combined & part if isinstance(node.op, ast.And) else combined | part
            return combined
        if isinstance(node, ast.UnaryOp):
            if isinstance(node.op, ast.Not):
                return ~_as_mask(self._eval(node.operand, frame), frame.index)
            operand = self._eval(node.operand, frame)
            return -operand if isinstance(node.op, ast.USub) else +operand
        if isinstance(node, ast.BinOp):
            left = self._eval(node.left, frame)
            right = self._eval(node.right, frame)
            return _BINARY[type(node.op)](left, right)
        if isinstance(node, ast.Compare):
            left = self._eval(node.left, frame)
            result: Any = None
            for op, comparator in zip(node.ops, node.comparators, strict=True):
                right = self._eval(comparator, frame)
                if isinstance(op, (ast.In, ast.NotIn)):
                    values = right if isinstance(right, (list, tuple, set)) else [right]
                    part = (
                        left.isin(list(values))
                        if isinstance(left, pd.Series)
                        else pd.Series(left in list(values), index=frame.index)
                    )
                    if isinstance(op, ast.NotIn):
                        part = ~part
                else:
                    part = _COMPARATORS[type(op)](left, right)
                part = _as_mask(part, frame.index)
                result = part if result is None else result & part
                left = right
            return result
        if isinstance(node, ast.Name):
            return frame[node.id]
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, (ast.List, ast.Tuple)):
            return [self._eval(element, frame) for element in node.elts]
        raise RuleError(f"unsupported expression node {type(node).__name__}")


def _as_mask(value: Any, index: pd.Index) -> pd.Series:
    """Coerce an evaluation result into a NaN-free boolean mask."""
    if isinstance(value, pd.Series):
        return value.fillna(False).astype(bool)
    if isinstance(value, np.ndarray):
        return pd.Series(value, index=index).fillna(False).astype(bool)
    return pd.Series(bool(value), index=index)


# ---------------------------------------------------------------------------
# engine
# ---------------------------------------------------------------------------
class RuleEngine:
    """Compiles and evaluates rules and free-form expressions."""

    #: Metric names a rule or expression may reference.
    ALLOWED_FIELDS: frozenset[str] = ALLOWED_FIELDS

    def validate(self, rule: Rule) -> None:
        """Raise :class:`RuleError` when a rule is malformed.

        Raises:
            RuleError: unknown metric, wrong value type for the operator, an
                inverted ``between`` range or a percentile outside (0, 100].
        """
        spec = METRICS.get(rule.field)
        if spec is None:
            raise RuleError(
                f"unknown metric {rule.field!r}; expected one of {sorted(ALLOWED_FIELDS)}"
            )
        if rule.operator.cross_sectional:
            if spec.categorical:
                raise RuleError(f"{rule.field!r} is categorical and cannot be ranked")
            percent = _coerce_number(rule.value, rule.field)
            if not 0 < percent <= 100:
                raise RuleError(
                    f"a percentile cut must be in (0, 100]; got {rule.value!r}"
                )
            return
        if rule.operator in (Operator.IN, Operator.NOT_IN):
            if rule.unconstrained:
                return
            if not isinstance(rule.value, (str, list, tuple)):
                raise RuleError(f"{rule.operator.value!r} needs a value or list of values")
            return
        if rule.operator is Operator.BETWEEN:
            low = _coerce_number(rule.value, rule.field)
            high = _coerce_number(rule.value2, rule.field)
            if low > high:
                raise RuleError(f"between needs low <= high; got {low} and {high}")
            return
        if spec.categorical:
            if rule.operator not in (Operator.EQ, Operator.NEQ):
                raise RuleError(
                    f"{rule.field!r} is categorical; use ==, !=, in or not_in, not "
                    f"{rule.operator.value!r}"
                )
            return
        _coerce_number(rule.value, rule.field)

    def validate_screen(self, screen: Screen) -> None:
        """Validate every rule and the expression of ``screen``."""
        for rule in screen.rules:
            self.validate(rule)
        if screen.expression:
            self.compile_expression(screen.expression)
        if screen.rank_by not in RANKABLE:
            raise RuleError(
                f"cannot rank by {screen.rank_by!r}; expected one of {sorted(RANKABLE)}"
            )

    def compile_expression(self, expression: str) -> CompiledExpression:
        """Parse and validate an expression, returning a callable evaluator.

        Raises:
            RuleError: on syntax errors, unknown names or disallowed nodes.
        """
        text = expression.strip()
        if not text:
            raise RuleError("expression is empty")
        try:
            tree = ast.parse(text, mode="eval")
        except SyntaxError as exc:
            raise RuleError(f"could not parse expression: {exc.msg}") from exc
        names = _validate_nodes(tree)
        return CompiledExpression(text, tree, names)

    # --- evaluation --------------------------------------------------------
    def mask(self, frame: pd.DataFrame, rule: Rule) -> pd.Series:
        """Return the row-wise boolean mask of a non-percentile rule."""
        if rule.operator.cross_sectional:
            raise RuleError(
                f"{rule.operator.value!r} ranks the surviving set; use apply_rule instead"
            )
        if rule.unconstrained:
            return pd.Series(True, index=frame.index)
        if rule.field not in frame.columns:
            raise RuleError(f"metric {rule.field!r} is not present in the frame")
        column = frame[rule.field]
        spec = METRICS[rule.field]

        if rule.operator in (Operator.IN, Operator.NOT_IN):
            values = (
                list(rule.value) if isinstance(rule.value, (list, tuple)) else [rule.value]
            )
            hit = column.isin(values)
            return (hit if rule.operator is Operator.IN else ~hit).fillna(False).astype(bool)
        if spec.categorical:
            hit = column == rule.value
            return (hit if rule.operator is Operator.EQ else ~hit).fillna(False).astype(bool)

        numeric = pd.to_numeric(column, errors="coerce")
        if rule.operator is Operator.BETWEEN:
            low = _coerce_number(rule.value, rule.field)
            high = _coerce_number(rule.value2, rule.field)
            hit = (numeric >= low) & (numeric <= high)
        else:
            value = _coerce_number(rule.value, rule.field)
            comparisons: dict[Operator, Callable[[pd.Series], pd.Series]] = {
                Operator.GT: lambda s: s > value,
                Operator.GTE: lambda s: s >= value,
                Operator.LT: lambda s: s < value,
                Operator.LTE: lambda s: s <= value,
                Operator.EQ: lambda s: s == value,
                Operator.NEQ: lambda s: s != value,
            }
            hit = comparisons[rule.operator](numeric)
        # A metric a company does not report can never satisfy a threshold.
        return (hit & numeric.notna()).fillna(False).astype(bool)

    def apply_rule(self, frame: pd.DataFrame, rule: Rule) -> pd.DataFrame:
        """Return the rows of ``frame`` that survive ``rule``.

        Percentile rules rank the rows they are given (optionally within each
        sector) and keep ``ceil(n * pct)`` of them; rows whose metric is NaN
        are dropped first, because an unknown value cannot be ranked.
        """
        if rule.unconstrained or not rule.enabled:
            return frame
        if not rule.operator.cross_sectional:
            return frame[self.mask(frame, rule)]

        if rule.field not in frame.columns:
            raise RuleError(f"metric {rule.field!r} is not present in the frame")
        keep = _coerce_number(rule.value, rule.field) / 100.0
        ascending = rule.operator is Operator.BOTTOM_PCTL
        ranked = frame[pd.to_numeric(frame[rule.field], errors="coerce").notna()]
        if ranked.empty:
            return ranked

        def slice_group(group: pd.DataFrame) -> pd.DataFrame:
            """Keep the requested slice of one ranking group."""
            ordered = group.sort_values(rule.field, ascending=ascending, kind="stable")
            return ordered.head(max(1, math.ceil(len(ordered) * keep)))

        if rule.scope is Scope.SECTOR and "sector" in ranked.columns:
            parts = [
                slice_group(group)
                for _sector, group in ranked.groupby("sector", dropna=False, sort=True)
            ]
            survivors = pd.concat(parts) if parts else ranked.iloc[0:0]
            # Restore the incoming order; grouping reorders by sector.
            return frame[frame.index.isin(survivors.index)]
        return slice_group(ranked)

    def evaluate(self, screen: Screen, frame: pd.DataFrame) -> pd.DataFrame:
        """Return ``frame`` with a boolean column per rule plus ``passes``.

        Columns are named ``rule_0 ... rule_n`` and are *cumulative*: a row is
        ``True`` in ``rule_i`` when it survived rules 0..i. That is what makes
        the funnel counts (and percentile rules, which depend on the surviving
        set) consistent with the result table.
        """
        self.validate_screen(screen)
        out = frame.copy()
        surviving = frame
        for index, rule in enumerate(screen.rules):
            if rule.enabled:
                surviving = self.apply_rule(surviving, rule)
            out[f"rule_{index}"] = out.index.isin(surviving.index)
        if screen.expression:
            expression_mask = self.compile_expression(screen.expression)(surviving)
            surviving = surviving[expression_mask]
            out["rule_expression"] = out.index.isin(surviving.index)
        out["passes"] = out.index.isin(surviving.index)
        return out

    def survivors(self, screen: Screen, frame: pd.DataFrame) -> pd.DataFrame:
        """Return only the rows that pass every enabled rule, in rank order."""
        evaluated = self.evaluate(screen, frame)
        passed = evaluated[evaluated["passes"]]
        if screen.rank_by in passed.columns:
            passed = passed.sort_values(
                screen.rank_by,
                ascending=METRICS[screen.rank_by].higher_is_better is False,
                kind="stable",
                na_position="last",
            )
        return passed

    def count(self, screen: Screen, frame: pd.DataFrame) -> int:
        """Return how many rows pass every enabled rule (live rule-count badge)."""
        surviving = frame
        for rule in screen.rules:
            if rule.enabled:
                surviving = self.apply_rule(surviving, rule)
        if screen.expression:
            surviving = surviving[self.compile_expression(screen.expression)(surviving)]
        return int(len(surviving))


def _validate_nodes(tree: ast.Expression) -> frozenset[str]:
    """Walk a parsed expression, rejecting anything outside the whitelist."""
    names: set[str] = set()
    allowed_nodes = (
        ast.Expression,
        ast.BoolOp,
        ast.And,
        ast.Or,
        ast.UnaryOp,
        ast.Not,
        ast.USub,
        ast.UAdd,
        ast.BinOp,
        ast.Compare,
        ast.Name,
        ast.Load,
        ast.Constant,
        ast.List,
        ast.Tuple,
        *_BINARY,
        *_COMPARATORS,
        ast.In,
        ast.NotIn,
    )
    for node in ast.walk(tree):
        if not isinstance(node, allowed_nodes):
            raise RuleError(f"{type(node).__name__} is not allowed in a screen expression")
        if isinstance(node, ast.Name):
            if node.id not in ALLOWED_FIELDS:
                raise RuleError(
                    f"unknown metric {node.id!r}; expected one of {sorted(ALLOWED_FIELDS)}"
                )
            names.add(node.id)
        if isinstance(node, ast.Constant) and not isinstance(
            node.value, (int, float, str, bool)
        ):
            raise RuleError(f"unsupported constant {node.value!r} in a screen expression")
    return frozenset(names)


def _coerce_number(value: Any, field_name: str) -> float:
    """Return ``value`` as a float, or raise a helpful :class:`RuleError`."""
    if isinstance(value, bool) or value is None:
        raise RuleError(f"{field_name!r} needs a numeric value; got {value!r}")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise RuleError(f"{field_name!r} needs a numeric value; got {value!r}") from exc
    if math.isnan(number) or math.isinf(number):
        raise RuleError(f"{field_name!r} needs a finite value; got {value!r}")
    return number


# ---------------------------------------------------------------------------
# metric frame construction
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class MetricFrame:
    """The metric panel a screen is evaluated against."""

    frame: pd.DataFrame
    as_of: date | None
    built_at: datetime
    coverage: dict[str, Any] = field(default_factory=dict)
    missing: dict[str, str] = field(default_factory=dict)

    def __len__(self) -> int:
        return int(len(self.frame))

    def as_dict(self) -> dict[str, Any]:
        """Return the coverage report (not the frame itself) for the API."""
        return {
            "rows": len(self.frame),
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "built_at": self.built_at.isoformat(),
            "coverage": dict(self.coverage),
            "missing": dict(self.missing),
        }


class MetricBuilder:
    """Builds the metric panel for a set of symbols from the store + providers.

    Price metrics come from the Parquet store (bars that ingestion already
    persisted); symbols the store does not cover are fetched once from the
    market provider and written back, so the second run of a screen is local.
    Fundamental metrics reuse :func:`tradinglib.research.company.derive_metrics`,
    so the screener and the Stock Research page cannot disagree about what a
    company's FCF margin is.
    """

    def __init__(
        self,
        *,
        market: DataProvider | None = None,
        store: TimeSeriesStore | None = None,
        cache: DataCache | None = None,
        concurrency: int = 6,
        max_symbols: int = DEFAULT_MAX_SYMBOLS,
        deep_fundamentals: bool = True,
    ) -> None:
        """Create the builder.

        Args:
            market: market-data provider (yfinance by default).
            store: time-series store used for price history.
            cache: shared data cache (fundamentals/statements TTLs live there).
            concurrency: parallel vendor requests.
            max_symbols: ceiling on symbols priced per run; the coverage report
                says when a universe was truncated.
            deep_fundamentals: also pull full statements so growth, margins and
                leverage are derived from filings rather than vendor summary
                fields. Costs one extra request per symbol (cached).
        """
        self._market = market
        self._store = store
        self._cache = cache
        self.concurrency = max(1, concurrency)
        self.max_symbols = max(1, max_symbols)
        self.deep_fundamentals = deep_fundamentals

    @property
    def market(self) -> DataProvider:
        """The market-data provider (built lazily)."""
        if self._market is None:
            from tradinglib.data.providers import build_provider

            self._market = build_provider("yfinance")
        return self._market

    @property
    def store(self) -> TimeSeriesStore:
        """The time-series store."""
        if self._store is None:
            self._store = get_store()
        return self._store

    @property
    def cache(self) -> DataCache:
        """The shared data cache."""
        if self._cache is None:
            self._cache = get_cache()
        return self._cache

    # --- prices ------------------------------------------------------------
    async def _closes(self, symbols: Sequence[str], as_of: date | None) -> dict[str, pd.DataFrame]:
        """Return a per-symbol daily close/volume frame, back-filling the store."""
        end = (
            datetime.combine(as_of, datetime.max.time(), tzinfo=timezone.utc)
            if as_of
            else utcnow()
        )
        start = end - timedelta(days=PRICE_LOOKBACK_DAYS)
        stored = await asyncio.to_thread(self._read_panel, list(symbols), start, end)
        out: dict[str, pd.DataFrame] = {}
        missing: list[str] = []
        for symbol in symbols:
            block = stored.get(symbol)
            if block is not None and len(block) >= WINDOW_1M + 2:
                out[symbol] = block
            else:
                missing.append(symbol)

        if missing:
            semaphore = asyncio.Semaphore(self.concurrency)

            async def fetch(symbol: str) -> tuple[str, pd.DataFrame | None]:
                """Fetch and persist one symbol's daily bars."""
                async with semaphore:
                    try:
                        bars = await self.market.get_bars(
                            symbol, start=start, end=end, interval=BarInterval.DAY_1
                        )
                    except ProviderError as exc:
                        logger.info("no bars for %s: %s", symbol, exc.message)
                        return symbol, None
                    if not bars:
                        return symbol, None
                    await self.store.awrite_bars(list(bars), interval=BarInterval.DAY_1)
                    frame = pd.DataFrame(
                        {
                            "close": [bar.close for bar in bars],
                            "volume": [bar.volume for bar in bars],
                        },
                        index=pd.Index([bar.ts for bar in bars], name="ts"),
                    ).sort_index()
                    return symbol, frame

            for symbol, frame in await asyncio.gather(*(fetch(s) for s in missing)):
                if frame is not None:
                    out[symbol] = frame
        return out

    def _read_panel(
        self, symbols: Sequence[str], start: datetime, end: datetime
    ) -> dict[str, pd.DataFrame]:
        """Read the stored daily bars in the window (blocking; runs in a thread)."""
        wanted = {symbol.upper() for symbol in symbols}
        frame = self.store.query(
            "SELECT symbol, ts, close, volume FROM bars "
            "WHERE interval = $interval AND ts >= $start AND ts <= $end ORDER BY symbol, ts",
            interval=str(BarInterval.DAY_1),
            start=start,
            end=end,
        )
        if frame.empty:
            return {}
        out: dict[str, pd.DataFrame] = {}
        for symbol, group in frame.groupby("symbol"):
            if str(symbol) not in wanted:
                continue
            out[str(symbol)] = group.set_index("ts")[["close", "volume"]].sort_index()
        return out

    @staticmethod
    def price_metrics(closes: pd.DataFrame) -> dict[str, float]:
        """Compute the price-derived metrics from one symbol's daily bars.

        Windows that the history does not cover yield ``NaN`` rather than a
        shorter-window substitute, so a freshly listed name is never compared
        against a full year of another company's momentum.
        """
        close = pd.to_numeric(closes["close"], errors="coerce").dropna()
        volume = pd.to_numeric(closes.get("volume"), errors="coerce")
        out: dict[str, float] = {}
        if close.empty:
            return out
        last = float(close.iloc[-1])
        out["price"] = last

        def total_return(window: int) -> float:
            """Percentage return over ``window`` sessions, or NaN."""
            if len(close) <= window:
                return float("nan")
            past = float(close.iloc[-1 - window])
            if past <= 0:
                return float("nan")
            return (last / past - 1.0) * 100.0

        out["mom_1m"] = total_return(WINDOW_1M)
        out["mom_3m"] = total_return(WINDOW_3M)
        out["mom_6m"] = total_return(WINDOW_6M)
        out["mom_12m"] = total_return(WINDOW_12M)
        if len(close) > WINDOW_12M:
            recent = float(close.iloc[-1 - WINDOW_1M])
            old = float(close.iloc[-1 - WINDOW_12M])
            out["mom_12_1"] = (recent / old - 1.0) * 100.0 if old > 0 else float("nan")
        else:
            out["mom_12_1"] = float("nan")

        returns = close.pct_change().dropna()
        window = returns.tail(WINDOW_3M)
        out["vol_3m"] = (
            float(window.std(ddof=1) * math.sqrt(SESSIONS_PER_YEAR) * 100.0)
            if len(window) >= 20
            else float("nan")
        )
        if volume is not None and not volume.dropna().empty:
            dollar = (close * volume.reindex(close.index)).dropna().tail(WINDOW_3M)
            shares = volume.dropna().tail(WINDOW_3M)
            out["adv_usd"] = float(dollar.mean()) if not dollar.empty else float("nan")
            out["adv_shares"] = float(shares.mean()) if not shares.empty else float("nan")
        else:
            out["adv_usd"] = float("nan")
            out["adv_shares"] = float("nan")
        return out

    # --- fundamentals ------------------------------------------------------
    async def _fundamental_metrics(self, symbol: str) -> tuple[dict[str, Any], str | None]:
        """Return one symbol's fundamental metrics and an error, if any."""
        try:
            entry = await self.cache.get_or_fetch(
                provider_key(self.market.name, "fundamentals", symbol),
                lambda: self.market.get_fundamentals(symbol),
                ttl=3600.0,
                source=self.market.name,
            )
            fundamentals: Fundamentals | None = entry.value
        except ProviderError as exc:
            return {}, exc.message
        except Exception as exc:  # noqa: BLE001 - a vendor SDK may raise anything
            logger.warning("fundamentals failed for %s", symbol, exc_info=True)
            return {}, f"{type(exc).__name__}: {exc}"

        statements: dict[str, Any] = {}
        error: str | None = None
        if self.deep_fundamentals and hasattr(self.market, "get_financial_statements"):
            try:
                statement_entry = await self.cache.get_or_fetch(
                    provider_key(self.market.name, "statements", symbol),
                    lambda: self.market.get_financial_statements(symbol),  # type: ignore[attr-defined]
                    ttl=21_600.0,
                    source=self.market.name,
                )
                statements = statement_entry.value or {}
            except ProviderError as exc:
                error = exc.message
            except Exception as exc:  # noqa: BLE001
                error = f"{type(exc).__name__}: {exc}"

        metrics = derive_metrics(statements, fundamentals=fundamentals)
        extras = (fundamentals.extras if fundamentals else {}) or {}
        country = extras.get("country")
        row: dict[str, Any] = {
            "name": fundamentals.name if fundamentals else None,
            "sector": fundamentals.sector if fundamentals else None,
            "industry": fundamentals.industry if fundamentals else None,
            "country": country,
            "region": region_for(country),
            "exchange": extras.get("exchange"),
            "mkt_cap": metrics["market_cap"].value,
            "rev_growth": _pct(metrics["revenue_yoy"].value),
            "gross_margin": _pct(metrics["gross_margin"].value),
            "ebitda_margin": _pct(metrics["ebitda_margin"].value),
            "fcf_margin": _pct(metrics["fcf_margin"].value),
            "net_margin": _pct(metrics["net_margin"].value),
            "roic": _pct(metrics["return_on_invested_capital"].value),
            "roe": _pct(metrics["return_on_equity"].value),
            "net_debt_ebitda": metrics["net_debt_to_ebitda"].value,
            "ev_sales": metrics["ev_to_sales"].value,
            "ev_ebitda": metrics["ev_to_ebitda"].value,
            "pe": metrics["price_to_earnings"].value,
            "pb": metrics["price_to_book"].value,
            "fcf_yield": _pct(metrics["fcf_yield"].value),
        }
        if row["roe"] is None and fundamentals is not None:
            vendor_roe = extras.get("returnOnEquity")
            if isinstance(vendor_roe, (int, float)):
                row["roe"] = float(vendor_roe) * 100.0
        return row, error

    # --- assembly ----------------------------------------------------------
    async def build(
        self,
        symbols: Sequence[str],
        *,
        as_of: date | None = None,
        sectors: dict[str, str] | None = None,
        names: dict[str, str] | None = None,
    ) -> MetricFrame:
        """Build the metric panel for ``symbols``.

        Args:
            symbols: tickers to evaluate (universe members).
            as_of: evaluate prices as of this date (point-in-time screening).
            sectors: sector per symbol from the universe, used when the vendor
                does not publish one.
            names: company name per symbol from the universe.

        Returns:
            A :class:`MetricFrame` whose ``coverage`` records how many symbols
            got price and fundamental data, and whose ``missing`` maps each
            dropped symbol to the reason it was dropped.
        """
        wanted = list(dict.fromkeys(s.upper() for s in symbols if s))
        truncated = len(wanted) > self.max_symbols
        if truncated:
            logger.info(
                "screen limited to %d of %d symbols (max_symbols)", self.max_symbols, len(wanted)
            )
            wanted = wanted[: self.max_symbols]

        closes = await self._closes(wanted, as_of)
        semaphore = asyncio.Semaphore(self.concurrency)

        async def one(symbol: str) -> tuple[str, dict[str, Any], str | None]:
            """Build one row (price metrics + fundamentals)."""
            async with semaphore:
                row, error = await self._fundamental_metrics(symbol)
            block = closes.get(symbol)
            if block is not None:
                row.update(self.price_metrics(block))
            return symbol, row, error

        results = await asyncio.gather(*(one(symbol) for symbol in wanted))
        missing: dict[str, str] = {}
        records: dict[str, dict[str, Any]] = {}
        for symbol, row, error in results:
            if not row:
                missing[symbol] = error or "no fundamental or price data available"
                continue
            if symbol not in closes:
                row.setdefault("price", float("nan"))
            if sectors and not row.get("sector"):
                row["sector"] = sectors.get(symbol)
            if names and not row.get("name"):
                row["name"] = names.get(symbol)
            records[symbol] = row

        frame = pd.DataFrame.from_dict(records, orient="index")
        if frame.empty:
            frame = pd.DataFrame(columns=[key for key in METRICS if key != "symbol"])
        frame.index.name = "symbol"
        frame.insert(0, "symbol", frame.index)
        for key, spec in METRICS.items():
            if key not in frame.columns:
                frame[key] = pd.Series(
                    [None] * len(frame) if spec.categorical else np.nan, index=frame.index
                )
        frame = add_scores(frame)

        priced = int(sum(1 for symbol in records if symbol in closes))
        with_growth = int(pd.to_numeric(frame.get("rev_growth"), errors="coerce").notna().sum())
        coverage = {
            "requested": len(symbols),
            "evaluated": len(frame),
            "priced": priced,
            "with_fundamentals": with_growth,
            "truncated": truncated,
            "max_symbols": self.max_symbols,
            "deep_fundamentals": self.deep_fundamentals,
            "price_source": "parquet store (back-filled from the market provider)",
        }
        return MetricFrame(
            frame=frame,
            as_of=as_of,
            built_at=utcnow(),
            coverage=coverage,
            missing=missing,
        )


def _pct(value: float | None) -> float | None:
    """Convert a fraction to the percent units the screener uses."""
    return None if value is None else value * 100.0


def percentile(series: pd.Series) -> pd.Series:
    """Return the cross-sectional percentile rank of ``series`` in [0, 1]."""
    numeric = pd.to_numeric(series, errors="coerce")
    return numeric.rank(pct=True, na_option="keep")


def add_scores(frame: pd.DataFrame) -> pd.DataFrame:
    """Add the derived quality/composite scores and their percentile inputs.

    ``quality`` is the mean percentile of gross margin, FCF margin, return on
    capital and inverted net-debt/EBITDA; ``composite`` is the mean percentile
    of return on capital, 12-1 momentum and quality. Both require at least two
    available components - a company with one known ratio does not get a score.
    """
    out = frame.copy()
    if out.empty:
        for column in ("roic_pctl", "mom_12_1_pctl", "quality_pctl", "quality", "composite"):
            out[column] = pd.Series(dtype="float64")
        return out

    def column(name: str) -> pd.Series:
        """Return a numeric column, or an all-NaN series when it is absent."""
        if name not in out.columns:
            return pd.Series(np.nan, index=out.index, dtype="float64")
        return pd.to_numeric(out[name], errors="coerce")

    returns_on_capital = column("roic").fillna(column("roe"))
    quality_parts = pd.DataFrame(
        {
            "gross_margin": percentile(column("gross_margin")),
            "fcf_margin": percentile(column("fcf_margin")),
            "return_on_capital": percentile(returns_on_capital),
            "leverage": percentile(-column("net_debt_ebitda")),
        }
    )
    quality = quality_parts.mean(axis=1, skipna=True) * 100.0
    quality[quality_parts.notna().sum(axis=1) < 2] = np.nan

    out["roic_pctl"] = percentile(returns_on_capital)
    out["mom_12_1_pctl"] = percentile(column("mom_12_1"))
    out["quality"] = quality
    out["quality_pctl"] = percentile(quality)

    composite_parts = pd.DataFrame(
        {
            "roic": out["roic_pctl"],
            "momentum": out["mom_12_1_pctl"],
            "quality": out["quality_pctl"],
        }
    )
    composite = composite_parts.mean(axis=1, skipna=True) * 100.0
    composite[composite_parts.notna().sum(axis=1) < 2] = np.nan
    out["composite"] = composite
    return out


def frame_records(frame: pd.DataFrame, *, columns: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """Render a metric frame as JSON-safe records (NaN becomes ``None``)."""
    wanted = list(columns) if columns is not None else list(frame.columns)
    present = [column for column in wanted if column in frame.columns]
    records: list[dict[str, Any]] = []
    for _index, row in frame[present].iterrows():
        record: dict[str, Any] = {}
        for column in present:
            value = row[column]
            if isinstance(value, (np.floating, float)):
                record[column] = None if pd.isna(value) else float(value)
            elif isinstance(value, (np.integer, int)) and not isinstance(value, bool):
                record[column] = int(value)
            elif value is None or (not isinstance(value, (list, dict)) and pd.isna(value)):
                record[column] = None
            else:
                record[column] = value
        records.append(record)
    return records
