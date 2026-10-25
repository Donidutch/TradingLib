import { z } from "zod";
import { nnum, nstr } from "./common";

/** One credential slot. The API reports status and source, never the key. */
const KeyStatus = z.looseObject({
  provider: z.string(),
  label: z.string(),
  env: z.string(),
  configured: z.boolean(),
  /** "stored", "environment", "config", or null when not configured. */
  source: nstr,
  updated_at: nstr,
});

/** GET and PUT /api/settings (tradinglib/api/routes/settings.py). */
export const Settings = z.looseObject({
  theme: z.string(),
  density: z.string(),
  timezone: z.string(),
  benchmark: z.string(),
  default_universe: nstr,
  active_universe: nstr,
  sources: z.record(z.string(), z.string()),
  updated_at: nstr,
  options: z.looseObject({
    themes: z.array(z.string()),
    densities: z.array(z.string()),
    timezones: z.array(z.string()),
    benchmarks: z.array(z.looseObject({ symbol: z.string(), label: z.string() })),
    universes: z.array(z.looseObject({ id: z.string(), name: z.string(), builtin: z.boolean() })),
  }),
  keys: z.array(KeyStatus),
  providers: z.array(
    z.looseObject({
      provider: z.string(),
      configured: z.boolean(),
      note: z.string().optional(),
      keys: z.array(z.string()),
    }),
  ),
  budgets: z.looseObject({ per_run_usd: nnum, daily_usd: nnum, monthly_usd: nnum }),
  config_path: nstr,
  secret_storage: z.string(),
  /** On a save that only takes effect after a restart. */
  restart_detail: z.string().optional(),
});

export interface SettingsPatch {
  theme?: string;
  density?: string;
  timezone?: string;
  benchmark?: string;
  default_universe?: string;
  /** provider → new value; "" clears the stored key. */
  keys?: Record<string, string>;
}

/** GET /api/health: liveness and the build version. */
export const Health = z.looseObject({ ok: z.boolean(), version: z.string() });

export type Settings = z.infer<typeof Settings>;
export type KeyStatus = z.infer<typeof KeyStatus>;
