/* The Strategy Lab, ported from ui/js/pages/lab.js (map: docs/UI_PORT.md, S4):
 * header, five-card funnel and tab bar as /lab/<tab>?id=, with Hypotheses and
 * Experiments here. The other four tabs open in the current console until S5
 * and S6. Live `lab` frames stream the run log and refetch what they touch. */
import { useQueryClient, type QueryKey } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { useMemo, useState } from "react";
import { useActiveScope } from "../../api/hooks";
import { LabFrame } from "../../api/schemas/frames";
import { useChannel } from "../../live/context";
import { openLegacy } from "../../shell/legacy";
import { cn } from "../../ui/lib/cn";
import { Button } from "../../ui/primitives/button";
import { ExperimentsTab } from "./ExperimentsTab";
import { HypothesesTab } from "./HypothesesTab";
import { afterRun, labKeys, useLabData } from "./hooks";
import { appendLog, funnelCards } from "./rules";
import { LabContext, type ExperimentSeed, type LabState } from "./state";
import { LAB_TABS, legacyLab, type PortedLabTab } from "./tabs";

function useLiveLab(setLog: LabState["setLog"]) {
  const client = useQueryClient();
  const refetch = (list: readonly QueryKey[]) => list.forEach((queryKey) => void client.invalidateQueries({ queryKey }));
  useChannel("lab", LabFrame, (frame) => {
    const f = frame as LabFrame & { level?: string; message?: string; passed_gates?: boolean };
    if (f.kind === "experiment") {
      if (f.phase === "started") setLog(() => ({ experimentId: f.experiment_id ?? null, lines: appendLog([], { ts: f.ts, message: `run ${f.id ?? ""} started` }) }));
      else if (f.phase === "log") setLog((log) => ({ experimentId: log.experimentId ?? f.experiment_id ?? null, lines: appendLog(log.lines, f) }));
      else if (f.phase === "complete") {
        setLog((log) => ({ ...log, lines: appendLog(log.lines, { ts: f.ts, message: `run complete · acceptance ${f.passed_gates ? "passed" : "not met"}` }) }));
        refetch(afterRun);
      } else if (f.phase === "failed") {
        setLog((log) => ({ ...log, lines: appendLog(log.lines, { ts: f.ts, level: "error", message: f.error || "run failed" }) }));
        refetch([labKeys.summary, labKeys.experiments, ["lab", "backtests"], ["lab", "experiment"]]);
      }
    } else if (f.kind === "backtest" && (f.phase === "complete" || f.phase === "failed")) refetch([labKeys.summary, ["lab", "backtests"], labKeys.evaluation]);
    else if (f.kind === "validation") refetch([labKeys.reports, labKeys.evaluation]);
    else if (f.kind === "promotion") refetch([labKeys.strategies, labKeys.experiments, labKeys.reports, labKeys.evaluation, labKeys.summary]);
    else if (f.kind === "analysis" && (f.phase === "complete" || f.phase === "failed")) refetch([labKeys.analyses]);
  });
}

export function LabPage({ tab, id }: { tab: PortedLabTab; id: string | undefined }) {
  const navigate = useNavigate();
  const data = useLabData();
  const scope = useActiveScope();
  const [stage, setStage] = useState<string | null>(null);
  const [compare, setCompare] = useState<ReadonlySet<string>>(new Set());
  const [comparing, setComparing] = useState(false);
  const [draft, setDraft] = useState<ExperimentSeed | null>(null);
  const [hypothesisMode, setHypothesisMode] = useState<"view" | "new" | "edit">("view");
  const [log, setLog] = useState<LabState["log"]>({ experimentId: null, lines: [] });
  useLiveLab(setLog);

  const state = useMemo<LabState>(
    () => ({
      stage,
      setStage,
      compare,
      setCompared: (key, on) =>
        setCompare((current) => {
          const next = new Set(current);
          if (on) next.add(key);
          else next.delete(key);
          return next;
        }),
      comparing,
      setComparing,
      draft,
      setDraft,
      hypothesisMode,
      setHypothesisMode,
      log,
      setLog,
      go: (next, entity) => {
        const target = LAB_TABS.find((t) => t.value === next);
        if (!target) return;
        if (!target.ported) return openLegacy(legacyLab(next, entity));
        void navigate({ to: "/lab/$tab", params: { tab: next }, search: entity ? { id: entity } : {} });
      },
    }),
    [stage, compare, comparing, draft, hypothesisMode, log, navigate],
  );

  const summary = data.summary.data;
  const experiments = data.experiments.data?.experiments ?? [];
  const hypotheses = data.hypotheses.data?.hypotheses ?? [];
  const counts: Record<string, number | null> = {
    hypotheses: summary?.hypotheses.total ?? hypotheses.length,
    experiments: summary?.experiments ?? experiments.length,
    backtests: summary?.backtests ?? data.backtests.data?.backtests.length ?? 0,
    validation: data.reports.data?.reports.length ?? 0,
    evaluation: null,
    pipeline: data.strategies.data?.strategies.length ?? 0,
  };
  const cards = funnelCards(data.evaluation.data?.funnel?.stages ?? null, experiments);

  const selected = (() => {
    if (tab === "experiments") {
      if (draft) return "new experiment";
      if (comparing) return `comparing ${compare.size} experiments`;
      const exp = experiments.find((e) => e.id === id) ?? (id ? undefined : experiments[0]);
      return exp ? exp.name : "all experiments";
    }
    if (hypothesisMode === "new") return "new hypothesis";
    const hypothesis = hypotheses.find((h) => h.id === id) ?? (id ? undefined : hypotheses[0]);
    return hypothesis ? `${hypothesis.id} · ${hypothesis.title}` : "all hypotheses";
  })();
  // The active universe, as the old page reads it (null while a watchlist is active).
  const scopeName = scope.data?.universe?.name ?? scope.data?.universe?.id;

  return (
    <LabContext.Provider value={state}>
      <div>
        <header className="flex flex-wrap items-center gap-2.5">
          <span className="text-[14px] text-text-strong">&gt; STRATEGY LAB</span>
          <span className="text-text-subtle" aria-label="Breadcrumb">
            / {[selected, scopeName].filter(Boolean).join(" · ")}
          </span>
          <div className="ml-auto flex gap-1.5">
            <Button
              size="sm"
              onClick={() => {
                setComparing(false);
                setDraft({ clone: null, hypothesis: null });
                state.go("experiments");
              }}
            >
              + NEW EXPERIMENT
            </Button>
            <Button
              size="sm"
              onClick={() => {
                setHypothesisMode("new");
                state.go("hypotheses");
              }}
            >
              + NEW HYPOTHESIS
            </Button>
          </div>
        </header>

        <div className="mt-3 grid grid-cols-5 gap-2 max-[900px]:grid-cols-2" role="group" aria-label="Funnel">
          {cards.map((card) => (
            <button
              key={card.stage}
              type="button"
              aria-pressed={stage === card.stage}
              onClick={() => {
                setStage(stage === card.stage ? null : card.stage);
                setComparing(false);
                setDraft(null);
                if (tab !== "experiments") state.go("experiments");
              }}
              className={cn(
                "rounded-[var(--ds-radius-card)] border bg-surface p-2.5 text-left",
                stage === card.stage ? "border-accent" : "border-border hover:border-border-strong",
              )}
            >
              <small className="block text-[9px] tracking-[1px] text-text-subtle">{card.stage.toUpperCase()}</small>
              <span className={cn("block text-[20px]", card.count ? "text-text-strong" : "text-text-muted")}>{card.count}</span>
              <span className="block text-[10px] text-text-muted">{card.caption}</span>
            </button>
          ))}
        </div>

        <div role="tablist" aria-label="Strategy Lab" className="mt-3 mb-3 flex flex-wrap gap-1 border-b border-border-subtle">
          {LAB_TABS.map((t) => (
            <button
              key={t.value}
              type="button"
              role="tab"
              aria-selected={t.value === tab}
              onClick={() => state.go(t.value)}
              className={cn("-mb-px border-b-2 px-3 py-2 text-[12px]", t.value === tab ? "border-accent text-text" : "border-transparent text-text-muted hover:text-text")}
            >
              {t.label}
              {counts[t.value] !== null && counts[t.value] !== undefined && <>{" "}<span className="text-text-subtle">{counts[t.value]}</span></>}
              {!t.ported && " ↗"}
            </button>
          ))}
        </div>

        <div role="tabpanel" aria-label={tab === "hypotheses" ? "Hypotheses" : "Experiments"}>
          {tab === "hypotheses" ? <HypothesesTab id={id} /> : <ExperimentsTab id={id} />}
        </div>
      </div>
    </LabContext.Provider>
  );
}
