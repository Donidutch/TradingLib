/* Strategy Lab queries and writes. Each read is its own query, so one dead
 * endpoint degrades one panel and says so instead of showing a zero. */
import { useQuery, type QueryKey } from "@tanstack/react-query";
import { useAction } from "../../api/actions";
import { useApi } from "../../api/context";
import { errorReason } from "../../api/errors";
import type { ExperimentBody, HypothesisBody } from "../../api/endpoints";
import type { Experiment } from "../../api/schemas/lab";
import { useLab } from "./state";

export const labKeys = {
  summary: ["lab", "summary"] as const,
  /** Distinct from the shell's `["lab","hypotheses",q]`; invalidate by the `["lab","hypotheses"]` prefix. */
  hypotheses: ["lab", "hypotheses", "with-evidence"] as const,
  experiments: ["lab", "experiments"] as const,
  experiment: (id: string) => ["lab", "experiment", id] as const,
  /** Distinct from the shell's `["lab","backtests"]`, which the prefix also covers. */
  backtests: ["lab", "backtests", "all"] as const,
  gates: ["lab", "gates"] as const,
  reports: ["lab", "reports"] as const,
  strategies: ["lab", "strategies"] as const,
  evaluation: ["lab", "evaluation"] as const,
  analyses: ["lab", "analyses"] as const,
  screens: ["lab", "screens"] as const,
  screenRuns: ["lab", "screenRuns"] as const,
  quality: ["lab", "quality"] as const,
  agentRuns: ["lab", "agentRuns"] as const,
  definitions: ["lab", "definitions"] as const,
};

/** Prefixes that also refresh the shell's own lab queries (palette, overview). */
const HYPOTHESES = ["lab", "hypotheses"] as const;
const BACKTESTS = ["lab", "backtests"] as const;

/** Everything a finished experiment run changes. */
export const afterRun: QueryKey[] = [labKeys.summary, labKeys.experiments, BACKTESTS, labKeys.reports, labKeys.evaluation, labKeys.strategies, ["lab", "experiment"]];

export function useLabData() {
  const api = useApi();
  return {
    summary: useQuery({ queryKey: labKeys.summary, queryFn: ({ signal }) => api.lab.summary(signal) }),
    hypotheses: useQuery({ queryKey: labKeys.hypotheses, queryFn: ({ signal }) => api.lab.hypotheses({ include_evidence: true }, signal) }),
    experiments: useQuery({ queryKey: labKeys.experiments, queryFn: ({ signal }) => api.lab.experiments(signal) }),
    backtests: useQuery({ queryKey: labKeys.backtests, queryFn: ({ signal }) => api.lab.backtests({ limit: 200 }, signal) }),
    reports: useQuery({ queryKey: labKeys.reports, queryFn: ({ signal }) => api.lab.validation(signal) }),
    strategies: useQuery({ queryKey: labKeys.strategies, queryFn: ({ signal }) => api.lab.strategies(signal) }),
    evaluation: useQuery({ queryKey: labKeys.evaluation, queryFn: ({ signal }) => api.lab.evaluation(signal) }),
  };
}

export function useExperimentDetail(id: string | null) {
  const api = useApi();
  return useQuery({ queryKey: labKeys.experiment(id ?? ""), queryFn: ({ signal }) => api.lab.experiment(id ?? "", signal), enabled: Boolean(id) });
}

export function useAnalyses() {
  const api = useApi();
  return useQuery({ queryKey: labKeys.analyses, queryFn: ({ signal }) => api.lab.analyses(signal) });
}

export function useJudgement() {
  const api = useApi();
  return {
    gates: useQuery({ queryKey: labKeys.gates, queryFn: ({ signal }) => api.lab.gates(signal) }),
    quality: useQuery({ queryKey: labKeys.quality, queryFn: ({ signal }) => api.quality.summary(signal) }),
  };
}

export function useScreening() {
  const api = useApi();
  return {
    screens: useQuery({ queryKey: labKeys.screens, queryFn: ({ signal }) => api.screens.list(signal) }),
    runs: useQuery({ queryKey: labKeys.screenRuns, queryFn: ({ signal }) => api.screens.runs(signal) }),
  };
}

export function useAgentActivity() {
  const api = useApi();
  return {
    runs: useQuery({ queryKey: labKeys.agentRuns, queryFn: ({ signal }) => api.agents.runs(signal) }),
    definitions: useQuery({ queryKey: labKeys.definitions, queryFn: ({ signal }) => api.agents.definitions(signal), staleTime: Infinity }),
  };
}

export function useHypothesisActions() {
  const api = useApi();
  const touched = () => [HYPOTHESES, labKeys.summary];
  return {
    create: useAction({
      run: (body: HypothesisBody & { status: string }) => api.lab.createHypothesis(body),
      invalidate: touched,
      done: (created) => `Created ${created.id}`,
      failed: () => "Could not create the hypothesis",
    }),
    update: useAction({
      run: ({ id, body }: { id: string; body: HypothesisBody }) => api.lab.updateHypothesis(id, body),
      invalidate: touched,
      done: (_r, { id }) => `Saved ${id}`,
      failed: () => "Could not save the hypothesis",
    }),
    setStatus: useAction({
      run: ({ id, status }: { id: string; status: string }) => api.lab.setHypothesisStatus(id, status),
      invalidate: touched,
      done: (_r, { id, status }) => `${id} → ${status.toUpperCase()}`,
      failed: () => "Transition refused",
    }),
    remove: useAction({
      run: (id: string) => api.lab.deleteHypothesis(id),
      invalidate: touched,
      done: (_r, id) => `Deleted ${id}`,
      failed: () => "Could not delete the hypothesis",
    }),
    link: useAction({
      run: ({ id, body }: { id: string; body: { kind: string; ref: string; label: string | null } }) => api.lab.addEvidence(id, body),
      invalidate: touched,
      done: () => "Evidence linked",
      failed: () => "Could not link evidence",
    }),
    unlink: useAction({
      run: ({ id, evidence }: { id: string; evidence: string | number }) => api.lab.removeEvidence(id, evidence),
      invalidate: touched,
      done: () => "Evidence detached",
      failed: () => "Could not detach evidence",
    }),
  };
}

export function useExperimentActions() {
  const api = useApi();
  return {
    create: useAction({
      run: (body: ExperimentBody) => api.lab.createExperiment(body),
      invalidate: () => [labKeys.experiments, labKeys.summary],
      done: (created) => `Created experiment ${created.name}`,
      failed: () => "Could not create the experiment",
    }),
    remove: useAction({
      run: (exp: Experiment) => api.lab.deleteExperiment(exp.id),
      invalidate: () => [labKeys.experiments, labKeys.summary, BACKTESTS, labKeys.evaluation],
      done: (_r, exp) => `Deleted ${exp.name}`,
      failed: () => "Could not delete the experiment",
    }),
    run: useAction({
      run: (exp: Experiment) => api.lab.runExperiment(exp.id),
      invalidate: () => afterRun,
      done: (run) =>
        run.status === "complete"
          ? `Experiment run ${run.id} complete${
              run.results && typeof run.results.passed_gates === "boolean" ? (run.results.passed_gates ? " · acceptance passed" : " · acceptance not met") : ""
            }`
          : { text: `Experiment run ${run.id} ${run.status}${run.error ? ` — ${run.error}` : ""}`, tone: "warn" },
      failed: () => "The experiment run failed",
    }),
    backtest: useAction({
      run: (exp: Experiment) => api.lab.runBacktest({ config: exp.config, experiment_id: exp.id, hypothesis_id: exp.hypothesis_id, source: "manual" }),
      invalidate: () => [labKeys.summary, BACKTESTS, labKeys.evaluation],
      done: (record) => `Backtest ${record.id} ${record.status}`,
      failed: () => "The backtest failed",
    }),
  };
}

export function useScreenActions() {
  const api = useApi();
  return {
    run: useAction({
      run: (screen: { id: string; name: string }) => api.screens.run(screen.id),
      invalidate: () => [labKeys.screens, labKeys.screenRuns],
      done: (run, screen) => `${screen.name}: ${run.pass_count} of ${run.base_count} names passed`,
      failed: () => "Could not run the screen",
    }),
    queue: useAction({
      run: (screen: { id: string; name: string }) => api.screens.queueBacktest(screen.id, {}),
      invalidate: () => [BACKTESTS, labKeys.summary],
      done: (request, screen) =>
        request.backtest_id
          ? `Queued backtest ${request.backtest_id} from ${screen.name}`
          : { text: `${screen.name}: ${request.detail || "the lab did not accept the request"}`, tone: "warn" },
      failed: () => "Could not queue a backtest from the screen",
    }),
  };
}

/** Run an experiment with its log: a request line first, then the stored log when it finishes. */
export function useStartRun() {
  const lab = useLab();
  const { run } = useExperimentActions();
  return {
    pending: (id: string) => run.isPending && run.variables.id === id,
    start: (exp: Experiment) => {
      lab.setLog(() => ({ experimentId: exp.id, lines: [{ ts: new Date().toISOString(), level: "info", message: `requesting run of ${exp.name}…` }] }));
      run.mutate(exp, {
        onSuccess: (result) => {
          // The stored log replaces what streamed: it is the complete record.
          if (result.log?.length) {
            lab.setLog(() => ({
              experimentId: exp.id,
              lines: (result.log ?? []).map((l) => ({ ts: l.ts ?? "", level: l.level ?? "info", message: l.message ?? "" })),
            }));
          }
        },
        onError: (err) => lab.setLog((log) => ({ ...log, lines: [...log.lines, { ts: new Date().toISOString(), level: "error", message: errorReason(err) }] })),
      });
    },
  };
}
