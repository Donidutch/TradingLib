/* Display formatters, ported from ui/js/components/dom.js. Unknown values
 * render as "—", never as a made-up number. */

const NY_TIME = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York",
  hour12: false,
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
});

const DASH = "—";

type Numeric = number | null | undefined;

function isNumber(value: Numeric): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

/** New York wall-clock time, "15:04:05". */
export function etClock(date: Date = new Date()): string {
  return NY_TIME.format(date);
}

/** Compact age: "12s", "4m", "3h", "2d", or "—" when unknown. */
export function ago(value: string | null | undefined, now: number = Date.now()): string {
  if (!value) return DASH;
  const time = new Date(value).getTime();
  if (Number.isNaN(time)) return DASH;
  const seconds = Math.max(0, Math.round((now - time) / 1000));
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h`;
  return `${Math.round(seconds / 86400)}d`;
}

/** Fixed-decimal number with separators. */
export function num(value: Numeric, decimals = 2): string {
  if (!isNumber(value)) return DASH;
  return value.toLocaleString("en-US", { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}

/** Integer with thousands separators. */
export function int(value: Numeric): string {
  return isNumber(value) ? Math.round(value).toLocaleString("en-US") : DASH;
}

/** Money: "$1,234.50", or compact "$1.23K". */
export function usd(value: Numeric, options: { decimals?: number; compact?: boolean } = {}): string {
  if (!isNumber(value)) return DASH;
  const sign = value < 0 ? "-$" : "$";
  const abs = Math.abs(value);
  if (options.compact) return sign + compact(abs, options.decimals);
  return sign + num(abs, options.decimals ?? 2);
}

/** 1.23e9 → "1.23B". */
export function compact(value: Numeric, decimals = 2): string {
  if (!isNumber(value)) return DASH;
  const abs = Math.abs(value);
  for (const [size, unit] of [
    [1e12, "T"],
    [1e9, "B"],
    [1e6, "M"],
    [1e3, "K"],
  ] as const) {
    if (abs >= size) return (value / size).toFixed(decimals) + unit;
  }
  return value.toFixed(decimals);
}

/** Percentage; `fraction` means 0.184 rather than 18.4, `sign` prefixes "+". */
export function pct(value: Numeric, options: { fraction?: boolean; sign?: boolean; decimals?: number } = {}): string {
  if (!isNumber(value)) return DASH;
  const n = value * (options.fraction ? 100 : 1);
  const body = `${Math.abs(n).toFixed(options.decimals ?? 1)}%`;
  if (n < 0) return `−${body}`;
  return (options.sign ? "+" : "") + body;
}

/** "up" for positive, "down" for negative, null for zero or unknown. */
export function toneFor(value: Numeric): "up" | "down" | null {
  if (!isNumber(value) || value === 0) return null;
  return value > 0 ? "up" : "down";
}

const NY_DATE = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", month: "short", day: "2-digit" });

/**
 * "Aug 12" in New York. A date-only string is a calendar day, not an instant:
 * it is anchored at UTC noon so formatting in New York keeps the same day.
 */
export function etDate(value: string | null | undefined): string {
  if (!value) return DASH;
  const date = /^\d{4}-\d{2}-\d{2}$/.test(value) ? new Date(`${value}T12:00:00Z`) : new Date(value);
  return Number.isNaN(date.getTime()) ? DASH : NY_DATE.format(date);
}

/** "HEALTHY" from "healthy", "—" when absent. */
export function statusText(value: string | null | undefined): string {
  return value ? value.replace(/_/g, " ").toUpperCase() : DASH;
}

export interface Formatted {
  text: string;
  tone: "up" | "down" | null;
  title: string;
}

/** A derived metric by its unit, with its basis (or why it is missing) as the title. */
export function metricText(
  metric: { value: number | null; unit: string; basis: string | null; reason: string | null } | null | undefined,
  options: { sign?: boolean; decimals?: number } = {},
): Formatted {
  if (!metric) return { text: DASH, tone: null, title: "not part of this payload" };
  const value = metric.value;
  if (value === null) return { text: DASH, tone: null, title: metric.reason ?? metric.basis ?? "no value" };
  let text: string;
  if (metric.unit === "usd") text = usd(value, { compact: true, decimals: 2 });
  else if (metric.unit === "ratio") text = pct(value, { fraction: true, sign: options.sign });
  else if (metric.unit === "x") text = `${num(value, 1)}×`;
  else text = num(value, options.decimals ?? 2);
  return { text, tone: options.sign ? toneFor(value) : null, title: metric.basis ?? "" };
}

export const toneText = { up: "text-up", down: "text-down" } as const;
