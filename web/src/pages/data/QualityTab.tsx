/* Data Quality (COVERAGE F.5): headline cards, incidents, the named check
 * suite with SLA rows and tickets, and the outlier review queue whose
 * decisions change what every store read returns. */
import { useRef, useState, type ReactNode } from "react";
import { errorReason } from "../../api/errors";
import { QualityFrame } from "../../api/schemas/frames";
import type { Outlier } from "../../api/schemas/quality";
import { useLiveInvalidate } from "../../live/context";
import { int, num } from "../../ui/format";
import { cn } from "../../ui/lib/cn";
import { Badge } from "../../ui/primitives/badge";
import { Button } from "../../ui/primitives/button";
import { Panel } from "../../ui/primitives/card";
import { ingestKeys, useQualityActions, useQualityData } from "./ingestHooks";
import { agoText, checkRows, qualityNote, type CheckRow } from "./ingestRules";

const toneText = (t: "up" | "warn" | "down" | null | undefined) => (t === "up" ? "text-up" : t === "warn" ? "text-warn" : t === "down" ? "text-down" : "text-text-muted");
const SHOWN_OPEN = 25;
const SHOWN_DECIDED = 10;
const SHOWN_DETAIL = 30;

function Stat({ label, value, tone, children }: { label: string; value: string; tone?: "up" | "warn" | "down" | null; children?: ReactNode }) {
  return (
    <div className="rounded-[var(--ds-radius-card)] border border-border bg-surface p-3">
      <small className="text-[10px] tracking-[1px] text-text-subtle">{label}</small>
      <div className={cn("mt-1 text-[18px]", toneText(tone))}>{value}</div>
      {children}
    </div>
  );
}

type Decision = "quarantine" | "accept" | "escalate" | "open";

function OutlierRow({ row, onDecide, busy }: { row: Outlier; onDecide: (row: Outlier, d: Decision) => void; busy: boolean }) {
  const awaiting = row.needs_review;
  const label = row.label || row.dataset;
  const when = row.ts.slice(0, 10);
  // The detector's own verb for "escalate" is "human".
  const suggested = (d: Decision) => row.suggestion === d || (row.suggestion === "human" && d === "escalate");
  const decide = (d: Decision, text: string) => (
    <Button key={d} size="sm" variant={suggested(d) ? "primary" : "secondary"} disabled={busy} onClick={() => onDecide(row, d)}>
      {text}
    </Button>
  );
  const undo = (text: string) => (
    <button type="button" className="text-[10px] text-accent underline" disabled={busy} onClick={() => onDecide(row, "open")}>
      {text}
    </button>
  );
  return (
    <div className={cn("border-b border-border-subtle py-1.5 text-[11px]", !awaiting && "opacity-70", row.state === "escalated" && "border-l-2 border-l-warn pl-1.5")}>
      <b className="text-text-strong">
        {label}
        {row.value === null ? "" : ` ${num(row.value, 4)}`} · z {num(row.z, 1)}
      </b>
      {awaiting ? (
        <>
          <div className="text-text-muted">
            {when} · {row.basis} change {num(row.change, 4)} · agent suggests: {row.suggestion === "human" ? "needs human" : row.suggestion}
            {row.state === "escalated" && <span className="text-warn"> · escalated by {row.decided_by || "user"} — value still in the data, awaiting a human</span>}
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-1">
            {decide("quarantine", "Quarantine")}
            {decide("accept", "Accept value")}
            {row.state === "escalated" ? undo("clear escalation") : decide("escalate", "Escalate")}
          </div>
        </>
      ) : (
        <div className="text-text-muted">
          {when} · <span className={row.state === "quarantined" ? "text-down" : "text-up"}>{row.state} by {row.decided_by || "user"}</span> · {undo("undo")}
        </div>
      )}
    </div>
  );
}

export function QualityTab() {
  const quality = useQualityData();
  const actions = useQualityActions();
  const [detail, setDetail] = useState<CheckRow | null>(null);
  const [announcement, setAnnouncement] = useState("");
  const queueRef = useRef<HTMLDivElement>(null);
  useLiveInvalidate("quality", QualityFrame, () => [ingestKeys.quality]);

  if (quality.isPending) return <p className="py-2 text-text-muted">Loading data quality…</p>;
  if (quality.isError) return <p className="py-2 text-down">Data quality: {errorReason(quality.error)}</p>;
  const { summary, series, incidents, datasets, outliers, sla, tickets } = quality.data;

  const overall = summary.overall_quality_pct;
  const openIncidents = incidents.incidents.filter((i) => i.status === "open").length;
  const rows = checkRows(series, sla);
  const counts = outliers.counts;
  const awaiting = counts.awaiting_review;
  const open = outliers.outliers.filter((o) => o.needs_review);
  const decided = outliers.outliers.filter((o) => !o.needs_review);
  const note = qualityNote(summary, counts);
  const openTickets = tickets.tickets;

  const act = (row: CheckRow) => {
    if (row.action === "ticket" && row.sla) return actions.ticket.mutate(row.sla);
    if (row.name === "Outlier detection") {
      queueRef.current?.scrollIntoView?.({ block: "nearest" });
      return setAnnouncement("Outlier review queue focused.");
    }
    setDetail(row);
    setAnnouncement(`${row.name}: ${row.failing.length} dataset(s) failing.`);
  };

  return (
    <div className="flex flex-col gap-3">
      <div className="grid grid-cols-4 gap-3 max-[900px]:grid-cols-2">
        <Stat label="OVERALL QUALITY" value={overall === null ? "no run yet" : `${num(overall, 1)}%`} tone={overall === null ? null : overall >= 95 ? "up" : "warn"}>
          <div className="mt-1.5 h-1 rounded bg-surface-3" aria-hidden>
            <i className="block h-1 rounded bg-up" style={{ width: `${overall ?? 0}%` }} />
          </div>
        </Stat>
        <Stat label="STALE SERIES" value={int(summary.stale_series)} tone={summary.stale_series ? "warn" : "up"} />
        <Stat label="MISSING VALUES" value={`${num(summary.missing_values_pct, 2)}%`} />
        <Stat label="FAILED CHECKS" value={int(summary.failed_checks)} tone={summary.failed_checks ? "down" : "up"} />
      </div>

      <Panel title="Quality incidents" actions={<Badge tone={openIncidents ? "down" : "neutral"}>{`${openIncidents} open`}</Badge>}>
        <table className="w-full text-[11px]" aria-label="Quality incidents">
          <thead className="text-[9px] text-text-muted">
            <tr>
              {["CHECK", "DATASET", "SEVERITY", "STATUS", "ACTION"].map((h) => (
                <th key={h} className={cn("px-1.5 py-1 font-normal", h === "ACTION" ? "text-right" : "text-left")}>
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {!incidents.incidents.length && (
              <tr>
                <td colSpan={5} className="px-1.5 py-2 text-text-subtle">
                  {summary.last_run ? "No incidents recorded — every check passed on the last run." : "No checks have run yet. Run the suite below to record real results."}
                </td>
              </tr>
            )}
            {incidents.incidents.map((incident) => (
              <tr key={incident.id} className="border-t border-border-subtle">
                <td className="px-1.5 py-1">{incident.check}</td>
                <td className="px-1.5 py-1">{incident.dataset}</td>
                <td className={cn("px-1.5 py-1", incident.severity === "error" ? "text-down" : incident.severity === "warn" ? "text-warn" : "text-text-muted")}>{incident.severity}</td>
                <td className="px-1.5 py-1">{incident.status}</td>
                <td className="px-1.5 py-1 text-right">
                  {incident.status === "open" ? (
                    <Button size="sm" disabled={actions.resolve.isPending && actions.resolve.variables === incident.id} onClick={() => actions.resolve.mutate(incident.id)}>
                      Resolve
                    </Button>
                  ) : (
                    <span className="text-text-muted">—</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>

      <div className="grid grid-cols-[1.4fr_1fr] gap-3 max-[1100px]:grid-cols-1">
        <Panel title="Check suite" actions={<Badge>{summary.last_run ? `last run ${agoText(summary.last_run)}` : "never run"}</Badge>}>
          <table className="w-full text-[11px]" aria-label="Check suite">
            <thead className="text-[9px] text-text-muted">
              <tr>
                {["CHECK", "SCOPE", "RESULT", "ACTION"].map((h) => (
                  <th key={h} className={cn("px-1.5 py-1 font-normal", h === "ACTION" ? "text-right" : "text-left")}>
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {!series.datasets.length && (
                <tr>
                  <td colSpan={4} className="px-1.5 py-2 text-text-subtle">
                    No check has been recorded yet. “Run checks” runs completeness, staleness, outlier, duplicate, schema-drift and look-ahead detection over every stored dataset.
                  </td>
                </tr>
              )}
              {rows.map((row) => (
                <tr key={row.name} className="border-t border-border-subtle">
                  <td className="px-1.5 py-1">{row.name}</td>
                  <td className="px-1.5 py-1 text-text-muted">{row.scope}</td>
                  <td className={cn("px-1.5 py-1", toneText(row.tone))} title={row.title || undefined}>
                    {row.result}
                  </td>
                  <td className="px-1.5 py-1 text-right">
                    {row.action ? (
                      <Button size="sm" disabled={row.action === "ticket" && actions.ticket.isPending} onClick={() => act(row)}>
                        {row.action === "list" ? "List" : row.action === "ticket" ? "Ticket" : "Review"}
                      </Button>
                    ) : (
                      <span className="text-text-muted">—</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="mt-2.5 flex items-center gap-2">
            <Button size="sm" variant="primary" disabled={actions.run.isPending} onClick={() => actions.run.mutate()}>
              Run checks
            </Button>
            <span className="text-[11px] text-text-subtle">
              {datasets.datasets.length} dataset(s) in the store · {series.datasets.length} scored on the last run
            </span>
          </div>
          {detail && (
            <div className="mt-2 rounded-[4px] border border-border-subtle p-2 text-[11px]" aria-label="Check detail">
              <b>
                {detail.name} · {detail.failing.length} dataset(s)
              </b>
              {detail.failing.slice(0, SHOWN_DETAIL).map((f, i) => (
                <div key={`${f.label}-${i}`}>
                  {f.label} — {f.detail}
                </div>
              ))}
              {detail.failing.length > SHOWN_DETAIL && <div className="text-text-subtle">… {detail.failing.length - SHOWN_DETAIL} more</div>}
            </div>
          )}
          {openTickets.length > 0 && (
            <p className="mt-2 text-[11px] text-text-muted">
              {openTickets.length} open quality ticket(s): {openTickets.slice(0, 3).map((t) => t.title).join(" · ")}. Tickets are recorded in this workspace — no external tracker is configured.
            </p>
          )}
        </Panel>
        <div className="flex flex-col gap-3">
          <Panel
            title="Outlier review queue"
            actions={<Badge>{awaiting ? `${awaiting} open${counts.escalated ? ` · ${counts.escalated} escalated` : ""}` : "cleared"}</Badge>}
          >
            <div ref={queueRef} aria-label="Outlier review queue">
              {!outliers.outliers.length && (
                <p className="text-[11px] text-text-subtle">Nothing flagged. The outlier check populates this queue when a robust z-score exceeds the configured threshold.</p>
              )}
              {open.slice(0, SHOWN_OPEN).map((row) => (
                <OutlierRow
                  key={row.id}
                  row={row}
                  busy={actions.decide.isPending && actions.decide.variables.id === row.id}
                  onDecide={(r, decision) => actions.decide.mutate({ id: r.id, label: r.label || r.dataset, decision })}
                />
              ))}
              {open.length > SHOWN_OPEN && <p className="text-[11px] text-text-subtle">… {open.length - SHOWN_OPEN} more open flag(s)</p>}
              {decided.slice(0, SHOWN_DECIDED).map((row) => (
                <OutlierRow
                  key={row.id}
                  row={row}
                  busy={actions.decide.isPending && actions.decide.variables.id === row.id}
                  onDecide={(r, decision) => actions.decide.mutate({ id: r.id, label: r.label || r.dataset, decision })}
                />
              ))}
            </div>
          </Panel>
          <p className={cn("text-[11px]", note.warn ? "rounded-[4px] border border-warn p-2 text-warn" : "text-text-muted")}>{note.text}</p>
        </div>
      </div>
      <span className="sr-only" role="status" aria-live="polite">
        {announcement || note.text}
      </span>
    </div>
  );
}
