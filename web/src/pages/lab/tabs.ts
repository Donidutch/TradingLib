/* The Strategy Lab's tabs, shared by the page, the router and the hash map.
 * Backtests, Validation, Evaluation and Pipeline arrive in S5 and S6. */
export const LAB_TABS = [
  { value: "hypotheses", label: "HYPOTHESES", ported: true },
  { value: "experiments", label: "EXPERIMENTS", ported: true },
  { value: "backtests", label: "BACKTESTS", ported: false },
  { value: "validation", label: "VALIDATION", ported: false },
  { value: "evaluation", label: "EVALUATION", ported: false },
  { value: "pipeline", label: "PIPELINE", ported: false },
] as const;

export type LabTab = (typeof LAB_TABS)[number]["value"];
export type PortedLabTab = "hypotheses" | "experiments";

export const isLabTab = (value: string | undefined): value is LabTab => LAB_TABS.some((t) => t.value === value);
export const isPortedLabTab = (value: string | undefined): value is PortedLabTab => value === "hypotheses" || value === "experiments";

/** The current console's address for a lab tab, optionally on one entity. */
export const legacyLab = (tab: string, id?: string | null) => `/#tl/lab/${tab}${id ? `?id=${encodeURIComponent(id)}` : ""}`;
