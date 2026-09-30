"""Strategy Lab endpoints (``docs/SPEC.md`` section 5).

Four domains behind one prefix, in the order the research lifecycle runs:

* ``/api/lab/hypotheses`` - CRUD, evidence links and the validated status
  machine (an illegal transition is a 400, not a silent no-op);
* ``/api/lab/experiments`` - definitions plus ``POST {id}/run``, which executes
  a real backtest and a real information-coefficient study and streams its log
  on the ``lab`` channel;
* ``/api/lab/backtests`` - the run ledger, ``POST /run`` (run now) and
  ``GET {id}/results`` (stats + the equity curve the chart draws);
* ``/api/lab/validation`` - the gate panel, ``GET /gates`` for the suite
  definitions and ``POST {id}/promote``, which refuses on outstanding gates
  (409) instead of promoting anyway;
* ``/api/lab/evaluation`` - the candidate table, scorecard, funnel and the
  out-of-sample tracking of promoted strategies.

Long operations (running an experiment or a backtest) execute inline and the
response is the finished record; progress meanwhile streams over WebSocket, so
the UI can show the run log while the request is still open.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import date
from typing import Annotated, Any, TypeVar

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Response
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from tradinglib.api.deps import (
    get_analyses,
    get_backtests,
    get_evaluation,
    get_experiments,
    get_hypotheses,
    get_validation,
    translate_errors,
)
from tradinglib.research.analyses import (
    DEFAULT_MC_PATHS,
    DEFAULT_MC_SHOWN,
    MAX_MC_PATHS,
    MAX_SWEEP_CELLS,
    METRICS,
    SWEEPABLE,
    AnalysisEngine,
    AnalysisKind,
    AnalysisRecord,
    AnalysisStatus,
    SweepSpec,
    WalkForwardSpec,
)
from tradinglib.research.backtest import BacktestConfig, BacktestEngine, BacktestStatus
from tradinglib.research.evaluation import EvaluationEngine
from tradinglib.research.experiments import (
    AcceptanceCriteria,
    ExperimentEngine,
    ExperimentKind,
    ExperimentStage,
)
from tradinglib.research.hypotheses import HypothesisRegistry, HypothesisStatus
from tradinglib.research.validation import GateThresholds, ValidationEngine

router = APIRouter(prefix="/api/lab", tags=["lab"])

Hypotheses = Annotated[HypothesisRegistry, Depends(get_hypotheses)]
Experiments = Annotated[ExperimentEngine, Depends(get_experiments)]
Backtests = Annotated[BacktestEngine, Depends(get_backtests)]
Validation = Annotated[ValidationEngine, Depends(get_validation)]
Evaluation = Annotated[EvaluationEngine, Depends(get_evaluation)]
Analyses = Annotated[AnalysisEngine, Depends(get_analyses)]

T = TypeVar("T")


async def _thread(fn: Callable[[], T]) -> T:
    """Run a blocking (SQLite) domain call on a worker thread."""
    return await asyncio.to_thread(fn)


# ---------------------------------------------------------------------------
# request bodies
# ---------------------------------------------------------------------------
class HypothesisCreate(BaseModel):
    """Body of ``POST /api/lab/hypotheses``.

    ``falsifier`` is required by the domain (a hypothesis nobody could kill is
    not a hypothesis), so it is required here too rather than defaulted.
    """

    model_config = ConfigDict(extra="ignore")

    title: str
    statement: str
    falsifier: str
    rationale: str | None = None
    owner: str | None = None
    origin: str | None = None
    universe_id: str | None = None
    tags: list[str] = Field(default_factory=list)
    status: HypothesisStatus = HypothesisStatus.DRAFT
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    hypothesis_id: str | None = Field(default=None, validation_alias="id")


class HypothesisUpdate(BaseModel):
    """Body of ``PUT /api/lab/hypotheses/{id}``; status moves via ``/status``."""

    model_config = ConfigDict(extra="ignore")

    title: str | None = None
    statement: str | None = None
    falsifier: str | None = None
    rationale: str | None = None
    owner: str | None = None
    origin: str | None = None
    universe_id: str | None = None
    tags: list[str] | None = None
    metrics: dict[str, Any] | None = None


class StatusChange(BaseModel):
    """Body of ``POST /api/lab/hypotheses/{id}/status``."""

    model_config = ConfigDict(extra="ignore")

    status: HypothesisStatus
    note: str | None = None
    actor: str | None = None


class EvidenceCreate(BaseModel):
    """Body of ``POST /api/lab/hypotheses/{id}/evidence``."""

    model_config = ConfigDict(extra="ignore")

    kind: str
    ref: str
    label: str | None = None
    url: str | None = None
    supports: bool = True


class ExperimentCreate(BaseModel):
    """Body of ``POST /api/lab/experiments``.

    ``config`` is a :class:`~tradinglib.research.backtest.BacktestConfig` mapping;
    it is what makes the experiment reproducible, so it is stored verbatim.
    """

    model_config = ConfigDict(extra="ignore")

    name: str
    config: dict[str, Any]
    hypothesis_id: str | None = None
    kind: ExperimentKind = ExperimentKind.COMBINED
    criteria: dict[str, Any] | None = None
    horizon: int = 21
    owner: str | None = None
    notes: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    experiment_id: str | None = Field(default=None, validation_alias="id")


class ExperimentUpdate(BaseModel):
    """Body of ``PUT /api/lab/experiments/{id}``."""

    model_config = ConfigDict(extra="ignore")

    name: str | None = None
    hypothesis_id: str | None = None
    kind: ExperimentKind | None = None
    stage: ExperimentStage | None = None
    config: dict[str, Any] | None = None
    criteria: dict[str, Any] | None = None
    horizon: int | None = None
    owner: str | None = None
    notes: str | None = None
    params: dict[str, Any] | None = None


class BacktestRun(BaseModel):
    """Body of ``POST /api/lab/backtests`` and ``POST /api/lab/backtests/run``."""

    model_config = ConfigDict(extra="ignore")

    config: dict[str, Any]
    hypothesis_id: str | None = None
    experiment_id: str | None = None
    screen_id: str | None = None
    source: str = "manual"


class AnalysisRequest(BaseModel):
    """Shared body of the two ``POST /api/lab/analyses/*`` endpoints."""

    model_config = ConfigDict(extra="ignore")

    spec: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] | None = None
    backtest_id: str | None = None
    experiment_id: str | None = None
    hypothesis_id: str | None = None


class SweepRequest(AnalysisRequest):
    """Body of ``POST /api/lab/analyses/sweep``."""


class WalkForwardRequest(AnalysisRequest):
    """Body of ``POST /api/lab/analyses/walk-forward``."""


class MonteCarloRequest(AnalysisRequest):
    """Body of ``POST /api/lab/analyses/monte-carlo``.

    ``spec`` is unused here - a resample has no grid, only these four knobs.
    """

    paths: int = Field(default=DEFAULT_MC_PATHS, ge=100, le=MAX_MC_PATHS)
    shown: int = Field(default=DEFAULT_MC_SHOWN, ge=10, le=500)
    block: int | None = Field(default=None, ge=2, le=252)
    seed: int = 20260828
    name: str | None = None


class ValidationRequest(BaseModel):
    """Body of ``POST /api/lab/validation/{backtest_id}``."""

    model_config = ConfigDict(extra="ignore")

    thresholds: dict[str, Any] | None = None


class PromoteRequest(BaseModel):
    """Body of ``POST /api/lab/validation/{backtest_id}/promote``."""

    model_config = ConfigDict(extra="ignore")

    comment: str | None = None
    overrides: list[str] = Field(default_factory=list)
    stage: ExperimentStage = ExperimentStage.PAPER
    actor: str | None = None


class StrategyStatus(BaseModel):
    """Body of ``POST /api/lab/validation/strategies/{id}/status``."""

    model_config = ConfigDict(extra="ignore")

    status: str
    note: str | None = None


# ---------------------------------------------------------------------------
# hypotheses
# ---------------------------------------------------------------------------
@router.get("/hypotheses", summary="List hypotheses")
async def list_hypotheses(
    registry: Hypotheses,
    status: Annotated[HypothesisStatus | None, Query()] = None,
    owner: Annotated[str | None, Query()] = None,
    universe_id: Annotated[str | None, Query()] = None,
    q: Annotated[str | None, Query(description="substring of the title or statement")] = None,
    include_evidence: Annotated[bool, Query()] = True,
) -> dict[str, Any]:
    """Return hypotheses newest first, with the per-status summary counts."""
    with translate_errors("hypothesis"):
        items = await _thread(
            lambda: registry.list(
                status=status,
                owner=owner,
                universe_id=universe_id,
                query=q,
                include_evidence=include_evidence,
            )
        )
        summary = await _thread(registry.summary)
    return {
        "hypotheses": [item.as_dict(include_evidence=include_evidence) for item in items],
        "count": len(items),
        "summary": summary,
    }


@router.post("/hypotheses", status_code=201, summary="Create a hypothesis")
async def create_hypothesis(registry: Hypotheses, body: HypothesisCreate) -> dict[str, Any]:
    """Create a hypothesis. A missing falsifier is refused with 400."""
    with translate_errors("hypothesis"):
        item = await _thread(
            lambda: registry.create(
                body.title,
                body.statement,
                body.falsifier,
                rationale=body.rationale,
                owner=body.owner,
                origin=body.origin,
                universe_id=body.universe_id,
                tags=body.tags,
                status=body.status,
                evidence=body.evidence,
                hypothesis_id=body.hypothesis_id,
            )
        )
    return item.as_dict()


@router.get("/hypotheses/{hypothesis_id}", summary="Return one hypothesis")
async def get_hypothesis(registry: Hypotheses, hypothesis_id: str) -> dict[str, Any]:
    """Return one hypothesis with its evidence and its real experiment counts."""
    with translate_errors("hypothesis"):
        item = await _thread(lambda: registry.get(hypothesis_id))
        stats = await _thread(lambda: registry.stats(hypothesis_id))
    payload = item.as_dict()
    payload["stats"] = stats
    return payload


@router.put("/hypotheses/{hypothesis_id}", summary="Update a hypothesis")
async def update_hypothesis(
    registry: Hypotheses, hypothesis_id: str, body: HypothesisUpdate
) -> dict[str, Any]:
    """Update the editable fields; the status is changed through ``/status``."""
    changes = body.model_dump(exclude_none=True)
    with translate_errors("hypothesis"):
        if not changes:
            item = await _thread(lambda: registry.get(hypothesis_id))
        else:
            item = await _thread(lambda: registry.update(hypothesis_id, **changes))
    return item.as_dict()


@router.delete("/hypotheses/{hypothesis_id}", summary="Delete a hypothesis")
async def delete_hypothesis(registry: Hypotheses, hypothesis_id: str) -> dict[str, Any]:
    """Delete a hypothesis and its evidence. Experiments keep their reference."""
    with translate_errors("hypothesis"):
        await _thread(lambda: registry.delete(hypothesis_id))
    return {"deleted": hypothesis_id}


@router.post("/hypotheses/{hypothesis_id}/status", summary="Transition a hypothesis")
async def set_hypothesis_status(
    registry: Hypotheses, hypothesis_id: str, body: StatusChange
) -> dict[str, Any]:
    """Move a hypothesis through its status machine and announce it on ``lab``."""
    with translate_errors("hypothesis"):
        item = await registry.set_status_and_publish(
            hypothesis_id, body.status, note=body.note, actor=body.actor
        )
    return item.as_dict()


@router.post("/hypotheses/{hypothesis_id}/evidence", status_code=201, summary="Attach evidence")
async def add_evidence(
    registry: Hypotheses, hypothesis_id: str, body: EvidenceCreate
) -> dict[str, Any]:
    """Attach one evidence link (run, backtest, note, filing, ...)."""
    with translate_errors("hypothesis"):
        link = await _thread(
            lambda: registry.add_evidence(
                hypothesis_id,
                body.kind,
                body.ref,
                label=body.label,
                url=body.url,
                supports=body.supports,
            )
        )
    return link.as_dict()


@router.delete(
    "/hypotheses/{hypothesis_id}/evidence/{evidence_id}", summary="Detach evidence"
)
async def remove_evidence(
    registry: Hypotheses, hypothesis_id: str, evidence_id: int
) -> dict[str, Any]:
    """Detach one evidence link from a hypothesis."""
    with translate_errors("evidence"):
        await _thread(lambda: registry.remove_evidence(hypothesis_id, evidence_id))
    return {"deleted": evidence_id, "hypothesis_id": hypothesis_id}


# ---------------------------------------------------------------------------
# experiments
# ---------------------------------------------------------------------------
@router.get("/experiments", summary="List experiments")
async def list_experiments(
    engine: Experiments,
    hypothesis_id: Annotated[str | None, Query()] = None,
    stage: Annotated[ExperimentStage | None, Query()] = None,
    q: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """Return experiment definitions, newest first."""
    with translate_errors("experiment"):
        items = await _thread(
            lambda: engine.list(hypothesis_id=hypothesis_id, stage=stage, query=q)
        )
    return {"experiments": [item.as_dict() for item in items], "count": len(items)}


@router.post("/experiments", status_code=201, summary="Create an experiment")
async def create_experiment(engine: Experiments, body: ExperimentCreate) -> dict[str, Any]:
    """Create an experiment from a reproducible backtest configuration."""
    with translate_errors("experiment"):
        config = BacktestConfig.from_dict(body.config)
        item = await _thread(
            lambda: engine.create(
                body.name,
                config,
                hypothesis_id=body.hypothesis_id,
                kind=body.kind,
                criteria=AcceptanceCriteria.from_dict(body.criteria),
                horizon=body.horizon,
                owner=body.owner,
                notes=body.notes,
                params=body.params,
                experiment_id=body.experiment_id,
            )
        )
    return item.as_dict()


@router.get("/experiments/runs", summary="Runs across every experiment")
async def list_all_experiment_runs(
    engine: Experiments,
    hypothesis_id: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> dict[str, Any]:
    """Return experiment runs across the workspace, newest first."""
    with translate_errors("experiment run"):
        runs = await _thread(
            lambda: engine.all_runs(hypothesis_id=hypothesis_id, limit=limit)
        )
    return {"runs": [run.as_dict(include_log=False) for run in runs], "count": len(runs)}


@router.get("/experiments/runs/{run_id}", summary="One experiment run")
async def get_experiment_run(engine: Experiments, run_id: str) -> dict[str, Any]:
    """Return one run with its ordered log and its results."""
    with translate_errors("experiment run"):
        run = await _thread(lambda: engine.get_run(run_id))
    return run.as_dict()


@router.get("/experiments/{experiment_id}", summary="Return one experiment")
async def get_experiment(engine: Experiments, experiment_id: str) -> dict[str, Any]:
    """Return one experiment definition with its recent runs."""
    with translate_errors("experiment"):
        item = await _thread(lambda: engine.get(experiment_id))
        runs = await _thread(lambda: engine.runs(experiment_id, limit=20))
    payload = item.as_dict()
    payload["runs"] = [run.as_dict(include_log=False) for run in runs]
    payload["run_count"] = len(runs)
    return payload


@router.put("/experiments/{experiment_id}", summary="Update an experiment")
async def update_experiment(
    engine: Experiments, experiment_id: str, body: ExperimentUpdate
) -> dict[str, Any]:
    """Update an experiment definition."""
    changes = body.model_dump(exclude_none=True)
    with translate_errors("experiment"):
        if not changes:
            item = await _thread(lambda: engine.get(experiment_id))
        else:
            item = await _thread(lambda: engine.update(experiment_id, **changes))
    return item.as_dict()


@router.delete("/experiments/{experiment_id}", summary="Delete an experiment")
async def delete_experiment(engine: Experiments, experiment_id: str) -> dict[str, Any]:
    """Delete an experiment and its runs."""
    with translate_errors("experiment"):
        await _thread(lambda: engine.delete(experiment_id))
    return {"deleted": experiment_id}


@router.get("/experiments/{experiment_id}/runs", summary="Runs of one experiment")
async def list_experiment_runs(
    engine: Experiments,
    experiment_id: str,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> dict[str, Any]:
    """Return the runs of one experiment, newest first."""
    with translate_errors("experiment"):
        await _thread(lambda: engine.get(experiment_id))
        runs = await _thread(lambda: engine.runs(experiment_id, limit=limit))
    return {
        "experiment_id": experiment_id,
        "runs": [run.as_dict(include_log=False) for run in runs],
        "count": len(runs),
    }


@router.post("/experiments/{experiment_id}/run", summary="Run an experiment")
async def run_experiment(engine: Experiments, experiment_id: str) -> dict[str, Any]:
    """Execute an experiment: a real backtest plus an information-coefficient study.

    The finished run - log, metrics and acceptance verdict - is the response;
    every log line was also streamed on the ``lab`` channel while it ran.
    """
    with translate_errors("experiment"):
        run = await engine.run(experiment_id)
    return run.as_dict()


# ---------------------------------------------------------------------------
# backtests
# ---------------------------------------------------------------------------
@router.get("/backtests", summary="List backtest runs")
async def list_backtests(
    engine: Backtests,
    hypothesis_id: Annotated[str | None, Query()] = None,
    experiment_id: Annotated[str | None, Query()] = None,
    status: Annotated[BacktestStatus | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    """Return persisted backtest runs, newest first (without their curves)."""
    with translate_errors("backtest"):
        records = await _thread(
            lambda: engine.list_runs(
                hypothesis_id=hypothesis_id,
                experiment_id=experiment_id,
                status=status,
                limit=limit,
            )
        )
    return {"backtests": [r.as_dict() for r in records], "count": len(records)}


@router.post("/backtests", status_code=201, summary="Queue a backtest")
async def queue_backtest(engine: Backtests, body: BacktestRun) -> dict[str, Any]:
    """Persist a queued backtest without running it. ``POST {id}/run`` executes it."""
    with translate_errors("backtest"):
        config = BacktestConfig.from_dict(body.config)
        record = await _thread(
            lambda: engine.queue(
                config,
                hypothesis_id=body.hypothesis_id,
                experiment_id=body.experiment_id,
                screen_id=body.screen_id,
                source=body.source,
            )
        )
    return record.as_dict()


@router.post("/backtests/validate", summary="Dry run: validate a config without running it")
async def validate_backtest_config(engine: Backtests, body: BacktestRun) -> dict[str, Any]:
    """Check a backtest configuration and report what a run *would* do.

    This is the *DRY RUN · VALIDATE CONFIG ONLY* control: it parses the config,
    resolves the universe, loads the price panel and reports the symbols, the
    covered window and every warning the real run would raise - without writing
    a row to the ledger or spending any compute on the walk-forward loop.
    Problems come back as ``ok: false`` plus the reason, never as a 500.
    """
    issues: list[str] = []
    try:
        config = BacktestConfig.from_dict(body.config)
    except Exception as exc:  # noqa: BLE001 - a bad config is the answer, not a crash
        return {
            "ok": False,
            "stage": "config",
            "issues": [f"{type(exc).__name__}: {exc}"],
            "would_run": False,
        }
    payload: dict[str, Any] = {
        "ok": True,
        "stage": "data",
        "config": config.as_dict(),
        "signal": config.signal.describe(),
        "would_run": True,
    }
    try:
        prices, _volumes, benchmark, warnings = await asyncio.to_thread(engine.prepare, config)
    except Exception as exc:  # noqa: BLE001 - report the reason the run would fail
        payload.update(
            {
                "ok": False,
                "issues": [f"{type(exc).__name__}: {exc}"],
                "would_run": False,
            }
        )
        return payload
    index = prices.index
    payload.update(
        {
            "symbols": [str(column) for column in prices.columns],
            "symbol_count": int(prices.shape[1]),
            "bars": int(prices.shape[0]),
            "covered": {
                "start": _stamp(index[0])[:10] if len(index) else None,
                "end": _stamp(index[-1])[:10] if len(index) else None,
            },
            "benchmark": {
                "symbol": config.benchmark,
                "bars": 0 if benchmark is None else int(benchmark.size),
                "available": benchmark is not None and not benchmark.empty,
            },
            "warnings": list(warnings),
        }
    )
    if prices.shape[0] < 2:
        issues.append("the loaded panel has fewer than two bars; a backtest needs at least two")
    if prices.shape[1] == 0:
        issues.append("the configuration resolved to no symbols with stored history")
    payload["issues"] = issues
    payload["ok"] = not issues
    payload["would_run"] = not issues
    return payload


@router.post("/backtests/run", status_code=201, summary="Run a backtest now")
async def run_backtest(engine: Backtests, body: BacktestRun) -> dict[str, Any]:
    """Run a backtest from a configuration and return the finished record.

    A failure is recorded on the row and reported: the response is a 400/404
    naming what went wrong (an unknown universe, a symbol with no stored
    history), never an empty result that looks like a flat equity curve.
    """
    with translate_errors("backtest"):
        config = BacktestConfig.from_dict(body.config)
        record = await engine.run(
            config,
            hypothesis_id=body.hypothesis_id,
            experiment_id=body.experiment_id,
            screen_id=body.screen_id,
            source=body.source,
        )
    return record.as_dict(include_curve=True)


@router.get("/backtests/{backtest_id}", summary="Return one backtest")
async def get_backtest(engine: Backtests, backtest_id: str) -> dict[str, Any]:
    """Return one persisted run without its equity curve."""
    with translate_errors("backtest"):
        record = await _thread(lambda: engine.get(backtest_id))
    return record.as_dict()


@router.get("/backtests/{backtest_id}/results", summary="Backtest results and curve")
async def backtest_results(engine: Backtests, backtest_id: str) -> dict[str, Any]:
    """Return the full result payload: stats, windows and the equity curve."""
    with translate_errors("backtest"):
        return await _thread(lambda: engine.results(backtest_id))


@router.post("/backtests/{backtest_id}/run", summary="Execute a queued backtest")
async def run_queued_backtest(engine: Backtests, backtest_id: str) -> dict[str, Any]:
    """Execute a previously queued run, keeping its identifier."""
    with translate_errors("backtest"):
        record = await engine.run_queued(backtest_id)
    return record.as_dict(include_curve=True)


# ---------------------------------------------------------------------------
# resampled views of a finished run
# ---------------------------------------------------------------------------
@router.get("/backtests/{backtest_id}/fan", summary="Equity-path fan chart")
async def backtest_fan(
    engine: Backtests,
    analyses: Analyses,
    backtest_id: str,
    paths: Annotated[int, Query(ge=100, le=MAX_MC_PATHS)] = DEFAULT_MC_PATHS,
    shown: Annotated[int, Query(ge=10, le=500)] = DEFAULT_MC_SHOWN,
    refresh: Annotated[bool, Query(description="recompute instead of reusing")] = False,
) -> dict[str, Any]:
    """Return the block-bootstrap path cone the tearsheet's fan chart draws.

    Circular blocks of consecutive returns are resampled with replacement, so
    short-horizon autocorrelation survives and only the *sequencing* is
    destroyed - which is the luck the cone is there to expose.

    The newest stored Monte-Carlo analysis for this run is reused when its path
    count matches, so opening a tearsheet does not recompute 5,000 resamples
    every time; ``refresh=true`` forces a fresh one. The resample is seeded, so
    a reused cone and a fresh one are the same cone.
    """
    with translate_errors("backtest"):
        if not refresh:
            stored = await _thread(
                lambda: analyses.list_analyses(
                    kind=AnalysisKind.MONTE_CARLO, backtest_id=backtest_id, limit=10
                )
            )
            for record in stored:
                if record.status is not AnalysisStatus.COMPLETE:
                    continue
                if int(record.summary.get("paths") or 0) != int(paths):
                    continue
                return {"reused": True, **_cone_payload(record)}
        run = await _thread(lambda: engine.get(backtest_id))
        record = await analyses.run_monte_carlo(
            run.config,
            paths=paths,
            shown=shown,
            backtest_id=backtest_id,
            experiment_id=run.experiment_id,
            hypothesis_id=run.hypothesis_id,
        )
        return {"reused": False, **_cone_payload(record)}


def _cone_payload(record: AnalysisRecord) -> dict[str, Any]:
    """Flatten a Monte-Carlo analysis record into the fan chart's payload."""
    cells = record.cells or []
    cone = dict(cells[0].get("cone") or {}) if cells else {}
    return {
        "analysis_id": record.id,
        "backtest_id": record.backtest_id,
        "name": record.name,
        "summary": dict(record.summary),
        "warnings": list(record.warnings),
        "created_at": record.created_at.isoformat(),
        **cone,
    }


@router.get("/backtests/{backtest_id}/tearsheet.pdf", summary="Export the tearsheet as PDF")
async def backtest_tearsheet(engine: Backtests, backtest_id: str) -> Response:
    """Download a completed run's tearsheet as a self-contained PDF.

    Every number on the page is read from the persisted run, so the file can
    never disagree with the screen it was exported from.
    """
    with translate_errors("backtest"):
        body, filename = await _thread(lambda: engine.tearsheet(backtest_id))
    return Response(
        content=body,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.delete("/backtests/{backtest_id}", summary="Delete a backtest")
async def delete_backtest(engine: Backtests, backtest_id: str) -> dict[str, Any]:
    """Delete one persisted run."""
    with translate_errors("backtest"):
        await _thread(lambda: engine.delete(backtest_id))
    return {"deleted": backtest_id}


@router.get("/backtests/{backtest_id}/trades", summary="Trade log of one backtest")
async def backtest_trades(
    engine: Backtests,
    backtest_id: str,
    limit: Annotated[int, Query(ge=1, le=2000)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Return the round-trip trade ledger recorded with the run.

    Runs executed before the ledger existed have no stored trades; the response
    says so instead of returning an empty table that looks like "no trades".
    """
    with translate_errors("backtest"):
        record = await _thread(lambda: engine.get(backtest_id))
    log = record.trade_log
    if not log:
        return {
            "backtest_id": backtest_id,
            "rows": [],
            "total": 0,
            "available": False,
            "reason": (
                "this run was executed before the trade ledger was recorded; re-run it to "
                "populate the trade log"
            ),
        }
    rows = list(log.get("rows") or [])
    return {
        "backtest_id": backtest_id,
        "available": True,
        "total": int(log.get("total") or 0),
        "closed": log.get("closed"),
        "open": log.get("open"),
        "win_rate": log.get("win_rate"),
        "average_return": log.get("average_return"),
        "average_bars": log.get("average_bars"),
        "offset": offset,
        "rows": rows[offset : offset + limit],
        "returned": len(rows[offset : offset + limit]),
        "stored": len(rows),
    }


@router.get("/backtests/{backtest_id}/artifact", summary="Artifact bundle of one backtest")
async def backtest_artifact(engine: Backtests, backtest_id: str) -> dict[str, Any]:
    """Return the pinned ``cfg + data + code`` bundle that reproduces the run."""
    with translate_errors("backtest"):
        record = await _thread(lambda: engine.get(backtest_id))
    bundle = record.artifact_bundle
    if not bundle:
        return {
            "backtest_id": backtest_id,
            "available": False,
            "reason": (
                "this run predates artifact pinning; re-run it to pin its configuration, "
                "data and code digests"
            ),
        }
    return {"backtest_id": backtest_id, "available": True, **bundle}


@router.get(
    "/backtests/{backtest_id}/returns.csv",
    summary="Download the return series as CSV",
    response_class=PlainTextResponse,
)
async def backtest_returns_csv(engine: Backtests, backtest_id: str) -> PlainTextResponse:
    """Stream the run's equity, return, drawdown and benchmark columns as CSV.

    The rows are derived from the stored equity curve. A very long run is
    persisted thinned, in which case the header comment names the sampling step
    so nobody mistakes a sampled series for every bar.
    """
    with translate_errors("backtest"):
        record = await _thread(lambda: engine.get(backtest_id))
    curve = record.equity_curve or {}
    dates = list(curve.get("dates") or [])
    if not dates:
        raise HTTPException(
            status_code=409,
            detail=f"backtest {backtest_id} has no equity curve to export (status {record.status})",
        )
    equity = [float(value) for value in curve.get("equity") or []]
    drawdown = list(curve.get("drawdown") or [])
    benchmark = list(curve.get("benchmark") or [])
    has_benchmark = len(benchmark) == len(dates)
    lines: list[str] = []
    step = int(curve.get("step") or 1)
    lines.append(f"# tradinglib backtest {backtest_id} · {record.name}")
    lines.append(
        f"# {len(dates)} rows"
        + (f" · SAMPLED every {step} bars of {curve.get('bars')}" if step > 1 else " · every bar")
    )
    header = ["date", "equity", "return", "drawdown"]
    if has_benchmark:
        header += ["benchmark_equity", "benchmark_return"]
    lines.append(",".join(header))
    for index, stamp in enumerate(dates):
        previous = equity[index - 1] if index else None
        period = (equity[index] / previous - 1.0) if previous else None
        row = [
            str(stamp),
            f"{equity[index]:.10g}",
            "" if period is None else f"{period:.10g}",
            "" if index >= len(drawdown) or drawdown[index] is None else f"{drawdown[index]:.10g}",
        ]
        if has_benchmark:
            current = benchmark[index]
            prior = benchmark[index - 1] if index else None
            bench_return = (
                (current / prior - 1.0) if index and current is not None and prior else None
            )
            row.append("" if current is None else f"{float(current):.10g}")
            row.append("" if bench_return is None else f"{bench_return:.10g}")
        lines.append(",".join(row))
    filename = f"{backtest_id}-returns.csv"
    return PlainTextResponse(
        "\n".join(lines) + "\n",
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# attached analyses: sweeps and walk-forward
# ---------------------------------------------------------------------------
@router.get("/analyses/options", summary="Sweepable parameters and metrics")
async def analysis_options() -> dict[str, Any]:
    """Return what a sweep axis may vary and what it may be ranked on.

    The Strategy Lab's *+ SWEEP* / *+ WALK-FORWARD* forms build their selects
    from this, so the UI can never offer a parameter the engine would reject.
    """
    return {
        "parameters": [
            {"path": path, "type": getattr(caster, "__name__", str(caster))}
            for path, caster in sorted(SWEEPABLE.items())
        ],
        "metrics": [
            {"name": name, "higher_is_better": higher} for name, higher in sorted(METRICS.items())
        ],
        "max_cells": MAX_SWEEP_CELLS,
        "windows": ["full", "in_sample", "out_of_sample"],
    }


@router.get("/analyses", summary="List attached analyses")
async def list_analyses(
    engine: Analyses,
    kind: Annotated[AnalysisKind | None, Query()] = None,
    experiment_id: Annotated[str | None, Query()] = None,
    backtest_id: Annotated[str | None, Query()] = None,
    hypothesis_id: Annotated[str | None, Query()] = None,
    include_cells: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    """Return the analyses attached to an experiment/backtest, newest first."""
    with translate_errors("analysis"):
        records = await _thread(
            lambda: engine.list_analyses(
                kind=kind,
                experiment_id=experiment_id,
                backtest_id=backtest_id,
                hypothesis_id=hypothesis_id,
                limit=limit,
            )
        )
    return {
        "analyses": [record.as_dict(include_cells=include_cells) for record in records],
        "count": len(records),
    }


@router.get("/analyses/counts", summary="Trial-ledger analysis counts")
async def analysis_counts(engine: Analyses) -> dict[str, Any]:
    """Return sweep/walk-forward batch and cell counts for the trial ledger."""
    with translate_errors("analysis"):
        return await _thread(engine.counts)


@router.post("/analyses/sweep", status_code=201, summary="Run a parameter sweep")
async def run_sweep(engine: Analyses, backtests: Backtests, body: SweepRequest) -> dict[str, Any]:
    """Run a parameter sweep over a configuration and persist every cell."""
    with translate_errors("sweep"):
        config = await _resolve_analysis_config(backtests, body)
        spec = SweepSpec.from_dict(body.spec)
        record = await engine.run_sweep(
            config,
            spec,
            experiment_id=body.experiment_id,
            hypothesis_id=body.hypothesis_id,
            backtest_id=body.backtest_id,
        )
    return record.as_dict()


@router.post("/analyses/walk-forward", status_code=201, summary="Run a walk-forward analysis")
async def run_walk_forward(
    engine: Analyses, backtests: Backtests, body: WalkForwardRequest
) -> dict[str, Any]:
    """Run a walk-forward analysis: select on each train window, score on the test."""
    with translate_errors("walk-forward"):
        config = await _resolve_analysis_config(backtests, body)
        spec = WalkForwardSpec.from_dict(body.spec)
        record = await engine.run_walk_forward(
            config,
            spec,
            experiment_id=body.experiment_id,
            hypothesis_id=body.hypothesis_id,
            backtest_id=body.backtest_id,
        )
    return record.as_dict()


@router.post("/analyses/monte-carlo", status_code=201, summary="Run a Monte-Carlo resample")
async def run_monte_carlo(
    engine: Analyses, backtests: Backtests, body: MonteCarloRequest
) -> dict[str, Any]:
    """Block-bootstrap a configuration's realised returns into an equity cone.

    The sweep asks whether the parameter matters and the walk-forward whether
    the selection survives; this asks how much of the realised curve was the
    order the returns happened to arrive in.
    """
    with translate_errors("monte carlo"):
        config = await _resolve_analysis_config(backtests, body)
        record = await engine.run_monte_carlo(
            config,
            paths=body.paths,
            shown=body.shown,
            block=body.block,
            seed=body.seed,
            name=body.name,
            experiment_id=body.experiment_id,
            hypothesis_id=body.hypothesis_id,
            backtest_id=body.backtest_id,
        )
    return record.as_dict()


@router.get("/analyses/{analysis_id}", summary="Return one analysis")
async def get_analysis(engine: Analyses, analysis_id: str) -> dict[str, Any]:
    """Return one analysis with all of its cells or folds."""
    with translate_errors("analysis"):
        record = await _thread(lambda: engine.get(analysis_id))
    return record.as_dict()


@router.delete("/analyses/{analysis_id}", summary="Delete an analysis")
async def delete_analysis(engine: Analyses, analysis_id: str) -> dict[str, Any]:
    """Delete one analysis and its cells."""
    with translate_errors("analysis"):
        await _thread(lambda: engine.delete(analysis_id))
    return {"deleted": analysis_id}


async def _resolve_analysis_config(
    backtests: BacktestEngine, body: SweepRequest | WalkForwardRequest
) -> BacktestConfig:
    """Return the base configuration an analysis should vary.

    An explicit ``config`` wins; otherwise the configuration is read back from
    the backtest or experiment the analysis is being attached to, so a sweep
    always starts from the run the user is looking at.
    """
    if body.config:
        return BacktestConfig.from_dict(body.config)
    if body.backtest_id:
        record = await _thread(lambda: backtests.get(body.backtest_id or ""))
        return record.config
    raise ValueError("an analysis needs either an explicit config or a backtest_id to read one from")


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------
@router.get("", summary="Strategy Lab summary")
async def lab_summary(
    hypotheses: Hypotheses, experiments: Experiments, backtests: Backtests
) -> dict[str, Any]:
    """Return the counts the Strategy Lab tab bar shows, all read from tables."""
    with translate_errors("lab"):
        summary = await _thread(hypotheses.summary)
        experiment_count = len(await _thread(lambda: experiments.list()))
        run_count = await _thread(lambda: experiments.count_runs())
        backtest_count = await _thread(lambda: backtests.count())
    return {
        "hypotheses": summary,
        "experiments": experiment_count,
        "experiment_runs": run_count,
        "backtests": backtest_count,
    }


@router.get("/validation", summary="List validation reports")
async def list_reports(
    engine: Validation,
    backtest_id: Annotated[str | None, Query()] = None,
    hypothesis_id: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    """Return persisted gate reports, newest first."""
    with translate_errors("validation report"):
        reports = await _thread(
            lambda: engine.reports(
                backtest_id=backtest_id, hypothesis_id=hypothesis_id, limit=limit
            )
        )
    return {"reports": [report.as_dict() for report in reports], "count": len(reports)}


@router.get("/validation/gates", summary="The gate suite")
async def validation_gates(engine: Validation) -> dict[str, Any]:
    """Return the gate definitions and thresholds, with nothing observed yet.

    This is the "workspace defaults" panel: each gate's id, name, threshold,
    comparator and whether it is hard (never overridable).
    """
    with translate_errors("validation gate"):
        gates = engine.default_gates()
    return {
        "gates": [gate.as_dict() for gate in gates],
        "count": len(gates),
        "thresholds": engine.thresholds.as_dict(),
        "hard_gates": [gate.id for gate in gates if gate.hard],
    }


@router.get("/validation/strategies", summary="Promoted strategies")
async def list_strategies(
    engine: Validation,
    include_killed: Annotated[bool, Query()] = True,
) -> dict[str, Any]:
    """Return the promoted strategies, newest first."""
    with translate_errors("strategy"):
        rows = await _thread(lambda: engine.promoted(include_killed=include_killed))
    return {"strategies": rows, "count": len(rows)}


@router.post("/validation/strategies/{strategy_id}/status", summary="Set a strategy's status")
async def set_strategy_status(
    engine: Validation, strategy_id: str, body: StrategyStatus
) -> dict[str, Any]:
    """Mark a promoted strategy ``active``, ``held`` or ``killed``."""
    with translate_errors("strategy"):
        return await _thread(
            lambda: engine.set_strategy_status(strategy_id, body.status, note=body.note)
        )


@router.get("/validation/reports/{report_id}", summary="Return one report")
async def get_report(engine: Validation, report_id: str) -> dict[str, Any]:
    """Return one persisted validation report."""
    with translate_errors("validation report"):
        report = await _thread(lambda: engine.get_report(report_id))
    return report.as_dict()


@router.post("/validation/{backtest_id}", status_code=201, summary="Evaluate the gates")
async def evaluate_backtest(
    engine: Validation,
    backtest_id: str,
    body: Annotated[ValidationRequest, Body()] = ValidationRequest(),
) -> dict[str, Any]:
    """Evaluate every gate against a completed backtest and persist the report.

    The cost-sensitivity gate genuinely re-runs the backtest with the cost
    model scaled, so this endpoint does real work rather than reading a stored
    number.
    """
    with translate_errors("backtest"):
        report = await engine.evaluate(
            backtest_id,
            thresholds=GateThresholds.from_dict(body.thresholds) if body.thresholds else None,
        )
    return report.as_dict()


@router.get("/validation/{backtest_id}/gates", summary="Gate outcomes for one backtest")
async def backtest_gates(engine: Validation, backtest_id: str) -> dict[str, Any]:
    """Return the newest gate outcomes for a backtest.

    When the backtest has never been validated the gate *definitions* are
    returned with no observed values and ``evaluated: false`` - the UI shows
    them as pending rather than as failures nobody measured.
    """
    with translate_errors("validation report"):
        report = await _thread(lambda: engine.latest_report(backtest_id))
        if report is None:
            gates = engine.default_gates()
            return {
                "backtest_id": backtest_id,
                "evaluated": False,
                "report": None,
                "gates": [gate.as_dict() for gate in gates],
                "thresholds": engine.thresholds.as_dict(),
                "detail": "this backtest has not been validated yet",
            }
    payload = report.as_dict()
    return {
        "backtest_id": backtest_id,
        "evaluated": True,
        "report": payload,
        "gates": payload["gates"],
        "thresholds": payload["thresholds"],
        "detail": None,
    }


@router.post("/validation/{backtest_id}/promote", summary="Promote a strategy")
async def promote(
    engine: Validation,
    backtest_id: str,
    body: Annotated[PromoteRequest, Body()] = PromoteRequest(),
) -> dict[str, Any]:
    """Record one approver's sign-off on promoting a validated backtest.

    Promotion is a two-key operation: the first named approver's call records a
    sign-off and returns ``promotion_pending``; the strategy is created only
    when a second, different approver signs the same backtest.

    Raises:
        HTTPException: 409 when a gate is still outstanding, when a hard gate
            was overridden, when an override arrived without a note, when the
            approver is unnamed or when the same person signs twice. The
            response body carries the domain's own explanation.
    """
    with translate_errors("backtest"):
        report = await engine.promote(
            backtest_id,
            comment=body.comment,
            overrides=body.overrides,
            stage=body.stage,
            actor=body.actor,
        )
        state = await _thread(
            lambda: engine.promotion_state(backtest_id, stage=body.stage)
        )
    return {**report.as_dict(), "promotion": state}


@router.get("/validation/{backtest_id}/approvals", summary="Promotion sign-offs")
async def promotion_approvals(
    engine: Validation,
    backtest_id: str,
    stage: Annotated[ExperimentStage, Query()] = ExperimentStage.PAPER,
) -> dict[str, Any]:
    """Return who has signed this backtest's promotion and who is still needed."""
    with translate_errors("backtest"):
        return await _thread(lambda: engine.promotion_state(backtest_id, stage=stage))


@router.delete(
    "/validation/{backtest_id}/approvals/{actor}", summary="Withdraw a sign-off"
)
async def withdraw_promotion_approval(
    engine: Validation, backtest_id: str, actor: str
) -> dict[str, Any]:
    """Withdraw one outstanding sign-off.

    A sign-off that already released a strategy is history and cannot be
    withdrawn; only a pending one can.
    """
    with translate_errors("backtest"):
        removed = await _thread(
            lambda: engine.withdraw_promotion_approval(backtest_id, actor)
        )
        state = await _thread(lambda: engine.promotion_state(backtest_id))
    return {"withdrawn": removed, "promotion": state}


@router.get("/validation/promotions/pending", summary="Promotions awaiting a co-signer")
async def pending_promotions(
    engine: Validation,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    """Return every backtest carrying a partial set of promotion sign-offs."""
    with translate_errors("backtest"):
        rows = await _thread(lambda: engine.pending_promotions(limit=limit))
    return {"pending": rows, "count": len(rows), "required": engine.approvals_required}


# ---------------------------------------------------------------------------
# evaluation
# ---------------------------------------------------------------------------
@router.get("/evaluation", summary="Evaluation workspace payload")
async def evaluation(
    engine: Evaluation,
    days: Annotated[int, Query(ge=1, le=730, description="gate-outcome window")] = 180,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    """Return the whole Evaluation tab: summary, funnel, scorecard, candidates."""
    with translate_errors("evaluation"):
        summary = await _thread(engine.summary)
        funnel = await _thread(engine.funnel)
        scorecard = await _thread(engine.scorecard)
        candidates = await _thread(lambda: engine.candidates(limit=limit))
        outcomes = await _thread(lambda: engine.gate_outcomes(days=days))
        tracking = await _thread(engine.list)
    return {
        "summary": summary,
        "funnel": funnel,
        "scorecard": scorecard,
        "candidates": candidates,
        "gate_outcomes": outcomes,
        "tracking": [record.as_dict() for record in tracking],
    }


@router.get("/evaluation/candidates", summary="Candidate table")
async def evaluation_candidates(
    engine: Evaluation, limit: Annotated[int, Query(ge=1, le=500)] = 100
) -> dict[str, Any]:
    """Return one row per experiment: its newest backtest and gate outcome."""
    with translate_errors("evaluation"):
        rows = await _thread(lambda: engine.candidates(limit=limit))
    return {"candidates": rows, "count": len(rows)}


@router.get("/evaluation/tracking", summary="Promoted-strategy tracking")
async def evaluation_tracking(engine: Evaluation) -> dict[str, Any]:
    """Return the stored out-of-sample tracking records of promoted strategies."""
    with translate_errors("evaluation"):
        records = await _thread(engine.list)
    return {"tracking": [record.as_dict() for record in records], "count": len(records)}


@router.post("/evaluation/refresh", summary="Refresh every tracking record")
async def refresh_all_tracking(engine: Evaluation) -> dict[str, Any]:
    """Re-measure every promoted strategy over its post-promotion window."""
    with translate_errors("evaluation"):
        records = await engine.update_all()
    return {"tracking": [record.as_dict() for record in records], "count": len(records)}


@router.get("/evaluation/{strategy_id}", summary="One tracking record")
async def evaluation_record(engine: Evaluation, strategy_id: str) -> dict[str, Any]:
    """Return the stored tracking record of one promoted strategy."""
    with translate_errors("strategy"):
        record = await _thread(lambda: engine.get(strategy_id))
    return record.as_dict()


@router.post("/evaluation/{strategy_id}/refresh", summary="Refresh one tracking record")
async def refresh_tracking(engine: Evaluation, strategy_id: str) -> dict[str, Any]:
    """Re-run the promoted specification over data since the promotion.

    This is a genuine out-of-sample measurement of the promoted rule, not a
    simulation of fills: there is no execution model behind it, and the record
    says so.
    """
    with translate_errors("strategy"):
        record = await engine.update(strategy_id)
    return record.as_dict()


@router.get("/evaluation/{strategy_id}/decay", summary="Rolling-Sharpe decay series")
async def evaluation_decay(
    engine: Evaluation,
    strategy_id: str,
    window: Annotated[int, Query(ge=5, le=756)] = 63,
) -> dict[str, Any]:
    """Return the rolling Sharpe of a promoted strategy since promotion."""
    with translate_errors("strategy"):
        series = await engine.decay(strategy_id, window=window)
    return {
        "strategy_id": strategy_id,
        "window": window,
        "points": [
            {"date": _stamp(index), "sharpe": None if value != value else float(value)}
            for index, value in series.items()
        ],
        "count": int(len(series)),
    }


def _stamp(value: Any) -> str:
    """Render a pandas index label as an ISO date string."""
    if isinstance(value, date):
        return value.isoformat()
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if callable(isoformat) else str(value)
