import { z } from "zod";
import { nnum, nstr } from "./common";

/** tradinglib.agents.inbox.InboxItem.as_dict (plus per-kind extras). */
export const InboxItem = z.looseObject({
  id: z.string(),
  type: z.string(),
  title: z.string(),
  reason: z.string(),
  urgency: z.string(),
  status: z.string(),
  symbol: z.string(),
  source: nstr,
  owner: nstr,
  action: z.string(),
  route: z.string(),
  context: z.record(z.string(), z.unknown()),
  created_at: nstr,
  age_seconds: nnum,
  confidence_pct: nnum,
});

export const Inbox = z.looseObject({
  items: z.array(InboxItem),
  count: z.number(),
  total: z.number(),
  types: z.array(z.string()),
  counts: z.record(z.string(), z.number()),
  sources_available: z.record(z.string(), z.boolean()),
  pending_approvals: z.number(),
  unacknowledged_findings: z.number(),
});

/** tradinglib.agents.findings.Finding.as_dict */
export const Finding = z.looseObject({
  id: z.string(),
  title: z.string(),
  summary: z.string(),
  confidence: z.string(),
  confidence_pct: nnum,
  status: z.string(),
  symbols: z.array(z.string()),
  symbol_label: z.string(),
  tags: z.array(z.string()),
  run_id: nstr,
  agent_id: nstr,
  hypothesis_id: nstr,
  evidence_count: z.number(),
  acknowledged: z.boolean(),
  created_at: nstr,
  updated_at: nstr,
});

export const FindingList = z.looseObject({
  findings: z.array(Finding),
  count: z.number(),
  counts: z.record(z.string(), z.number()),
});

/** tradinglib.agents.runs: a pipeline run, as the lab's activity panel reads it. */
export const AgentRun = z.looseObject({
  id: z.string(),
  name: nstr.optional(),
  pipeline_id: nstr.optional(),
  status: z.string(),
  trigger: nstr.optional(),
  current_stage_id: nstr.optional(),
  error: nstr.optional(),
  cost_usd: nnum.optional(),
  started_at: nstr.optional(),
  finished_at: nstr.optional(),
  created_at: nstr.optional(),
  progress: z.looseObject({ completed: z.number().optional(), total: z.number().optional() }).nullable().optional(),
  stages: z
    .array(z.looseObject({ stage_id: nstr.optional(), name: nstr.optional(), status: nstr.optional(), agent_id: nstr.optional() }))
    .optional(),
});
export const AgentRuns = z.looseObject({ runs: z.array(AgentRun), count: z.number() });

export const AgentDefinitions = z.looseObject({ agents: z.array(z.looseObject({ id: z.string(), name: z.string() })), count: z.number() });

export type AgentRun = z.infer<typeof AgentRun>;
export type InboxItem = z.infer<typeof InboxItem>;
export type Inbox = z.infer<typeof Inbox>;
export type Finding = z.infer<typeof Finding>;
export type FindingList = z.infer<typeof FindingList>;
