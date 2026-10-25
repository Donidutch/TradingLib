/* Every endpoint against a real response saved by `npm run check:api`
 * (SAVE_FIXTURES=1), and against the same response with one field broken. */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { ApiClient, ApiSchemaError } from "./client";
import { makeApi, readEndpoints } from "./endpoints";
import { BacktestList } from "./schemas/lab";
import { News } from "./schemas/research";

const fixture = (name: string): Record<string, unknown> =>
  JSON.parse(readFileSync(join(import.meta.dirname, "__fixtures__", `${name}.json`), "utf8")) as Record<
    string,
    unknown
  >;

/** A field each page reads, with a value of the wrong type. */
const BROKEN: Record<string, [string, unknown]> = {
  status: ["engine_health", null],
  health: ["version", 1],
  "universes.listWithMembers": ["universes", [{ id: "x" }]],
  "universes.changelog": ["entries", [{ as_of: "2026-10-25" }]],
  "screens.metrics": ["metrics", [{ key: "roic" }]],
  "screens.list": ["screens", [{ id: "x" }]],
  "screens.alerts": ["alerts", null],
  "screens.preview": ["base_count", "50"],
  "quality.datasets": ["datasets", [{ key: "x" }]],
  settings: ["keys", [{ provider: "fred" }]],
  search: ["results", {}],
  "universes.list": ["universes", null],
  "universes.active": ["scope", "us-quality-50"],
  "universes.members": ["members", {}],
  "universes.breadth": ["percent", "61%"],
  "watchlists.list": ["watchlists", [{ id: "x" }]],
  "macro.panel": ["series", [{ series_id: "DGS10" }]],
  "agents.inbox": ["items", [{ id: "x" }]],
  "agents.findings": ["findings", [{ id: "x" }]],
  "lab.hypotheses": ["hypotheses", [{ id: "x" }]],
  "lab.backtests": ["backtests", [{ id: "x" }]],
  "research.resolve": ["results", [{ name: "Apple" }]],
  "research.ranges": ["ranges", [{ key: "1y" }]],
  "research.company": ["profile", null],
  "research.price": ["points", [{ time: "2026-01-02", close: "1" }]],
  "research.financials": ["annual", [{ period: "FY2025" }]],
  "research.estimates": ["consensus", null],
  "research.valuation": ["multiples", { ev_to_sales: { value: 1 } }],
  "research.news": ["aggregate", null],
  "research.thesis": ["catalysts", [{ label: "x" }]],
  "research.altdata": ["signals", { google_trends: { value: "high" } }],
  "research.filings": ["items", [{ form: "10-K" }]],
  "research.memory": ["thesis", null],
  "research.freshness": ["components", { quote: { stale: "no" } }],
};

const fetchMock = vi.fn<(url: string) => Promise<Response>>();
beforeEach(() => vi.stubGlobal("fetch", fetchMock));
afterEach(() => vi.unstubAllGlobals());

function serve(body: unknown) {
  fetchMock.mockResolvedValueOnce(
    new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } }),
  );
}

const calls = (): Record<string, () => Promise<unknown>> =>
  readEndpoints(makeApi(new ApiClient()), "AAPL", "us-quality-50");
const endpoints = Object.keys(calls());

describe.each(endpoints)("%s", (name) => {
  const call = () => {
    const fn = calls()[name];
    if (!fn) throw new Error(`no endpoint ${name}`);
    return fn();
  };

  it("accepts the real response", async () => {
    serve(fixture(name));
    await expect(call()).resolves.toBeTruthy();
  });

  it("rejects a malformed response", async () => {
    const broken = BROKEN[name];
    if (!broken) throw new Error(`no broken field for ${name}`);
    serve({ ...fixture(name), [broken[0]]: broken[1] });
    await expect(call()).rejects.toBeInstanceOf(ApiSchemaError);
  });
});

it("breaks a field for every endpoint", () => {
  expect(Object.keys(BROKEN).sort()).toEqual([...endpoints].sort());
});

it("calls the routes the backend serves", async () => {
  const api = makeApi(new ApiClient());
  serve(fixture("research.price"));
  await api.research.price(" aapl ", "1y");
  serve(fixture("research.company"));
  await api.research.company("brk.b");
  expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
    "/api/research/company/AAPL/price?range=1y",
    "/api/research/company/BRK.B",
  ]);
});

/* The saved responses have no news and no backtests, so these two item shapes
 * are built from their serializers (NewsItem.as_dict plus the sentiment fields
 * CompanyResearch.news adds; BacktestRecord.as_dict). */
it("accepts a scored news item", () => {
  const item = {
    symbol: "AAPL",
    headline: "Apple beats estimates",
    url: "https://example.com/a",
    published_at: "2026-10-24T12:00:00+00:00",
    publisher: null,
    summary: null,
    sentiment: 0.42,
    source: "yfinance",
    sentiment_label: "positive",
    sentiment_scores: { neg: 0, neu: 0.6, pos: 0.4, compound: 0.42 },
    age_hours: 27.5,
  };
  expect(News.safeParse({ ...fixture("research.news"), items: [item], count: 1 }).success).toBe(true);
});

it("accepts a backtest row", () => {
  const row = {
    id: "bt_1",
    name: "Momentum 12-1",
    status: "finished",
    source: "lab",
    hypothesis_id: null,
    experiment_id: null,
    universe_id: "us-quality-50",
    screen_id: null,
    config: {},
    signal: "momentum",
    benchmark: "SPY",
    created_at: "2026-10-24T12:00:00+00:00",
    started_at: null,
    finished_at: null,
    duration_ms: null,
    symbols: ["AAPL"],
    symbol_count: 1,
    stats: null,
    in_sample: null,
    out_of_sample: null,
    warnings: [],
    error: null,
    periods_per_year: 252,
    artifact_bundle: null,
  };
  expect(BacktestList.safeParse({ backtests: [row], count: 1 }).success).toBe(true);
});
