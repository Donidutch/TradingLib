/* Small building blocks the Stock Research tabs share. */
import type { ReactNode } from "react";
import { metricText, toneText, type Formatted } from "../../ui/format";
import { cn } from "../../ui/lib/cn";
import type { Metric } from "../../api/schemas/common";

type Tone = "up" | "down" | "warn" | null | undefined;

const toneClass = (tone: Tone) =>
  tone === "warn" ? "text-warn" : tone ? toneText[tone] : undefined;

/** A label-over-value cell in a metric grid. */
export function Cell({ label, value, tone, title }: { label: string; value: ReactNode; tone?: Tone; title?: string }) {
  return (
    <div className="border-t border-border p-2.5">
      <dt className="text-[9px] text-text-muted">{label}</dt>
      <dd className={cn("mt-1 font-bold", toneClass(tone))} title={title}>
        {value}
      </dd>
    </div>
  );
}

/** A metric cell straight from a derived metric (basis or reason in the title). */
export function MetricCell({ label, metric, sign }: { label: string; metric: Metric | undefined; sign?: boolean }) {
  const out: Formatted = metricText(metric, { sign });
  return <Cell label={label} value={out.text} tone={out.tone} title={out.title} />;
}

/** A label-and-value row. */
export function InfoRow({ label, value, tone, title }: { label: ReactNode; value: ReactNode; tone?: Tone; title?: string }) {
  return (
    <div className="flex justify-between gap-3 border-b border-border px-2.5 py-2 text-[10px] last:border-b-0">
      <dt>{label}</dt>
      <dd className={cn("text-right font-bold", toneClass(tone))} title={title}>
        {value}
      </dd>
    </div>
  );
}

/** A line of text in a panel: loading, empty or (with `error`) a failure. */
export function Note({ children, error }: { children: ReactNode; error?: boolean }) {
  return <p className={cn("px-2.5 py-2.5", error ? "text-down" : "text-text-muted")}>{children}</p>;
}

/** A titled list row: bold lead, muted detail. */
export function ListRow({ lead, detail, detailTone, title }: { lead: ReactNode; detail?: ReactNode; detailTone?: Tone; title?: string }) {
  return (
    <div className="border-t border-border px-2.5 py-2 first:border-t-0" title={title}>
      <b className="block font-medium text-text-strong">{lead}</b>
      {detail != null && <span className={cn("text-[10px] text-text-muted", toneClass(detailTone))}>{detail}</span>}
    </div>
  );
}
