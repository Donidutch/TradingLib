/* The screener (tradinglib/research/screen): metric catalogue, saved screens,
 * previews, runs, the backtest hand-off and membership alerts. */
import { z } from "zod";
import { nnum, nstr } from "./common";

export const MetricDef = z.looseObject({
  key: z.string(),
  label: z.string(),
  /** "usd", "pct", "x", "cat", … */
  unit: z.string(),
  source: z.string(),
  description: nstr,
  categorical: z.boolean(),
  higher_is_better: z.boolean().nullable(),
});

export const MetricCatalogue = z.looseObject({
  metrics: z.array(MetricDef),
  operators: z.array(z.string()),
  scopes: z.array(z.string()),
});

const RuleValue = z.union([z.number(), z.string(), z.array(z.string()), z.null()]);

export const ScreenRule = z.looseObject({
  field: z.string(),
  operator: z.string(),
  value: RuleValue,
  value2: z.number().nullable().optional(),
  scope: z.string().nullable().optional(),
  source: nstr.optional(),
  enabled: z.boolean().optional(),
});

export const Screen = z.looseObject({
  id: z.string(),
  name: z.string(),
  description: nstr,
  universe_id: nstr,
  rules: z.array(ScreenRule),
  active_rule_count: z.number(),
  rank_by: z.string(),
  tags: z.array(z.string()),
  query: nstr,
  created_at: nstr,
  updated_at: nstr,
});

export const ScreenList = z.looseObject({ screens: z.array(Screen), count: z.number() });

const Coverage = z.looseObject({
  requested: nnum.optional(),
  evaluated: nnum.optional(),
  priced: nnum.optional(),
  with_fundamentals: nnum.optional(),
  price_source: nstr.optional(),
  truncated: z.boolean().optional(),
  max_symbols: nnum.optional(),
});

/** POST /api/screens/preview: counts for a rule list over a universe. */
export const Preview = z.looseObject({
  universe_id: z.string(),
  as_of: nstr,
  base_count: z.number(),
  count: z.number(),
  query: nstr,
  coverage: z.looseObject({ coverage: Coverage }).nullable(),
  symbols: z.array(z.union([z.string(), z.looseObject({ symbol: z.string() })])).optional(),
});

export const ScreenRun = z.looseObject({
  id: z.string(),
  base_count: z.number(),
  pass_count: z.number(),
  rank_by: z.string(),
  symbols: z.array(z.string()),
  survivors: z.array(z.record(z.string(), z.unknown())),
  stages: z.array(
    z.looseObject({ rule_index: z.number(), input_count: z.number(), output_count: z.number(), label: z.string() }),
  ),
  stats: z.looseObject({
    count: z.number(),
    median_market_cap: nnum.optional(),
    sector_mix: z.array(z.looseObject({ sector: z.string(), share: z.number(), count: z.number() })).optional(),
    turnover: z
      .looseObject({ entered_count: z.number(), exited_count: z.number(), compared_with_run: z.string() })
      .nullable()
      .optional(),
    live_book_overlap: z
      .looseObject({ source: nstr.optional(), count: z.number().optional(), reason: nstr.optional() })
      .nullable()
      .optional(),
  }),
  warnings: z.array(z.string()),
});

/** GET /api/screens/runs: persisted runs, newest first. */
export const ScreenRuns = z.looseObject({
  runs: z.array(z.looseObject({ id: z.string(), screen_id: nstr, ran_at: nstr, base_count: z.number(), pass_count: z.number() })),
  count: z.number(),
});

/** POST /api/screens/{id}/queue-backtest. */
export const QueuedBacktest = z.looseObject({
  id: z.string(),
  status: z.string(),
  backtest_id: nstr,
  detail: nstr,
  request: z.looseObject({ symbol_count: z.number(), benchmark: z.string() }).optional(),
});

export const ScreenAlert = z.looseObject({
  id: z.string(),
  screen_id: z.string(),
  screen_name: z.string(),
  headline: z.string(),
  entered: z.array(z.string()),
  exited: z.array(z.string()),
  pass_count: z.number(),
  previous_count: z.number(),
  detected_at: z.string(),
  acknowledged_at: nstr,
});

export const ScreenAlerts = z.looseObject({
  alerts: z.array(ScreenAlert),
  count: z.number(),
  monitor: z
    .looseObject({
      running: z.boolean(),
      interval_seconds: nnum,
      last_cycle_at: nstr,
      last_error: nstr,
      watched: z.array(z.string()),
    })
    .nullable()
    .optional(),
});

/** GET /api/quality/datasets: the stored series the quality engine knows. */
export const Datasets = z.looseObject({
  datasets: z.array(
    z.looseObject({ key: z.string(), kind: z.string(), label: z.string(), namespace: nstr, name: nstr, symbol: nstr, interval: nstr }),
  ),
  count: z.number(),
});

export interface PreviewRequest {
  universe_id: string;
  rules: z.input<typeof ScreenRule>[];
  rank_by?: string;
  as_of?: string | null;
}

export type MetricDef = z.infer<typeof MetricDef>;
export type MetricCatalogue = z.infer<typeof MetricCatalogue>;
export type ScreenRule = z.infer<typeof ScreenRule>;
export type Screen = z.infer<typeof Screen>;
export type Preview = z.infer<typeof Preview>;
export type ScreenRun = z.infer<typeof ScreenRun>;
export type ScreenAlerts = z.infer<typeof ScreenAlerts>;
