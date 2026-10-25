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
