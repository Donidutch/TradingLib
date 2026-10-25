/* One function per endpoint the ported pages use, each bound to its schema.
 * Pages call these instead of writing paths, so a route change lands here. */
import { z } from "zod";
import type { ApiClient } from "./client";
import { Breadth, ActiveScope, Changelog, Deleted, Members, Snapshot, Universe, UniverseList } from "./schemas/universes";
import {
  Datasets,
  MetricCatalogue,
  Preview,
  QueuedBacktest,
  Screen,
  ScreenAlerts,
  ScreenList,
  ScreenRun,
  ScreenRuns,
  type PreviewRequest,
} from "./schemas/screens";
import { AgentDefinitions, AgentRuns, FindingList, Inbox } from "./schemas/agents";
import {
  Analyses,
  Backtest,
  BacktestList,
  Evaluation,
  Experiment,
  ExperimentDetail,
  ExperimentList,
  ExperimentRun,
  GateSuite,
  Hypothesis,
  HypothesisList,
  LabSummary,
  Strategies,
  ValidationReports,
} from "./schemas/lab";
import { MacroPanel } from "./schemas/macro";
import {
  AltData,
  Company,
  Estimates,
  Filings,
  Financials,
  FreshnessReport,
  Memory,
  News,
  PriceHistory,
  PriceRanges,
  Resolve,
  Thesis,
  Valuation,
} from "./schemas/research";
import {
  BackfillStarted,
  ConnectionTest,
  SourceList,
  SourceProbe,
  SourceSaved,
  Subscription,
  SubscriptionList,
  type SourceForm,
} from "./schemas/ingest";
import {
  Incident,
  Incidents,
  OutlierDecided,
  Outliers,
  QualityRun,
  QualitySeries,
  QualitySummary,
  Sla,
  TicketOpened,
  Tickets,
} from "./schemas/quality";
import { Health, Settings, type SettingsPatch } from "./schemas/settings";
import { Search, Status } from "./schemas/status";
import { Watchlist, WatchlistList } from "./schemas/watchlists";

const enc = encodeURIComponent;
const company = (symbol: string, suffix = "") =>
  `/api/research/company/${enc(symbol.trim().toUpperCase())}${suffix}`;

export function makeApi(c: ApiClient) {
  return {
    client: c,
    status: (signal?: AbortSignal) => c.get("/api/status", Status, null, signal),
    health: (signal?: AbortSignal) => c.get("/api/health", Health, null, signal),

    settings: {
      get: (signal?: AbortSignal) => c.get("/api/settings", Settings, null, signal),
      /** Only the fields given change; the answer is the stored whole. */
      save: (patch: SettingsPatch) => c.request("PUT", "/api/settings", Settings, { body: patch }),
    },
    search: (q: string, signal?: AbortSignal) => c.get("/api/search", Search, { q }, signal),

    universes: {
      list: (signal?: AbortSignal) => c.get("/api/universes", UniverseList, null, signal),
      /** With every universe's members, for the collections table. */
      listWithMembers: (signal?: AbortSignal) =>
        c.get("/api/universes", UniverseList, { include_members: true }, signal),
      active: (signal?: AbortSignal) => c.get("/api/universes/active", ActiveScope, null, signal),
      /** A universe id or a `watchlist:<id>` reference. */
      setActive: (ref: string) => c.post("/api/universes/active", ActiveScope, { universe_id: ref }),
      /** `as_of` (YYYY-MM-DD) reconstructs point-in-time membership. */
      members: (id: string, query: { as_of?: string | null } = {}, signal?: AbortSignal) =>
        c.get(`/api/universes/${enc(id)}/members`, Members, query, signal),
      changelog: (id: string, limit = 6, signal?: AbortSignal) =>
        c.get(`/api/universes/${enc(id)}/changelog`, Changelog, { limit }, signal),
      create: (body: { name: string; symbols: string; description?: string | null; construction_method?: string; refresh?: string; make_active?: boolean }) =>
        c.post("/api/universes", Universe, body),
      update: (id: string, body: { name?: string; description?: string | null; symbols?: string }) =>
        c.request("PUT", `/api/universes/${enc(id)}`, Universe, { body }),
      remove: (id: string) => c.del(`/api/universes/${enc(id)}`, Deleted),
      refresh: (id: string) => c.post(`/api/universes/${enc(id)}/refresh`, Universe, {}),
      snapshot: (id: string) => c.post(`/api/universes/${enc(id)}/snapshot`, Snapshot, {}),
      /** Share of members above their own `window`-day average. */
      breadth: (id: string, query: { window?: number } = {}, signal?: AbortSignal) =>
        c.get(`/api/universes/${enc(id)}/breadth`, Breadth, query, signal),
    },

    watchlists: {
      list: (signal?: AbortSignal) => c.get("/api/watchlists", WatchlistList, null, signal),
      create: (body: { name: string; symbols?: string[] | string; is_default?: boolean }) =>
        c.post("/api/watchlists", Watchlist, body),
      get: (id: string, signal?: AbortSignal) => c.get(`/api/watchlists/${enc(id)}`, Watchlist, null, signal),
      update: (id: string, body: { name?: string; symbols?: string; is_default?: boolean }) =>
        c.request("PUT", `/api/watchlists/${enc(id)}`, Watchlist, { body }),
      remove: (id: string) => c.del(`/api/watchlists/${enc(id)}`, Deleted),
      addSymbol: (id: string, symbol: string) =>
        c.post(`/api/watchlists/${enc(id)}/symbols`, Watchlist, { symbol }),
      removeSymbol: (id: string, symbol: string) =>
        c.del(`/api/watchlists/${enc(id)}/symbols/${enc(symbol)}`, Watchlist),
    },

    screens: {
      metrics: (signal?: AbortSignal) => c.get("/api/screens/metrics", MetricCatalogue, null, signal),
      list: (signal?: AbortSignal) => c.get("/api/screens", ScreenList, null, signal),
      preview: (body: PreviewRequest, signal?: AbortSignal) =>
        c.request("POST", "/api/screens/preview", Preview, { body, signal, timeoutMs: 120_000 }),
      create: (body: { name: string; universe_id: string; rules: PreviewRequest["rules"]; rank_by: string; description?: string }) =>
        c.post("/api/screens", Screen, body),
      update: (id: string, body: { rules?: PreviewRequest["rules"]; rank_by?: string; universe_id?: string; tags?: string[] }) =>
        c.request("PUT", `/api/screens/${enc(id)}`, Screen, { body }),
      run: (id: string, limit = 200) =>
        c.request("POST", `/api/screens/${enc(id)}/run`, ScreenRun, { body: { limit }, timeoutMs: 180_000 }),
      runs: (signal?: AbortSignal) => c.get("/api/screens/runs", ScreenRuns, { limit: 50 }, signal),
      /** `run_id` defaults to the screen's latest run, `benchmark` to SPY. */
      queueBacktest: (id: string, body: { run_id?: string; benchmark?: string }) =>
        c.request("POST", `/api/screens/${enc(id)}/queue-backtest`, QueuedBacktest, { body, timeoutMs: 120_000 }),
      alerts: (signal?: AbortSignal) => c.get("/api/screens/alerts", ScreenAlerts, { limit: 20 }, signal),
      checkAlerts: () => c.post("/api/screens/alerts/check", ScreenAlerts.pick({ alerts: true, count: true }), {}),
      acknowledge: (id: string) => c.post(`/api/screens/alerts/${enc(id)}/acknowledge`, z.unknown(), {}),
    },

    ingest: {
      sources: (signal?: AbortSignal) => c.get("/api/ingest/sources", SourceList, null, signal),
      subscriptions: (signal?: AbortSignal) => c.get("/api/ingest/subscriptions", SubscriptionList, null, signal),
      subscribe: (body: { source_id: string; channel: string; scope: string[]; active: boolean; feed_mode: string; preserve_pit: boolean; granularity: string }) =>
        c.post("/api/ingest/subscriptions", Subscription, body),
      unsubscribe: (id: string) => c.del(`/api/ingest/subscriptions/${enc(id)}`, Deleted),
      /** Probe a configured source now. */
      probe: (id: string) => c.request("POST", `/api/ingest/sources/${enc(id)}/test`, SourceProbe, { body: {}, timeoutMs: 90_000 }),
      /** `range` and `end` travel in the query string, which is where the route reads them. */
      backfill: (id: string, range: string, end: string) =>
        c.request("POST", `/api/ingest/sources/${enc(id)}/backfill`, BackfillStarted, { query: { range, end } }),
      setEnabled: (id: string, enabled: boolean) => c.post(`/api/ingest/sources/${enc(id)}/enabled`, SourceSaved, { enabled }),
      /** Test the form as filled in, before anything is saved. */
      test: (form: SourceForm) => c.request("POST", "/api/ingest/sources/test", ConnectionTest, { body: form, timeoutMs: 90_000 }),
      create: (form: SourceForm) => c.post("/api/ingest/sources", SourceSaved, form),
      update: (id: string, body: SourceForm | Record<string, unknown>) => c.request("PUT", `/api/ingest/sources/${enc(id)}`, SourceSaved, { body }),
      remove: (id: string) => c.del(`/api/ingest/sources/${enc(id)}`, Deleted),
    },

    quality: {
      datasets: (signal?: AbortSignal) => c.get("/api/quality/datasets", Datasets, null, signal),
      summary: (signal?: AbortSignal) => c.get("/api/quality/summary", QualitySummary, null, signal),
      series: (signal?: AbortSignal) => c.get("/api/quality/series", QualitySeries, null, signal),
      incidents: (signal?: AbortSignal) => c.get("/api/quality/incidents", Incidents, { include_resolved: true }, signal),
      outliers: (signal?: AbortSignal) => c.get("/api/quality/outliers", Outliers, { limit: 200 }, signal),
      sla: (signal?: AbortSignal) => c.get("/api/quality/sla", Sla, null, signal),
      tickets: (signal?: AbortSignal) => c.get("/api/quality/tickets", Tickets, null, signal),
      run: () => c.request("POST", "/api/quality/run", QualityRun, { body: {}, timeoutMs: 300_000 }),
      resolve: (id: string) => c.post(`/api/quality/incidents/${enc(id)}/resolve`, Incident, {}),
      openTicket: (body: { title: string; dataset: string; check: string; body: string; severity: string }) =>
        c.post("/api/quality/tickets", TicketOpened, body),
      decide: (id: string, decision: "quarantine" | "accept" | "escalate" | "open") =>
        c.post(`/api/quality/outliers/${enc(id)}/decide`, OutlierDecided, { decision, by: "user" }),
    },

    macro: {
      panel: (signal?: AbortSignal) => c.get("/api/macro/series", MacroPanel, null, signal),
    },

    agents: {
      inbox: (signal?: AbortSignal) => c.get("/api/agents/inbox", Inbox, null, signal),
      runs: (signal?: AbortSignal) => c.get("/api/agents/runs", AgentRuns, { limit: 12 }, signal),
      definitions: (signal?: AbortSignal) => c.get("/api/agents/definitions", AgentDefinitions, { include_stats: false }, signal),
      findings: (query: { symbol?: string; limit?: number }, signal?: AbortSignal) =>
        c.get("/api/agents/findings", FindingList, query, signal),
    },

    lab: {
      summary: (signal?: AbortSignal) => c.get("/api/lab", LabSummary, null, signal),
      hypotheses: (query: { q?: string; limit?: number; include_evidence?: boolean }, signal?: AbortSignal) =>
        c.get("/api/lab/hypotheses", HypothesisList, query, signal),
      backtests: (query: { limit?: number }, signal?: AbortSignal) =>
        c.get("/api/lab/backtests", BacktestList, query, signal),
      experiments: (signal?: AbortSignal) => c.get("/api/lab/experiments", ExperimentList, null, signal),
      experiment: (id: string, signal?: AbortSignal) => c.get(`/api/lab/experiments/${enc(id)}`, ExperimentDetail, null, signal),
      gates: (signal?: AbortSignal) => c.get("/api/lab/validation/gates", GateSuite, null, signal),
      validation: (signal?: AbortSignal) => c.get("/api/lab/validation", ValidationReports, { limit: 200 }, signal),
      strategies: (signal?: AbortSignal) => c.get("/api/lab/validation/strategies", Strategies, null, signal),
      evaluation: (signal?: AbortSignal) => c.get("/api/lab/evaluation", Evaluation, null, signal),
      analyses: (signal?: AbortSignal) => c.get("/api/lab/analyses", Analyses, { limit: 100 }, signal),

      createHypothesis: (body: HypothesisBody & { status: string }) => c.post("/api/lab/hypotheses", Hypothesis, body),
      updateHypothesis: (id: string, body: HypothesisBody) => c.request("PUT", `/api/lab/hypotheses/${enc(id)}`, Hypothesis, { body }),
      setHypothesisStatus: (id: string, status: string) => c.post(`/api/lab/hypotheses/${enc(id)}/status`, Hypothesis, { status }),
      deleteHypothesis: (id: string) => c.del(`/api/lab/hypotheses/${enc(id)}`, z.unknown()),
      addEvidence: (id: string, body: { kind: string; ref: string; label: string | null }) =>
        c.post(`/api/lab/hypotheses/${enc(id)}/evidence`, z.unknown(), body),
      removeEvidence: (id: string, evidenceId: string | number) =>
        c.del(`/api/lab/hypotheses/${enc(id)}/evidence/${enc(String(evidenceId))}`, z.unknown()),

      createExperiment: (body: ExperimentBody) => c.post("/api/lab/experiments", Experiment, body),
      deleteExperiment: (id: string) => c.del(`/api/lab/experiments/${enc(id)}`, z.unknown()),
      /** Runs a real backtest and IC study; the log streams on `lab` meanwhile. */
      runExperiment: (id: string) => c.request("POST", `/api/lab/experiments/${enc(id)}/run`, ExperimentRun, { body: {}, timeoutMs: 600_000 }),
      runBacktest: (body: { config: Record<string, unknown>; experiment_id: string; hypothesis_id: string | null; source: string }) =>
        c.request("POST", "/api/lab/backtests/run", Backtest, { body, timeoutMs: 600_000 }),
    },

    research: {
      resolve: (q: string, limit = 8, signal?: AbortSignal) =>
        c.get("/api/research/resolve", Resolve, { q, limit }, signal),
      ranges: (signal?: AbortSignal) => c.get("/api/research/ranges", PriceRanges, null, signal),
      company: (symbol: string, signal?: AbortSignal) => c.get(company(symbol), Company, null, signal),
      price: (symbol: string, range: string, signal?: AbortSignal) =>
        c.get(company(symbol, "/price"), PriceHistory, { range }, signal),
      financials: (symbol: string, signal?: AbortSignal) =>
        c.get(company(symbol, "/financials"), Financials, null, signal),
      estimates: (symbol: string, signal?: AbortSignal) =>
        c.get(company(symbol, "/estimates"), Estimates, null, signal),
      valuation: (symbol: string, signal?: AbortSignal) =>
        c.get(company(symbol, "/valuation"), Valuation, null, signal),
      news: (symbol: string, query: { limit?: number } = {}, signal?: AbortSignal) =>
        c.get(company(symbol, "/news"), News, query, signal),
      thesis: (symbol: string, signal?: AbortSignal) => c.get(company(symbol, "/thesis"), Thesis, null, signal),
      altdata: (symbol: string, signal?: AbortSignal) =>
        c.get(company(symbol, "/altdata"), AltData, null, signal),
      filings: (symbol: string, signal?: AbortSignal) =>
        c.get(company(symbol, "/filings"), Filings, null, signal),
      memory: (symbol: string, signal?: AbortSignal) => c.get(company(symbol, "/memory"), Memory, null, signal),
      freshness: (symbol: string, signal?: AbortSignal) =>
        c.get(company(symbol, "/freshness"), FreshnessReport, null, signal),
    },
  };
}

export type Api = ReturnType<typeof makeApi>;

/** Every GET the app makes, with an example argument, for tests and check:api. */
/** What the hypothesis form sends; status moves through its own route. */
export interface HypothesisBody {
  title: string;
  statement: string;
  falsifier: string;
  rationale: string | null;
  owner: string | null;
  origin: string | null;
  universe_id: string | null;
  tags: string[];
}

export interface ExperimentBody {
  name: string;
  config: Record<string, unknown>;
  hypothesis_id: string | null;
  kind: string;
  horizon: number;
  owner: string | null;
  notes: string | null;
  criteria: { min_oos_sharpe: number; max_drawdown: number; min_trades: number };
}

export function readEndpoints(api: Api, symbol: string, universe: string) {
  const r = api.research;
  return {
    status: () => api.status(),
    health: () => api.health(),
    settings: () => api.settings.get(),
    search: () => api.search(symbol.slice(0, 2)),
    "universes.list": () => api.universes.list(),
    "universes.active": () => api.universes.active(),
    "universes.members": () => api.universes.members(universe),
    "universes.listWithMembers": () => api.universes.listWithMembers(),
    "universes.changelog": () => api.universes.changelog(universe),
    "screens.metrics": () => api.screens.metrics(),
    "screens.list": () => api.screens.list(),
    "screens.alerts": () => api.screens.alerts(),
    "screens.preview": () => api.screens.preview({ universe_id: universe, rules: [] }),
    "quality.datasets": () => api.quality.datasets(),
    "ingest.sources": () => api.ingest.sources(),
    "ingest.subscriptions": () => api.ingest.subscriptions(),
    "quality.summary": () => api.quality.summary(),
    "quality.series": () => api.quality.series(),
    "quality.incidents": () => api.quality.incidents(),
    "quality.outliers": () => api.quality.outliers(),
    "quality.sla": () => api.quality.sla(),
    "quality.tickets": () => api.quality.tickets(),
    "universes.breadth": () => api.universes.breadth(universe),
    "watchlists.list": () => api.watchlists.list(),
    "macro.panel": () => api.macro.panel(),
    "agents.inbox": () => api.agents.inbox(),
    "agents.findings": () => api.agents.findings({ symbol }),
    "lab.hypotheses": () => api.lab.hypotheses({ q: symbol }),
    "lab.backtests": () => api.lab.backtests({ limit: 200 }),
    "lab.summary": () => api.lab.summary(),
    "lab.hypothesesWithEvidence": () => api.lab.hypotheses({ include_evidence: true }),
    "lab.experiments": () => api.lab.experiments(),
    "lab.gates": () => api.lab.gates(),
    "lab.validation": () => api.lab.validation(),
    "lab.strategies": () => api.lab.strategies(),
    "lab.evaluation": () => api.lab.evaluation(),
    "lab.analyses": () => api.lab.analyses(),
    "screens.runs": () => api.screens.runs(),
    "agents.runs": () => api.agents.runs(),
    "agents.definitions": () => api.agents.definitions(),
    "research.resolve": () => r.resolve(symbol),
    "research.ranges": () => r.ranges(),
    "research.company": () => r.company(symbol),
    "research.price": () => r.price(symbol, "1Y"),
    "research.financials": () => r.financials(symbol),
    "research.estimates": () => r.estimates(symbol),
    "research.valuation": () => r.valuation(symbol),
    "research.news": () => r.news(symbol),
    "research.thesis": () => r.thesis(symbol),
    "research.altdata": () => r.altdata(symbol),
    "research.filings": () => r.filings(symbol),
    "research.memory": () => r.memory(symbol),
    "research.freshness": () => r.freshness(symbol),
  } satisfies Record<string, () => Promise<unknown>>;
}

