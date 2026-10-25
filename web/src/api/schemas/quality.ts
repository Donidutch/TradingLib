/* Data quality (tradinglib/data/quality.py): summary, per-dataset checks,
 * incidents, outliers, SLA and tickets. */
import { z } from "zod";
import { nnum, nstr } from "./common";

export const QualitySummary = z.looseObject({
  overall_quality_pct: nnum,
  stale_series: nnum,
  missing_values_pct: nnum,
  failed_checks: nnum,
  open_incidents: nnum.optional(),
  datasets: nnum.optional(),
  datasets_in_store: nnum.optional(),
  last_run: nstr,
});

const Check = z.looseObject({ check: z.string(), passed: z.boolean(), severity: z.string(), detail: z.string() });

export const QualitySeries = z.looseObject({
  datasets: z.array(z.looseObject({ dataset: z.string(), label: nstr, checks: z.array(Check) })),
  count: z.number(),
});

export const Incident = z.looseObject({
  id: z.string(),
  check: z.string(),
  dataset: z.string(),
  severity: z.string(),
  status: z.string(),
  message: nstr,
});

export const Incidents = z.looseObject({ incidents: z.array(Incident), count: z.number() });

export const Outlier = z.looseObject({
  id: z.string(),
  dataset: z.string(),
  label: nstr,
  value: nnum,
  z: nnum,
  change: nnum,
  basis: z.string(),
  ts: z.string(),
  state: z.string(),
  suggestion: z.string(),
  needs_review: z.boolean(),
  decided_by: nstr,
});

const OutlierCounts = z.looseObject({
  open: z.number(),
  quarantined: z.number(),
  accepted: z.number(),
  escalated: z.number(),
  awaiting_review: z.number(),
});

export const Outliers = z.looseObject({ outliers: z.array(Outlier), counts: OutlierCounts, count: z.number() });

export const OutlierDecided = z.looseObject({ outlier: Outlier, counts: OutlierCounts });

export const Sla = z.looseObject({
  sla: z.array(
    z.looseObject({
      source_id: z.string(),
      name: z.string(),
      schedule: z.string(),
      state: z.string(),
      age_seconds: nnum,
      detail: z.string(),
    }),
  ),
  count: z.number(),
});

export const Ticket = z.looseObject({ id: z.string(), title: z.string(), status: z.string(), severity: z.string() });
export const Tickets = z.looseObject({ tickets: z.array(Ticket), count: z.number() });
export const TicketOpened = z.looseObject({ ticket: Ticket, tracker: z.string() });

/** POST /api/quality/run. */
export const QualityRun = z.looseObject({ count: z.number(), summary: QualitySummary });

export type QualitySeries = z.infer<typeof QualitySeries>;
export type Outlier = z.infer<typeof Outlier>;
export type Outliers = z.infer<typeof Outliers>;
export type Sla = z.infer<typeof Sla>;
export type Incident = z.infer<typeof Incident>;
