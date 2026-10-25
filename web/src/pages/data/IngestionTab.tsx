/* Data Ingestion (COVERAGE F.4): the source form with a live connection test,
 * and the configured sources with test, edit, pause and delete. */
import { useState, type ReactNode } from "react";
import { errorReason } from "../../api/errors";
import { useUniverses, useWatchlists } from "../../api/hooks";
import { IngestFrame } from "../../api/schemas/frames";
import type { ConnectionTest, Source } from "../../api/schemas/ingest";
import { useLiveInvalidate } from "../../live/context";
import { useToast } from "../../shell/toast-context";
import { useNow } from "../../shell/useNow";
import { cn } from "../../ui/lib/cn";
import { Badge } from "../../ui/primitives/badge";
import { Button } from "../../ui/primitives/button";
import { Panel } from "../../ui/primitives/card";
import { ConfirmDialog } from "../../ui/primitives/confirm";
import { Field, Input, Select, Textarea } from "../../ui/primitives/field";
import { ingestKeys, rowsText, useIngestActions, useSources } from "./ingestHooks";
import { agoText, authLabels, EMPTY_FORM, formPayload, PROTOCOLS, sourceToState, type FormState, type ProtocolName } from "./ingestRules";

const AGE_TICK_MS = 5000;
const DOMAINS = ["Prices & quotes", "Fundamentals", "News", "Macro / economic", "Alternative data", "Reference data"];
const PROTOCOL_OPTIONS: [ProtocolName, string][] = [
  ["WebSocket", "WebSocket stream"],
  ["REST", "REST API"],
  ["Polling", "Polling API"],
  ["Database", "Database"],
  ["File", "Database file / flat file"],
];
const AUTHS = ["API key", "OAuth 2.0", "Basic auth", "Connection secret", "None"];
const FORMATS = ["JSON", "NDJSON", "CSV", "Parquet", "Arrow", "Database rows"];
const FREQUENCIES = ["Streaming", "Every second", "Every minute", "Every 5 minutes", "Hourly", "Daily", "On file change", "Custom cron…"];
const TIMEZONES = ["UTC", "America/New_York", "Europe/Copenhagen", "Exchange local"];
const RETRIES = ["Exponential backoff", "Fixed 30 seconds", "Immediate, max 5", "Manual only"];
const RANGES = ["All available history", "Last 10 years", "Last 5 years", "Last 1 year", "Custom start date…"];
const BATCHES = ["Automatic", "1 day", "1 month", "1 year", "10,000 rows"];

const Options = ({ items }: { items: readonly string[] }) => (
  <>
    {items.map((item) => (
      <option key={item}>{item}</option>
    ))}
  </>
);

function Fact({ k, v }: { k: string; v: ReactNode }) {
  return (
    <div className="flex justify-between text-[11px]">
      <span className="text-text-muted">{k}</span>
      <span>{v}</span>
    </div>
  );
}

const determined = (value: boolean | null) => (value === null ? "not determined" : String(value));

function TestResult({ result }: { result: ConnectionTest | { ok: false; message: string; error: string } }) {
  const full = "reachable" in result ? result : null;
  const message = (result.message ?? "").trim();
  const error = (result.error ?? "").trim();
  const schema = full?.inferred_schema ? Object.entries(full.inferred_schema) : [];
  return (
    <div className={cn("mt-2 flex flex-col gap-1 rounded-[4px] border p-2", result.ok ? "border-up" : "border-down")} role="status">
      <b className={result.ok ? "text-up" : "text-down"}>{result.ok ? "✓ Connection successful" : "✗ Connection failed"}</b>
      <Fact k="Reachable" v={full ? determined(full.reachable) : "not determined"} />
      <Fact k="Authenticated" v={full ? determined(full.authenticated) : "not determined"} />
      <Fact k="HTTP status" v={full?.status_code ?? "—"} />
      <Fact k="Latency" v={full?.latency_ms == null ? "—" : `${Math.round(full.latency_ms)} ms`} />
      <Fact k="Tested at" v={full?.tested_at ?? "—"} />
      {message && <div>{message}</div>}
      {/* A connector usually reports one sentence as both; twice reads like two problems. */}
      {error && error !== message && <div className="text-down">{error}</div>}
      {schema.length > 0 && <div className="text-text-muted">Inferred schema: {schema.map(([k, v]) => `${k}:${String(v)}`).join(", ")}</div>}
      {full?.sample && full.sample.length > 0 && (
        <>
          <div className="text-text-muted">Sample record(s):</div>
          <pre className="overflow-auto text-[10px]">{JSON.stringify(full.sample, null, 2)}</pre>
        </>
      )}
    </div>
  );
}

function SourceForm({ initial, onSaved }: { initial: Source | null; onSaved: () => void }) {
  const toast = useToast();
  const universes = useUniverses();
  const watchlists = useWatchlists();
  const { test, save } = useIngestActions();
  const [editing, setEditing] = useState<Source | null>(initial);
  const [form, setForm] = useState<FormState>(initial ? sourceToState(initial) : EMPTY_FORM);
  const [result, setResult] = useState<ConnectionTest | { ok: false; message: string; error: string } | null>(null);
  const [testing, setTesting] = useState<string | null>(null);

  const scopes = [
    ...(universes.data?.universes ?? []).map((u) => u.name),
    ...(watchlists.data?.watchlists ?? []).map((w) => w.name),
    "All available symbols",
    "Custom symbols…",
  ];
  const scope = form.scope || scopes[0] || "All available symbols";
  const set = <K extends keyof FormState>(key: K, value: FormState[K]) => setForm((f) => ({ ...f, [key]: value }));
  const protocol = PROTOCOLS[form.protocol];
  const [keyLabel, valueLabel] = authLabels(form.auth);
  const payload = () => formPayload({ ...form, scope }, editing?.id ?? null);

  const runTest = () => {
    const body = payload();
    if (!body.sourceName) return toast.warn("A source needs a name before it can be tested");
    setResult(null);
    setTesting(`Testing ${body.sourceName} via ${body.sourceProtocol}…`);
    test.mutate(body, {
      onSuccess: setResult,
      onError: (err) => setResult({ ok: false, message: "The request was rejected before it reached the vendor.", error: errorReason(err) }),
      onSettled: () => setTesting(null),
    });
  };

  const reset = () => {
    setEditing(null);
    setForm(EMPTY_FORM);
    setResult(null);
  };

  return (
    <Panel title="Configure ingestion source" actions={<Badge>{editing ? `editing ${editing.id}` : "new connection"}</Badge>}>
      <form
        aria-label="Ingestion source"
        className="flex flex-col gap-2.5"
        onSubmit={(e) => {
          e.preventDefault();
          const body = payload();
          if (!body.sourceName) return;
          save.mutate(body, {
            onSuccess: () => {
              reset();
              onSaved();
            },
          });
        }}
      >
        <div className="grid grid-cols-2 gap-2.5">
          <Field label="Source name" htmlFor="sourceName">
            <Input id="sourceName" required placeholder="e.g. Alpaca live bars" value={form.name} onChange={(e) => set("name", e.target.value)} />
          </Field>
          <Field label="Data domain" htmlFor="sourceDomain">
            <Select id="sourceDomain" value={form.domain} onChange={(e) => set("domain", e.target.value)}>
              <Options items={DOMAINS} />
            </Select>
          </Field>
        </div>
        <Field label="Protocol / connector" htmlFor="sourceProtocol" hint={protocol.hint}>
          <Select
            id="sourceProtocol"
            value={form.protocol}
            onChange={(e) => {
              const next = e.target.value as ProtocolName;
              // A new source takes the protocol's usual cadence; an edit keeps its own.
              setForm((f) => ({ ...f, protocol: next, frequency: editing ? f.frequency : PROTOCOLS[next].frequency }));
            }}
          >
            {PROTOCOL_OPTIONS.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </Select>
        </Field>
        <Field label={protocol.label} htmlFor="sourceEndpoint">
          <Input id="sourceEndpoint" required placeholder={protocol.placeholder} value={form.endpoint} onChange={(e) => set("endpoint", e.target.value)} />
        </Field>
        <div className="grid grid-cols-2 gap-2.5">
          <Field label="Authentication" htmlFor="sourceAuth">
            <Select id="sourceAuth" value={form.auth} onChange={(e) => set("auth", e.target.value)}>
              <Options items={AUTHS} />
            </Select>
          </Field>
          <Field label="Format" htmlFor="sourceFormat">
            <Select id="sourceFormat" value={form.format} onChange={(e) => set("format", e.target.value)}>
              <Options items={FORMATS} />
            </Select>
          </Field>
        </div>
        {form.auth !== "None" && (
          <div className="grid grid-cols-2 gap-2.5">
            <Field label={keyLabel} htmlFor="authKeyName">
              <Input id="authKeyName" placeholder="X-API-KEY" autoComplete="off" value={form.keyName} onChange={(e) => set("keyName", e.target.value)} />
            </Field>
            <Field label={valueLabel} htmlFor="authKeyValue" hint="Stored with the source; never returned by the API.">
              <Input
                id="authKeyValue"
                type="password"
                autoComplete="off"
                // The API never returns a stored key, so saving without it clears it.
                placeholder={editing?.auth.key_set ? "a key is stored — re-enter it or saving will clear it" : "write-only"}
                value={form.keyValue}
                onChange={(e) => set("keyValue", e.target.value)}
              />
            </Field>
          </div>
        )}
        <div className="grid grid-cols-2 gap-2.5">
          <Field label="Frequency / schedule" htmlFor="sourceFrequency">
            <Select id="sourceFrequency" value={form.frequency} onChange={(e) => set("frequency", e.target.value)}>
              <Options items={FREQUENCIES} />
            </Select>
          </Field>
          <Field label="Scope" htmlFor="sourceScope">
            <Select id="sourceScope" value={scope} onChange={(e) => set("scope", e.target.value)}>
              <Options items={scopes} />
            </Select>
          </Field>
        </div>
        {(form.frequency === "Custom cron…" || scope === "Custom symbols…") && (
          <div className="grid grid-cols-2 gap-2.5">
            {form.frequency === "Custom cron…" && (
              <Field label="Cron expression" htmlFor="sourceCron">
                <Input id="sourceCron" placeholder="*/5 * * * *" value={form.cron} onChange={(e) => set("cron", e.target.value)} />
              </Field>
            )}
            {scope === "Custom symbols…" && (
              <Field label="Custom symbols" htmlFor="sourceSymbols">
                <Input id="sourceSymbols" placeholder="AAPL, MSFT, NVDA" value={form.symbols} onChange={(e) => set("symbols", e.target.value)} />
              </Field>
            )}
          </div>
        )}
        <div className="grid grid-cols-2 gap-2.5">
          <Field label="Source timezone" htmlFor="sourceTimezone">
            <Select id="sourceTimezone" value={form.timezone} onChange={(e) => set("timezone", e.target.value)}>
              <Options items={TIMEZONES} />
            </Select>
          </Field>
          <Field label="Retry / reconnect policy" htmlFor="sourceRetries">
            <Select id="sourceRetries" value={form.retries} onChange={(e) => set("retries", e.target.value)}>
              <Options items={RETRIES} />
            </Select>
          </Field>
        </div>
        <label className="flex items-center gap-1.5 text-[12px]">
          <input type="checkbox" checked={form.backfillEnabled} onChange={(e) => set("backfillEnabled", e.target.checked)} /> Enable historical backfill on first connection
        </label>
        <div className={cn("grid grid-cols-2 gap-2.5", !form.backfillEnabled && "opacity-35")}>
          <Field label="Backfill range" htmlFor="backfillRange">
            <Select id="backfillRange" value={form.backfillRange} onChange={(e) => set("backfillRange", e.target.value)}>
              <Options items={RANGES} />
            </Select>
          </Field>
          <Field label="Backfill batch size" htmlFor="backfillBatch">
            <Select id="backfillBatch" value={form.backfillBatch} onChange={(e) => set("backfillBatch", e.target.value)}>
              <Options items={BATCHES} />
            </Select>
          </Field>
        </div>
        {form.backfillEnabled && form.backfillRange === "Custom start date…" && (
          <Field label="Backfill start date" htmlFor="backfillStart">
            <Input id="backfillStart" type="date" value={form.backfillStart} onChange={(e) => set("backfillStart", e.target.value)} />
          </Field>
        )}
        <Field label="Advanced configuration" htmlFor="advancedConfig">
          <Textarea id="advancedConfig" placeholder={protocol.advanced} value={form.advanced} onChange={(e) => set("advanced", e.target.value)} />
        </Field>
        <div className="flex justify-end gap-1.5">
          {editing && (
            <Button size="sm" type="button" onClick={reset}>
              Cancel edit
            </Button>
          )}
          <Button size="sm" type="button" disabled={test.isPending} onClick={runTest}>
            Test connection
          </Button>
          <Button size="sm" type="submit" variant="primary" disabled={save.isPending}>
            {editing ? "Save changes" : "Save data source"}
          </Button>
        </div>
        {testing && <div className="mt-2 rounded-[4px] border border-border-subtle p-2 text-text-muted">{testing}</div>}
        {result && <TestResult result={result} />}
      </form>
    </Panel>
  );
}

function SourceTable({ onEdit }: { onEdit: (source: Source) => void }) {
  // The ages tick, as the old tables did every five seconds.
  const now = useNow(AGE_TICK_MS);
  const sources = useSources();
  const { probe, setEnabled, remove } = useIngestActions();
  const [deleting, setDeleting] = useState<Source | null>(null);
  const list = sources.data?.sources ?? [];
  const connected = sources.data?.summary?.connected ?? list.filter((s) => s.state.status === "connected").length;
  return (
    <Panel
      title="Configured ingestion sources"
      actions={<Badge tone={connected > 0 ? "up" : "neutral"}>{sources.isPending ? "loading…" : `${connected} / ${list.length} connected`}</Badge>}
    >
      <table className="w-full text-[11px]" aria-label="Configured ingestion sources">
        <thead className="text-[9px] text-text-muted">
          <tr>
            {["SOURCE", "DOMAIN", "PROTOCOL", "FREQUENCY", "BACKFILL", "FRESHNESS", "COVERAGE", "STATUS", "ACTIONS"].map((h) => (
              <th key={h} className={cn("px-1.5 py-1 font-normal", h === "ACTIONS" ? "text-right" : "text-left")}>
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sources.isError && (
            <tr>
              <td colSpan={9} className="px-1.5 py-2 text-down">
                {errorReason(sources.error)}
              </td>
            </tr>
          )}
          {sources.isSuccess && !list.length && (
            <tr>
              <td colSpan={9} className="px-1.5 py-2 text-text-subtle">
                No ingestion sources are registered.
              </td>
            </tr>
          )}
          {list.map((source) => {
            const state = source.state;
            return (
              <tr key={source.id} className="border-t border-border-subtle">
                <td className="px-1.5 py-1 text-text-strong">{source.name}</td>
                <td className="px-1.5 py-1">{source.data_domain_label || source.data_domain}</td>
                <td className="px-1.5 py-1">{source.protocol_label || source.protocol}</td>
                <td className="px-1.5 py-1">{source.schedule_detail.label || source.schedule}</td>
                <td className={cn("px-1.5 py-1", source.backfill.enabled ? "text-up" : "text-text-muted")}>{source.backfill.enabled ? source.backfill.range : "Disabled"}</td>
                <td className="px-1.5 py-1">{agoText(state.last_ingest_at, now)}</td>
                <td className="px-1.5 py-1">{rowsText(state.rows_ingested)}</td>
                <td
                  className={cn("px-1.5 py-1", state.status === "connected" ? "text-up" : state.status === "unknown" ? "text-text-muted" : "text-warn")}
                  title={state.last_error ?? undefined}
                >
                  {state.status_label || "Unknown"}
                </td>
                <td className="px-1.5 py-1">
                  <div className="flex justify-end gap-1">
                    <Button size="sm" disabled={probe.isPending && probe.variables.id === source.id} onClick={() => probe.mutate(source)}>
                      Test
                    </Button>
                    <Button size="sm" onClick={() => onEdit(source)}>
                      Edit
                    </Button>
                    <Button size="sm" disabled={setEnabled.isPending && setEnabled.variables.id === source.id} onClick={() => setEnabled.mutate(source)}>
                      {source.enabled ? "Pause" : "Resume"}
                    </Button>
                    {!source.builtin && (
                      <Button size="sm" variant="danger" onClick={() => setDeleting(source)}>
                        Delete
                      </Button>
                    )}
                  </div>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <div className="mt-2.5 text-[11px]">
        <b className="text-text-strong">Ingestion lifecycle</b>{" "}
        <span className="text-text-muted">
          Test connection → save source → optional backfill → live or scheduled ingestion → monitor freshness, coverage, retries, and schema drift.
        </span>
      </div>
      <ConfirmDialog
        open={deleting !== null}
        title="Delete data source"
        confirmLabel="Delete"
        onCancel={() => setDeleting(null)}
        onConfirm={async () => {
          if (deleting) await remove.mutateAsync(deleting).catch(() => undefined);
          setDeleting(null);
        }}
      >
        {deleting && `Delete ${deleting.name}? Its scheduler job and subscriptions go with it; ingested data stays in the store.`}
      </ConfirmDialog>
    </Panel>
  );
}

/** `draft` is the source the catalog handed over (null for a blank form); its key resets the form. */
export function IngestionTab({ draft }: { draft: { source: Source | null; key: number } }) {
  const [editing, setEditing] = useState(draft);
  useLiveInvalidate("ingest", IngestFrame, () => [ingestKeys.sources]);
  return (
    <div className="grid grid-cols-2 gap-3 max-[1100px]:grid-cols-1">
      <SourceForm key={editing.key} initial={editing.source} onSaved={() => undefined} />
      <SourceTable
        onEdit={(source) => {
          setEditing({ source, key: Date.now() });
          document.getElementById("sourceName")?.scrollIntoView?.({ block: "center", behavior: "smooth" });
        }}
      />
    </div>
  );
}
