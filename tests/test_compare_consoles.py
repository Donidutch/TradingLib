"""The console comparison tool's matching rules (tools/compare_consoles.py)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("compare_consoles", ROOT / "tools" / "compare_consoles.py")
assert _spec and _spec.loader
compare_consoles = importlib.util.module_from_spec(_spec)
sys.modules["compare_consoles"] = compare_consoles
_spec.loader.exec_module(compare_consoles)
compare = compare_consoles.compare


def test_identical_figures_match_in_any_order() -> None:
    result = compare("stock/AAPL", "Overview", ["$5.07T", "+6.4%", "8"], ["8", "$5.07T", "+6.4%"])
    assert result.ok
    assert result.figures == 3


def test_a_missing_or_changed_figure_is_a_difference() -> None:
    result = compare("stock/AAPL", "Overview", ["$5.07T", "+6.4%", "8", "8"], ["$5.07T", "+6.5%", "8"])
    assert not result.ok
    assert sorted(result.only_old) == ["+6.4%", "8"]
    assert result.only_new == ["+6.5%"]


def test_a_known_difference_is_set_aside_with_its_reason() -> None:
    result = compare("stock/MSFT", "Financials", ["2022", "2023", "211,915"], ["2023", "211,915"])
    assert result.ok
    assert "isFinite(Number(null))" in result.allowed[0]


def test_a_known_difference_does_not_hide_other_figures() -> None:
    result = compare("stock/MSFT", "Financials", ["2022", "211,915"], ["245,122"])
    assert not result.ok
    assert result.only_old == ["211,915"]
    assert result.only_new == ["245,122"]


def test_the_rule_only_applies_to_its_own_tab_and_case() -> None:
    assert not compare("stock/MSFT", "Overview", ["2022"], []).ok
    assert not compare("overview", "Financials", ["2022"], []).ok


def test_a_tab_missing_on_one_side_is_reported() -> None:
    result = compare("stock/AAPL", "Research", ["1"], None)
    assert not result.ok
    assert result.missing == "the new page has no 'Research'"
