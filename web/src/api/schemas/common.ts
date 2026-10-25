/* Shapes shared across domains. Objects are loose: the server may add keys
 * without breaking the app, but a key the app reads must match. Nullability
 * follows the backend serializers (`as_dict`), not one sample response. */
import { z } from "zod";

export const nnum = z.number().nullable();
export const nstr = z.string().nullable();
/** A row copied from a vendor table (yfinance), keyed by its column names. */
export const VendorRow = z.record(z.string(), z.union([z.number(), z.string(), z.boolean(), z.null()]));

/** tradinglib.research.company.Freshness: how old one data component is. */
export const Freshness = z.looseObject({
  component: z.string(),
  source: z.string(),
  fetched_at: nstr,
  age_seconds: nnum,
  stale: z.boolean(),
  max_age_seconds: nnum,
  detail: nstr,
});

/** A derived metric with its formula and inputs, never a bare number. */
export const Metric = z.looseObject({
  key: z.string(),
  label: z.string(),
  value: nnum,
  unit: z.string(),
  basis: nstr,
  reason: nstr,
  inputs: z.record(z.string(), z.number().nullable()),
});

/** One quote, as served by /company and the tape.<SYMBOL> channel. */
export const Quote = z.looseObject({
  symbol: z.string(),
  price: nnum,
  ts: nstr,
  bid: nnum,
  ask: nnum,
  bid_size: nnum,
  ask_size: nnum,
  volume: nnum,
  prev_close: nnum,
  source: z.string(),
  change: nnum,
  change_pct: nnum,
});

export type Freshness = z.infer<typeof Freshness>;
export type Metric = z.infer<typeof Metric>;
export type Quote = z.infer<typeof Quote>;
