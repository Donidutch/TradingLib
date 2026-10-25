/* Stock Research payloads, /api/research/... Every tab's endpoint reports an
 * `error` (the vendor failure, if any) and a `freshness` record beside its
 * data, so the page can say "not available, because ..." instead of blank. */
import { z } from "zod";
import { Freshness, Metric, Quote, VendorRow, nnum, nstr } from "./common";

const Errored = { symbol: z.string(), error: nstr };

export const NextEarnings = z.looseObject({
  date: z.string(),
  days_away: z.number(),
  eps_estimate: nnum,
  source: z.string(),
  basis: z.string(),
});

/** GET /company/{symbol}: header, overview tab and the bar chart. 404 if unknown. */
export const Company = z.looseObject({
  symbol: z.string(),
  profile: z.looseObject({
    symbol: z.string(),
    name: z.string(),
    exchange: nstr,
    sector: nstr,
    industry: nstr,
    country: nstr,
    region: nstr,
    currency: nstr,
    quote_type: nstr,
    website: nstr,
    employees: nnum,
    summary: nstr,
    meta_line: nstr,
  }),
  quote: Quote.nullable(),
  metrics: z.record(z.string(), Metric),
  next_earnings: NextEarnings.nullable(),
  revenue_ebitda: z.array(
    z.looseObject({
      period: z.string(),
      year: z.number(),
      revenue: nnum,
      ebitda: nnum,
      free_cash_flow: nnum,
    }),
  ),
  freshness: z.record(z.string(), Freshness),
  warnings: z.array(z.string()),
});

export const PriceRanges = z.looseObject({
  ranges: z.array(z.looseObject({ key: z.string(), interval: z.string(), span_days: nnum })),
});

export const PriceHistory = z.looseObject({
  ...Errored,
  range: z.string(),
  interval: z.string(),
  source: z.string(),
  points: z.array(
    z.looseObject({
      time: z.string(),
      open: nnum,
      high: nnum,
      low: nnum,
      close: z.number(),
      volume: nnum,
    }),
  ),
  count: z.number(),
  first_close: nnum,
  last_close: nnum,
  change_pct: nnum,
  freshness: Freshness,
});

const FinancialPeriod = z.looseObject({
  period: z.string(),
  period_date: z.string(),
  revenue: nnum,
  gross_profit: nnum,
  operating_income: nnum,
  ebitda: nnum,
  net_income: nnum,
  eps_diluted: nnum,
  operating_cash_flow: nnum,
  capex: nnum,
  free_cash_flow: nnum,
  total_assets: nnum,
  total_debt: nnum,
  cash_and_investments: nnum,
  equity: nnum,
  shares_diluted: nnum,
  gross_margin: nnum,
  ebitda_margin: nnum,
  fcf_margin: nnum,
});

const Statements = z.looseObject({
  income_statement: z.array(VendorRow),
  balance_sheet: z.array(VendorRow),
  cash_flow: z.array(VendorRow),
});

export const Financials = z.looseObject({
  ...Errored,
  currency: nstr,
  source: z.string(),
  annual: z.array(FinancialPeriod),
  quarterly: z.array(FinancialPeriod),
  statements: Statements,
  quarterly_statements: Statements,
  freshness: Freshness,
});

export const Estimates = z.looseObject({
  ...Errored,
  source: z.string(),
  history: z.array(
    z.looseObject({
      date: z.string(),
      report_quarter: z.string(),
      fiscal_period: nstr,
      calendar_quarter: nstr,
      eps_estimate: nnum,
      eps_reported: nnum,
      surprise_pct: nnum,
      reaction_pct: nnum,
    }),
  ),
  next_earnings: NextEarnings.nullable(),
  consensus: z.looseObject({
    fy_revenue: nnum,
    fy_revenue_growth: nnum,
    fy_next_revenue: nnum,
    fy_eps: nnum,
    fy_next_eps: nnum,
    analysts: nnum,
    eps_high: nnum,
    eps_low: nnum,
    dispersion: nnum,
    revision_30d: nnum,
    basis: z.string(),
  }),
  // Vendor tables, passed through with the vendor's column names.
  earnings_estimate: z.array(VendorRow),
  revenue_estimate: z.array(VendorRow),
  eps_trend: z.array(VendorRow),
  eps_revisions: z.array(VendorRow),
  growth_estimates: z.array(VendorRow),
  recommendations: z.array(VendorRow),
  upgrades_downgrades: z.array(VendorRow),
  /** Empty for symbols without analyst coverage (ETFs). */
  price_targets: z.record(z.string(), nnum),
  freshness: Freshness,
});

const PeerRow = z.looseObject({
  symbol: z.string(),
  name: nstr,
  sector: nstr,
  industry: nstr,
  market_cap: nnum,
  revenue_ttm: nnum,
  revenue_yoy: nnum,
  gross_margin: nnum,
  fcf_margin: nnum,
  ev_to_sales: nnum,
  ev_to_ebitda: nnum,
  is_subject: z.boolean().optional(),
});

export const Valuation = z.looseObject({
  ...Errored,
  multiples: z.record(z.string(), Metric),
  peer_basis: z.string(),
  peers: z.array(PeerRow),
  peer_medians: z.record(z.string(), nnum),
  freshness: Freshness,
});

const NewsItem = z.looseObject({
  symbol: nstr,
  headline: z.string(),
  url: z.string(),
  published_at: z.string(),
  publisher: nstr,
  summary: nstr,
  source: z.string(),
  sentiment: z.number(),
  sentiment_label: z.string(),
  age_hours: z.number(),
});

export const News = z.looseObject({
  ...Errored,
  source: z.string(),
  scorer: z.string(),
  items: z.array(NewsItem),
  count: z.number(),
  aggregate: z.looseObject({
    mean_compound: nnum,
    recency_weighted: nnum,
    label: nstr,
    counts: z.record(z.string(), z.number()),
    half_life_days: z.number(),
  }),
  freshness: Freshness,
});

const ThesisPoint = z.looseObject({
  label: z.string(),
  basis: z.string(),
  kind: z.string(),
  metric: z.string(),
});

/** GET /thesis: measured catalysts and risks. 404 if the symbol is unknown. */
export const Thesis = z.looseObject({
  ...Errored,
  catalysts: z.array(ThesisPoint),
  risks: z.array(ThesisPoint),
  rules: z.record(z.string(), z.number()),
  basis: z.string(),
});

/** One alternative-data signal; each says why when it has no value. */
const Signal = z.looseObject({
  source: nstr.optional(),
  unit: z.string(),
  value: nnum,
  reason: nstr,
  basis: z.string().optional(),
  as_of: z.string().optional(),
  keyword: z.string().optional(),
  geo: z.string().optional(),
  timeframe: z.string().optional(),
  label: z.string().nullable().optional(),
  change_90d_pct: nnum.optional(),
  board: z.string().optional(),
  slug: z.string().optional(),
  open_roles: z.number().optional(),
  points: z.array(z.looseObject({ time: z.string(), value: z.number() })).optional(),
});

export const AltData = z.looseObject({
  symbol: z.string(),
  signals: z.record(z.string(), Signal),
  freshness: z.record(z.string(), Freshness),
});

export const Filings = z.looseObject({
  ...Errored,
  source: z.string(),
  items: z.array(
    z.looseObject({
      symbol: z.string(),
      cik: z.string(),
      form: z.string(),
      filed_at: z.string(),
      accession: z.string(),
      url: z.string(),
      title: nstr,
      period_of_report: nstr,
    }),
  ),
  count: z.number(),
  form_counts: z.record(z.string(), z.number()),
  freshness: Freshness,
});

const MemoryStatement = z.looseObject({
  text: z.string(),
  kind: z.string(),
  detail: nstr,
  refs: z.array(z.record(z.string(), z.unknown())),
});

export const Memory = z.looseObject({
  symbol: z.string(),
  thesis: MemoryStatement,
  questions: z.array(MemoryStatement),
  question_count: z.number(),
  counts: z.record(z.string(), z.number()),
  generated_at: nstr,
  basis: z.string(),
});

export const FreshnessReport = z.looseObject({
  symbol: z.string(),
  components: z.record(z.string(), Freshness),
  stale_components: z.array(z.string()),
  status: z.string(),
});

export const Resolve = z.looseObject({
  query: z.string(),
  count: z.number(),
  results: z.array(
    z.looseObject({
      symbol: z.string(),
      name: nstr,
      exchange: nstr,
      type: nstr,
      sector: nstr,
      industry: nstr,
      source: z.string(),
    }),
  ),
});

export type Company = z.infer<typeof Company>;
export type PriceRanges = z.infer<typeof PriceRanges>;
export type PriceHistory = z.infer<typeof PriceHistory>;
export type Financials = z.infer<typeof Financials>;
export type Estimates = z.infer<typeof Estimates>;
export type Valuation = z.infer<typeof Valuation>;
export type News = z.infer<typeof News>;
export type Thesis = z.infer<typeof Thesis>;
export type AltData = z.infer<typeof AltData>;
export type Filings = z.infer<typeof Filings>;
export type Memory = z.infer<typeof Memory>;
export type FreshnessReport = z.infer<typeof FreshnessReport>;
export type Resolve = z.infer<typeof Resolve>;
