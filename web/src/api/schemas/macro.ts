import { z } from "zod";
import { nnum, nstr } from "./common";

/** GET /api/macro/series: the FRED readings on the Overview market strip. */
export const MacroPanel = z.looseObject({
  namespace: z.string(),
  series: z.array(
    z.looseObject({
      series_id: z.string(),
      label: z.string(),
      transform: z.string(),
      unit: z.string(),
      description: nstr,
      value: nnum,
      as_of: nstr,
      previous: nnum,
      previous_as_of: nstr,
      change: nnum,
      year_ago: nnum,
      change_1y: nnum,
      observations: z.number(),
      source: z.string(),
      stale: z.boolean(),
      reason: nstr,
    }),
  ),
  available: z.number(),
  total: z.number(),
});

export type MacroPanel = z.infer<typeof MacroPanel>;
export type MacroSeries = MacroPanel["series"][number];
