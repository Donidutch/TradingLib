/* What the lab keeps between tabs: the funnel filter, the compare set, which
 * editor is open and with what seed, and the live run log. The page owns it;
 * the tabs read it through this context. */
import { createContext, useContext } from "react";
import type { Experiment } from "../../api/schemas/lab";
import type { LogLine } from "./rules";

export interface ExperimentSeed {
  clone: Experiment | null;
  hypothesis: string | null;
}

export interface LabState {
  stage: string | null;
  setStage: (stage: string | null) => void;
  compare: ReadonlySet<string>;
  setCompared: (id: string, on: boolean) => void;
  comparing: boolean;
  setComparing: (on: boolean) => void;
  /** null when the experiment editor is closed. */
  draft: ExperimentSeed | null;
  setDraft: (seed: ExperimentSeed | null) => void;
  hypothesisMode: "view" | "new" | "edit";
  setHypothesisMode: (mode: "view" | "new" | "edit") => void;
  log: { experimentId: string | null; lines: LogLine[] };
  setLog: (update: (log: { experimentId: string | null; lines: LogLine[] }) => { experimentId: string | null; lines: LogLine[] }) => void;
  /** Open a tab of the lab, optionally on one entity. */
  go: (tab: string, id?: string | null) => void;
}

export const LabContext = createContext<LabState | null>(null);

export function useLab(): LabState {
  const state = useContext(LabContext);
  if (!state) throw new Error("useLab needs the Strategy Lab page");
  return state;
}
