/* One function per endpoint the ported pages use, each bound to its schema.
 * Pages call these instead of writing paths, so a route change lands here. */
import type { ApiClient } from "./client";
import { Breadth, ActiveScope, Members, UniverseList } from "./schemas/universes";
import { FindingList, Inbox } from "./schemas/agents";
import { BacktestList, HypothesisList } from "./schemas/lab";
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
import { Search, Status } from "./schemas/status";
import { Watchlist, WatchlistList } from "./schemas/watchlists";

const enc = encodeURIComponent;
const company = (symbol: string, suffix = "") =>
  `/api/research/company/${enc(symbol.trim().toUpperCase())}${suffix}`;

export function makeApi(c: ApiClient) {
  return {
    client: c,
    status: (signal?: AbortSignal) => c.get("/api/status", Status, null, signal),
    search: (q: string, signal?: AbortSignal) => c.get("/api/search", Search, { q }, signal),

    universes: {
      list: (signal?: AbortSignal) => c.get("/api/universes", UniverseList, null, signal),
      active: (signal?: AbortSignal) => c.get("/api/universes/active", ActiveScope, null, signal),
      /** A universe id or a `watchlist:<id>` reference. */
      setActive: (ref: string) => c.post("/api/universes/active", ActiveScope, { universe_id: ref }),
      members: (id: string, signal?: AbortSignal) =>
        c.get(`/api/universes/${enc(id)}/members`, Members, null, signal),
      breadth: (id: string, signal?: AbortSignal) =>
        c.get(`/api/universes/${enc(id)}/breadth`, Breadth, null, signal),
    },

    watchlists: {
      list: (signal?: AbortSignal) => c.get("/api/watchlists", WatchlistList, null, signal),
      create: (body: { name: string; symbols?: string[]; is_default?: boolean }) =>
        c.post("/api/watchlists", Watchlist, body),
      addSymbol: (id: string, symbol: string) =>
        c.post(`/api/watchlists/${enc(id)}/symbols`, Watchlist, { symbol }),
      removeSymbol: (id: string, symbol: string) =>
        c.del(`/api/watchlists/${enc(id)}/symbols/${enc(symbol)}`, Watchlist),
    },

    macro: {
      panel: (signal?: AbortSignal) => c.get("/api/macro/series", MacroPanel, null, signal),
    },

    agents: {
      inbox: (signal?: AbortSignal) => c.get("/api/agents/inbox", Inbox, null, signal),
      findings: (query: { symbol?: string; limit?: number }, signal?: AbortSignal) =>
        c.get("/api/agents/findings", FindingList, query, signal),
    },

    lab: {
      hypotheses: (query: { q?: string; limit?: number }, signal?: AbortSignal) =>
        c.get("/api/lab/hypotheses", HypothesisList, query, signal),
      backtests: (query: { limit?: number }, signal?: AbortSignal) =>
        c.get("/api/lab/backtests", BacktestList, query, signal),
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
      news: (symbol: string, signal?: AbortSignal) => c.get(company(symbol, "/news"), News, null, signal),
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
export function readEndpoints(api: Api, symbol: string, universe: string) {
  const r = api.research;
  return {
    status: () => api.status(),
    search: () => api.search(symbol.slice(0, 2)),
    "universes.list": () => api.universes.list(),
    "universes.active": () => api.universes.active(),
    "universes.members": () => api.universes.members(universe),
    "universes.breadth": () => api.universes.breadth(universe),
    "watchlists.list": () => api.watchlists.list(),
    "macro.panel": () => api.macro.panel(),
    "agents.inbox": () => api.agents.inbox(),
    "agents.findings": () => api.agents.findings({ symbol }),
    "lab.hypotheses": () => api.lab.hypotheses({ q: symbol }),
    "lab.backtests": () => api.lab.backtests({}),
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

