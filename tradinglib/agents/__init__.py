"""LLM agent subsystem: providers, registry, pipelines, runs, approvals, audit.

Agents reach the library through tools (run a screen, fetch data, run a
backtest, save a finding) rather than being handed pre-computed answers, and
every run records real token usage and dollar cost against the configured
budgets (``docs/SPEC.md`` section 4).
"""

from __future__ import annotations

__all__: list[str] = []
