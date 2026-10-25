import { z } from "zod";
import { nstr } from "./common";

/** tradinglib.data.universe.Watchlist.as_dict */
export const Watchlist = z.looseObject({
  id: z.string(),
  name: z.string(),
  symbols: z.array(z.string()),
  symbol_count: z.number(),
  is_default: z.boolean(),
  active: z.boolean(),
  created_at: nstr,
  updated_at: nstr,
});

export const WatchlistList = z.looseObject({
  watchlists: z.array(Watchlist),
  count: z.number(),
  default: nstr,
});

export type Watchlist = z.infer<typeof Watchlist>;
export type WatchlistList = z.infer<typeof WatchlistList>;
