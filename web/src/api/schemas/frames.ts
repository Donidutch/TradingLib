/* Frames on the channels step 2's pages listen to, read from the backend's
 * publishers (tradinglib/research, agents/runner.py, agents/approvals.py,
 * data/ingest, data/quality.py). Envelopes are strict about the fields a page
 * routes on (kind, phase, ids) and loose about the rest; each page slice
 * tightens what it renders. */
import { z } from "zod";
import { nnum, nstr } from "./common";

const anyRecord = z.record(z.string(), z.unknown());

/** `lab`: backtests, analyses, experiments, hypotheses, validation, promotion, evaluation. */
export const LabFrame = z.looseObject({
  kind: z.enum(["backtest", "analysis", "experiment", "hypothesis", "validation", "promotion", "evaluation"]),
  phase: z.string(),
  id: z.string().optional(),
  ts: z.string().optional(),
  name: nstr.optional(),
  error: nstr.optional(),
  hypothesis_id: nstr.optional(),
  experiment_id: nstr.optional(),
  backtest_id: nstr.optional(),
  /** Analyses: "sweep", "walk_forward", "monte_carlo". */
  analysis: z.string().optional(),
  /** Experiment run-log lines arrive as phase "log" with the entry spread in. */
  message: z.string().optional(),
  level: z.string().optional(),
  stats: anyRecord.nullable().optional(),
});

/** `runs`: agent run progress, stage progress and log lines (agents/runner.py). */
const RunBase = { source: z.string().optional(), ts: z.string(), run_id: z.string(), status: z.string().optional() };

export const RunsFrame = z.discriminatedUnion("kind", [
  z.looseObject({
    kind: z.literal("run"),
    phase: z.string(),
    ...RunBase,
    pipeline_id: z.string().optional(),
    stage_id: nstr.optional(),
    progress: z.unknown().optional(),
    cost_usd: nnum.optional(),
    error: nstr.optional(),
  }),
  z.looseObject({
    kind: z.literal("stage"),
    phase: z.string(),
    ...RunBase,
    stage_id: z.string(),
    name: nstr.optional(),
    agent_id: nstr.optional(),
    provider: nstr.optional(),
    model: nstr.optional(),
    cost_usd: nnum.optional(),
    error: nstr.optional(),
  }),
  z.looseObject({
    kind: z.literal("log"),
    ...RunBase,
    level: z.string(),
    message: z.string(),
    stage_id: nstr,
    detail: anyRecord,
  }),
]);

/** `approvals`: the human gate's queue (agents/approvals.py and the runner). */
export const ApprovalFrame = z.looseObject({
  kind: z.literal("approval"),
  phase: z.string(),
  pending: z.number(),
  ts: z.string().optional(),
  approval: anyRecord.optional(),
});

/** `ingest`: catalog, scheduler, backfill and gateway events. */
export const IngestFrame = z.looseObject({
  event: z.string(),
  source_id: z.string().optional(),
  status: z.string().optional(),
  ok: z.boolean().optional(),
  error: nstr.optional(),
  message: nstr.optional(),
  latency_ms: nnum.optional(),
});

/** `quality`: check runs, outliers and incident changes (data/quality.py). */
export const QualityFrame = z.looseObject({
  type: z.string(),
  datasets: z.unknown().optional(),
  failed: z.unknown().optional(),
  summary: anyRecord.optional(),
  outlier: anyRecord.optional(),
  incident: anyRecord.optional(),
});

export type LabFrame = z.infer<typeof LabFrame>;
export type RunsFrame = z.infer<typeof RunsFrame>;
export type ApprovalFrame = z.infer<typeof ApprovalFrame>;
export type IngestFrame = z.infer<typeof IngestFrame>;
export type QualityFrame = z.infer<typeof QualityFrame>;
