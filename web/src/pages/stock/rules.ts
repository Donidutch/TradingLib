/* Stock Research's formatting rules, ported from stock.js and kept apart
 * from the components so each one is tested on its own. */
import type { Company } from "../../api/schemas/research";
import type { AltData } from "../../api/schemas/research";
import { ago, int, num, pct, toneFor } from "../../ui/format";

/** The symbol /app/stock opens with, as the old page did. */
export const DEFAULT_SYMBOL = "GRAB";

/** The server's own reason for a failure (shared with every page). */
export { errorReason as reason } from "../../api/errors";

/** Figures in millions, the unit the financials table uses. */
export function millions(value: number | null | undefined): string {
  return value == null || !Number.isFinite(value) ? "—" : int(value / 1e6);
}

/** DATA FRESHNESS: CURRENT unless a component is stale; each age in the title. */
export function freshnessSummary(freshness: Company["freshness"] | undefined): { text: string; tone?: "up" | "warn"; title: string } {
  const entries = Object.entries(freshness ?? {});
  if (!entries.length) return { text: "—", title: "no freshness report" };
  const stale = entries.some(([, report]) => report.stale);
  return {
    text: stale ? "STALE" : "CURRENT",
    tone: stale ? "warn" : "up",
    title: entries.map(([key, report]) => `${key} ${ago(report.fetched_at)} (${report.source})`).join(" · "),
  };
}

type Signal = AltData["signals"][string];

/** How a signal was derived, then its units and source. */
export function signalTitle(signal: Signal | undefined): string {
  if (!signal) return "";
  const label = typeof signal.label === "string" ? signal.label : null;
  return [
    signal.basis,
    signal.reason,
    signal.unit,
    label,
    signal.source ? `source ${signal.source}` : null,
    signal.as_of ? `as of ${signal.as_of.slice(0, 10)}` : null,
  ]
    .filter(Boolean)
    .join(" · ");
}

export interface SignalStyle {
  decimals?: number;
  /** A rank move: "↑ 4". */
  arrow?: boolean;
  /** Prefix "+" and tone by sign. */
  sign?: boolean;
  /** A percentage; `fraction` when it arrives as 0.118. */
  percent?: boolean;
  fraction?: boolean;
  /** A level, not a change: no sign, no tone. */
  level?: boolean;
}

/** A signal's display value and tone, or "—" with why. */
export function signalText(signal: Signal | undefined, style: SignalStyle = {}): { text: string; tone: "up" | "down" | null; title: string } {
  if (!signal || signal.value == null) {
    return { text: "—", tone: null, title: signalTitle(signal) || "no source configured" };
  }
  const n = signal.value;
  let text: string;
  if (style.arrow) text = `${n > 0 ? "↑ " : n < 0 ? "↓ " : "· "}${num(Math.abs(n), style.decimals ?? 0)}`;
  else if (style.percent) text = pct(n, { sign: !style.level, fraction: style.fraction });
  else text = `${style.sign && n > 0 ? "+" : ""}${num(n, style.decimals ?? 1)}`;
  const change = typeof signal.change_90d_pct === "number" ? `90d ${pct(signal.change_90d_pct, { sign: true })}` : null;
  const toned = (style.sign || style.arrow || style.percent) && !style.level;
  return { text, tone: toned ? toneFor(n) : null, title: [signalTitle(signal), change].filter(Boolean).join(" · ") };
}

/** Bar heights (% of the largest absolute figure, at least 1%) for the revenue/EBITDA chart. */
export function barHeights(rows: readonly { revenue: number | null; ebitda: number | null }[]): { revenue: number | null; ebitda: number | null }[] {
  const max = Math.max(0, ...rows.flatMap((r) => [Math.abs(r.revenue ?? 0), Math.abs(r.ebitda ?? 0)]));
  const scale = (v: number | null) => (v === null ? null : max ? Math.max(1, (Math.abs(v) / max) * 100) : 0);
  return rows.map((r) => ({ revenue: scale(r.revenue), ebitda: scale(r.ebitda) }));
}

/** "positive" → up, "negative" → down. */
export const sentimentTone = (label: string | null | undefined): "up" | "down" | null =>
  label === "positive" ? "up" : label === "negative" ? "down" : null;
