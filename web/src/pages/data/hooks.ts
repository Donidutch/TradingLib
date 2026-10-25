/* Market & Data's queries and writes. Every write refreshes what it changed,
 * including the shell's universe selector and status. */
import { useQuery } from "@tanstack/react-query";
import { useAction } from "../../api/actions";
import { useApi } from "../../api/context";
import { keys } from "../../api/hooks";
import type { PreviewRequest } from "../../api/schemas/screens";
import type { CollectionKind } from "./rules";

export const dataKeys = {
  collections: ["data", "collections"] as const,
  membership: (kind: CollectionKind, id: string, asOf: string | null) => ["data", "membership", kind, id, asOf] as const,
  changelog: (id: string) => ["data", "changelog", id] as const,
  coverage: (id: string, asOf: string | null) => ["data", "coverage", id, asOf] as const,
  metrics: ["data", "screens", "metrics"] as const,
  screens: ["data", "screens"] as const,
  alerts: ["data", "screens", "alerts"] as const,
};

/** Everything that shows a collection: this page, the shell selector, status. */
const collectionKeys = [dataKeys.collections, keys.universes, keys.watchlists, keys.status] as const;

export function useCollections() {
  const api = useApi();
  return useQuery({
    queryKey: dataKeys.collections,
    queryFn: async ({ signal }) => {
      const [universes, watchlists] = await Promise.all([api.universes.listWithMembers(signal), api.watchlists.list(signal)]);
      return { universes, watchlists };
    },
  });
}

export function useMembership(kind: CollectionKind, id: string, asOf: string | null) {
  const api = useApi();
  return useQuery({
    queryKey: dataKeys.membership(kind, id, asOf),
    queryFn: async ({ signal }) =>
      kind === "Universe"
        ? { kind, ...(await api.universes.members(id, asOf ? { as_of: asOf } : {}, signal)) }
        : { kind, watchlist: await api.watchlists.get(id, signal) },
  });
}

export function useChangelog(id: string, enabled: boolean) {
  const api = useApi();
  return useQuery({ queryKey: dataKeys.changelog(id), queryFn: ({ signal }) => api.universes.changelog(id, 6, signal), enabled });
}

/** The screener's metric panel over the universe at the as-of date, plus stored IV series. */
export function useCoverage(id: string, asOf: string | null, enabled: boolean) {
  const api = useApi();
  return useQuery({
    queryKey: dataKeys.coverage(id, asOf),
    enabled,
    queryFn: async ({ signal }) => {
      const [preview, datasets] = await Promise.all([
        api.screens.preview({ universe_id: id, rules: [], as_of: asOf }, signal),
        // Missing datasets only cost the Options IV row, as in the old page.
        api.quality.datasets(signal).catch(() => ({ datasets: [], count: 0 })),
      ]);
      return { preview, datasets: datasets.datasets };
    },
  });
}

export function useCollectionActions() {
  const api = useApi();
  const touched = () => [...collectionKeys];
  return {
    setActive: useAction({
      run: ({ id }: { id: string; name: string }) => api.universes.setActive(id),
      invalidate: touched,
      done: (_r, { name }) => `Active universe: ${name}`,
      failed: ({ name }) => `Could not activate ${name}`,
    }),
    refresh: useAction({
      run: ({ id }: { id: string; name: string }) => api.universes.refresh(id),
      invalidate: touched,
      done: (u, { name }) => `${name} · ${u.member_count} members fetched`,
      failed: ({ name }) => `Could not refresh ${name}`,
    }),
    snapshot: useAction({
      run: ({ id }: { id: string }) => api.universes.snapshot(id),
      invalidate: (_r, { id }) => [dataKeys.changelog(id), ["data", "membership"]],
      done: (snap) => `Snapshot ${snap.as_of} · ${snap.symbol_count} symbols`,
      failed: () => "Could not freeze a snapshot",
    }),
    remove: useAction({
      run: ({ kind, id }: { kind: CollectionKind; id: string; name: string }) =>
        kind === "Universe" ? api.universes.remove(id) : api.watchlists.remove(id),
      invalidate: touched,
      done: (_r, { name }) => `${name} deleted`,
      failed: ({ name }) => `Could not delete ${name}`,
    }),
    save: useAction({
      run: async (form: CollectionForm) => {
        if (form.editing && form.kind === "Universe") {
          return api.universes.update(form.editing, { name: form.name, description: form.description, symbols: form.symbols });
        }
        if (form.editing) {
          return api.watchlists.update(form.editing, { name: form.name, symbols: form.symbols, ...(form.makeDefault ? { is_default: true } : {}) });
        }
        const body = {
          name: form.name,
          symbols: form.symbols,
          description: form.description,
          construction_method: form.construction,
          refresh: form.refresh,
        };
        return form.kind === "Universe"
          ? api.universes.create({ ...body, make_active: form.makeDefault })
          : api.watchlists.create({ ...body, is_default: form.makeDefault });
      },
      invalidate: touched,
      done: (_r, form) => `${form.kind} “${form.name}” ${form.editing ? "saved" : "created"}`,
      failed: (form) => `Could not save ${form.name}`,
    }),
  };
}

export interface CollectionForm {
  kind: CollectionKind;
  /** The id being edited; null creates. */
  editing: string | null;
  name: string;
  description: string | null;
  construction: string;
  refresh: string;
  symbols: string;
  makeDefault: boolean;
}

// ------------------------------------------------------------------ screener

export function useScreenerData() {
  const api = useApi();
  const metrics = useQuery({ queryKey: dataKeys.metrics, queryFn: ({ signal }) => api.screens.metrics(signal), staleTime: Infinity });
  const screens = useQuery({ queryKey: dataKeys.screens, queryFn: ({ signal }) => api.screens.list(signal) });
  const alerts = useQuery({ queryKey: dataKeys.alerts, queryFn: ({ signal }) => api.client.optional(api.screens.alerts(signal)) });
  return { metrics, screens, alerts };
}

/** Counts after each rule prefix, from real previews; a new edit cancels the old chain. */
export function usePreviewFunnel(request: PreviewRequest | null) {
  const api = useApi();
  return useQuery({
    queryKey: ["data", "funnel", request],
    enabled: request !== null && request.rules.length > 0,
    retry: false,
    queryFn: async ({ signal }) => {
      if (!request) return [];
      const counts: { base: number; count: number; query: string | null }[] = [];
      for (let i = 1; i <= request.rules.length; i++) {
        const preview = await api.screens.preview({ ...request, rules: request.rules.slice(0, i) }, signal);
        counts.push({ base: preview.base_count, count: preview.count, query: preview.query });
      }
      return counts;
    },
  });
}

export function useAcknowledgeAlert() {
  const api = useApi();
  return useAction({
    run: (id: string) => api.screens.acknowledge(id),
    invalidate: () => [dataKeys.alerts],
    failed: () => "Could not acknowledge the alert",
  });
}
