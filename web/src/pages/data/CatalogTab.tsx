/* Source Catalog (COVERAGE F.3): the registered sources, one source's
 * protocols, subscriptions and observed state, the subscription panel with
 * frequency, poll interval and backfill, and every active subscription. */
import { useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { useState, type ReactNode } from "react";
import { errorReason } from "../../api/errors";
import { useUniverses, useWatchlists } from "../../api/hooks";
import type { Source, Subscription } from "../../api/schemas/ingest";
import { IngestFrame } from "../../api/schemas/frames";
import { useChannel } from "../../live/context";
import { useToast } from "../../shell/toast-context";
import { useNow } from "../../shell/useNow";
import { int } from "../../ui/format";
import { cn } from "../../ui/lib/cn";
import { Badge } from "../../ui/primitives/badge";
import { Button } from "../../ui/primitives/button";
import { Panel } from "../../ui/primitives/card";
import { ConfirmDialog } from "../../ui/primitives/confirm";
import { Input, Select } from "../../ui/primitives/field";
import { ingestKeys, scheduleRefusal, useIngestActions, useSources, useSubscriptions } from "./ingestHooks";
import {
  agoText,
  BACKFILL_PROTOCOLS,
  defaultChannel,
  frequencyNote,
  isPull,
  POLL_PRESETS,
  pollNote,
  PROTOCOLS,
  protocolAbbrev,
  statusPill,
  toSeconds,
} from "./ingestRules";

const AGE_TICK_MS = 5000;
const toneText = (t: "up" | "warn" | "down" | "accent" | null | undefined) =>
  t === "up" ? "text-up" : t === "warn" ? "text-warn" : t === "down" ? "text-down" : t === "accent" ? "text-accent" : undefined;
const Label = ({ children }: { children: ReactNode }) => <span className="mb-1 block text-[9px] tracking-[1px] text-text-subtle uppercase">{children}</span>;
const Note = ({ children, className }: { children: ReactNode; className?: string }) => <p className={cn("mt-1 text-[10px] text-text-muted", className)}>{children}</p>;

function Kv({ k, v, tone }: { k: string; v: ReactNode; tone?: "up" | "warn" | null }) {
  return (
    <div className="flex justify-between border-b border-border-subtle py-1 text-[11px]">
      <span className="text-text-muted">{k}</span>
      <span className={toneText(tone)}>{v}</span>
    </div>
  );
}

function Pill({ source }: { source: Source }) {
  const pill = statusPill(source.state);
  return (
    <Badge size="sm" tone={pill.tone ?? "neutral"} className="ml-1.5">
      {pill.text}
    </Badge>
  );
}

/** A row of one-of chips; the source's own one is live, the rest disabled. */
function Chips({ items, label }: { items: { text: string; on: boolean }[]; label: string }) {
  return (
    <div className="flex flex-wrap gap-1" role="group" aria-label={label}>
      {items.map((item) => (
        <button
          key={item.text}
          type="button"
          disabled={!item.on}
          aria-pressed={item.on}
          className={cn(
            "rounded-[4px] border px-2 py-0.5 text-[10px]",
            item.on ? "border-accent bg-accent-dim text-accent" : "border-border-subtle text-text-subtle opacity-60",
          )}
        >
          {item.text}
        </button>
      ))}
    </div>
  );
}

function Detail({ source, subscriptions, picked, onPick }: { source: Source; subscriptions: Subscription[]; picked: string | null; onPick: (id: string) => void }) {
  const navigate = useNavigate();
  const toast = useToast();
  const state = source.state;
  const mine = subscriptions.filter((s) => s.source_id === source.id);
  const protocols: { text: string; on: boolean }[] = (Object.keys(PROTOCOLS) as (keyof typeof PROTOCOLS)[]).map((name) => ({
    text: name,
    on: PROTOCOLS[name].token === source.protocol,
  }));
  if (source.protocol === "library") protocols.push({ text: "Python library", on: true });
  return (
    <div>
      <div className="mb-3 flex items-start justify-between gap-2">
        <div>
          <h2 className="text-[14px] text-text-strong">
            {source.name}
            <Pill source={source} />
          </h2>
          <p className="mt-1 text-[11px] text-text-muted">
            {source.endpoint || source.provider || "—"}
            <br />
            {[
              source.builtin ? "BUILT-IN" : "USER SOURCE",
              (source.protocol_label || source.protocol).toUpperCase(),
              `AUTH ${(source.auth.label || "none").toUpperCase()}${source.auth.configured ? " ✓" : " — NOT CONFIGURED"}`,
              source.schedule_detail.label || source.schedule,
            ].join(" · ")}
          </p>
        </div>
        <Button
          size="sm"
          onClick={() => {
            if (source.auth.configured) toast.ok(`${source.name} credentials are configured (${source.auth.label || "none"})`);
            else {
              toast.warn(`Add credentials for ${source.name} in Settings → API keys`);
              void navigate({ to: "/settings" });
            }
          }}
        >
          Credentials
        </Button>
      </div>
      <section className="mb-3">
        <Label>Protocols supported</Label>
        <Chips items={protocols} label="Protocols supported" />
        <Note>A source speaks exactly one protocol; the connector class follows from it.</Note>
      </section>
      <section className="mb-3">
        <Label>Subscriptions · select to configure</Label>
        <div className="grid grid-cols-2 gap-1.5">
          {!mine.length && (
            <Note className="col-span-2">No subscription configured for this source yet. The panel on the right creates one; the source still ingests its own configured scope.</Note>
          )}
          {mine.map((sub) => (
            <button
              key={sub.id}
              type="button"
              aria-pressed={sub.id === picked}
              onClick={() => onPick(sub.id)}
              className={cn("rounded-[4px] border p-2 text-left", sub.id === picked ? "border-accent" : "border-border-subtle")}
            >
              <strong className="block text-[11px] text-text-strong">{sub.channel}</strong>
              <span className="block text-[10px] text-text-muted">
                {sub.active ? "ACTIVE" : "PAUSED"} · {sub.scope.length} scope items
              </span>
              <Note>{sub.scope.join(", ") || "no explicit scope — the source's own scope applies"}</Note>
            </button>
          ))}
        </div>
      </section>
      <section>
        <Label>Observed state</Label>
        <Kv k="Status" v={state.status_label || "unknown"} tone={state.status === "connected" ? "up" : state.status === "unknown" ? null : "warn"} />
        <Kv k="Last ingest" v={agoText(state.last_ingest_at)} />
        <Kv k="Rows ingested" v={int(state.rows_ingested)} />
        <Kv k="Latency" v={state.latency_ms === null ? "—" : `${Math.round(state.latency_ms)} ms`} />
        <Kv k="Last probe" v={agoText(state.checked_at)} />
        {state.last_error && <Note className="text-down">{state.last_error}</Note>}
      </section>
    </div>
  );
}

function PollBlock({ source, granularity }: { source: Source; granularity: string | null }) {
  const toast = useToast();
  const { schedule } = useIngestActions();
  const current = source.schedule;
  const [custom, setCustom] = useState(POLL_PRESETS.includes(current as (typeof POLL_PRESETS)[number]) ? "" : toSeconds(current) ? current : "");
  if (!isPull(source)) {
    return (
      <Note>
        Not applicable — a {source.protocol_label || source.protocol} source pushes or lands data as it arrives, so there is no polling schedule to set.
      </Note>
    );
  }
  const set = (interval: string) => {
    const refusal = scheduleRefusal(source, interval);
    if (refusal) toast.warn(refusal);
    else schedule.mutate({ source, interval });
  };
  const commit = () => {
    const value = custom.trim();
    if (value && value !== current) set(value);
  };
  const note = pollNote(source, custom.trim() || current, granularity);
  return (
    <>
      <div className="flex flex-wrap items-center gap-1" role="group" aria-label="Poll interval">
        {POLL_PRESETS.map((preset) => (
          <button
            key={preset}
            type="button"
            aria-pressed={preset === current}
            disabled={schedule.isPending}
            onClick={() => set(preset)}
            className={cn("rounded-[4px] border px-2 py-0.5 text-[10px]", preset === current ? "border-accent bg-accent-dim text-accent" : "border-border-subtle")}
          >
            {preset}
          </button>
        ))}
        <Input
          aria-label="Custom poll interval"
          placeholder="custom"
          value={custom}
          className="h-6 w-20 text-[10px]"
          onChange={(e) => setCustom(e.target.value)}
          onBlur={commit}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              commit();
            }
          }}
        />
      </div>
      <Note className={toneText(note.tone)}>{note.text}</Note>
    </>
  );
}

function BackfillBlock({ source, status, onStart }: { source: Source; status: string; onStart: (text: string) => void }) {
  const toast = useToast();
  const { backfill } = useIngestActions();
  const [from, setFrom] = useState(source.backfill.start_date || source.backfill.range || "1y");
  const [to, setTo] = useState("now");
  return (
    <>
      <Chips items={BACKFILL_PROTOCOLS.map((p) => ({ text: p.label + (p.token === source.protocol ? " ●" : ""), on: p.token === source.protocol }))} label="Backfill protocols" />
      {!source.can_backfill ? (
        <Note>
          No history endpoint — {source.protocol_label || source.protocol} sources only see what the vendor publishes while the connector is subscribed, so there is nothing to backfill.
        </Note>
      ) : (
        <>
          <div className="mt-1.5 grid grid-cols-2 gap-1.5">
            <label>
              <Label>From</Label>
              <Input value={from} placeholder="1y, 5y, all, or 2016-01-04" onChange={(e) => setFrom(e.target.value)} />
            </label>
            <label>
              <Label>To</Label>
              <Input value={to} placeholder="now, or 2024-12-31" onChange={(e) => setTo(e.target.value)} />
            </label>
          </div>
          <Note>
            FROM takes a duration (1y, 6mo, 30d), “all” for everything the vendor has, or a calendar boundary (2016-01-04, 2009-Q2). TO takes “now” or a date. Configured default:{" "}
            {source.backfill.enabled ? `${source.backfill.range} · batches of ${source.backfill.batch}` : "backfill disabled on this source"}.
          </Note>
          <Note aria-live="polite">{status}</Note>
          <Button
            size="sm"
            className="mt-1.5"
            disabled={!source.auth.configured || backfill.isPending}
            title={source.auth.configured ? undefined : "This connector has no credentials configured"}
            onClick={() => {
              const range = from.trim();
              const end = to.trim() || "now";
              if (!range) return toast.warn("A backfill needs a FROM boundary");
              backfill.mutate(
                { source, range, end },
                { onSuccess: (r) => r && onStart(`Backfill started · ${range} → ${end}. Progress arrives on the ingest channel.`) },
              );
            }}
          >
            Run backfill
          </Button>
        </>
      )}
    </>
  );
}

const FEED_MODES = [
  ["scheduled", "Scheduled — poll on the source's own cadence"],
  ["live", "Live — stream as the source publishes"],
] as const;

function SubscriptionPanel({
  source,
  subscriptions,
  backfillStatus,
  onBackfill,
  onSubscribed,
  onCustom,
}: {
  source: Source;
  subscriptions: Subscription[];
  backfillStatus: string;
  onBackfill: (text: string) => void;
  onSubscribed: (id: string) => void;
  onCustom: () => void;
}) {
  const toast = useToast();
  const universes = useUniverses();
  const watchlists = useWatchlists();
  const { subscribe, probe } = useIngestActions();
  const schedule = source.schedule_detail;
  const streams = source.protocol === "websocket" || schedule.kind === "streaming";
  const needsCredentials = !source.auth.configured;
  const options = source.granularity_options;
  const [channel, setChannel] = useState(defaultChannel(source));
  const [feed, setFeed] = useState<string>(streams ? "live" : "scheduled");
  const [pit, setPit] = useState(false);
  const [granularity, setGranularity] = useState<string | null>(
    options.length ? (source.granularity_default && options.includes(source.granularity_default) ? source.granularity_default : (options[0] ?? null)) : null,
  );
  // Unset means the first option, as the old select defaulted.
  const [picked, setScope] = useState<string | null>(null);
  const [symbols, setSymbols] = useState("");
  const scheduleLabel = schedule.label || source.schedule || "manual";
  const kind = source.protocol_label || source.protocol;
  const feedNote =
    feed === "live"
      ? streams
        ? `${source.name} streams over ${kind}.`
        : `⚠ ${source.name} is a ${kind} source with no stream. The subscription is saved as live, but it will actually be polled on its ${scheduleLabel} schedule.`
      : streams
        ? `⚠ ${source.name} only streams; there is no polling cadence to fall back on.`
        : `Polled on the source's ${scheduleLabel} schedule (set below).`;
  const note = granularity ? frequencyNote(source, subscriptions, granularity) : null;
  const scopeOptions = [
    ...(universes.data?.universes ?? []).map((u) => ({ value: `universe:${u.id}`, label: `${u.name} (universe)` })),
    ...(watchlists.data?.watchlists ?? []).map((w) => ({ value: `watchlist:${w.id}`, label: `${w.name} (watchlist)` })),
    { value: "", label: "All available symbols" },
    { value: "__custom__", label: "Custom symbols…" },
  ];
  const scope = picked ?? scopeOptions[0]?.value ?? "";

  const add = () => {
    const name = channel.trim();
    if (!name) return toast.warn("A subscription needs a channel name");
    const chosen = scope === "__custom__" ? symbols.split(/[,\s]+/).filter(Boolean) : scope ? [scope] : [];
    subscribe.mutate(
      { source, channel: name, scope: chosen, feedMode: feed, preservePit: pit, granularity: granularity ?? "" },
      { onSuccess: (sub) => onSubscribed(sub.id) },
    );
  };

  return (
    <div className="flex flex-col gap-3 text-[11px]">
      <div className="text-text-muted">
        Subscription <b className="text-text-strong">{source.name}</b>
      </div>
      <p className={cn("rounded-[4px] border p-2 text-[10px]", needsCredentials ? "border-warn text-warn" : "border-up text-up")}>
        {needsCredentials
          ? `Credentials are required before this connector can ingest: ${source.state.last_error || "the configured auth has no key"}`
          : "Connector is implemented and configured — it can be subscribed."}
      </p>
      <label>
        <Label>Channel / dataset</Label>
        <Input value={channel} onChange={(e) => setChannel(e.target.value)} />
        <Note>The connector's channel name, e.g. bars, quotes, series, filings.</Note>
      </label>
      <label>
        <Label>Live / scheduled feed</Label>
        <Select value={feed} onChange={(e) => setFeed(e.target.value)}>
          {FEED_MODES.map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </Select>
        <Note>{feedNote}</Note>
      </label>
      <div>
        <label className="flex items-center gap-1.5">
          <input type="checkbox" checked={pit} onChange={(e) => setPit(e.target.checked)} />
          <span>Preserve point-in-time values</span>
        </label>
        <Note>With this on, an observation already stored for a timestamp is kept and a later vendor restatement of it is refused; only new timestamps are written.</Note>
      </div>
      <div>
        <Label>Frequency</Label>
        {!options.length ? (
          <Note>This source states no dataset resolutions.</Note>
        ) : (
          <>
            <div className="flex flex-wrap gap-1" role="group" aria-label="Frequency">
              {options.map((token) => (
                <button
                  key={token}
                  type="button"
                  aria-pressed={token === granularity}
                  onClick={() => setGranularity(token)}
                  className={cn("rounded-[4px] border px-2 py-0.5 text-[10px]", token === granularity ? "border-accent bg-accent-dim text-accent" : "border-border-subtle")}
                >
                  {token}
                </button>
              ))}
            </div>
            {note && <Note className={note.applies ? "text-up" : undefined}>{note.text}</Note>}
          </>
        )}
      </div>
      <div>
        <Label>Poll interval (source schedule)</Label>
        <PollBlock key={source.schedule} source={source} granularity={granularity} />
      </div>
      <div>
        <Label>Historical / backfill</Label>
        <BackfillBlock source={source} status={backfillStatus} onStart={onBackfill} />
      </div>
      <label>
        <Label>Universe / watchlist scope</Label>
        <Select value={scope} onChange={(e) => setScope(e.target.value)}>
          {scopeOptions.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </Select>
        {scope === "__custom__" && <Input className="mt-1.5" placeholder="AAPL, MSFT" aria-label="Custom scope symbols" value={symbols} onChange={(e) => setSymbols(e.target.value)} />}
      </label>
      <p className="text-[10px] text-text-muted">
        <b className="block text-text-strong">{int(source.state.rows_ingested)} rows ingested</b>
        {scheduleLabel} · last ingest {agoText(source.state.last_ingest_at)} · backfill {source.backfill.enabled ? source.backfill.range : "disabled"}
      </p>
      <div className="flex flex-wrap gap-1.5">
        <Button size="sm" variant="primary" disabled={needsCredentials || subscribe.isPending} onClick={add}>
          + Add subscription
        </Button>
        <Button size="sm" disabled={probe.isPending} onClick={() => probe.mutate(source)}>
          Dry run
        </Button>
        <Button size="sm" variant="ghost" onClick={onCustom}>
          Use custom configuration
        </Button>
      </div>
    </div>
  );
}

function ActiveSubscriptions({ sources, subscriptions }: { sources: Source[]; subscriptions: Subscription[] }) {
  // The ages tick, as the old tables did every five seconds.
  const now = useNow(AGE_TICK_MS);
  const { unsubscribe } = useIngestActions();
  const [removing, setRemoving] = useState<Subscription | null>(null);
  return (
    <Panel
      title="Active subscriptions"
      actions={<Badge>{`${subscriptions.length} ${subscriptions.length === 1 ? "feed" : "feeds"}`}</Badge>}
      className="mt-3"
    >
      <table className="w-full text-[11px]" aria-label="Active subscriptions">
        <thead className="text-[9px] text-text-muted">
          <tr>
            {["SOURCE", "DATATYPE", "PROTOCOL", "FREQ", "RANGE / MODE", "LAST INGEST", "STATUS", "ACTIONS"].map((h) => (
              <th key={h} className={cn("px-1.5 py-1 font-normal", h === "ACTIONS" ? "text-right" : "text-left")}>
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {!subscriptions.length && (
            <tr>
              <td colSpan={8} className="px-1.5 py-2 text-text-subtle">
                No subscriptions configured. Sources still ingest their own scope on their own schedule — a subscription narrows one to a specific channel and universe.
              </td>
            </tr>
          )}
          {subscriptions.map((sub) => {
            const source = sources.find((s) => s.id === sub.source_id);
            const state = source?.state;
            const abbrev = protocolAbbrev(source?.protocol ?? "");
            return (
              <tr key={sub.id} className="border-t border-border-subtle">
                <td className="px-1.5 py-1 text-text-strong">{source?.name ?? sub.source_id}</td>
                <td className="px-1.5 py-1 text-text-muted" title={sub.granularity ? (sub.granularity_detail ?? "") : undefined}>
                  {sub.channel}
                  {sub.granularity ? ` · ${sub.granularity}` : ""}
                </td>
                <td className={cn("px-1.5 py-1", toneText(abbrev.tone) ?? "text-text-muted")}>{abbrev.text}</td>
                <td className="px-1.5 py-1">{source?.schedule_detail.label || source?.schedule || "—"}</td>
                <td className="px-1.5 py-1 text-text-muted">{sub.scope.join(", ") || "source scope"}</td>
                <td className="px-1.5 py-1">{agoText(state?.last_ingest_at, now)}</td>
                <td className={cn("px-1.5 py-1", state?.status === "connected" && sub.active ? "text-up" : "text-warn")}>
                  {sub.active ? "" : "PAUSED · "}
                  {state?.status_label || "unknown"}
                </td>
                <td className="px-1.5 py-1 text-right">
                  <Button size="sm" onClick={() => setRemoving(sub)}>
                    Remove
                  </Button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <ConfirmDialog
        open={removing !== null}
        title="Remove subscription"
        confirmLabel="Remove"
        onCancel={() => setRemoving(null)}
        onConfirm={async () => {
          if (removing) await unsubscribe.mutateAsync(removing.id).catch(() => undefined);
          setRemoving(null);
        }}
      >
        {removing && `Stop the ${removing.channel} subscription on ${sources.find((s) => s.id === removing.source_id)?.name ?? removing.source_id}? The source keeps ingesting its own configured scope.`}
      </ConfirmDialog>
    </Panel>
  );
}

export function CatalogTab({ onRequest, onCustom }: { onRequest: () => void; onCustom: (source: Source) => void }) {
  const sources = useSources();
  const subscriptions = useSubscriptions();
  const client = useQueryClient();
  const toast = useToast();
  const [picked, setPicked] = useState<string | null>(null);
  const [pickedSub, setPickedSub] = useState<string | null>(null);
  // The backfill line lives here, so a catalog refetch does not wipe it.
  const [backfill, setBackfill] = useState<{ sourceId: string; text: string } | null>(null);

  useChannel("ingest", IngestFrame, (frame) => {
    if (frame.event.startsWith("backfill_")) {
      const f = frame as IngestFrame & { batch?: number; batches?: number; rows_written?: number; dataset?: string };
      if (backfill && f.source_id === backfill.sourceId) {
        if (f.event === "backfill_progress") {
          setBackfill({ ...backfill, text: `Batch ${f.batch ?? "?"} · ${int(f.rows_written)} rows written${f.dataset ? ` into ${f.dataset}` : ""}` });
        } else if (f.event === "backfill_finished") {
          setBackfill({
            ...backfill,
            text: f.ok
              ? `Backfill complete · ${f.batches ?? 0} batch${f.batches === 1 ? "" : "es"} · ${int(f.rows_written)} rows written`
              : `Backfill failed · ${f.error ?? ""}`,
          });
          if (f.ok) toast.ok(`Backfill finished: ${int(f.rows_written)} rows`);
          else toast.error(`Backfill failed: ${f.error ?? ""}`);
        }
      }
      if (f.event !== "backfill_finished") return;
    }
    void client.invalidateQueries({ queryKey: ingestKeys.sources });
    void client.invalidateQueries({ queryKey: ingestKeys.subscriptions });
  });

  if (sources.isError || subscriptions.isError) {
    return <p className="py-2 text-down">Source catalog: {errorReason(sources.error ?? subscriptions.error)}</p>;
  }
  const list = sources.data?.sources ?? [];
  const subs = subscriptions.data?.subscriptions ?? [];
  const source = list.find((s) => s.id === picked) ?? list[0] ?? null;
  const loading = sources.isPending || subscriptions.isPending;

  return (
    <>
      <div className="grid grid-cols-[220px_1fr_320px] gap-3 max-[1100px]:grid-cols-1">
        <Panel title="Source catalog" actions={<Badge>{loading ? "loading…" : `${list.length} ${list.length === 1 ? "source" : "sources"}`}</Badge>} bodyClassName="flex flex-col gap-1">
          <nav aria-label="Source catalog" className="flex flex-col gap-1">
            {list.map((s) => (
              <button
                key={s.id}
                type="button"
                aria-pressed={s.id === source?.id}
                onClick={() => {
                  setPicked(s.id);
                  setPickedSub(null);
                }}
                className={cn("rounded-[4px] border p-1.5 text-left", s.id === source?.id ? "border-accent bg-accent-dim" : "border-transparent hover:border-border-subtle")}
              >
                <span className="block text-[11px] text-text-strong">
                  {s.name}
                  <Pill source={s} />
                </span>
                <span className="block text-[10px] text-text-muted">
                  {s.protocol_label || s.protocol} · {s.data_domain_label}
                </span>
              </button>
            ))}
          </nav>
          <Button size="sm" className="mt-2" onClick={onRequest}>
            + Request new source
          </Button>
          <div className="mt-2 flex flex-col text-[10px] text-text-muted">
            <b className="text-text-strong">Protocol key</b>
            <span>
              <strong>WS</strong> · real-time streaming
            </span>
            <span>
              <strong>REST</strong> · request and backfill
            </span>
            <span>
              <strong>FILE / DB</strong> · batch or CDC
            </span>
            <span>
              <strong>POLL</strong> · scheduled pull
            </span>
          </div>
        </Panel>
        <Panel title="Source" accent={false} className="min-w-0">
          <div aria-live="polite">
            {loading ? (
              <p className="text-text-muted">Loading sources…</p>
            ) : source ? (
              <Detail source={source} subscriptions={subs} picked={pickedSub} onPick={setPickedSub} />
            ) : (
              <p className="text-text-subtle">No ingestion sources are registered in this build.</p>
            )}
          </div>
        </Panel>
        <Panel title="Subscribe" accent={false}>
          {source && (
            <SubscriptionPanel
              key={source.id}
              source={source}
              subscriptions={subs}
              backfillStatus={backfill?.sourceId === source.id ? backfill.text : ""}
              onBackfill={(text) => setBackfill({ sourceId: source.id, text })}
              onSubscribed={setPickedSub}
              onCustom={() => onCustom(source)}
            />
          )}
        </Panel>
      </div>
      <ActiveSubscriptions sources={list} subscriptions={subs} />
    </>
  );
}

