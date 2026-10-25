import { z } from "zod";
import { nnum, nstr } from "./common";

/** GET /api/status and the `status` channel: the datastrip payload. */
export const Status = z.looseObject({
  data_live: z.boolean(),
  gateway_ms: nnum,
  sources_connected: z.number(),
  sources_total: z.number(),
  last_ingest: nstr,
  delayed_series: z.number(),
  fundamentals_coverage: z
    .looseObject({
      universe_id: z.string(),
      universe_name: z.string(),
      covered: z.number(),
      total: z.number(),
      pct: nnum,
      missing: z.array(z.string()),
      missing_total: z.number(),
      measured_at: nstr,
    })
    .nullable(),
  trials_logged: z.number(),
  spend: z.looseObject({ used: z.number(), budget: nnum }),
  engine_health: z.string(),
  engine_detail: nstr,
  degraded_subsystems: z.array(z.string()),
  subsystems: z.record(z.string(), z.string()),
  universe: z
    .looseObject({
      id: z.string(),
      name: z.string(),
      kind: z.string(),
      symbols: z.number(),
      members_fetched_at: nstr,
    })
    .nullable(),
  quality: z.looseObject({
    overall_quality_pct: nnum,
    open_incidents: z.number(),
    failed_checks: z.number(),
    last_run: nstr,
  }),
  market_stream: z.looseObject({
    status: z.string(),
    configured: z.boolean(),
    running: z.boolean(),
    detail: nstr,
  }),
  ws_connections: z.number(),
  version: z.string(),
  server_time_utc: z.string(),
  uptime_seconds: z.number(),
});

/** GET /api/search: palette results across pages, collections and symbols. */
const SearchResult = z.looseObject({
  type: z.string(),
  title: z.string(),
  subtitle: nstr.optional(),
  symbol: nstr.optional(),
  hash: z.string(),
});

export const Search = z.looseObject({
  query: z.string(),
  count: z.number(),
  results: z.array(SearchResult),
  groups: z.record(z.string(), z.array(SearchResult)),
  warnings: z.array(z.string()),
});

export type Status = z.infer<typeof Status>;
export type Search = z.infer<typeof Search>;
export type SearchResult = z.infer<typeof SearchResult>;
