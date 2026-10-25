/* Universes & Watchlists (COVERAGE F.1): the collections table, point-in-time
 * membership with an as-of slider, the snapshot change log and coverage. */
import { useState } from "react";
import { errorReason } from "../../api/errors";
import type { Universe } from "../../api/schemas/universes";
import type { Watchlist } from "../../api/schemas/watchlists";
import { cn } from "../../ui/lib/cn";
import { Badge } from "../../ui/primitives/badge";
import { Button } from "../../ui/primitives/button";
import { Panel } from "../../ui/primitives/card";
import { ConfirmDialog } from "../../ui/primitives/confirm";
import { useChangelog, useCollectionActions, useCollections, useCoverage, useMembership } from "./hooks";
import { asOfFor, basisLabel, collectionRows, type CollectionKind } from "./rules";

const FIRST_YEAR = 2016;
const SHOWN_MEMBERS = 200;
const tone = (t: "up" | "warn" | null | undefined) => (t === "up" ? "text-up" : t === "warn" ? "text-warn" : undefined);

interface Selected {
  kind: CollectionKind;
  id: string;
}

function ruleText(kind: CollectionKind, record: Universe | Watchlist | undefined): string {
  if (!record) return "";
  const text = (value: unknown) => (typeof value === "string" && value ? value : null);
  const rule = text(record.rule);
  const description = text(record.description);
  return rule || description || (kind === "Watchlist" ? "Manual watchlist · no automatic refresh" : "No construction rule recorded");
}

function Membership({ selected, record, year }: { selected: Selected; record: Universe | Watchlist | undefined; year: number }) {
  const asOf = selected.kind === "Universe" ? asOfFor(year) : null;
  const membership = useMembership(selected.kind, selected.id, asOf);
  const name = record?.name ?? selected.id;
  if (membership.isPending) return <p className="py-2 text-text-muted">Loading members…</p>;
  if (membership.isError) return <p className="py-2 text-down">{errorReason(membership.error)}</p>;
  const data = membership.data;
  const members =
    data.kind === "Universe"
      ? data.members
      : data.watchlist.symbols.map((symbol) => ({ symbol, name: null, added_on: null, removed_on: null }));
  const basis = basisLabel(data.kind === "Universe" ? data.basis : "manual list");
  const gone = members.filter((m) => m.removed_on);
  const builtin = record && "builtin" in record && record.builtin;
  return (
    <>
      <div className="flex flex-wrap items-center gap-2.5">
        <span className="font-semibold text-text-strong">
          {name} · membership @ {asOf ?? "today"}
        </span>
        <Badge tone={basis.tone ?? "neutral"}>{basis.text}</Badge>
        <span className="ml-auto text-text-subtle">
          {members.length} {members.length === 1 ? "name" : "names"}
        </span>
      </div>
      <table className="mt-2.5 w-full" aria-label="Members">
        <thead className="text-[9px] text-text-muted">
          <tr>
            {["TICKER", "NAME", "IN COLLECTION", "STATUS TODAY"].map((h) => (
              <th key={h} className="px-1.5 py-1 text-left font-normal">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {!members.length && (
            <tr>
              <td colSpan={4} className="px-1.5 py-2 text-text-subtle">
                {data.kind === "Universe" && builtin ? "No members stored yet — use “Refresh members” to fetch the constituents." : "No members at this as-of date."}
              </td>
            </tr>
          )}
          {members.slice(0, SHOWN_MEMBERS).map((m) => (
            <tr key={`${m.symbol}-${m.added_on ?? ""}`} className="border-t border-border-subtle">
              <td className="px-1.5 py-1 text-text-strong">{m.symbol}</td>
              <td className="px-1.5 py-1">{m.name ?? "—"}</td>
              <td className="px-1.5 py-1">
                {m.added_on ?? "—"} → {m.removed_on ?? "now"}
              </td>
              <td className={cn("px-1.5 py-1", m.removed_on ? "text-down" : "text-up")}>{m.removed_on ? `REMOVED ${m.removed_on}` : "ACTIVE"}</td>
            </tr>
          ))}
          {members.length > SHOWN_MEMBERS && (
            <tr>
              <td colSpan={4} className="px-1.5 py-1 text-text-subtle">
                … {members.length - SHOWN_MEMBERS} more
              </td>
            </tr>
          )}
        </tbody>
      </table>
      {/* Announced when the selection or the as-of date changes, as the old page did. */}
      <span className="sr-only" role="status" aria-live="polite">
        {name}, {members.length} names as of {asOf ?? "today"}
      </span>
      <p className="mt-2.5 text-[11px] text-text-muted">
        {gone.length
          ? `✓ ${gone.length} former member(s) are kept with their [added, removed] window, so a backtest over this period includes them while they were members — survivorship bias is guarded by construction.`
          : "No member has left this collection, so there is nothing to reconstruct for survivorship. Membership dates are stored for every name that has them."}
      </p>
    </>
  );
}

function ChangeLog({ id, rule }: { id: string; rule: string }) {
  const log = useChangelog(id, true);
  if (log.isPending) return <p className="text-text-subtle">Loading…</p>;
  if (log.isError) return <p className="text-down">{errorReason(log.error)}</p>;
  return (
    <>
      {!log.data.entries.length && <p className="text-[11px] text-text-subtle">No snapshot recorded yet. “Freeze snapshot” stores today's membership as point-in-time truth.</p>}
      {log.data.entries.map((entry) => {
        const attribution = entry.rule ? `rule: ${entry.rule}` : `source: ${entry.source ?? "manual"}`;
        const added = entry.added_by_rule ?? entry.added;
        const dropped = entry.dropped_by_rule ?? entry.dropped;
        return (
          <div key={entry.as_of} className="mb-1.5 text-[11px]" data-entry={entry.as_of}>
            <div>
              <span className="text-accent">{entry.as_of}</span> {entry.symbol_count} symbols
              {entry.net ? <span className={entry.net > 0 ? "text-up" : "text-down"}> {entry.net > 0 ? "+" : ""}{entry.net}</span> : null}
              <span className="text-text-subtle"> ({entry.source ?? "manual"})</span>
            </div>
            {entry.baseline ? (
              <div className="text-text-subtle">baseline snapshot · no predecessor to diff against</div>
            ) : (
              <>
                {added.length > 0 && (
                  <div>
                    <span className="text-up">+ IN</span> {added.join(", ")} <span className="text-text-subtle">({attribution})</span>
                  </div>
                )}
                {dropped.length > 0 && (
                  <div>
                    <span className="text-down">− OUT</span> {dropped.join(", ")} <span className="text-text-subtle">({attribution})</span>
                  </div>
                )}
                {entry.corporate_actions.map((a) => (
                  <div key={`${a.from}-${a.to}`} title={a.detail}>
                    <span className="text-warn">Δ CORP</span> {a.from}/{a.to} handled
                  </div>
                ))}
                {entry.rule_changed && (
                  <div>
                    <span className="text-warn">Δ RULE</span> construction rule changed at this snapshot
                  </div>
                )}
                {!added.length && !dropped.length && !entry.corporate_actions.length && <div className="text-text-subtle">no membership change</div>}
              </>
            )}
          </div>
        );
      })}
      <p className="mt-1.5 border-t border-border-subtle pt-1.5 text-[11px] text-text-subtle">{log.data.rule_version ? `Rule def v${log.data.rule_version} · ${rule}` : rule}</p>
    </>
  );
}

function Coverage({ id, year }: { id: string; year: number }) {
  const asOf = asOfFor(year);
  const coverage = useCoverage(id, asOf, true);
  if (coverage.isPending) return <p className="text-[11px] text-text-subtle">measuring…</p>;
  if (coverage.isError) return <p className="text-[11px] text-down">{errorReason(coverage.error)}</p>;
  const c = coverage.data.preview.coverage?.coverage ?? {};
  const symbols = (coverage.data.preview.symbols ?? []).map((s) => (typeof s === "string" ? s : s.symbol).toUpperCase());
  const withIv = coverage.data.datasets
    .filter((d) => d.namespace === "options" && /^iv\d+_/.test(d.name ?? ""))
    .map((d) => (d.name ?? "").split("_").slice(1).join("_").toUpperCase());
  const ivCovered = symbols.length ? symbols.filter((s) => withIv.includes(s)).length : withIv.length;
  const rows: [string, number | null | undefined, number | null | undefined][] = [
    ["Requested", c.requested, c.requested],
    ["Evaluated", c.evaluated, c.requested],
    ["Priced", c.priced, c.requested],
    ["Fundamentals", c.with_fundamentals, c.requested],
    ["Options IV", ivCovered, symbols.length || c.requested],
  ];
  return (
    <>
      <dl>
        {rows
          .filter(([, value]) => value != null)
          .map(([label, value, total]) => (
            <div key={label} className="flex justify-between text-[11px]">
              <dt>{label}</dt>
              <dd className={total && (value ?? 0) < total ? "text-warn" : "text-up"}>
                {value}
                {total ? `/${total}` : ""}
              </dd>
            </div>
          ))}
      </dl>
      <p className="mt-1.5 border-t border-border-subtle pt-1.5 text-[11px] text-text-subtle">
        {c.price_source ? `Source: ${c.price_source}${c.truncated ? ` · truncated at ${c.max_symbols} symbols` : ""}` : "Measured by the screener's metric panel."} Options IV counts members
        with an ingested options/iv30 series{withIv.length ? "" : " — subscribe the options-iv source to populate it."}.
      </p>
    </>
  );
}

export function CollectionsTab({ onEdit }: { onEdit: (kind: CollectionKind, record: Universe | Watchlist) => void }) {
  const collections = useCollections();
  const actions = useCollectionActions();
  const [selected, setSelected] = useState<Selected | null>(null);
  const [year, setYear] = useState(new Date().getUTCFullYear());
  const [pendingYear, setPendingYear] = useState<number | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);

  const data = collections.data;
  const rows = data ? collectionRows(data.universes.universes, data.universes.active, data.watchlists.watchlists, data.watchlists.default) : [];
  const current = (selected && rows.find((r) => r.kind === selected.kind && r.id === selected.id)) ?? rows[0];
  const record =
    current && data
      ? current.kind === "Universe"
        ? data.universes.universes.find((u) => u.id === current.id)
        : data.watchlists.watchlists.find((w) => w.id === current.id)
      : undefined;
  const isUniverse = current?.kind === "Universe";
  const universe = isUniverse ? (record as Universe | undefined) : undefined;
  const thisYear = new Date().getUTCFullYear();

  return (
    <>
      <div className="grid grid-cols-[1fr_300px] gap-[11px]">
        <Panel
          title="Universes & watchlists"
          accent={false}
          bodyClassName="p-0"
          actions={<Badge tone={collections.isError ? "down" : "neutral"}>{collections.isError ? "failed" : data ? `${rows.length} ${rows.length === 1 ? "collection" : "collections"}` : "loading…"}</Badge>}
        >
          <table className="w-full" aria-label="Collections">
            <thead className="text-[9px] text-text-muted">
              <tr>
                {["Name", "Type", "Construction", "Symbols", "Refresh", "Status"].map((h) => (
                  <th key={h} className={cn("px-2.5 py-1.5 font-normal", h === "Symbols" ? "text-right" : "text-left")}>
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {collections.isError && (
                <tr>
                  <td colSpan={6} className="px-2.5 py-2 text-down">
                    {errorReason(collections.error)}
                  </td>
                </tr>
              )}
              {data && !rows.length && (
                <tr>
                  <td colSpan={6} className="px-2.5 py-2 text-text-muted">
                    No universes or watchlists yet — create one with “+ New universe”.
                  </td>
                </tr>
              )}
              {rows.map((row) => {
                const isCurrent = current?.kind === row.kind && current.id === row.id;
                const choose = () => setSelected({ kind: row.kind, id: row.id });
                return (
                  <tr
                    key={`${row.kind}-${row.id}`}
                    tabIndex={0}
                    aria-selected={isCurrent}
                    onClick={choose}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        choose();
                      }
                    }}
                    className={cn("cursor-pointer border-t border-border hover:bg-surface-2", isCurrent && "bg-active")}
                  >
                    <td className="px-2.5 py-1.5">{row.name}</td>
                    <td className="px-2.5 py-1.5">
                      <Badge>{row.kind}</Badge>
                    </td>
                    <td className="px-2.5 py-1.5">{row.construction}</td>
                    <td className="mono px-2.5 py-1.5 text-right">{row.symbols}</td>
                    <td className="px-2.5 py-1.5">{row.refresh}</td>
                    <td className={cn("px-2.5 py-1.5", tone(row.status.tone))}>{row.status.text}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </Panel>
        <Panel title="How collections work" accent={false} bodyClassName="p-0">
          {[
            ["Universes", "Reusable research populations built statically, from screens, or from rules. They can refresh automatically and feed agents, experiments, and backtests."],
            ["Watchlists", "Focused monitoring lists. They can be manual or derived from a universe, screen, earnings calendar, or agent findings."],
            ["One selector everywhere", "The global selector can switch between either type while retaining the collection definition."],
          ].map(([lead, text]) => (
            <div key={lead} className="border-t border-border px-2.5 py-2 first:border-t-0">
              <b className="block font-medium text-text-strong">{lead}</b>
              <span className="text-text-muted">{text}</span>
            </div>
          ))}
        </Panel>
      </div>

      {current && (
        <section className="mt-[11px]" aria-label="Point-in-time membership">
          <h2 className="mb-2 text-[10px] tracking-[1px] text-text-subtle">POINT-IN-TIME MEMBERSHIP</h2>
          <div className="grid grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)] gap-[11px]">
            <div data-panel="" className="rounded-[var(--ds-radius-card)] border border-border bg-surface p-[var(--ds-panel-pad)]">
              {isUniverse && (
                <label className="mb-2.5 flex items-center gap-2.5 text-[10px] text-text-subtle">
                  {FIRST_YEAR}
                  <input
                    type="range"
                    min={FIRST_YEAR}
                    max={thisYear}
                    aria-label="As-of year"
                    value={pendingYear ?? year}
                    onChange={(e) => setPendingYear(Number(e.target.value))}
                    // Load on release, as the old page did, not on every step.
                    onPointerUp={() => pendingYear !== null && (setYear(pendingYear), setPendingYear(null))}
                    onKeyUp={() => pendingYear !== null && (setYear(pendingYear), setPendingYear(null))}
                  />
                  today
                  {pendingYear !== null && <span>{pendingYear}-06-30</span>}
                </label>
              )}
              <Membership key={`${current.kind}-${current.id}`} selected={current} record={record} year={year} />
              {record && (
                <div className="mt-2.5 flex flex-wrap gap-1.5">
                  {isUniverse && universe && current.id !== data?.universes.active && (
                    <Button size="sm" variant="primary" disabled={actions.setActive.isPending} onClick={() => actions.setActive.mutate({ id: current.id, name: universe.name })}>
                      Set active
                    </Button>
                  )}
                  {isUniverse && universe?.builtin && (
                    <Button size="sm" disabled={actions.refresh.isPending} onClick={() => actions.refresh.mutate({ id: current.id, name: universe.name })}>
                      Refresh members
                    </Button>
                  )}
                  {isUniverse && (
                    <Button size="sm" disabled={actions.snapshot.isPending} onClick={() => actions.snapshot.mutate({ id: current.id })}>
                      Freeze snapshot
                    </Button>
                  )}
                  <Button size="sm" onClick={() => onEdit(current.kind, record)}>
                    Edit
                  </Button>
                  {!(universe?.builtin ?? false) && (
                    <Button size="sm" variant="danger" onClick={() => setConfirmDelete(true)}>
                      Delete
                    </Button>
                  )}
                </div>
              )}
            </div>
            <div className="flex flex-col gap-3">
              <Panel title="Change log · snapshots" accent={false}>
                {isUniverse ? <ChangeLog id={current.id} rule={ruleText(current.kind, record)} /> : (
                  <>
                    <p className="text-[11px] text-text-subtle">Watchlists carry no snapshot history.</p>
                    <p className="mt-1.5 border-t border-border-subtle pt-1.5 text-[11px] text-text-subtle">{ruleText(current.kind, record)}</p>
                  </>
                )}
              </Panel>
              <Panel title="Coverage @ as-of date" accent={false}>
                {isUniverse ? <Coverage id={current.id} year={year} /> : <p className="text-[11px] text-text-subtle">Coverage is computed for universes.</p>}
              </Panel>
            </div>
          </div>
        </section>
      )}

      <ConfirmDialog
        open={confirmDelete && Boolean(record)}
        title={`Delete ${current?.kind.toLowerCase()} “${record?.name}”?`}
        confirmLabel="Delete"
        onCancel={() => setConfirmDelete(false)}
        onConfirm={async () => {
          if (!current || !record) return;
          await actions.remove.mutateAsync({ kind: current.kind, id: current.id, name: record.name }).catch(() => undefined);
          setConfirmDelete(false);
          setSelected(null);
        }}
      >
        {current?.kind === "Universe" ? "Its members, snapshots and change log go with it." : "Its symbols go with it."} This cannot be undone.
      </ConfirmDialog>
    </>
  );
}
