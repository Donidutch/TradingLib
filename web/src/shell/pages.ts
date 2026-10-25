/* The console's workspaces. Ported ones are routes in this app; the rest
 * still live in the vanilla console at /, reached through its hash routes. */

import { isDataTab, type DataTab } from "../pages/data/tabs";

export interface Workspace {
  key: string;
  label: string;
  glyph: string;
  /** Path in this app, for workspaces already ported. */
  path?: "/" | "/stock" | "/data" | "/docs" | "/settings";
  /** Hash in the vanilla console, for the rest. */
  legacyHash?: string;
  section: "RESEARCH" | "PLATFORM";
}

export const WORKSPACES: readonly Workspace[] = [
  { key: "overview", label: "Overview", glyph: "⌂", path: "/", section: "RESEARCH" },
  { key: "stock", label: "Stock Research", glyph: "⌕", path: "/stock", section: "RESEARCH" },
  { key: "lab", label: "Strategy Lab", glyph: "⚗", legacyHash: "#tl/lab", section: "RESEARCH" },
  { key: "agents", label: "Agent Center", glyph: "A", legacyHash: "#agents", section: "RESEARCH" },
  { key: "data", label: "Market & Data", glyph: "D", path: "/data", section: "RESEARCH" },
  { key: "docs", label: "Documentation", glyph: "?", path: "/docs", section: "PLATFORM" },
  { key: "settings", label: "Settings", glyph: "⚙", path: "/settings", section: "PLATFORM" },
];

/** Where a console hash (as /api/search returns them) leads. */
export type Target =
  | { kind: "app"; to: "/"; symbol?: string }
  | { kind: "app"; to: "/stock" }
  | { kind: "app"; to: "/stock/$symbol"; symbol: string }
  | { kind: "app"; to: "/docs" | "/settings" | "/data" }
  | { kind: "app"; to: "/data/$tab"; tab: DataTab }
  | { kind: "app"; to: "/docs/$article"; article: string }
  | { kind: "legacy"; href: string };

/**
 * Map a vanilla-console hash to this app where the page is ported:
 * "#overview" → /, "#overview/NVDA" → /?symbol=NVDA, "#stock/NVDA" →
 * /stock/NVDA, anything else stays a link
 * into the old console ("/#tl/lab/backtests?id=BT-3").
 */
export function targetForHash(hash: string): Target {
  const clean = hash.replace(/^#/, "");
  const [path = "", query] = clean.split("?", 2);
  const [key, sub] = path.split("/", 2);
  if (!query) {
    if (key === "" || key === "overview") {
      return sub ? { kind: "app", to: "/", symbol: decodeURIComponent(sub).toUpperCase() } : { kind: "app", to: "/" };
    }
    if (key === "stock" && !sub) return { kind: "app", to: "/stock" };
    if (key === "stock" && sub) return { kind: "app", to: "/stock/$symbol", symbol: decodeURIComponent(sub) };
    if (key === "settings") return { kind: "app", to: "/settings" };
    if (key === "data" && !sub) return { kind: "app", to: "/data" };
    if (key === "data" && isDataTab(sub)) return { kind: "app", to: "/data/$tab", tab: sub };
    if (key === "docs") return sub ? { kind: "app", to: "/docs/$article", article: decodeURIComponent(sub) } : { kind: "app", to: "/docs" };
  }
  return { kind: "legacy", href: `/#${clean}` };
}
