/* TanStack Query hooks, one per resource. Keys live in `keys` so mutations
 * and the live stream can invalidate exactly what they change. */
import { useQuery, type QueryClient } from "@tanstack/react-query";
import { useAction } from "./actions";
import { useApi } from "./context";
import type { Api } from "./endpoints";
import type { SettingsPatch } from "./schemas/settings";
import type { Watchlist, WatchlistList } from "./schemas/watchlists";
import { useToast } from "../shell/toast-context";

export const keys = {
  status: ["status"] as const,
  health: ["health"] as const,
  settings: ["settings"] as const,
  search: (q: string) => ["search", q] as const,
  universes: ["universes"] as const,
  activeScope: ["universes", "active"] as const,
  members: (id: string) => ["universes", id, "members"] as const,
  breadth: (id: string) => ["universes", id, "breadth"] as const,
  watchlists: ["watchlists"] as const,
  macro: ["macro"] as const,
  inbox: ["agents", "inbox"] as const,
  findings: (symbol: string) => ["agents", "findings", symbol] as const,
  hypotheses: (q: string) => ["lab", "hypotheses", q] as const,
  backtests: ["lab", "backtests"] as const,
  resolve: (q: string) => ["research", "resolve", q] as const,
  ranges: ["research", "ranges"] as const,
  research: (symbol: string, part: string, ...rest: string[]) =>
    ["research", symbol.toUpperCase(), part, ...rest] as const,
};

/** The datastrip payload; `refetchInterval` is the REST fallback cadence. */
export function useStatus(refetchInterval?: number) {
  const api = useApi();
  return useQuery({
    queryKey: keys.status,
    queryFn: ({ signal }) => api.status(signal),
    refetchInterval,
  });
}

export function useHealth() {
  const api = useApi();
  return useQuery({ queryKey: keys.health, queryFn: ({ signal }) => api.health(signal), staleTime: Infinity });
}

/** Stored preferences; also what the app applies at load (theme, density, clock). */
export function useSettings() {
  const api = useApi();
  return useQuery({ queryKey: keys.settings, queryFn: ({ signal }) => api.settings.get(signal), staleTime: Infinity });
}

/** Save part of the settings; the page and the app follow the server's answer. */
export function useSaveSettings() {
  const api = useApi();
  const toast = useToast();
  return useAction({
    run: ({ patch }: { patch: SettingsPatch; description: string }) => api.settings.save(patch),
    apply: (client, saved, { patch }) => {
      client.setQueryData(keys.settings, saved);
      // The default universe is the active one: the selector and status follow.
      if (patch.default_universe) {
        void client.invalidateQueries({ queryKey: keys.universes });
        void client.invalidateQueries({ queryKey: keys.status });
      }
      if (saved.restart_detail) toast.warn(saved.restart_detail, 6000);
    },
    done: (_saved, { description }) => description,
    failed: () => "Could not save",
  });
}

export function useSearch(q: string) {
  const api = useApi();
  const query = q.trim();
  return useQuery({
    queryKey: keys.search(query),
    queryFn: ({ signal }) => api.search(query, signal),
    enabled: query.length > 0,
  });
}

export function useUniverses() {
  const api = useApi();
  return useQuery({ queryKey: keys.universes, queryFn: ({ signal }) => api.universes.list(signal) });
}

export function useActiveScope() {
  const api = useApi();
  return useQuery({ queryKey: keys.activeScope, queryFn: ({ signal }) => api.universes.active(signal) });
}

/** Select a universe or `watchlist:<id>`; everything scoped to it refetches. */
export function useSetActiveScope() {
  const api = useApi();
  return useAction({
    run: (ref: string) => api.universes.setActive(ref),
    apply: (client, scope) => {
      client.setQueryData(keys.activeScope, scope);
      // The list's `active` flags changed too; the scope itself is fresh.
      void client.invalidateQueries({ queryKey: keys.universes, exact: true });
    },
    done: (scope) => `Active ${scope.scope.kind === "watchlist" ? "watchlist" : "universe"}: ${scope.scope.name}`,
    failed: () => "Could not switch scope",
  });
}

export function useMembers(id: string | undefined) {
  const api = useApi();
  return useQuery({
    queryKey: keys.members(id ?? ""),
    queryFn: ({ signal }) => api.universes.members(id ?? "", signal),
    enabled: Boolean(id),
  });
}

export function useBreadth(id: string | undefined, window?: number) {
  const api = useApi();
  return useQuery({
    queryKey: [...keys.breadth(id ?? ""), window ?? null],
    queryFn: ({ signal }) => api.universes.breadth(id ?? "", { window }, signal),
    enabled: Boolean(id),
  });
}

export function useWatchlists() {
  const api = useApi();
  return useQuery({ queryKey: keys.watchlists, queryFn: ({ signal }) => api.watchlists.list(signal) });
}

/** Write a changed watchlist back into the list cache, server copy wins. */
function storeWatchlist(client: QueryClient, changed: Watchlist) {
  client.setQueryData<WatchlistList>(keys.watchlists, (list) =>
    list ? { ...list, watchlists: list.watchlists.map((w) => (w.id === changed.id ? changed : w)) } : list,
  );
}

export function useCreateWatchlist() {
  const api = useApi();
  return useAction({
    run: api.watchlists.create,
    apply: (client, created) => storeWatchlist(client, created),
    invalidate: () => [keys.watchlists],
    done: (created, body) => `${body.symbols?.join(", ") ?? created.name} added to ${created.name}`,
    failed: (body) => `Could not add ${body.symbols?.join(", ") ?? body.name}`,
  });
}

export function useAddWatchSymbol() {
  const api = useApi();
  return useAction({
    run: ({ id, symbol }: { id: string; symbol: string }) => api.watchlists.addSymbol(id, symbol),
    apply: (client, changed) => storeWatchlist(client, changed),
    invalidate: () => [keys.watchlists],
    done: (changed, { symbol }) => `${symbol} added to ${changed.name}`,
    failed: ({ symbol }) => `Could not add ${symbol}`,
  });
}

export function useRemoveWatchSymbol() {
  const api = useApi();
  return useAction({
    run: ({ id, symbol }: { id: string; symbol: string }) => api.watchlists.removeSymbol(id, symbol),
    apply: (client, changed) => storeWatchlist(client, changed),
    invalidate: () => [keys.watchlists],
    done: (changed, { symbol }) => `${symbol} removed from ${changed.name}`,
    failed: ({ symbol }) => `Could not remove ${symbol}`,
  });
}

export function useMacro() {
  const api = useApi();
  return useQuery({ queryKey: keys.macro, queryFn: ({ signal }) => api.macro.panel(signal) });
}

/** The agent inbox; null when this build has no agents routes. */
export function useInbox() {
  const api = useApi();
  return useQuery({
    queryKey: keys.inbox,
    queryFn: ({ signal }) => api.client.optional(api.agents.inbox(signal)),
  });
}

export function useFindings(symbol: string) {
  const api = useApi();
  return useQuery({
    queryKey: keys.findings(symbol),
    queryFn: ({ signal }) => api.client.optional(api.agents.findings({ symbol }, signal)),
    enabled: Boolean(symbol),
  });
}

export function useHypotheses(q: string) {
  const api = useApi();
  return useQuery({
    queryKey: keys.hypotheses(q),
    queryFn: ({ signal }) => api.client.optional(api.lab.hypotheses({ q }, signal)),
  });
}

export function useBacktests() {
  const api = useApi();
  return useQuery({
    queryKey: keys.backtests,
    queryFn: ({ signal }) => api.client.optional(api.lab.backtests({}, signal)),
  });
}

export function useResolve(q: string) {
  const api = useApi();
  const query = q.trim();
  return useQuery({
    queryKey: keys.resolve(query),
    queryFn: ({ signal }) => api.research.resolve(query, 8, signal),
    enabled: query.length > 0,
  });
}

export function usePriceRanges() {
  const api = useApi();
  return useQuery({
    queryKey: keys.ranges,
    queryFn: ({ signal }) => api.research.ranges(signal),
    staleTime: Infinity,
  });
}

/** One Stock Research payload for `symbol`, cached per symbol and part. */
function useResearchQuery<T>(
  part: string,
  symbol: string,
  fetch: (api: Api, symbol: string, signal: AbortSignal) => Promise<T>,
) {
  const api = useApi();
  return useQuery({
    queryKey: keys.research(symbol, part),
    queryFn: ({ signal }) => fetch(api, symbol, signal),
    enabled: Boolean(symbol),
  });
}

/** 404s (ApiError.missing) for an unknown ticker; the page says so. */
export const useCompany = (symbol: string) =>
  useResearchQuery("company", symbol, (a, s, signal) => a.research.company(s, signal));
export const useFinancials = (symbol: string) =>
  useResearchQuery("financials", symbol, (a, s, signal) => a.research.financials(s, signal));
export const useEstimates = (symbol: string) =>
  useResearchQuery("estimates", symbol, (a, s, signal) => a.research.estimates(s, signal));
export const useValuation = (symbol: string) =>
  useResearchQuery("valuation", symbol, (a, s, signal) => a.research.valuation(s, signal));
/** The 25 newest items, shared by the Overview tab brief and the News tab. */
export const useNews = (symbol: string) =>
  useResearchQuery("news", symbol, (a, s, signal) => a.research.news(s, { limit: 25 }, signal));
/** null for an unknown ticker, like the old page's optional() call. */
export const useThesis = (symbol: string) =>
  useResearchQuery("thesis", symbol, (a, s, signal) => a.client.optional(a.research.thesis(s, signal)));
export const useAltData = (symbol: string) =>
  useResearchQuery("altdata", symbol, (a, s, signal) => a.research.altdata(s, signal));
export const useFilings = (symbol: string) =>
  useResearchQuery("filings", symbol, (a, s, signal) => a.research.filings(s, signal));
export const useMemory = (symbol: string) =>
  useResearchQuery("memory", symbol, (a, s, signal) => a.client.optional(a.research.memory(s, signal)));
export const useFreshness = (symbol: string) =>
  useResearchQuery("freshness", symbol, (a, s, signal) => a.research.freshness(s, signal));

export function usePrice(symbol: string, range: string) {
  const api = useApi();
  return useQuery({
    queryKey: keys.research(symbol, "price", range),
    queryFn: ({ signal }) => api.research.price(symbol, range, signal),
    enabled: Boolean(symbol),
  });
}
