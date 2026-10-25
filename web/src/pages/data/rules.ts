/* Market & Data's display and editing rules, ported from data.js and kept
 * apart from the components so each is tested on its own. */
import type { MetricDef, ScreenRule } from "../../api/schemas/screens";
import type { Universe } from "../../api/schemas/universes";
import type { Watchlist } from "../../api/schemas/watchlists";

export type CollectionKind = "Universe" | "Watchlist";

export interface CollectionRow {
  kind: CollectionKind;
  id: string;
  name: string;
  construction: string;
  symbols: number;
  refresh: string;
  status: { text: string; tone: "up" | "warn" | null };
}

/** Both kinds as one list, universes first, as the old table shows them. */
export function collectionRows(
  universes: readonly Universe[],
  activeUniverse: string | null,
  watchlists: readonly Watchlist[],
  defaultWatchlist: string | null,
): CollectionRow[] {
  const rows: CollectionRow[] = universes.map((u) => {
    const count = u.member_count || u.symbol_count || 0;
    return {
      kind: "Universe",
      id: u.id,
      name: u.name,
      construction:
        u.source === "builtin-static"
          ? "Static built-in definition"
          : u.source === "wikipedia:constituents"
            ? "Published index constituents"
            : u.description || "Manual symbols",
      symbols: count,
      refresh: u.builtin ? "On demand" : "Manual",
      status:
        u.id === activeUniverse
          ? { text: "Active", tone: "up" }
          : count
            ? { text: "Ready", tone: null }
            : { text: "Not fetched", tone: "warn" },
    };
  });
  for (const w of watchlists) {
    rows.push({
      kind: "Watchlist",
      id: w.id,
      name: w.name,
      construction: "Manual symbols",
      symbols: w.symbols.length,
      refresh: "Manual",
      status: w.id === defaultWatchlist ? { text: "Default", tone: "up" } : { text: "Active", tone: null },
    });
  }
  return rows;
}

/** The as-of slider's date: mid-year for a past year, none for this year. */
export function asOfFor(year: number, now = new Date()): string | null {
  return year < now.getUTCFullYear() ? `${year}-06-30` : null;
}

/** The membership basis chip. */
export function basisLabel(basis: string): { text: string; tone: "up" | "warn" | null } {
  if (basis === "snapshot") return { text: "PIT ✓ snapshot", tone: "up" };
  if (basis === "membership-dates") return { text: "PIT · membership dates", tone: "warn" };
  if (basis === "current") return { text: "current membership", tone: null };
  return { text: basis, tone: null };
}

// ------------------------------------------------------------------ screener

export interface EditableRule {
  field: string;
  operator: string;
  value: number | string;
  value2?: number | null;
  scope?: string | null;
}

/** The rules a new screen starts with: the reference's, where this build has the metric. */
export function defaultRules(metrics: readonly MetricDef[]): EditableRule[] {
  const known = new Set(metrics.map((m) => m.key));
  const wanted: EditableRule[] = [
    { field: "mkt_cap", operator: ">", value: 2e9 },
    { field: "rev_growth", operator: ">", value: 15 },
    { field: "fcf_margin", operator: ">", value: 5 },
    { field: "country", operator: "in", value: "Any" },
    { field: "roic", operator: "top_pctl", value: 30, scope: "sector" },
    { field: "mom_12_1", operator: "top_pctl", value: 25 },
    { field: "net_debt_ebitda", operator: "<", value: 2.0 },
  ];
  return wanted.filter((r) => known.has(r.field));
}

export const isPercentile = (op: string) => op === "top_pctl" || op === "bottom_pctl";
export const isSet = (op: string) => op === "in" || op === "not_in";

export function operatorLabel(op: string): string {
  return { top_pctl: "top pctl", bottom_pctl: "bottom pctl", in: "is", not_in: "is not" }[op] ?? op;
}

/** Market cap is edited in $bn, percentiles as plain numbers. */
export function inputValue(rule: EditableRule, def: MetricDef | undefined): string {
  if (!isPercentile(rule.operator) && def?.unit === "usd" && typeof rule.value === "number") return String(rule.value / 1e9);
  return String(rule.value ?? "");
}

export function unitFor(rule: EditableRule, def: MetricDef | undefined): string {
  if (isPercentile(rule.operator)) return "%";
  return { usd: "B", pct: "%", x: "×" }[def?.unit ?? ""] ?? "";
}

/** A new field resets the operator to one that fits the metric. */
export function withField(rule: EditableRule, field: string, def: MetricDef | undefined): EditableRule {
  if (def?.categorical) return { field, operator: "in", value: "Any" };
  if (isSet(rule.operator)) return { field, operator: ">", value: 0 };
  return { ...rule, field };
}

export function withOperator(rule: EditableRule, operator: string): EditableRule {
  if (isSet(operator)) return { ...rule, operator, value: "Any" };
  if (isPercentile(operator)) return { ...rule, operator, value: 25, scope: rule.scope ?? "all" };
  return { ...rule, operator, value: typeof rule.value === "number" ? rule.value : 0 };
}

/** The typed value back into the rule ($bn → dollars; numbers stay numbers). */
export function withValue(rule: EditableRule, text: string, def: MetricDef | undefined): EditableRule {
  if (isSet(rule.operator)) return { ...rule, value: text };
  let value = Number(text);
  if (Number.isNaN(value)) value = 0;
  if (def?.unit === "usd" && !isPercentile(rule.operator)) value *= 1e9;
  return { ...rule, value };
}

/** Rules as the API takes them. */
export function apiRules(rules: readonly EditableRule[]): ScreenRule[] {
  return rules.map((r) => ({
    field: r.field,
    operator: r.operator,
    value: r.value,
    ...(r.value2 != null ? { value2: r.value2 } : {}),
    ...(r.scope ? { scope: r.scope } : {}),
  }));
}

/** A saved screen's rules, for editing. */
export function editableRules(rules: readonly ScreenRule[]): EditableRule[] {
  return rules.map((r) => ({
    field: r.field,
    operator: r.operator,
    value: typeof r.value === "number" || typeof r.value === "string" ? r.value : Array.isArray(r.value) ? r.value.join(", ") : "Any",
    value2: r.value2 ?? null,
    scope: r.scope ?? null,
  }));
}

/** Survivors best first by the rank metric; missing values last. */
export function ranked(survivors: readonly Record<string, unknown>[], rankBy: string): Record<string, unknown>[] {
  const value = (row: Record<string, unknown>) => {
    const n = Number(row[rankBy]);
    return Number.isFinite(n) ? n : null;
  };
  return [...survivors].sort((a, b) => {
    const x = value(a);
    const y = value(b);
    if (x === null && y === null) return 0;
    if (x === null) return 1;
    if (y === null) return -1;
    return y - x;
  });
}
