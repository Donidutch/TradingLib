/* Source Catalog, Data Ingestion and Data Quality rules, ported from data.js
 * and kept apart from the components so each is tested on its own. */
import type { Source, SourceForm, Subscription } from "../../api/schemas/ingest";
import type { Outliers, QualitySeries, Sla } from "../../api/schemas/quality";
import { ago, int } from "../../ui/format";

/** The protocol table: label, placeholder, hint and default frequency per protocol. */
export const PROTOCOLS = {
  WebSocket: {
    token: "websocket",
    label: "WebSocket endpoint",
    placeholder: "wss://provider.example/v2/stream",
    hint: "Persistent streaming connection for live events. Configure endpoint, subscriptions, heartbeat, and reconnect behavior.",
    frequency: "Streaming",
    advanced: '{"subscriptions": ["AAPL", "MSFT"], "subscribe_message": {"action": "subscribe"}}',
  },
  REST: {
    token: "rest",
    label: "REST base URL",
    placeholder: "https://api.provider.example/v1",
    hint: "Request-response connector. Configure authentication, rate limits, pagination, and resource paths.",
    frequency: "Daily",
    advanced: "Optional headers, subscriptions, SQL query, file glob, pagination, schema mapping…",
  },
  Polling: {
    token: "polling",
    label: "Polling endpoint",
    placeholder: "https://api.provider.example/v1/updates",
    hint: "Scheduled API polling. Configure frequency, cursor, pagination, and deduplication.",
    frequency: "Every 5 minutes",
    advanced: "Optional headers, subscriptions, SQL query, file glob, pagination, schema mapping…",
  },
  Database: {
    token: "database",
    label: "Connection URI / database path",
    placeholder: "postgresql://host/db or /data/market.duckdb",
    hint: "Read from PostgreSQL, DuckDB, or another database. Put table, SQL query, and watermark column in advanced configuration.",
    frequency: "Hourly",
    advanced: '{"sql": "SELECT ts, symbol, close FROM bars WHERE ts > :watermark"}',
  },
  File: {
    token: "file",
    label: "File path / discovery pattern",
    placeholder: "/data/import/**/*.parquet",
    hint: "Ingest database files or flat files. Configure discovery pattern, schema mapping, and archive behavior.",
    frequency: "On file change",
    advanced: '{"glob": "/data/import/**/*.parquet", "schema_mapping": {"ts": "timestamp"}}',
  },
} as const;
export type ProtocolName = keyof typeof PROTOCOLS;

export const PROTOCOL_BY_TOKEN: Record<string, ProtocolName> = {
  websocket: "WebSocket",
  rest: "REST",
  polling: "Polling",
  database: "Database",
  file: "File",
};

export const POLL_PRESETS = ["15s", "30s", "1m", "5m", "15m", "1h", "1d"] as const;

export const BACKFILL_PROTOCOLS = [
  { token: "rest", label: "REST" },
  { token: "polling", label: "POLL" },
  { token: "database", label: "DB" },
  { token: "file", label: "FILE" },
  { token: "library", label: "LIB" },
] as const;

const STATUS_PILL: Record<string, { text: string; tone: "up" | "warn" | null }> = {
  connected: { text: "Connected", tone: "up" },
  degraded: { text: "Degraded", tone: "warn" },
  disconnected: { text: "Disconnected", tone: "warn" },
  not_configured: { text: "Credentials required", tone: "warn" },
  unknown: { text: "Not probed", tone: null },
};

export function statusPill(state: Source["state"]): { text: string; tone: "up" | "warn" | null } {
  return STATUS_PILL[state.status] ?? { text: state.status_label || "Unknown", tone: null };
}

/** Seconds in an interval token ("15s", "5m", "1h", "1d"), or null. */
export function toSeconds(token: string | null | undefined): number | null {
  const match = /^\s*(\d+(?:\.\d+)?)\s*(s|m|h|d)\s*$/i.exec(String(token ?? ""));
  if (!match) return null;
  const unit = { s: 1, m: 60, h: 3600, d: 86400 }[(match[2] ?? "s").toLowerCase() as "s" | "m" | "h" | "d"];
  return Number(match[1]) * unit;
}

/** A dataset resolution as an interval token; reporting cadences have none. */
export function barToken(granularity: string | null | undefined): string {
  const token = String(granularity ?? "").toLowerCase();
  return ({ "1wk": "7d", "1mo": "30d" } as Record<string, string>)[token] ?? (toSeconds(token) ? token : "");
}

export function humanInterval(seconds: number | null): string {
  if (seconds === null) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h`;
  return `${Math.round(seconds / 86400)}d`;
}

/** "advisory: x" → "Advisory: x." */
export function sentence(text: string | null | undefined): string {
  const value = String(text ?? "").trim();
  if (!value) return "";
  return value.charAt(0).toUpperCase() + value.slice(1) + (/[.!?]$/.test(value) ? "" : ".");
}

export const agoText = (iso: string | null | undefined, now?: number) => (iso ? `${ago(iso, now)} ago` : "never");

export const isPull = (source: Source) => source.protocol === "polling" || source.protocol === "rest" || source.protocol === "library";

export function defaultChannel(source: Source): string {
  const map: Record<string, string> = { market: "bars", fundamentals: "filings", macro: "series", news: "news", alternative: "trends", reference: "reference" };
  return map[source.data_domain] ?? (source.data_domain || "default");
}

/**
 * Whether the chosen resolution reaches the fetch. A subscription already at
 * this resolution carries the API's own verdict, quoted; otherwise the note
 * predicts it from the source's `granularity_reaches_fetch` and the bar sizes
 * the source's other active subscriptions ask for.
 */
export function frequencyNote(source: Source, subscriptions: readonly Subscription[], token: string): { text: string; applies: boolean } {
  const mine = subscriptions.filter((s) => s.source_id === source.id);
  const decided = mine
    .filter((s) => s.granularity === token && typeof s.granularity_applied === "boolean")
    .sort((a, b) => Number(b.active) - Number(a.active))[0];
  if (decided) return { text: sentence(decided.granularity_detail), applies: decided.granularity_applied === true };
  const reaches = source.granularity_reaches_fetch && Boolean(barToken(token));
  const clashing = Array.from(new Set(mine.filter((s) => s.active && s.granularity !== token && barToken(s.granularity)).map((s) => s.granularity)));
  const applies = reaches && !clashing.length;
  const text = !reaches
    ? `Advisory: ${token} would be stored on the subscription, but ${source.name}'s fetch path has no bar-size parameter to apply it to.`
    : clashing.length
      ? `Subscribing at ${token} would disagree with this source's active ${clashing.join(", ")} subscription${clashing.length === 1 ? "" : "s"}, and the scheduler imposes none of them — ${source.name} would keep its configured resolution.`
      : `The scheduler will fetch ${token} bars for this source while the subscription is active.`;
  return { text, applies };
}

/** The advice under the poll control: request cost and agreement with the bar size. */
export function pollNote(source: Source, candidate: string, granularity: string | null): { text: string; tone: "up" | "warn" | null } {
  const seconds = toSeconds(candidate);
  if (!seconds) {
    return {
      text:
        candidate && candidate !== source.schedule
          ? "Unrecognised interval — use a value like 45s, 2m, 4h or 1d."
          : `Current schedule: ${source.schedule_detail.label || source.schedule}. Pick an interval to switch this source to a periodic poll.`,
      tone: "warn",
    };
  }
  const perDay = Math.round(86400 / seconds);
  const cost = `≈ ${int(perDay)} connector run${perDay === 1 ? "" : "s"}/day.`;
  const bar = toSeconds(barToken(granularity));
  if (!bar) return { text: `${cost} Changing this rewrites the source's schedule and re-registers it with the ingest scheduler.`, tone: null };
  if (seconds < bar) {
    return { text: `Polling faster than the ${humanInterval(bar)} bar — ${Math.floor(bar / seconds)}× of these runs return the same unchanged bar. ${cost}`, tone: "warn" };
  }
  if (seconds > bar) {
    return {
      text: `Polling slower than the ${humanInterval(bar)} bar — up to ${Math.max(0, Math.floor(seconds / bar) - 1)} bar(s) can be missed between runs; backfill closes the gap. ${cost}`,
      tone: "warn",
    };
  }
  return { text: `Aligned with the ${humanInterval(bar)} bar interval. ${cost}`, tone: "up" };
}

/** A source re-posted with changes, in the shape the API accepts. */
export function sourceToForm(source: Source, overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: source.id,
    name: source.name,
    data_domain: source.data_domain,
    protocol: source.protocol,
    endpoint: source.endpoint,
    auth: source.auth,
    format: source.format,
    schedule: source.schedule,
    scope: source.scope,
    scope_label: source.scope_label,
    source_tz: source.source_tz,
    // The mode token: from_form reads `retry_policy` as a mode name first.
    retry_policy: source.retry_policy.mode || "exponential",
    backfill: source.backfill,
    advanced: source.advanced,
    provider: source.provider,
    builtin: source.builtin,
    enabled: source.enabled,
    ...overrides,
  };
}

export function protocolAbbrev(protocol: string): { text: string; tone: "up" | "warn" | "accent" | null } {
  const p = protocol.toLowerCase();
  if (p === "polling") return { text: "POLL", tone: "warn" };
  if (p === "websocket") return { text: "WS", tone: "up" };
  if (p === "file" || p === "database") return { text: "FILE/DB", tone: null };
  if (p === "library") return { text: "LIB", tone: "accent" };
  return { text: p.toUpperCase(), tone: "accent" };
}

// ------------------------------------------------------------------ the form

export interface FormState {
  name: string;
  domain: string;
  protocol: ProtocolName;
  endpoint: string;
  auth: string;
  format: string;
  frequency: string;
  scope: string;
  timezone: string;
  retries: string;
  backfillEnabled: boolean;
  backfillRange: string;
  backfillBatch: string;
  backfillStart: string;
  advanced: string;
  cron: string;
  symbols: string;
  keyName: string;
  keyValue: string;
}

export const EMPTY_FORM: FormState = {
  name: "",
  domain: "Prices & quotes",
  protocol: "WebSocket",
  endpoint: "",
  auth: "API key",
  format: "JSON",
  frequency: "Streaming",
  scope: "",
  timezone: "UTC",
  retries: "Exponential backoff",
  backfillEnabled: true,
  backfillRange: "All available history",
  backfillBatch: "Automatic",
  backfillStart: "",
  advanced: "",
  cron: "",
  symbols: "",
  keyName: "",
  keyValue: "",
};

/** The form as SourceConfig.from_form reads it; credentials only when typed. */
export function formPayload(form: FormState, editingId: string | null): SourceForm {
  const payload: SourceForm = {
    sourceName: form.name.trim(),
    sourceDomain: form.domain,
    sourceProtocol: PROTOCOLS[form.protocol].token,
    sourceEndpoint: form.endpoint.trim(),
    sourceAuth: form.auth,
    sourceFormat: form.format,
    sourceFrequency: form.frequency,
    sourceScope: form.scope,
    sourceTimezone: form.timezone,
    sourceRetries: form.retries,
    backfillEnabled: form.backfillEnabled,
    backfillRange: form.backfillRange,
    backfillBatch: form.backfillBatch,
    advancedConfig: form.advanced.trim() || null,
  };
  if (editingId) payload.id = editingId;
  const name = form.keyName.trim();
  const value = form.keyValue;
  if (form.auth === "Basic auth") {
    if (name) payload.auth_username = name;
    if (value) payload.auth_password = value;
  } else if (form.auth === "OAuth 2.0") {
    if (name) payload.auth_client_id = name;
    if (value) payload.auth_client_secret = value;
  } else if (form.auth !== "None") {
    if (name) payload.auth_key_name = name;
    if (value) payload.auth_key_value = value;
  }
  if (form.frequency === "Custom cron…") payload.cron = form.cron.trim();
  if (form.scope === "Custom symbols…") payload.scope = form.symbols;
  if (form.backfillEnabled && form.backfillRange === "Custom start date…") payload.backfillStart = form.backfillStart;
  return payload;
}

/** An existing source back into the form, for editing. */
export function sourceToState(source: Source): FormState {
  const custom = source.scope.length > 0;
  return {
    ...EMPTY_FORM,
    name: source.name,
    domain: source.data_domain_label,
    protocol: PROTOCOL_BY_TOKEN[source.protocol] ?? "REST",
    endpoint: source.endpoint ?? "",
    auth: source.auth.label || "None",
    format: source.format_label,
    frequency: source.schedule_detail.label || "Daily",
    scope: custom ? "Custom symbols…" : source.scope_label || "All available symbols",
    symbols: custom ? source.scope.join(", ") : "",
    timezone: source.source_tz || "UTC",
    retries: source.retry_policy.label || "Exponential backoff",
    backfillEnabled: source.backfill.enabled,
    advanced: source.advanced && Object.keys(source.advanced).length ? JSON.stringify(source.advanced, null, 2) : "",
    keyName: source.auth.key_name ?? "",
  };
}

export function authLabels(auth: string): [string, string] {
  if (auth === "Basic auth") return ["Username", "Password"];
  if (auth === "OAuth 2.0") return ["Client id", "Client secret"];
  if (auth === "Connection secret") return ["Secret name", "Secret value"];
  return ["Key / header name", "Key value"];
}

// ------------------------------------------------------------------ quality

const NAMED_CHECKS = [
  ["completeness", "Gap scan", "all stored series", "GAP", "review"],
  ["outliers", "Outlier detection", "all stored series", "FLAG", "review"],
  ["staleness", "Stale tickers", "all stored series", "STALE", "list"],
  ["schema_drift", "Schema drift", "all sources", "DRIFTED", "review"],
  ["duplicates", "Dupe rows", "all ingests", "DUPE", "review"],
  ["lookahead", "Lookahead audit", "feature store", "OFFENDER", "review"],
] as const;

export interface CheckRow {
  name: string;
  scope: string;
  result: string;
  tone: "up" | "warn" | "down" | null;
  title: string;
  action: "review" | "list" | "ticket" | null;
  failing: { label: string; detail: string }[];
  sla?: Sla["sla"][number];
}

/** The six named checks aggregated over every dataset, then the SLA rows. */
export function checkRows(series: QualitySeries, sla: Sla): CheckRow[] {
  const rows: CheckRow[] = NAMED_CHECKS.map(([id, name, scope, unit, action]) => {
    let seen = 0;
    const failing: { label: string; detail: string; error: boolean }[] = [];
    for (const dataset of series.datasets) {
      for (const check of dataset.checks) {
        if (check.check !== id) continue;
        seen += 1;
        if (!check.passed) failing.push({ label: dataset.label || dataset.dataset, detail: check.detail, error: check.severity === "error" });
      }
    }
    const clean = id === "duplicates" ? "0" : id === "schema_drift" ? "NONE" : "CLEAN";
    return {
      name,
      scope: seen ? `${scope} · ${seen} checked` : "not checked yet",
      result: !seen ? "NOT RUN" : failing.length ? `${failing.length} ${unit}${failing.length > 1 ? "S" : ""}` : clean,
      tone: !seen ? null : failing.length ? (failing.some((f) => f.error) ? "down" : "warn") : "up",
      title: failing.slice(0, 8).map((f) => `${f.label}: ${f.detail}`).join("\n"),
      action: failing.length ? action : null,
      failing,
    };
  });
  const late = sla.sla.filter((r) => r.state !== "ok");
  for (const row of late) {
    const hours = row.age_seconds === null ? null : row.age_seconds / 3600;
    rows.push({
      name: `SLA · ${row.source_id}`,
      scope: `${row.schedule} schedule`,
      result: row.state === "breached" ? `BREACHED${hours === null ? "" : ` ${hours.toFixed(1)}H`}` : "PENDING",
      tone: row.state === "breached" ? "down" : "warn",
      title: row.detail,
      action: "ticket",
      failing: [],
      sla: row,
    });
  }
  if (!sla.sla.length) rows.push({ name: "SLA", scope: "enabled sources", result: "NO SOURCES", tone: null, title: "", action: null, failing: [] });
  else if (!late.length) rows.push({ name: "SLA", scope: `${sla.sla.length} enabled sources`, result: "ALL WITHIN CADENCE", tone: "up", title: "", action: null, failing: [] });
  return rows;
}

/** The note under the outlier queue: what quarantine and escalation actually do. */
export function qualityNote(
  summary: { last_run: string | null; open_incidents?: number | null; datasets?: number | null; datasets_in_store?: number | null },
  counts: Outliers["counts"] | undefined,
): { text: string; warn: boolean } {
  const quarantined = counts?.quarantined ?? 0;
  const escalated = counts?.escalated ?? 0;
  const openIncidents = summary.open_incidents ?? 0;
  const escalation = escalated
    ? ` ${escalated} point(s) are escalated for human review: their raw values are still being used, which is what escalation means.`
    : "";
  if (quarantined) {
    return {
      text: `⚠ ${quarantined} quarantined observation(s) are excluded from every store read, so no feature, screen or backtest sees them until the decision is undone.${escalation}`,
      warn: true,
    };
  }
  if (escalated) return { text: `⚠ Nothing is quarantined.${escalation}`, warn: true };
  if (openIncidents) {
    return { text: `⚠ ${openIncidents} open incident(s). Flagged observations stay in the data until somebody quarantines them in the queue.`, warn: true };
  }
  return {
    text: summary.last_run
      ? `✓ No open incidents. ${summary.datasets ?? 0} dataset(s) scored on the last run, ${summary.datasets_in_store ?? 0} present in the store.`
      : "No checks have run yet, so nothing is known about the stored data's quality.",
    warn: false,
  };
}
