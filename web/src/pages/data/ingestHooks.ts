/* Source Catalog, Data Ingestion and Data Quality: queries and writes. A
 * source change refreshes the catalog, the ingestion table and the shell's
 * status; a quality decision refreshes the whole quality tab. */
import { useQuery } from "@tanstack/react-query";
import { useAction } from "../../api/actions";
import { ApiError } from "../../api/client";
import { useApi } from "../../api/context";
import { keys } from "../../api/hooks";
import type { Source, SourceForm } from "../../api/schemas/ingest";
import { useToast } from "../../shell/toast-context";
import { int } from "../../ui/format";
import { sourceToForm, toSeconds } from "./ingestRules";

export const ingestKeys = {
  sources: ["data", "ingest", "sources"] as const,
  subscriptions: ["data", "ingest", "subscriptions"] as const,
  quality: ["data", "quality"] as const,
};

const sourceKeys = [ingestKeys.sources, ingestKeys.subscriptions, keys.status] as const;

export function useSources() {
  const api = useApi();
  return useQuery({ queryKey: ingestKeys.sources, queryFn: ({ signal }) => api.ingest.sources(signal) });
}

export function useSubscriptions() {
  const api = useApi();
  return useQuery({ queryKey: ingestKeys.subscriptions, queryFn: ({ signal }) => api.ingest.subscriptions(signal) });
}

/** The quality tab's seven reads, loaded together as the old page did. */
export function useQualityData() {
  const api = useApi();
  return useQuery({
    queryKey: ingestKeys.quality,
    queryFn: async ({ signal }) => {
      const [summary, series, incidents, datasets, outliers, sla, tickets] = await Promise.all([
        api.quality.summary(signal),
        api.quality.series(signal),
        api.quality.incidents(signal),
        api.quality.datasets(signal),
        api.quality.outliers(signal),
        api.quality.sla(signal),
        api.quality.tickets(signal),
      ]);
      return { summary, series, incidents, datasets, outliers, sla, tickets };
    },
  });
}

function probeText(source: Source, state: Source["state"]) {
  return {
    text:
      `${source.name} · ${state.status_label || state.status}` +
      (state.latency_ms ? ` · ${Math.round(state.latency_ms)} ms` : "") +
      (state.last_error ? ` · ${state.last_error}` : ""),
    tone: state.status === "connected" ? ("ok" as const) : ("warn" as const),
  };
}

export interface SubscribeArgs {
  source: Source;
  channel: string;
  scope: string[];
  feedMode: string;
  preservePit: boolean;
  granularity: string;
}

export function useIngestActions() {
  const api = useApi();
  const toast = useToast();
  const touched = () => [...sourceKeys];
  return {
    subscribe: useAction({
      run: (a: SubscribeArgs) =>
        api.ingest.subscribe({
          source_id: a.source.id,
          channel: a.channel,
          scope: a.scope,
          active: true,
          feed_mode: a.feedMode,
          preserve_pit: a.preservePit,
          granularity: a.granularity,
        }),
      invalidate: touched,
      done: (sub, a) => {
        // What the source could not honour follows as its own warning.
        if (sub.feed_mode_supported === false && sub.feed_mode_detail) toast.warn(sub.feed_mode_detail);
        if (sub.granularity && sub.granularity_applied === false && sub.granularity_detail) toast.warn(sub.granularity_detail);
        return (
          `Subscribed ${a.source.name} · ${sub.channel} · ${sub.feed_mode}` +
          (sub.granularity ? ` · ${sub.granularity}` : "") +
          (sub.preserve_pit ? " · point-in-time" : "")
        );
      },
      failed: (a) => `Could not subscribe ${a.source.name}`,
    }),
    unsubscribe: useAction({
      run: (id: string) => api.ingest.unsubscribe(id),
      invalidate: touched,
      done: () => "Subscription removed",
      failed: () => "Could not remove the subscription",
    }),
    probe: useAction({
      run: (source: Source) => api.ingest.probe(source.id),
      invalidate: touched,
      done: (result, source) => probeText(source, result.state),
      failed: (source) => `Could not test ${source.name}`,
    }),
    backfill: useAction({
      run: async ({ source, range, end }: { source: Source; range: string; end: string }) => {
        try {
          return await api.ingest.backfill(source.id, range, end);
        } catch (err) {
          // A second backfill of the same source is a 409, not a failure.
          const detail = err instanceof ApiError ? (err.payload as { detail?: { error?: string } } | null)?.detail : null;
          if (detail && typeof detail === "object" && detail.error === "backfill_running") return null;
          throw err;
        }
      },
      done: (result, { source, range, end }) =>
        result
          ? `Backfilling ${source.name} from ${range} to ${end || "now"}${result.started ? "" : " (ran inline)"}`
          : { text: `${source.name} is already backfilling; wait for it to finish.`, tone: "warn" },
      failed: ({ source }) => `Could not backfill ${source.name}`,
    }),
    schedule: useAction({
      run: ({ source, interval }: { source: Source; interval: string }) => api.ingest.update(source.id, sourceToForm(source, { schedule: interval })),
      invalidate: touched,
      done: (result, { source, interval }) => `${source.name} now polls every ${interval}${result.job ? " (scheduler job updated)" : ""}`,
      failed: ({ source }) => `Could not change ${source.name}'s schedule`,
    }),
    setEnabled: useAction({
      run: (source: Source) => api.ingest.setEnabled(source.id, !source.enabled),
      invalidate: touched,
      done: (_r, source) => `${source.name} ${source.enabled ? "paused" : "resumed"}`,
      failed: (source) => `Could not ${source.enabled ? "pause" : "resume"} ${source.name}`,
    }),
    remove: useAction({
      run: (source: Source) => api.ingest.remove(source.id),
      invalidate: touched,
      done: (_r, source) => `${source.name} deleted`,
      failed: (source) => `Could not delete ${source.name}`,
    }),
    test: useAction({
      run: (form: SourceForm) => api.ingest.test(form),
      done: (result) => ({
        text: result.ok ? `Connection successful · ${result.message ?? ""}` : `Connection failed · ${result.error || result.message || ""}`,
        tone: result.ok ? "ok" : "warn",
      }),
      failed: (form) => `Could not test ${form.sourceName}`,
    }),
    save: useAction({
      run: (form: SourceForm) => (form.id ? api.ingest.update(form.id, form) : api.ingest.create(form)),
      invalidate: touched,
      done: (result, form) => {
        // "Enable historical backfill on first connection" is acted on by the
        // save endpoint; what it actually did is reported either way.
        const backfill = result.backfill;
        if (backfill?.started) toast.ok(`Historical backfill started over ${backfill.range ?? ""} · progress on the ingest channel`);
        else if (backfill?.reason) toast.warn(`Backfill not started: ${backfill.reason}`);
        return `Data source “${result.source.name || form.sourceName}” ${form.id ? "updated" : "saved"}${result.job ? ` · scheduled ${result.job.schedule}` : ""}`;
      },
      failed: (form) => `Could not save ${form.sourceName}`,
    }),
  };
}

/** Refuses an interval the scheduler cannot parse, and a re-post that would clear a stored key. */
export function scheduleRefusal(source: Source, interval: string): string | null {
  if (toSeconds(interval) === null) return `Unrecognised interval “${interval}” — use a value like 45s, 2m, 4h or 1d.`;
  if (source.auth.key_set) {
    return `${source.name} stores a credential the API never returns. Change its schedule on the Data Ingestion tab and re-enter the key, so saving cannot clear it.`;
  }
  return null;
}

export function useQualityActions() {
  const api = useApi();
  const touched = () => [ingestKeys.quality];
  return {
    run: useAction({
      run: () => api.quality.run(),
      invalidate: touched,
      done: (result) => `Quality suite ran over ${result.count} dataset(s)`,
      failed: () => "Could not run the quality suite",
    }),
    resolve: useAction({
      run: (id: string) => api.quality.resolve(id),
      invalidate: touched,
      done: () => "Incident resolved",
      failed: () => "Could not resolve the incident",
    }),
    ticket: useAction({
      run: (row: { name: string; detail: string; schedule: string; state: string }) =>
        api.quality.openTicket({
          title: `SLA breach · ${row.name}`,
          dataset: "",
          check: "sla",
          body: `${row.detail} (schedule: ${row.schedule})`,
          severity: row.state === "breached" ? "error" : "warn",
        }),
      invalidate: touched,
      done: (result) => `Ticket ${result.ticket.id.slice(0, 8)} opened · ${result.tracker}`,
      failed: () => "Could not open a ticket",
    }),
    decide: useAction({
      run: ({ id, decision }: { id: string; label: string; decision: "quarantine" | "accept" | "escalate" | "open" }) => api.quality.decide(id, decision),
      invalidate: touched,
      done: (result, { label }) =>
        `${label} · ${result.outlier.state === "escalated" ? "escalated for human review — still in the queue, value untouched" : result.outlier.state}`,
      failed: ({ label }) => `Could not decide ${label}`,
    }),
  };
}

export const rowsText = (rows: number | null | undefined) => (rows ? `${int(rows)} rows` : "—");
