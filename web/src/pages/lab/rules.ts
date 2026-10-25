/* Strategy Lab rules, ported from lab.js and kept apart from the components
 * so each is tested on its own. */
import type { AgentRun } from "../../api/schemas/agents";
import type { Backtest, Experiment, ValidationReport } from "../../api/schemas/lab";
import { etClock, etDate, int, num, pct } from "../../ui/format";

export const STAGES = ["research", "validated", "paper", "approved", "live"] as const;
export const HYPOTHESIS_STATES = ["draft", "active", "testing", "validated", "rejected", "archived"] as const;
export const EVIDENCE_KINDS = ["paper", "knowledge", "run", "backtest", "experiment", "finding", "screen", "note", "url"] as const;
export const SIGNAL_KINDS = ["momentum", "mean_reversion", "ma_cross", "low_volatility", "expression", "static"] as const;
export const WEIGHTINGS = ["equal", "signal", "inverse_vol"] as const;
export const REBALANCES = ["daily", "weekly", "monthly", "quarterly"] as const;

export type Tone = "up" | "down" | "warn" | "accent" | "muted" | null;

/** The text colour for a tone. */
export const toneText = (tone: Tone | undefined) =>
  tone === "up" ? "text-up" : tone === "down" ? "text-down" : tone === "warn" ? "text-warn" : tone === "accent" ? "text-accent" : tone === "muted" ? "text-text-subtle" : undefined;


export const ratio = (value: unknown) => num(toNumber(value), 2);
export const percent = (value: unknown, decimals = 1) => pct(toNumber(value), { fraction: true, decimals });
export const signedPercent = (value: unknown, decimals = 1) => pct(toNumber(value), { fraction: true, sign: true, decimals });

export function toNumber(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

/** "Aug 12 14:03" in New York. */
export function when(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return `${etDate(value)} ${etClock(date).slice(0, 5)}`;
}

export function stageTone(stage: string): Tone {
  if (stage === "live" || stage === "approved") return "accent";
  if (stage === "paper" || stage === "validated") return "up";
  if (stage === "killed") return "down";
  return null;
}

export function statusTone(status: string): Tone {
  if (status === "validated") return "up";
  if (status === "rejected") return "down";
  if (status === "archived") return "muted";
  if (status === "testing") return "accent";
  return null;
}

export function runTone(status: string): Tone {
  if (status === "complete") return "up";
  if (status === "failed" || status === "cancelled") return "down";
  if (status === "running") return "accent";
  return "warn";
}

// ------------------------------------------------------------------ lookups

/** Newest completed backtest per experiment (the list is newest first). */
export function newestByExperiment(backtests: readonly Backtest[]): Record<string, Backtest> {
  const out: Record<string, Backtest> = {};
  for (const record of backtests) {
    if (record.experiment_id && !out[record.experiment_id] && record.status === "complete") out[record.experiment_id] = record;
  }
  return out;
}

/** Newest validation report per backtest. */
export function reportsByBacktest(reports: readonly ValidationReport[]): Record<string, ValidationReport> {
  const out: Record<string, ValidationReport> = {};
  for (const report of reports) if (!out[report.backtest_id]) out[report.backtest_id] = report;
  return out;
}

export function deflatedOf(report: ValidationReport | null | undefined): number | null {
  const test = report?.tests?.deflated_sharpe as { deflated_sharpe?: unknown } | undefined;
  return test ? toNumber(test.deflated_sharpe) : null;
}

/** The window a gate judges: out-of-sample stats when present. */
export function judgedWindow(record: Backtest | null | undefined): Record<string, unknown> | null {
  if (!record) return null;
  return record.out_of_sample ?? record.stats ?? null;
}

export type Origin = "manual" | "agent" | "screen";

/** Where an experiment came from, derived from its runs — never invented. */
export function originOf(experiment: Experiment, backtests: readonly Backtest[]): Origin {
  const runs = backtests.filter((b) => b.experiment_id === experiment.id);
  if (runs.some((b) => Boolean(b.screen_id))) return "screen";
  if ((experiment.owner ?? "").toLowerCase() === "agent") return "agent";
  if (runs.some((b) => b.source === "agent")) return "agent";
  return "manual";
}

/** Whether an experiment sits at `stage` or further down the funnel. */
export function reachedStage(experiment: Experiment, stage: string): boolean {
  const wanted = (STAGES as readonly string[]).indexOf(stage);
  if (wanted < 0) return experiment.stage === stage;
  const actual = (STAGES as readonly string[]).indexOf(experiment.stage);
  if (actual < 0) return wanted === 0;
  return actual >= wanted;
}

export interface ExperimentFilter {
  stage: string | null;
  query: string;
  origin: "all" | Origin;
}

export function filterExperiments(experiments: readonly Experiment[], backtests: readonly Backtest[], filter: ExperimentFilter): Experiment[] {
  const query = filter.query.trim().toLowerCase();
  return experiments.filter((exp) => {
    if (filter.stage && !reachedStage(exp, filter.stage)) return false;
    if (filter.origin !== "all" && originOf(exp, backtests) !== filter.origin) return false;
    if (!query) return true;
    return [exp.name, exp.hypothesis_id, exp.universe_id, exp.signal, exp.owner].filter(Boolean).join(" ").toLowerCase().includes(query);
  });
}

// ------------------------------------------------------------------ funnel

export interface FunnelCard {
  stage: (typeof STAGES)[number];
  count: number;
  caption: string;
}

/**
 * Five cards counting the experiments that reached each stage or went past
 * it, so each is a subset of the one before; killed ones were still logged,
 * so they count as research. From the evaluation funnel when it loaded, else
 * from the experiments themselves.
 */
export function funnelCards(stages: readonly { stage: string; count: number }[] | null, experiments: readonly Experiment[]): FunnelCard[] {
  const at: Record<string, number> = {};
  if (stages) for (const row of stages) at[row.stage] = row.count;
  else for (const exp of experiments) at[exp.stage] = (at[exp.stage] ?? 0) + 1;
  const counts: Record<string, number> = {};
  let carried = 0;
  for (let i = STAGES.length - 1; i >= 0; i -= 1) {
    const stage = STAGES[i] ?? "research";
    carried += at[stage] ?? 0;
    counts[stage] = carried;
  }
  counts.research = (counts.research ?? 0) + (at.killed ?? 0);
  const total = counts.research;
  const validated = counts.validated ?? 0;
  const live = counts.live ?? 0;
  const captions: Record<string, string> = {
    research: "experiments logged",
    validated: total ? `${percent(validated / total, 1)} of research` : "gates passed",
    paper: validated ? `${percent((counts.paper ?? 0) / validated, 1)} of validated` : "promoted to paper",
    approved: "awaiting deploy",
    live: `${counts.approved ?? 0} approved, ${live ? `${live} deployed` : "none deployed"}`,
  };
  return STAGES.map((stage) => ({ stage, count: counts[stage] ?? 0, caption: captions[stage] ?? "" }));
}

// ------------------------------------------------------------------ strategy

type Config = Record<string, unknown>;
const record = (value: unknown): Config => (value && typeof value === "object" && !Array.isArray(value) ? (value as Config) : {});
const list = (value: unknown): unknown[] => (Array.isArray(value) ? value : []);

/** The persisted config as the rule rows the old page drew. */
export function strategyRules(config: Config): [string, string, string, string][] {
  const signal = record(config.signal);
  const costs = record(config.costs);
  const rules: [string, string, string, string][] = [];
  rules.push(["signal", String(signal.kind), "lookback", `${String(signal.lookback)}d`]);
  if (signal.skip) rules.push(["signal", "skip", "=", `${String(signal.skip)}d`]);
  if (signal.kind === "ma_cross") rules.push(["signal", "sma", "=", `${String(signal.fast)}/${String(signal.slow)}`]);
  if (signal.expression) rules.push(["expression", String(signal.expression), "", ""]);
  if (signal.filter_expression) rules.push(["filter", String(signal.filter_expression), "", ""]);
  if (signal.top_n) rules.push(["rank", "top n", "=", String(signal.top_n)]);
  if (signal.min_signal !== null && signal.min_signal !== undefined) rules.push(["gate", "min signal", ">=", String(signal.min_signal)]);
  rules.push(["weight", "", "", String(signal.weighting)]);
  rules.push(["rebalance", "", "", String(config.rebalance)]);
  rules.push(["universe", "", "", typeof config.universe_id === "string" && config.universe_id ? config.universe_id : `${list(config.symbols).length} symbols`]);
  rules.push(["benchmark", "", "", typeof config.benchmark === "string" && config.benchmark ? config.benchmark : "none"]);
  rules.push(["costs", "bps", "=", `${Number(costs.commission_bps) + Number(costs.slippage_bps)} one-way`]);
  rules.push(["split", "OOS", "=", config.oos_fraction ? percent(config.oos_fraction, 0) : typeof config.oos_start === "string" && config.oos_start ? config.oos_start : "none"]);
  return rules;
}

/** "a.b" → scalar, for the compare view's config diff. */
export function flattenConfig(config: Config): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(config)) {
    if (value && typeof value === "object" && !Array.isArray(value)) {
      for (const [inner, v] of Object.entries(value as Config)) out[`${key}.${inner}`] = v;
    } else if (Array.isArray(value)) out[key] = `${value.length} items`;
    else out[key] = value;
  }
  return out;
}

export interface CompareItem {
  experiment: Experiment;
  record: Backtest | null;
  report: ValidationReport | null;
}

interface CompareRow {
  label: string;
  best: boolean;
  values: (number | null)[];
  text: string[];
  bestIndex: number[];
}

/** The compare view's metric rows, with the best value marked where higher is better. */
export function compareRows(items: readonly CompareItem[]): CompareRow[] {
  const stat = (i: CompareItem, key: string) => toNumber(i.record?.stats?.[key]);
  const specs: { label: string; best: boolean; value: (i: CompareItem) => number | null; format: (v: number) => string }[] = [
    { label: "OOS Sharpe", best: true, value: (i) => toNumber(judgedWindow(i.record)?.sharpe), format: ratio },
    { label: "Deflated Sharpe", best: true, value: (i) => deflatedOf(i.report), format: ratio },
    { label: "Max drawdown", best: true, value: (i) => stat(i, "max_drawdown"), format: (v) => percent(v) },
    { label: "Total return", best: true, value: (i) => stat(i, "total_return"), format: (v) => signedPercent(v) },
    { label: "Excess vs benchmark", best: true, value: (i) => toNumber(judgedWindow(i.record)?.excess_return), format: (v) => signedPercent(v) },
    { label: "Trades", best: false, value: (i) => stat(i, "trades"), format: int },
    { label: "Turnover", best: false, value: (i) => stat(i, "turnover"), format: ratio },
  ];
  return specs.map((spec) => {
    const values = items.map(spec.value);
    const present = values.filter((v): v is number => v !== null);
    const top = spec.best && present.length ? Math.max(...present) : null;
    return {
      label: spec.label,
      best: spec.best,
      values,
      text: values.map((v) => (v === null ? "—" : spec.format(v))),
      bestIndex: top === null ? [] : values.flatMap((v, i) => (v === top ? [i] : [])),
    };
  });
}

/** Config fields that differ between the compared experiments, and how many are identical. */
export function configDiff(items: readonly CompareItem[]): { keys: string[]; values: Record<string, unknown>[]; identical: number } {
  const flat = items.map((i) => flattenConfig(i.experiment.config));
  const all: string[] = [];
  for (const map of flat) for (const key of Object.keys(map)) if (!all.includes(key)) all.push(key);
  const keys = all.filter((key) => new Set(flat.map((map) => String(map[key]))).size > 1);
  return { keys, values: flat, identical: all.length - keys.length };
}

// ------------------------------------------------------------------ the config editor

export interface ConfigForm {
  name: string;
  universe: string;
  symbols: string;
  kind: string;
  lookback: string;
  skip: string;
  fast: string;
  slow: string;
  topN: string;
  weighting: string;
  expression: string;
  filterExpression: string;
  start: string;
  end: string;
  benchmark: string;
  rebalance: string;
  capital: string;
  maxPositions: string;
  longOnly: boolean;
  commission: string;
  slippage: string;
  borrow: string;
  oosFraction: string;
}

const text = (value: unknown, fallback: string) => (value === undefined || value === null ? fallback : String(value));

/** The editor seeded from a stored config, or the old page's defaults. */
export function configForm(initial: Config | null, activeUniverse: string): ConfigForm {
  const config = initial ?? {};
  const signal = record(config.signal);
  const costs = record(config.costs);
  return {
    name: text(config.name, ""),
    universe: config.universe_id !== undefined && config.universe_id !== null ? String(config.universe_id) : initial ? "" : activeUniverse,
    symbols: list(config.symbols).map(String).join(", "),
    kind: text(signal.kind, "momentum"),
    lookback: text(signal.lookback, "252"),
    skip: text(signal.skip, "21"),
    fast: text(signal.fast, "50"),
    slow: text(signal.slow, "200"),
    topN: text(signal.top_n, "10"),
    weighting: text(signal.weighting, "equal"),
    expression: text(signal.expression, ""),
    filterExpression: text(signal.filter_expression, ""),
    start: text(config.start, ""),
    end: text(config.end, ""),
    benchmark: config.benchmark === undefined ? "SPY" : text(config.benchmark, ""),
    rebalance: text(config.rebalance, "monthly"),
    capital: text(config.initial_capital, "1000000"),
    maxPositions: text(config.max_positions, ""),
    longOnly: config.long_only === undefined ? true : Boolean(config.long_only),
    commission: text(costs.commission_bps, "1"),
    slippage: text(costs.slippage_bps, "5"),
    borrow: text(costs.borrow_bps_annual, "0"),
    oosFraction: text(config.oos_fraction, "0.3"),
  };
}

function number(value: string, fallback: number | null): number | null {
  if (value === "") return fallback;
  const n = Number(value);
  return Number.isNaN(n) ? fallback : n;
}

/** The editor back into a BacktestConfig, with the old page's fallbacks. */
export function readConfig(form: ConfigForm): Config {
  return {
    name: form.name.trim() || "backtest",
    universe_id: form.universe || null,
    symbols: form.symbols
      .split(",")
      .map((s) => s.trim().toUpperCase())
      .filter(Boolean),
    signal: {
      kind: form.kind,
      lookback: number(form.lookback, 252),
      skip: number(form.skip, 21),
      fast: number(form.fast, 50),
      slow: number(form.slow, 200),
      top_n: number(form.topN, null),
      weighting: form.weighting,
      expression: form.expression.trim() || null,
      filter_expression: form.filterExpression.trim() || null,
    },
    start: form.start || null,
    end: form.end || null,
    benchmark: form.benchmark.trim() || null,
    rebalance: form.rebalance,
    initial_capital: number(form.capital, 1000000),
    max_positions: number(form.maxPositions, null),
    long_only: form.longOnly,
    costs: { commission_bps: number(form.commission, 1), slippage_bps: number(form.slippage, 5), borrow_bps_annual: number(form.borrow, 0) },
    oos_fraction: number(form.oosFraction, 0.3),
  };
}

// ------------------------------------------------------------------ agent activity

type Stage = NonNullable<AgentRun["stages"]>[number];

/** The stage that explains a run's state. */
export function tellingStage(run: AgentRun): Stage | null {
  const stages = run.stages ?? [];
  const by = (status: string) => stages.filter((s) => s.status === status);
  return (
    by("running")[0] ??
    by("awaiting_approval")[0] ??
    by("failed")[0] ??
    stages.find((s) => s.stage_id === run.current_stage_id) ??
    by("succeeded").at(-1) ??
    stages[0] ??
    null
  );
}

export function activityActor(run: AgentRun, definitions: readonly { id: string; name: string }[]): string {
  const stage = tellingStage(run);
  const definition = stage?.agent_id ? definitions.find((d) => d.id === stage.agent_id) : undefined;
  if (definition) return definition.name;
  if (stage?.name) return stage.name;
  return run.name || run.pipeline_id || run.id;
}

export function activitySentence(run: AgentRun): string {
  const stage = tellingStage(run);
  const label = run.name || run.pipeline_id || run.id;
  const progress = run.progress ?? {};
  const done = progress.total ? `${progress.completed ?? 0} of ${progress.total} stages` : "";
  const cost = run.cost_usd ? ` · $${num(run.cost_usd, 2)}` : "";
  const stamp = ` · ${when(run.finished_at || run.started_at || run.created_at)}`;
  const at = (fallback: string) => stage?.name || fallback;
  switch (run.status) {
    case "running":
      return `Running ${at("a stage")} in ${label}${done ? ` · ${done}` : ""}${cost}${stamp}`;
    case "awaiting_approval":
      return `Blocked on a human decision at ${at("the approval gate")} in ${label}${stamp}`;
    case "paused":
      return `Paused before ${at("the next stage")} in ${label}${stamp}`;
    case "queued":
      return `Queued: ${label}${done ? ` · ${done}` : ""}${stamp}`;
    case "failed":
      return `Failed at ${at("an unknown stage")} in ${label}${run.error ? ` — ${run.error}` : ""}${cost}${stamp}`;
    case "killed":
      return `Cancelled at ${at("an unknown stage")} in ${label}${run.error ? ` — ${run.error}` : ""}${stamp}`;
    default:
      return `Completed ${label}${done ? ` · ${done}` : ""}${cost}${stamp}`;
  }
}

export function activityBadge(runs: readonly AgentRun[]): { text: string; live: boolean } {
  const live = runs.filter((r) => r.status === "running" || r.status === "awaiting_approval" || r.status === "queued").length;
  const autonomous = runs.filter((r) => r.trigger === "schedule").length;
  return { text: live ? `${live} live` : autonomous ? `${autonomous} autonomous` : runs.length ? "idle" : "none", live: live > 0 };
}

// ------------------------------------------------------------------ run log

export interface LogLine {
  ts: string;
  level: string;
  message: string;
}

const MAX_LOG_LINES = 400;

/** Append a line unless the same one (time and text) is already shown; keep the last 400. */
export function appendLog(lines: readonly LogLine[], entry: { ts?: string | null; level?: string | null; message?: string | null }, now = new Date()): LogLine[] {
  const line = { ts: entry.ts || now.toISOString(), level: entry.level || "info", message: entry.message || "" };
  if (lines.some((l) => l.ts === line.ts && l.message === line.message)) return lines as LogLine[];
  return [...lines, line].slice(-MAX_LOG_LINES);
}
