/* Display names for watch rows, ported from overview.js: "NVIDIA Corporation"
 * plus "Information Technology" reads "NVIDIA · Technology"; the full text
 * stays in the row's title. */

const NAME_NOISE =
  /(,?\s+(incorporated|corporation|company|holdings?|group|limited|inc|corp|co|plc|nv|sa|ag|llc|lp|ltd|s\.a|n\.v)\.?)+$/i;

const SECTOR_SHORT: Record<string, string> = {
  "information technology": "Technology",
  "consumer discretionary": "Consumer Disc.",
  "consumer staples": "Staples",
  "communication services": "Communications",
  "health care": "Healthcare",
  healthcare: "Healthcare",
  "real estate": "Real Estate",
  financials: "Financials",
  industrials: "Industrials",
  materials: "Materials",
  energy: "Energy",
  utilities: "Utilities",
};

export function shortName(name: string): string {
  const trimmed = name
    .replace(/^The\s+/i, "")
    .replace(NAME_NOISE, "")
    // "JPMorgan Chase & Co." leaves a dangling conjunction once "Co." goes.
    .replace(/\s*(&|and)$/i, "")
    .replace(/[\s,]+$/, "")
    .trim();
  return trimmed || name;
}

export function shortSector(sector: string): string {
  return SECTOR_SHORT[sector.trim().toLowerCase()] ?? sector;
}

/** [label, title] for a watch row; "—" when no name is known. */
export function sublabel(name: string | undefined, sector: string | undefined): [string, string] {
  if (!name) return ["—", "—"];
  if (!sector) return [shortName(name), name];
  return [`${shortName(name)} · ${shortSector(sector)}`, `${name} · ${sector}`];
}

/** BMO / AMC from the New York hour of an earnings timestamp. */
export function sessionTiming(value: string): { text: string; title: string } {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return { text: "—", title: "no timestamp" };
  const zone = { timeZone: "America/New_York", hour12: false } as const;
  const hour = Number(new Intl.DateTimeFormat("en-US", { ...zone, hour: "2-digit" }).format(date));
  const clock = new Intl.DateTimeFormat("en-US", { ...zone, hour: "2-digit", minute: "2-digit" }).format(date);
  if (hour >= 16) return { text: "AMC", title: `vendor timestamp ${clock} ET (after the close)` };
  if (hour < 9) return { text: "BMO", title: `vendor timestamp ${clock} ET (before the open)` };
  return { text: `${clock} ET`, title: "vendor timestamp during the session" };
}
