// @vitest-environment node
/* npm run check:api — validate every schema against a running server.
 *
 * Not part of `npm test` or CI: it needs the API up (uv run python -m
 * tradinglib.api) and live vendors. TRADINGLIB_URL picks the server
 * (default http://127.0.0.1:8200); SAVE_FIXTURES=1 also writes the first
 * symbol's responses, trimmed, to __fixtures__/ for the unit tests, and
 * SAVE_ONLY=a,b limits that to the named endpoints.
 */
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { ApiClient, ApiError, ApiSchemaError } from "./client";
import { makeApi, readEndpoints } from "./endpoints";

const base = process.env.TRADINGLIB_URL ?? "http://127.0.0.1:8200";
const symbols = (process.env.SYMBOLS ?? "AAPL,MSFT,NVDA,SPY").split(",");
const save = process.env.SAVE_FIXTURES === "1";
const saveOnly = process.env.SAVE_ONLY ? new Set(process.env.SAVE_ONLY.split(",")) : null;
const fixtures = join(import.meta.dirname, "__fixtures__");

/** Keep fixtures small: at most three items per array. */
function trim(value: unknown): unknown {
  if (Array.isArray(value)) return value.slice(0, 3).map(trim);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, trim(v)]));
  }
  return value;
}

const api = makeApi(new ApiClient({ base, timeoutMs: 120_000 }));
const active = await api.universes.active();
const universe = active.universe?.id ?? "us-quality-50";

describe.each(symbols)("%s against " + base, (symbol) => {
  const calls = Object.entries(readEndpoints(api, symbol, universe));

  it.each(calls)("%s matches its schema", async (name, call) => {
    try {
      const body = await call();
      if (save && symbol === symbols[0] && (!saveOnly || saveOnly.has(name))) {
        mkdirSync(fixtures, { recursive: true });
        writeFileSync(join(fixtures, `${name}.json`), `${JSON.stringify(trim(body), null, 2)}\n`);
      }
    } catch (err) {
      if (err instanceof ApiSchemaError) {
        throw new Error(`${err.message}\n${JSON.stringify(err.issues, null, 2)}`, { cause: err });
      }
      throw err;
    }
  }, 180_000);
});

describe("an unknown ticker", () => {
  it("is a 404 from company and thesis, not an empty payload", async () => {
    for (const call of [api.research.company, api.research.thesis]) {
      const failure = await call("ZZZZQ").catch((err: unknown) => err);
      expect(failure).toBeInstanceOf(ApiError);
      expect((failure as ApiError).missing).toBe(true);
    }
  }, 60_000);
});
