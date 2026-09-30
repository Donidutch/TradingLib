"""FastAPI application factory.

Boots the cross-cutting core (settings, database, event bus, WebSocket hub),
constructs the domain services the REST routers depend on, mounts the routers,
exposes ``/ws`` and serves the ``ui/`` directory as static files at ``/``.

What the lifespan starts, in order:

1. settings directories and the SQLite schema;
2. the universe and watchlist registries (built-in universes are created, but
   nothing is fetched from a vendor here);
3. the ingestion source catalog with its five built-in sources, the ingest
   scheduler (bound to a scope resolver backed by the universe registry) and
   the ingest gateway;
4. the data-quality engine;
5. the research engines - Stock Research, the screener and the Strategy Lab -
   which are also installed as the process-wide instances so the domain
   modules that reach for each other (and the screener's backtest queue) share
   this application's store, registries and event bus;
6. the Alpaca market stream, when credentials are configured - otherwise its
   status stays an honest "not_configured";
7. the ``status`` and ``tape`` live publishers and the ``lab`` -> ``runs``
   bridge.

Starting the ingest scheduler means real vendor requests, so it is gated on
``TRADINGLIB_INGEST_AUTOSTART`` (default on; tests turn it off and drive
``tick()``/``run_now()`` explicitly).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, WebSocket
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from tradinglib import __version__
from tradinglib.api.live import (
    DEFAULT_STATUS_INTERVAL,
    DEFAULT_TAPE_INTERVAL,
    LabRunBridge,
    StatusBroadcaster,
    TapePublisher,
)
from tradinglib.api.routes import agents as agent_routes
from tradinglib.api.routes import ingest as ingest_routes
from tradinglib.api.routes import lab as lab_routes
from tradinglib.api.routes import macro as macro_routes
from tradinglib.api.routes import quality as quality_routes
from tradinglib.api.routes import research as research_routes
from tradinglib.api.routes import screens as screen_routes
from tradinglib.api.routes import search as search_routes
from tradinglib.api.routes import settings as settings_routes
from tradinglib.api.routes import status as status_routes
from tradinglib.api.routes import trade as trade_routes
from tradinglib.api.routes import universes as universe_routes
from tradinglib.api.routes import watchlists as watchlist_routes
from tradinglib.api.scope import build_scope_resolver
from tradinglib.api.ws import WsHub
from tradinglib.config import Settings, get_settings
from tradinglib.data.ingest.catalog import SourceCatalog
from tradinglib.data.ingest.gateway import IngestGateway
from tradinglib.data.ingest.scheduler import IngestScheduler
from tradinglib.data.macro import MacroService
from tradinglib.data.quality import QualityEngine
from tradinglib.data.store import TimeSeriesStore
from tradinglib.data.universe import UniverseRegistry
from tradinglib.db import dispose_engine, init_db
from tradinglib.events import EventBus, get_event_bus

logger = logging.getLogger(__name__)


def _env_flag(name: str, default: bool) -> bool:
    """Read a boolean environment flag (``1/true/yes/on`` are true)."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    """Read a float environment setting, falling back to ``default``."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number; using %.3f", name, raw, default)
        return default


def _build_services(app: FastAPI) -> None:
    """Create the domain services on ``app.state``. Local work only, no vendors."""
    settings: Settings = app.state.settings
    bus: EventBus = app.state.bus

    store = TimeSeriesStore()
    universes = UniverseRegistry()
    universes.ensure_builtins()
    watchlists = universes.watchlists

    catalog = SourceCatalog(bus, connector_factory=app.state.connector_factory)
    catalog.register_builtins()
    scheduler = IngestScheduler(
        catalog, bus, store=store, scope_resolver=build_scope_resolver(universes)
    )
    gateway = IngestGateway(catalog, bus)
    quality = QualityEngine(store, bus=bus)

    app.state.store = store
    app.state.universe_registry = universes
    app.state.watchlist_registry = watchlists
    app.state.source_catalog = catalog
    app.state.ingest_scheduler = scheduler
    app.state.ingest_gateway = gateway
    app.state.quality_engine = quality
    app.state.macro_service = MacroService(store)
    _build_research_services(app, store=store, universes=universes, bus=bus)
    _build_agent_services(app, store=store, universes=universes, bus=bus, settings=settings)
    logger.info(
        "domain services ready (%d sources, %d universes, data_dir=%s)",
        len(catalog.list_sources()),
        len(universes.list_universes(include_members=False)),
        settings.data_dir,
    )


def _build_research_services(
    app: FastAPI, *, store: TimeSeriesStore, universes: UniverseRegistry, bus: EventBus
) -> None:
    """Create the research engines (Stock Research, screener, Strategy Lab).

    Every engine is bound to *this* application's store, universe registry and
    event bus, and each is also installed as the process-wide instance
    (``reset_*``): the domain modules reach for each other through those
    singletons (a hypothesis asks the experiment engine for its trial counts,
    the screener asks whichever backtest queue is registered), and a server
    whose engines disagreed about which bus to publish on would stream progress
    into the void.

    Constructing them creates their SQLite tables (``init_db`` has already run)
    but touches no vendor: the first request is what fetches data.
    """
    from tradinglib.research.backtest import (
        BacktestEngine,
        LabBacktestQueue,
        install_backtest_queue,
        reset_backtest_engine,
    )
    from tradinglib.research.analyses import AnalysisEngine, reset_analysis_engine
    from tradinglib.research.company import CompanyResearch
    from tradinglib.research.evaluation import EvaluationEngine, reset_evaluation_engine
    from tradinglib.research.experiments import ExperimentEngine, reset_experiment_engine
    from tradinglib.research.hypotheses import HypothesisRegistry, reset_hypothesis_registry
    from tradinglib.research.screen.funnel import FunnelEvaluator, ScreenRegistry
    from tradinglib.research.screen.rules import MetricBuilder
    from tradinglib.research.validation import ValidationEngine, reset_validation_engine

    research = CompanyResearch(store=store, universes=universes)
    screens = ScreenRegistry()
    funnel = FunnelEvaluator(
        registry=screens,
        universes=universes,
        builder=MetricBuilder(store=store),
        bus=bus,
    )

    backtests = reset_backtest_engine(BacktestEngine(store=store, bus=bus, registry=universes))
    hypotheses = reset_hypothesis_registry(HypothesisRegistry(bus=bus))
    experiments = reset_experiment_engine(ExperimentEngine(bus=bus, backtests=backtests))
    validation = reset_validation_engine(
        ValidationEngine(bus=bus, backtests=backtests, experiments=experiments)
    )
    evaluation = reset_evaluation_engine(
        EvaluationEngine(
            bus=bus,
            backtests=backtests,
            experiments=experiments,
            validation=validation,
            hypotheses=hypotheses,
        )
    )
    # "Backtest this screen" must create a real lab record: register the lab's
    # queue, bound to this application's engine, as the process-wide one.
    queue = install_backtest_queue(LabBacktestQueue(backtests))
    analyses = reset_analysis_engine(AnalysisEngine(backtests=backtests, bus=bus))

    from tradinglib.research.screen.alerts import ScreenAlertMonitor

    app.state.company_research = research
    app.state.screen_registry = screens
    app.state.funnel_evaluator = funnel
    # "PIN + ALERT ON CHANGES" is real: pinned screens are re-run on a cadence
    # and membership changes become alerts on the `notifications` channel.
    app.state.screen_alerts = ScreenAlertMonitor(funnel, bus)
    app.state.backtest_engine = backtests
    app.state.backtest_queue = queue
    app.state.hypothesis_registry = hypotheses
    app.state.experiment_engine = experiments
    app.state.validation_engine = validation
    app.state.evaluation_engine = evaluation
    app.state.analysis_engine = analyses


def _build_agent_services(
    app: FastAPI,
    *,
    store: TimeSeriesStore,
    universes: UniverseRegistry,
    bus: EventBus,
    settings: Settings,
) -> None:
    """Create the Agent Center services (``docs/SPEC.md`` sections 4 and 5).

    Everything is bound to *this* application's event bus, so run progress,
    stage logs and approval decisions reach ``api/ws.py`` and therefore the
    browser on the ``runs`` and ``approvals`` channels. Each service is also
    installed as the process-wide instance (``reset_*``): the domain modules
    resolve each other through those singletons - the approval gate looks up
    the runner, the runner looks up the audit log - and a second, unbound set
    would publish into a bus nobody is listening to.

    The agents' tools are wired to the same engines the REST API serves, so a
    stage that runs a screen or a backtest touches the real registries rather
    than a private copy. Budgets saved through ``PUT /api/agents/config`` are
    applied here; without that file the configured (TOML/environment) budgets
    stay in force.

    Constructing these creates their SQLite tables and seeds the shipped agent
    roster and default pipeline, but starts no timer and calls no vendor.
    """
    from tradinglib.agents.approvals import ApprovalGate, reset_approval_gate
    from tradinglib.agents.audit import AuditLog, reset_audit_log
    from tradinglib.agents.autopipeline import AutoPipeline, reset_autopipeline
    from tradinglib.agents.findings import FindingsStore, reset_findings_store
    from tradinglib.agents.knowledge import KnowledgeBase, reset_knowledge_base
    from tradinglib.research.memory import ResearchMemoryComposer, reset_memory_composer
    from tradinglib.agents.pipeline import PipelineRegistry, reset_pipeline_registry
    from tradinglib.agents.providers import ProviderRegistry, reset_provider_registry
    from tradinglib.agents.registry import AgentRegistry, reset_agent_registry
    from tradinglib.agents.runner import LibraryToolset, PipelineRunner, reset_pipeline_runner
    from tradinglib.api.routes.agents import load_saved_budgets

    audit = reset_audit_log(AuditLog())
    findings = reset_findings_store(FindingsStore(bus=bus))
    knowledge = reset_knowledge_base(KnowledgeBase(bus=bus))
    providers = reset_provider_registry(ProviderRegistry(settings=settings))
    agents = reset_agent_registry(AgentRegistry(bus=bus))
    pipelines = reset_pipeline_registry(PipelineRegistry(bus=bus))
    agents.ensure_defaults()
    pipelines.ensure_defaults()

    toolset = LibraryToolset(
        universes=universes,
        funnel=app.state.funnel_evaluator,
        screens=app.state.screen_registry,
        research=app.state.company_research,
        store=store,
        backtests=app.state.backtest_engine,
        findings=findings,
        knowledge=knowledge,
        settings=settings,
    )
    runner = reset_pipeline_runner(
        PipelineRunner(
            bus,
            pipelines=pipelines,
            agents=agents,
            providers=providers,
            toolset=toolset,
            settings=settings,
            audit=audit,
        )
    )
    saved_budgets = load_saved_budgets(settings.data_dir)
    if saved_budgets is not None:
        runner.ledger.set_budgets(saved_budgets)
        logger.info("agent budgets loaded from %s", settings.data_dir / "agent_budgets.json")
    gate = reset_approval_gate(ApprovalGate(bus, runner=runner, audit=audit))
    auto = reset_autopipeline(AutoPipeline(runner, bus, audit=audit))

    app.state.audit_log = audit
    app.state.findings_store = findings
    app.state.knowledge_base = knowledge
    app.state.provider_registry = providers
    app.state.agent_registry = agents
    app.state.pipeline_registry = pipelines
    app.state.pipeline_runner = runner
    app.state.approval_gate = gate
    app.state.autopipeline = auto
    # The research-memory composer spans both halves (Strategy Lab hypotheses +
    # agent findings/knowledge), so it is wired once both exist.
    app.state.memory_composer = reset_memory_composer(
        ResearchMemoryComposer(
            hypotheses=app.state.hypothesis_registry,
            findings=findings,
            knowledge=knowledge,
        )
    )


async def _start_alpaca_stream(app: FastAPI) -> None:
    """Start the Alpaca stream when credentials exist; report honestly otherwise."""
    settings: Settings = app.state.settings
    if not (settings.alpaca_api_key and settings.alpaca_secret_key):
        app.state.alpaca_stream = None
        app.state.alpaca_stream_status = {
            "status": "not_configured",
            "configured": False,
            "running": False,
            "detail": "ALPACA_API_KEY / ALPACA_SECRET_KEY are not configured",
        }
        logger.info("alpaca stream not started: credentials are not configured")
        return

    from tradinglib.data.providers.alpaca_provider import AlpacaStream

    stream = AlpacaStream(
        app.state.bus,
        api_key=settings.alpaca_api_key,
        secret_key=settings.alpaca_secret_key,
        feed=settings.alpaca_data_feed,
    )
    try:
        await stream.start()
    except Exception as exc:  # noqa: BLE001 - a vendor SDK may raise anything
        app.state.alpaca_stream = None
        app.state.alpaca_stream_status = {
            "status": "error",
            "configured": True,
            "running": False,
            "detail": f"{type(exc).__name__}: {exc}",
        }
        logger.warning("alpaca stream failed to start: %s", exc, exc_info=True)
        return
    app.state.alpaca_stream = stream
    app.state.alpaca_stream_status = {"status": "up", **stream.status()}
    logger.info("alpaca stream started (feed=%s)", stream.feed)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start and stop the process-wide subsystems around the server's life."""
    settings: Settings = app.state.settings
    settings.ensure_dirs()
    init_db()
    # Credentials saved from the Settings page are exported into the
    # environment and re-resolved here, so a key typed into the UI has exactly
    # the same effect as one exported before the server started.
    app.state.settings = settings = settings_routes.apply_persisted_settings()

    bus: EventBus = app.state.bus
    hub: WsHub = app.state.ws_hub
    hub.attach()

    _build_services(app)
    await _start_alpaca_stream(app)

    scheduler: IngestScheduler = app.state.ingest_scheduler
    if app.state.autostart_ingest:
        await scheduler.start()
    else:
        logger.info("ingest scheduler not autostarted (TRADINGLIB_INGEST_AUTOSTART is off)")

    broadcaster = StatusBroadcaster(
        app,
        bus,
        hub,
        interval=_env_float("TRADINGLIB_STATUS_INTERVAL", DEFAULT_STATUS_INTERVAL),
    )
    tape = TapePublisher(
        bus,
        hub,
        universes=app.state.universe_registry,
        watchlists=app.state.watchlist_registry,
        stream_getter=lambda: getattr(app.state, "alpaca_stream", None),
        interval=_env_float("TRADINGLIB_TAPE_INTERVAL", DEFAULT_TAPE_INTERVAL),
    )
    lab_bridge = LabRunBridge(bus)
    lab_bridge.attach()
    app.state.lab_bridge = lab_bridge
    app.state.status_broadcaster = broadcaster
    app.state.tape_publisher = tape
    await broadcaster.start()
    await tape.start()

    await _start_autopipeline(app)
    monitor = getattr(app.state, "screen_alerts", None)
    if monitor is not None:
        await monitor.start()
    _seed_builtin_universes(app)

    _warn_if_forking_loop()
    app.state.started_monotonic = time.monotonic()
    logger.info("tradinglib %s ready (data_dir=%s)", __version__, settings.data_dir)
    await bus.publish("status", {"event": "server_started", "version": __version__})
    # A browser that connects mid-flight must see the real approval queue
    # rather than waiting for the next decision to tell it.
    with contextlib.suppress(Exception):
        await app.state.approval_gate.publish_pending()

    try:
        yield
    finally:
        await bus.publish("status", {"event": "server_stopping"})
        await tape.stop()
        await broadcaster.stop()
        lab_bridge.detach()
        seed_task = getattr(app.state, "universe_seed_task", None)
        if seed_task is not None and not seed_task.done():
            seed_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await seed_task
        await _stop_autopipeline(app)
        if monitor is not None:
            with contextlib.suppress(Exception):
                await monitor.stop()
        await _drain_runs(app)
        await _drain_backtests(app)
        await _cancel_backfills(app)
        await trade_routes.close_brokers(app)
        with contextlib.suppress(Exception):
            await scheduler.stop()
        stream = getattr(app.state, "alpaca_stream", None)
        if stream is not None:
            with contextlib.suppress(Exception):
                await stream.stop()
        hub.detach()
        bus.clear()
        dispose_engine()
        logger.info("tradinglib shut down")


def _seed_builtin_universes(app: FastAPI) -> None:
    """Fetch the constituents of any built-in universe that is still empty.

    ``ensure_builtins()`` creates the ``sp500`` and ``nasdaq100`` rows without
    members, because materialising them needs the network and startup must not
    block on a vendor. A universe the topbar offers but that resolves to no
    symbols reads as a broken app, so this pulls the real constituent list once,
    in the background. It never blocks the server, never overwrites a universe
    that already has members, and a fetch failure only leaves the universe as
    empty as it already was - Market & Data's "Refresh members" stays the
    explicit way to fix it.
    """
    registry = getattr(app.state, "universe_registry", None)
    if registry is None:  # pragma: no cover - services are always built first
        return
    if not _env_flag("TRADINGLIB_SEED_UNIVERSES", True):
        logger.info("built-in universe seeding is off (TRADINGLIB_SEED_UNIVERSES)")
        return

    async def seed() -> None:
        try:
            outcome = await registry.seed_unpopulated_builtins()
        except Exception:  # noqa: BLE001 - never fatal
            logger.warning("built-in universe seeding failed", exc_info=True)
            return
        if outcome:
            logger.info("seeded built-in universes: %s", outcome)

    app.state.universe_seed_task = asyncio.create_task(seed(), name="tradinglib-seed-universes")


async def _start_autopipeline(app: FastAPI) -> None:
    """Start the AutoPipeline timers, unless they are switched off.

    Starting them means scheduled pipeline runs really fire, which spends
    money, so it is gated on ``TRADINGLIB_AUTOPIPELINE_AUTOSTART`` exactly as the
    ingest scheduler is. A schedule that exists but is not running is reported
    honestly by ``GET /api/agents/autopipeline`` (``running: false``).
    """
    auto = getattr(app.state, "autopipeline", None)
    if auto is None:
        return
    if not _env_flag("TRADINGLIB_AUTOPIPELINE_AUTOSTART", True):
        logger.info("autopipeline not autostarted (TRADINGLIB_AUTOPIPELINE_AUTOSTART is off)")
        return
    try:
        await auto.start()
    except Exception:  # noqa: BLE001 - a broken timer must not stop the server
        logger.warning("autopipeline failed to start", exc_info=True)


async def _stop_autopipeline(app: FastAPI) -> None:
    """Stop the AutoPipeline timers at shutdown."""
    auto = getattr(app.state, "autopipeline", None)
    if auto is None:
        return
    with contextlib.suppress(Exception):
        await auto.stop()


async def _drain_runs(app: FastAPI, *, timeout: float = 20.0) -> None:
    """Wait briefly for agent pipeline runs still executing at shutdown.

    A run that does not finish inside ``timeout`` is cancelled with the loop and
    its row stays as it was - the honest record of a server that stopped
    mid-run, not a fabricated completion.
    """
    runner = getattr(app.state, "pipeline_runner", None)
    if runner is None:
        return
    try:
        await runner.drain(timeout=timeout)
    except TimeoutError:
        logger.warning("an agent run was still executing at shutdown; it was cancelled")
    except Exception:  # noqa: BLE001 - shutdown must not raise
        logger.warning("draining agent runs failed", exc_info=True)


async def _drain_backtests(app: FastAPI, *, timeout: float = 20.0) -> None:
    """Wait briefly for backtests the screener started in the background.

    A run that does not finish inside ``timeout`` is cancelled with the rest of
    the loop; its row stays ``running``, which is the truth about what happened
    rather than a fabricated result.
    """
    queue = getattr(app.state, "backtest_queue", None)
    if queue is None:
        return
    try:
        await asyncio.wait_for(queue.drain(), timeout=timeout)
    except TimeoutError:
        logger.warning("a queued backtest was still running at shutdown; it was cancelled")
    except Exception:  # noqa: BLE001 - shutdown must not raise
        logger.warning("draining queued backtests failed", exc_info=True)


async def _cancel_backfills(app: FastAPI) -> None:
    """Cancel any backfill still running when the server stops."""
    tasks: dict[str, asyncio.Task[Any]] = getattr(app.state, "backfill_tasks", {})
    for source_id, task in list(tasks.items()):
        if task.done():
            continue
        logger.info("cancelling in-flight backfill of %s", source_id)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    tasks.clear()


def _warn_if_forking_loop() -> None:
    """Warn when the server is running on an event loop that cannot spawn the LLM CLI.

    ``uvloop`` spawns subprocesses with ``fork()`` and then runs
    ``PyOS_AfterFork_Child`` in the child. In this process that tears down the
    thread states holding ``curl_cffi`` handles (yfinance's HTTP transport) and
    the child dies with SIGSEGV before it can ``exec`` - which is exactly what
    an agent stage does when it starts the Claude Code CLI. The standard
    asyncio loop spawns without running those handlers and is unaffected, so
    the server must be started with ``--loop asyncio`` (which is what
    :func:`serve` does).
    """
    loop = asyncio.get_running_loop()
    if type(loop).__module__.split(".")[0] != "uvloop":
        return
    logger.warning(
        "running on uvloop: LLM agent stages will fail with 'Cannot write to terminated "
        "process (exit code: -11)' because uvloop spawns subprocesses by forking this "
        "process. Restart with --loop asyncio (or `python -m tradinglib.api`)."
    )


def serve(
    host: str = "127.0.0.1",
    port: int = 8200,
    *,
    reload: bool = False,
    log_level: str = "info",
) -> None:
    """Run the API server on an event loop the agent stages can spawn from.

    Prefer this over a bare ``uvicorn`` invocation: it pins ``loop="asyncio"``,
    without which every LLM stage dies in the forked child (see
    :func:`_warn_if_forking_loop`).
    """
    import uvicorn

    uvicorn.run(
        "tradinglib.api.app:create_app",
        factory=True,
        host=host,
        port=port,
        reload=reload,
        log_level=log_level,
        loop="asyncio",
    )


class _NoCacheStaticFiles(StaticFiles):
    """Serve ``ui/`` with revalidation forced on every request.

    Starlette's ``StaticFiles`` sends only ``etag``/``last-modified``, which
    lets a browser keep executing a cached copy of an edited module until its
    heuristic freshness lifetime expires. The SPA is served from the same host
    it is developed on, so the correct trade is to always revalidate: the
    conditional request still answers ``304`` when nothing changed, but an
    edited file is picked up on the next reload.
    """

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        """Add ``Cache-Control: no-cache`` to every static response."""
        response = super().file_response(*args, **kwargs)
        response.headers["cache-control"] = "no-cache, must-revalidate"
        return response


def create_app(
    settings: Settings | None = None,
    *,
    bus: EventBus | None = None,
    autostart_ingest: bool | None = None,
    connector_factory: Any | None = None,
) -> FastAPI:
    """Build the TradingLib FastAPI application.

    Args:
        settings: override settings (tests pass a temporary data directory).
        bus: override event bus (tests pass an isolated bus).
        autostart_ingest: start the ingestion scheduler in the lifespan.
            Defaults to the ``TRADINGLIB_INGEST_AUTOSTART`` environment flag, which
            itself defaults to on: a real server ingests as soon as it boots.
        connector_factory: override used by tests to inject connectors instead
            of reaching real vendors.

    Returns:
        A configured :class:`~fastapi.FastAPI` instance.
    """
    resolved = settings or get_settings()
    app = FastAPI(
        title="TradingLib",
        version=__version__,
        description="Trading-research library, service and research console.",
        lifespan=lifespan,
    )

    app.state.settings = resolved
    app.state.bus = bus or get_event_bus()
    app.state.ws_hub = WsHub(app.state.bus)
    app.state.started_monotonic = time.monotonic()
    app.state.autostart_ingest = (
        _env_flag("TRADINGLIB_INGEST_AUTOSTART", True) if autostart_ingest is None else autostart_ingest
    )
    app.state.connector_factory = connector_factory
    app.state.backfill_tasks = {}
    # Execution adapters, created on first /api/trade request and closed by the
    # lifespan; see tradinglib.api.routes.trade.
    app.state.brokers = {}
    # Installed by the lifespan; absent means "not running", which the routes
    # report as 503 and /api/status as "not_started".
    app.state.store = None
    app.state.universe_registry = None
    app.state.watchlist_registry = None
    app.state.source_catalog = None
    app.state.ingest_scheduler = None
    app.state.ingest_gateway = None
    app.state.quality_engine = None
    app.state.alpaca_stream = None
    app.state.alpaca_stream_status = None
    app.state.status_broadcaster = None
    app.state.tape_publisher = None
    app.state.pipeline_runner = None
    app.state.lab_bridge = None
    app.state.company_research = None
    app.state.screen_registry = None
    app.state.screen_alerts = None
    app.state.funnel_evaluator = None
    app.state.backtest_engine = None
    app.state.backtest_queue = None
    app.state.hypothesis_registry = None
    app.state.experiment_engine = None
    app.state.validation_engine = None
    app.state.evaluation_engine = None
    app.state.analysis_engine = None
    app.state.agent_registry = None
    app.state.pipeline_registry = None
    app.state.provider_registry = None
    app.state.findings_store = None
    app.state.knowledge_base = None
    app.state.approval_gate = None
    app.state.audit_log = None
    app.state.autopipeline = None

    app.include_router(status_routes.router)
    app.include_router(universe_routes.router)
    app.include_router(watchlist_routes.router)
    app.include_router(ingest_routes.router)
    app.include_router(quality_routes.router)
    app.include_router(macro_routes.router)
    app.include_router(research_routes.router)
    app.include_router(screen_routes.router)
    app.include_router(lab_routes.router)
    app.include_router(agent_routes.router)
    app.include_router(trade_routes.router)
    app.include_router(settings_routes.router)
    app.include_router(search_routes.router)

    @app.get("/api/health", tags=["status"], summary="Liveness probe")
    async def health() -> dict[str, Any]:
        """Return a minimal liveness payload that needs no subsystem."""
        return {"ok": True, "version": __version__}

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        """Serve the subscribe protocol described in ``tradinglib.api.ws``."""
        hub: WsHub = websocket.app.state.ws_hub
        await hub.handle(websocket)

    ui_dir = resolved.ui_dir
    if ui_dir.is_dir():
        # Mounted last so /api and /ws win; html=True serves index.html at "/".
        app.mount("/", _NoCacheStaticFiles(directory=str(ui_dir), html=True), name="ui")
    else:  # pragma: no cover - only when the UI directory is missing
        logger.warning("UI directory %s not found; static serving disabled", ui_dir)

        @app.get("/")
        async def missing_ui() -> JSONResponse:
            """Report honestly that the UI directory is absent."""
            return JSONResponse(
                status_code=503,
                content={"error": "ui_not_found", "detail": str(ui_dir)},
            )

    return app


#: Module-level application for ``uvicorn tradinglib.api.app:app``.
app = create_app()
