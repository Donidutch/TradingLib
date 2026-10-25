import { z } from "zod";
import { nnum, nstr } from "./common";

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
  basis: z.string(),
  members: z.array(
    z.looseObject({
      symbol: z.string(),
      name: nstr,
      sector: nstr,
      weight: nnum,
      added_on: nstr,
      removed_on: nstr,
    }),
  ),
  count: z.number(),
  members_fetched_at: nstr,
});

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
