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
  hypothesis_id: nstr,
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

export type Hypothesis = z.infer<typeof Hypothesis>;
export type HypothesisList = z.infer<typeof HypothesisList>;
export type Backtest = z.infer<typeof Backtest>;
export type BacktestList = z.infer<typeof BacktestList>;
