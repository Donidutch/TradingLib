"""Shared FastAPI dependencies and error translation for the REST routers.

Two jobs live here:

* reaching the subsystems the application lifespan installed on
  ``app.state`` (source catalog, ingest scheduler, gateway, registries,
  quality engine) and failing honestly with ``503`` when one is not running;
* turning the domain layer's own exceptions into HTTP status codes, so route
  bodies stay free of ``try``/``except`` noise: ``KeyError`` is a missing
  entity (404), ``ValueError`` a bad request (400) and a provider that is not
  configured is reported as such (503) rather than as a server crash.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, TypeVar

from fastapi import HTTPException, Request, status

from tradinglib.agents.providers import BudgetExceeded
from tradinglib.agents.providers import ProviderError as LLMProviderError
from tradinglib.agents.providers import ProviderNotAvailable as LLMProviderNotAvailable
from tradinglib.data.base import (
    ProviderError,
    ProviderNotConfigured,
    ProviderRateLimited,
    ProviderUnavailable,
    SymbolNotFound,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tradinglib.agents.approvals import ApprovalGate
    from tradinglib.agents.audit import AuditLog
    from tradinglib.agents.autopipeline import AutoPipeline
    from tradinglib.agents.findings import FindingsStore
    from tradinglib.agents.knowledge import KnowledgeBase
    from tradinglib.agents.pipeline import PipelineRegistry
    from tradinglib.agents.providers import ProviderRegistry
    from tradinglib.agents.registry import AgentRegistry
    from tradinglib.agents.runner import PipelineRunner
    from tradinglib.data.ingest.catalog import SourceCatalog
    from tradinglib.data.ingest.gateway import IngestGateway
    from tradinglib.data.ingest.scheduler import IngestScheduler
    from tradinglib.data.macro import MacroService
    from tradinglib.data.quality import QualityEngine
    from tradinglib.data.universe import UniverseRegistry, WatchlistRegistry
    from tradinglib.research.analyses import AnalysisEngine
    from tradinglib.research.backtest import BacktestEngine
    from tradinglib.research.company import CompanyResearch
    from tradinglib.research.evaluation import EvaluationEngine
    from tradinglib.research.experiments import ExperimentEngine
    from tradinglib.research.hypotheses import HypothesisRegistry
    from tradinglib.research.memory import ResearchMemoryComposer
    from tradinglib.research.screen.funnel import FunnelEvaluator, ScreenRegistry
    from tradinglib.research.validation import ValidationEngine

T = TypeVar("T")


def _state(request: Request, name: str) -> Any:
    """Return ``request.app.state.<name>`` or ``None`` when it is absent."""
    return getattr(request.app.state, name, None)


def _require(value: T | None, subsystem: str) -> T:
    """Return ``value``, or raise ``503`` naming the subsystem that is down."""
    if value is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "subsystem_unavailable",
                "subsystem": subsystem,
                "detail": f"{subsystem} is not running in this process",
            },
        )
    return value


# --- subsystem dependencies ---------------------------------------------
def get_catalog(request: Request) -> SourceCatalog:
    """The ingestion source catalog installed by the lifespan."""
    return _require(_state(request, "source_catalog"), "source_catalog")


def get_scheduler(request: Request) -> IngestScheduler:
    """The ingestion scheduler installed by the lifespan."""
    return _require(_state(request, "ingest_scheduler"), "ingest_scheduler")


def get_gateway(request: Request) -> IngestGateway:
    """The ingestion gateway installed by the lifespan."""
    return _require(_state(request, "ingest_gateway"), "ingest_gateway")


def get_universes(request: Request) -> UniverseRegistry:
    """The universe registry installed by the lifespan."""
    return _require(_state(request, "universe_registry"), "universe_registry")


def get_watchlists(request: Request) -> WatchlistRegistry:
    """The watchlist registry installed by the lifespan."""
    return _require(_state(request, "watchlist_registry"), "watchlist_registry")


def get_quality(request: Request) -> QualityEngine:
    """The data-quality engine installed by the lifespan."""
    return _require(_state(request, "quality_engine"), "quality_engine")


def get_macro(request: Request) -> MacroService:
    """The macro-series service installed by the lifespan."""
    return _require(_state(request, "macro_service"), "macro_service")


def get_research(request: Request) -> CompanyResearch:
    """The Stock Research engine installed by the lifespan."""
    return _require(_state(request, "company_research"), "company_research")


def get_memory(request: Request) -> ResearchMemoryComposer:
    """The research-memory composer installed by the lifespan."""
    return _require(_state(request, "memory_composer"), "memory_composer")


def get_screen_registry(request: Request) -> ScreenRegistry:
    """The saved-screen registry installed by the lifespan."""
    return _require(_state(request, "screen_registry"), "screen_registry")


def get_funnel(request: Request) -> FunnelEvaluator:
    """The screener's funnel evaluator installed by the lifespan."""
    return _require(_state(request, "funnel_evaluator"), "funnel_evaluator")


def get_screen_alerts(request: Request) -> Any:
    """The pinned-screen membership alert monitor installed by the lifespan."""
    return _require(_state(request, "screen_alerts"), "screen_alerts")


def get_backtests(request: Request) -> BacktestEngine:
    """The Strategy Lab backtest engine installed by the lifespan."""
    return _require(_state(request, "backtest_engine"), "backtest_engine")


def get_analyses(request: Request) -> AnalysisEngine:
    """The sweep / walk-forward analysis engine installed by the lifespan."""
    return _require(_state(request, "analysis_engine"), "analysis_engine")


def get_experiments(request: Request) -> ExperimentEngine:
    """The experiment engine installed by the lifespan."""
    return _require(_state(request, "experiment_engine"), "experiment_engine")


def get_hypotheses(request: Request) -> HypothesisRegistry:
    """The hypothesis registry installed by the lifespan."""
    return _require(_state(request, "hypothesis_registry"), "hypothesis_registry")


def get_validation(request: Request) -> ValidationEngine:
    """The validation engine installed by the lifespan."""
    return _require(_state(request, "validation_engine"), "validation_engine")


def get_evaluation(request: Request) -> EvaluationEngine:
    """The evaluation engine installed by the lifespan."""
    return _require(_state(request, "evaluation_engine"), "evaluation_engine")


# --- agent subsystems -----------------------------------------------------
def get_runner(request: Request) -> PipelineRunner:
    """The pipeline runner installed by the lifespan."""
    return _require(_state(request, "pipeline_runner"), "pipeline_runner")


def get_agents(request: Request) -> AgentRegistry:
    """The agent-definition registry installed by the lifespan."""
    return _require(_state(request, "agent_registry"), "agent_registry")


def get_pipelines(request: Request) -> PipelineRegistry:
    """The pipeline registry installed by the lifespan."""
    return _require(_state(request, "pipeline_registry"), "pipeline_registry")


def get_providers(request: Request) -> ProviderRegistry:
    """The LLM provider registry installed by the lifespan."""
    return _require(_state(request, "provider_registry"), "provider_registry")


def get_findings(request: Request) -> FindingsStore:
    """The findings store installed by the lifespan."""
    return _require(_state(request, "findings_store"), "findings_store")


def get_knowledge(request: Request) -> KnowledgeBase:
    """The knowledge base installed by the lifespan."""
    return _require(_state(request, "knowledge_base"), "knowledge_base")


def get_approvals(request: Request) -> ApprovalGate:
    """The human-approval gate installed by the lifespan."""
    return _require(_state(request, "approval_gate"), "approval_gate")


def get_audit(request: Request) -> AuditLog:
    """The append-only agent audit log installed by the lifespan."""
    return _require(_state(request, "audit_log"), "audit_log")


def get_autopipeline(request: Request) -> AutoPipeline:
    """The AutoPipeline scheduler installed by the lifespan."""
    return _require(_state(request, "autopipeline"), "autopipeline")


# --- error translation ----------------------------------------------------
@contextmanager
def translate_errors(what: str) -> Iterator[None]:
    """Map domain exceptions onto HTTP status codes.

    Args:
        what: the entity being acted on, used in the error payload
            (``"universe"``, ``"ingestion source"``...).

    Raises:
        HTTPException: 404 for :class:`KeyError` and an unknown symbol, 400 for
            :class:`ValueError`, 409 for :class:`PermissionError` (a domain
            refusal such as a promotion blocked by an outstanding validation
            gate - the request was well formed, the state forbids it), 503 for
            a provider that is not configured or unreachable, 429 when a vendor
            rate-limited us and 502 for any other vendor failure. A spend budget
            or an AutoPipeline guardrail that refuses the work is a 409 - the
            request was valid, the ceiling says no. Anything else propagates
            unchanged so it is logged as the server fault it is.
    """
    try:
        yield
    except BudgetExceeded as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "budget_exceeded",
                "entity": what,
                "scope": exc.scope,
                "used": exc.used,
                "budget": exc.budget,
                "detail": str(exc),
            },
        ) from exc
    except LLMProviderNotAvailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "not_configured", "entity": what, "detail": str(exc)},
        ) from exc
    except LLMProviderError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "provider_error", "entity": what, "detail": str(exc)},
        ) from exc
    except (KeyError, SymbolNotFound) as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found", "entity": what, "detail": _message(exc)},
        ) from exc
    except ProviderNotConfigured as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "not_configured", "entity": what, "detail": str(exc)},
        ) from exc
    except ProviderRateLimited as exc:
        headers = {"Retry-After": str(int(exc.retry_after))} if exc.retry_after else None
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"error": "rate_limited", "entity": what, "detail": str(exc)},
            headers=headers,
        ) from exc
    except ProviderUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "provider_unavailable", "entity": what, "detail": str(exc)},
        ) from exc
    except ProviderError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "provider_error", "entity": what, "detail": str(exc)},
        ) from exc
    except PermissionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "blocked", "entity": what, "detail": str(exc)},
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_request", "entity": what, "detail": str(exc)},
        ) from exc


def _message(exc: BaseException) -> str:
    """Return an exception's message without ``KeyError``'s repr quoting."""
    if isinstance(exc, KeyError) and exc.args:
        return str(exc.args[0])
    return str(exc)


__all__ = [
    "get_agents",
    "get_approvals",
    "get_audit",
    "get_autopipeline",
    "get_backtests",
    "get_catalog",
    "get_evaluation",
    "get_experiments",
    "get_findings",
    "get_funnel",
    "get_gateway",
    "get_hypotheses",
    "get_knowledge",
    "get_pipelines",
    "get_providers",
    "get_quality",
    "get_research",
    "get_runner",
    "get_scheduler",
    "get_screen_alerts",
    "get_screen_registry",
    "get_universes",
    "get_validation",
    "get_watchlists",
    "translate_errors",
]
