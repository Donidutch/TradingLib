// @vitest-environment node
/* npm run check:api:writes — every write the app makes, and the live frames
 * they cause, against a throwaway API (src/test/throwaway-server.ts). Grows
 * with each page slice. */
import { inject } from "vitest";
import { z } from "zod";
import { ApiClient } from "./client";
import { makeApi } from "./endpoints";
import { IngestFrame, LabFrame, QualityFrame } from "./schemas/frames";
import { Notification } from "./schemas/stream";

const base = inject("writesUrl");
const api = makeApi(new ApiClient({ base, timeoutMs: 120_000 }));

/** Every frame the socket receives while the checks run, by channel. */
const frames: { channel: string; payload: unknown }[] = [];
let socket: WebSocket;

beforeAll(async () => {
  socket = new WebSocket(`${base.replace("http", "ws")}/ws`);
  await new Promise((resolve, reject) => {
    socket.onopen = resolve;
    socket.onerror = reject;
  });
  socket.onmessage = (event) => {
    const frame = JSON.parse(String(event.data)) as { type: string; channel?: string; payload?: unknown };
    if (frame.type === "event" && frame.channel) frames.push({ channel: frame.channel, payload: frame.payload });
  };
  for (const channel of ["lab", "quality", "ingest", "notifications", "runs", "approvals"]) {
    socket.send(JSON.stringify({ action: "subscribe", channel, params: {} }));
  }
});

afterAll(() => socket.close());

async function framesOn(channel: string, count = 1, ms = 30_000) {
  const deadline = Date.now() + ms;
  while (frames.filter((f) => f.channel === channel).length < count && Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 200));
  }
  return frames.filter((f) => f.channel === channel).map((f) => f.payload);
}

function expectAll<S extends z.ZodType>(schema: S, payloads: unknown[]) {
  for (const payload of payloads) {
    const parsed = schema.safeParse(payload);
    if (!parsed.success) throw new Error(`${JSON.stringify(payload).slice(0, 300)}\n${JSON.stringify(parsed.error.issues)}`);
  }
}

describe("watchlists and scope", () => {
  it("creates, adds, removes and selects, each answer matching its schema", async () => {
    const created = await api.watchlists.create({ name: "Writes check", symbols: ["AAPL"] });
    expect(created.symbols).toEqual(["AAPL"]);
    expect((await api.watchlists.addSymbol(created.id, "NVDA")).symbols).toEqual(["AAPL", "NVDA"]);
    expect((await api.watchlists.removeSymbol(created.id, "AAPL")).symbols).toEqual(["NVDA"]);
    const scope = await api.universes.setActive(`watchlist:${created.id}`);
    expect(scope.scope).toMatchObject({ kind: "watchlist", id: created.id });
    expect((await api.universes.setActive("universe:us-quality-50")).scope.id).toBe("us-quality-50");
  }, 60_000);
});

describe("collections", () => {
  it("creates, edits, snapshots and deletes a universe and a watchlist", async () => {
    const universe = await api.universes.create({ name: "Writes semis", symbols: "NVDA, AVGO, TXN", description: "check" });
    expect(universe.member_count).toBe(3);
    expect((await api.universes.update(universe.id, { name: "Writes semis 2", symbols: "NVDA, AVGO" })).name).toBe("Writes semis 2");
    expect((await api.universes.snapshot(universe.id)).symbol_count).toBe(2);
    expect((await api.universes.changelog(universe.id)).entries.length).toBeGreaterThan(0);
    expect((await api.universes.members(universe.id, { as_of: "2019-06-30" })).basis).toBeTruthy();
    expect((await api.universes.refresh("us-quality-50")).member_count).toBeGreaterThan(0);
    expect((await api.universes.remove(universe.id)).deleted).toBe(universe.id);

    const watchlist = await api.watchlists.create({ name: "Writes watch", symbols: "AAPL, MSFT" });
    expect((await api.watchlists.update(watchlist.id, { name: "Writes watch 2", symbols: "AAPL" })).symbols).toEqual(["AAPL"]);
    expect((await api.watchlists.get(watchlist.id)).name).toBe("Writes watch 2");
    expect((await api.watchlists.remove(watchlist.id)).deleted).toBe(watchlist.id);
  }, 120_000);
});

describe("screener", () => {
  it("saves, runs, queues a backtest, pins and checks alerts", async () => {
    const rules = [{ field: "mkt_cap", operator: ">", value: 2e9 }];
    expect((await api.screens.preview({ universe_id: "us-quality-50", rules })).base_count).toBeGreaterThan(0);
    const screen = await api.screens.create({ name: "Writes screen", universe_id: "us-quality-50", rules, rank_by: "composite" });
    const updated = await api.screens.update(screen.id, { rules: [...rules, { field: "roic", operator: "top_pctl", value: 30, scope: "sector" }] });
    expect(updated.rules).toHaveLength(2);
    const run = await api.screens.run(screen.id);
    expect(run.pass_count).toBeGreaterThan(0);
    const queued = await api.screens.queueBacktest(screen.id, { run_id: run.id, benchmark: "SPY" });
    expect(queued.backtest_id).toBeTruthy();
    expect((await api.screens.update(screen.id, { tags: ["pinned", "alert"] })).tags).toEqual(["pinned", "alert"]);
    await api.screens.checkAlerts();
    expect((await api.screens.alerts()).monitor?.watched).toContain(screen.id);
  }, 240_000);
});

describe("settings", () => {
  it("saves one field at a time and answers with the stored whole", async () => {
    const before = await api.settings.get();
    const light = await api.settings.save({ theme: "light" });
    expect(light.theme).toBe("light");
    expect(light.density).toBe(before.density);
    expect((await api.settings.save({ density: "comfortable", timezone: "UTC" })).timezone).toBe("UTC");
    expect((await api.settings.save({ theme: "dark", density: "compact", timezone: before.timezone })).theme).toBe("dark");
  }, 60_000);

  it("stores a key without ever returning it, and clears it", async () => {
    const secret = "writes-check-not-a-real-key";
    const saved = await api.settings.save({ keys: { fred: secret } });
    expect(JSON.stringify(saved)).not.toContain(secret);
    expect(saved.keys.find((k) => k.provider === "fred")).toMatchObject({ configured: true, source: "stored" });
    expect(JSON.stringify(await api.settings.get())).not.toContain(secret);
    const cleared = await api.settings.save({ keys: { fred: "" } });
    expect(cleared.keys.find((k) => k.provider === "fred")?.source).not.toBe("stored");
  }, 60_000);
});

describe("live frames", () => {
  it("quality: a check run publishes frames the app accepts", async () => {
    // The response body is the S3 slice's to type; here only its frames matter.
    await api.client.post("/api/quality/run", z.unknown(), {});
    const payloads = await framesOn("quality");
    expect(payloads.length).toBeGreaterThan(0);
    expectAll(QualityFrame, payloads);
  }, 120_000);

  it("lab: a real backtest publishes started and complete frames the app accepts", async () => {
    const config = {
      name: "writes-check",
      symbols: ["AAPL", "MSFT"],
      signal: { kind: "static", symbols: ["AAPL", "MSFT"], weighting: "equal" },
      benchmark: "SPY",
      rebalance: "monthly",
      oos_fraction: 0.3,
    };
    const response = await fetch(`${base}/api/lab/backtests/run`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ config }),
    });
    expect(response.status, await response.clone().text()).toBe(201);
    const payloads = await framesOn("lab", 2);
    expectAll(LabFrame, payloads);
    expect(payloads.map((p) => (p as { phase: string }).phase)).toEqual(expect.arrayContaining(["started", "complete"]));
  }, 120_000);

  it("every frame seen on any channel matches its schema", async () => {
    const by = (channel: string) => frames.filter((f) => f.channel === channel).map((f) => f.payload);
    expectAll(LabFrame, by("lab"));
    expectAll(QualityFrame, by("quality"));
    expectAll(IngestFrame, by("ingest"));
    expectAll(Notification, by("notifications"));
  });
});
