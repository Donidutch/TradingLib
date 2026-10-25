/* Ingestion sources and subscriptions (tradinglib/api/routes/ingest.py). The
 * API never returns a stored credential: `auth` says whether one is set. */
import { z } from "zod";
import { nnum, nstr } from "./common";

export const SourceState = z.looseObject({
  source_id: z.string(),
  /** connected, degraded, disconnected, not_configured, unknown */
  status: z.string(),
  status_label: z.string(),
  last_ingest_at: nstr,
  last_error: nstr,
  latency_ms: nnum,
  rows_ingested: nnum,
  checked_at: nstr,
  backfilled_at: nstr.optional(),
});

export const Source = z.looseObject({
  id: z.string(),
  name: z.string(),
  builtin: z.boolean(),
  enabled: z.boolean(),
  data_domain: z.string(),
  data_domain_label: z.string(),
  protocol: z.string(),
  protocol_label: z.string(),
  can_backfill: z.boolean(),
  endpoint: nstr,
  provider: nstr,
  format: z.string(),
  format_label: z.string(),
  auth: z.looseObject({ type: z.string(), label: z.string(), configured: z.boolean(), key_set: z.boolean(), key_name: nstr }),
  schedule: z.string(),
  schedule_detail: z.looseObject({ kind: z.string(), label: z.string(), periodic: z.boolean(), interval_seconds: nnum, cron: nstr }),
  scope: z.array(z.string()),
  scope_label: z.string(),
  source_tz: z.string(),
  retry_policy: z.looseObject({ mode: z.string(), label: z.string() }),
  backfill: z.looseObject({ enabled: z.boolean(), range: z.string(), batch: z.string(), start_date: nstr }),
  advanced: z.record(z.string(), z.unknown()).nullable(),
  granularity_options: z.array(z.string()),
  granularity_default: nstr,
  granularity_reaches_fetch: z.boolean(),
  state: SourceState,
});

export const SourceList = z.looseObject({
  sources: z.array(Source),
  count: z.number(),
  summary: z.looseObject({ connected: z.number(), sources: z.number() }).optional(),
});

export const Subscription = z.looseObject({
  id: z.string(),
  source_id: z.string(),
  channel: z.string(),
  scope: z.array(z.string()),
  active: z.boolean(),
  feed_mode: z.string(),
  preserve_pit: z.boolean(),
  granularity: z.string(),
  feed_mode_supported: z.boolean().optional(),
  feed_mode_detail: nstr.optional(),
  granularity_applied: z.boolean().optional(),
  granularity_detail: nstr.optional(),
});

export const SubscriptionList = z.looseObject({ subscriptions: z.array(Subscription), count: z.number() });

const Job = z.looseObject({ source_id: z.string(), schedule: z.string() }).nullable().optional();

/** POST /api/ingest/sources and PUT /api/ingest/sources/{id}. */
export const SourceSaved = z.looseObject({
  source: Source,
  job: Job,
  backfill: z.looseObject({ started: z.boolean().optional(), range: nstr.optional(), reason: nstr.optional() }).nullable().optional(),
});

/** POST /api/ingest/sources/{id}/test: the probe's new state. */
export const SourceProbe = z.looseObject({ source_id: z.string(), state: SourceState });

/** POST /api/ingest/sources/test: a connection test of the form as filled in. */
export const ConnectionTest = z.looseObject({
  ok: z.boolean(),
  reachable: z.boolean().nullable(),
  authenticated: z.boolean().nullable(),
  latency_ms: nnum,
  status_code: nnum,
  message: nstr,
  error: nstr,
  tested_at: nstr,
  inferred_schema: z.record(z.string(), z.unknown()).nullable(),
  sample: z.array(z.unknown()).nullable(),
});

export const BackfillStarted = z.looseObject({ started: z.boolean(), source_id: z.string(), range: z.string() });

/** The Data Ingestion form, in the field names SourceConfig.from_form reads. */
export interface SourceForm {
  id?: string;
  sourceName: string;
  sourceDomain: string;
  sourceProtocol: string;
  sourceEndpoint: string;
  sourceAuth: string;
  sourceFormat: string;
  sourceFrequency: string;
  sourceScope: string;
  sourceTimezone: string;
  sourceRetries: string;
  backfillEnabled: boolean;
  backfillRange: string;
  backfillBatch: string;
  advancedConfig: string | null;
  cron?: string;
  scope?: string;
  backfillStart?: string;
  auth_username?: string;
  auth_password?: string;
  auth_client_id?: string;
  auth_client_secret?: string;
  auth_key_name?: string;
  auth_key_value?: string;
}

export type Source = z.infer<typeof Source>;
export type Subscription = z.infer<typeof Subscription>;
export type ConnectionTest = z.infer<typeof ConnectionTest>;
