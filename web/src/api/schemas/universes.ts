import { z } from "zod";
import { nnum, nstr } from "./common";

const Member = z.looseObject({
  symbol: z.string(),
  name: nstr,
  sector: nstr,
  weight: nnum,
  added_on: nstr,
  removed_on: nstr,
});

export const Universe = z.looseObject({
  id: z.string(),
  name: z.string(),
  description: nstr,
  builtin: z.boolean(),
  source: nstr,
  rule: nstr,
  active: z.boolean(),
  symbol_count: z.number(),
  member_count: z.number(),
  created_at: nstr,
  updated_at: nstr,
  members_fetched_at: nstr,
  /** With `include_members=true`. */
  members: z.array(Member).optional(),
});

export const UniverseList = z.looseObject({
  universes: z.array(Universe),
  count: z.number(),
  active: nstr,
});

/** GET and POST /api/universes/active: a universe or a watchlist scope. */
export const ActiveScope = z.looseObject({
  active: z.string(),
  scope: z.looseObject({
    kind: z.string(),
    id: z.string(),
    name: z.string(),
    ref: z.string(),
    symbols: z.array(z.string()),
    symbol_count: z.number(),
  }),
  universe: Universe.nullable(),
  symbols: z.array(z.string()),
});

export const Members = z.looseObject({
  universe: z.string(),
  name: z.string(),
  as_of: nstr,
  /** "current", "snapshot" or "membership-dates". */
  basis: z.string(),
  snapshot_as_of: nstr.optional(),
  members: z.array(Member),
  count: z.number(),
  members_fetched_at: nstr,
});

/** GET /api/universes/{id}/changelog: snapshot-to-snapshot membership changes. */
export const Changelog = z.looseObject({
  universe: z.string(),
  count: z.number(),
  rule_version: nnum,
  entries: z.array(
    z.looseObject({
      as_of: z.string(),
      symbol_count: z.number(),
      net: z.number(),
      source: nstr,
      rule: nstr,
      baseline: z.boolean(),
      rule_changed: z.boolean(),
      added: z.array(z.string()),
      dropped: z.array(z.string()),
      added_by_rule: z.array(z.string()).optional(),
      dropped_by_rule: z.array(z.string()).optional(),
      corporate_actions: z.array(z.looseObject({ from: z.string(), to: z.string(), detail: z.string().optional() })),
    }),
  ),
});

/** POST /api/universes/{id}/snapshot. */
export const Snapshot = z.looseObject({ universe: z.string(), as_of: z.string(), symbol_count: z.number(), source: z.string() });

/** DELETE /api/universes/{id} and /api/watchlists/{id}. */
export const Deleted = z.looseObject({ deleted: z.string() });

/** Share of members above their moving average, with what it could not cover. */
export const Breadth = z.looseObject({
  window: z.number(),
  interval: z.string(),
  requested: z.number(),
  covered: z.number(),
  above: z.number(),
  below: z.number(),
  percent: nnum,
  as_of: nstr,
  uncovered: z.array(z.string()),
  reason: nstr,
  universe_id: z.string(),
  universe: z.string(),
});

export type Universe = z.infer<typeof Universe>;
export type UniverseList = z.infer<typeof UniverseList>;
export type ActiveScope = z.infer<typeof ActiveScope>;
export type Members = z.infer<typeof Members>;
export type Breadth = z.infer<typeof Breadth>;
export type Changelog = z.infer<typeof Changelog>;
