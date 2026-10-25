import { z } from "zod";
import { nnum, nstr } from "./common";

/** tradinglib.research.hypotheses.Hypothesis.as_dict */
export const Hypothesis = z.looseObject({
  id: z.string(),
  title: z.string(),
  statement: z.string(),
  falsifier: z.string(),
  rationale: nstr,
  status: z.string(),
  owner: nstr,
  origin: nstr,
  universe_id: nstr,
  tags: z.array(z.string()),
  created_at: nstr,
  updated_at: nstr,
  evidence_count: z.number(),
  /** Present with `include_evidence=true` and on a single hypothesis. */
  evidence: z
    .array(z.looseObject({ id: z.union([z.number(), z.string()]), kind: z.string(), ref: z.string(), label: nstr, url: nstr.optional() }))
    .optional(),
  status_history: z
    .array(z.looseObject({ from: nstr.optional(), to: nstr.optional(), status: nstr.optional(), at: nstr.optional(), ts: nstr.optional(), note: nstr.optional(), actor: nstr.optional() }))
    .optional(),
});

export const HypothesisList = z.looseObject({
  hypotheses: z.array(Hypothesis),
  count: z.number(),
  summary: z.looseObject({ total: z.number(), by_status: z.record(z.string(), z.number()) }),
});

/** tradinglib.research.backtest.BacktestRecord.as_dict, list form. */
export const Backtest = z.looseObject({
  id: z.string(),
  name: z.string(),
  status: z.string(),
  source: nstr.optional(),
  hypothesis_id: nstr,
  experiment_id: nstr.optional(),
  screen_id: nstr.optional(),
  config: z.record(z.string(), z.unknown()).optional(),
  /** The window gates judge, when the run had an out-of-sample split. */
  out_of_sample: z.record(z.string(), z.unknown()).nullable().optional(),
  universe_id: nstr,
  created_at: z.string(),
  finished_at: nstr,
  duration_ms: nnum,
  symbols: z.array(z.string()),
  symbol_count: z.number(),
  stats: z.record(z.string(), z.unknown()).nullable(),
  error: nstr,
});

export const BacktestList = z.looseObject({
  backtests: z.array(Backtest),
  count: z.number(),
});

/** GET /api/lab: the tab bar's counts. */
export const LabSummary = z.looseObject({
  hypotheses: z.looseObject({ total: z.number(), by_status: z.record(z.string(), z.number()) }),
  experiments: z.number(),
  experiment_runs: z.number(),
  backtests: z.number(),
});

/** BacktestConfig as stored, loose: the editor reads what it knows. */
export const BacktestConfig = z.record(z.string(), z.unknown());

/** tradinglib.research.experiments.Experiment.as_dict */
export const Experiment = z.looseObject({
  id: z.string(),
  name: z.string(),
  hypothesis_id: nstr,
  kind: z.string(),
  stage: z.string(),
  universe_id: nstr,
  config: BacktestConfig,
  signal: nstr,
  criteria: z.looseObject({ min_oos_sharpe: nnum.optional(), max_drawdown: nnum.optional(), min_trades: nnum.optional() }).nullable(),
  horizon: z.number(),
  owner: nstr,
  notes: nstr,
  created_at: nstr,
  updated_at: nstr,
});

export const ExperimentList = z.looseObject({ experiments: z.array(Experiment), count: z.number() });

export const ExperimentRun = z.looseObject({
  id: z.string(),
  experiment_id: z.string(),
  backtest_id: nstr.optional(),
  status: z.string(),
  started_at: nstr,
  finished_at: nstr.optional(),
  duration_ms: nnum,
  error: nstr.optional(),
  results: z
    .looseObject({ passed_gates: z.boolean().nullable().optional(), ic: z.looseObject({ mean_ic: nnum.optional() }).nullable().optional() })
    .nullable()
    .optional(),
  log: z.array(z.looseObject({ ts: nstr.optional(), level: nstr.optional(), message: nstr.optional() })).optional(),
});

export const ExperimentDetail = Experiment.extend({ runs: z.array(ExperimentRun), run_count: z.number() });

export const Gate = z.looseObject({ id: z.string(), name: z.string(), status: z.string(), passed: z.boolean().optional() });
export const GateSuite = z.looseObject({ gates: z.array(Gate), count: z.number(), thresholds: z.record(z.string(), z.unknown()).nullable().optional() });

export const ValidationReport = z.looseObject({
  id: z.string(),
  backtest_id: z.string(),
  experiment_id: nstr.optional(),
  evaluated_at: nstr.optional(),
  gates_passed: z.number(),
  gates_total: z.number(),
  passed: z.boolean(),
  verdict: z.string(),
  tests: z.record(z.string(), z.unknown()).nullable().optional(),
});
export const ValidationReports = z.looseObject({ reports: z.array(ValidationReport), count: z.number() });

export const Strategies = z.looseObject({ strategies: z.array(z.looseObject({ id: z.string() })), count: z.number() });

/** GET /api/lab/evaluation; S4 reads only its funnel. */
export const Evaluation = z.looseObject({
  funnel: z.looseObject({ stages: z.array(z.looseObject({ stage: z.string(), count: z.number() })), total: z.number() }).optional(),
});

export const Analysis = z.looseObject({
  id: z.string(),
  kind: z.string(),
  experiment_id: nstr.optional(),
  backtest_id: nstr.optional(),
  chip: z.string().optional(),
  tone: nstr.optional(),
});
export const Analyses = z.looseObject({ analyses: z.array(Analysis), count: z.number() });

export type Hypothesis = z.infer<typeof Hypothesis>;
export type LabSummary = z.infer<typeof LabSummary>;
export type Experiment = z.infer<typeof Experiment>;
export type ExperimentDetail = z.infer<typeof ExperimentDetail>;
export type ExperimentRun = z.infer<typeof ExperimentRun>;
export type ValidationReport = z.infer<typeof ValidationReport>;
export type Analysis = z.infer<typeof Analysis>;
export type HypothesisList = z.infer<typeof HypothesisList>;
export type Backtest = z.infer<typeof Backtest>;
export type BacktestList = z.infer<typeof BacktestList>;
