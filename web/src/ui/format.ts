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
