import { z } from "zod";
import { TapeQuote } from "../api/schemas/stream";
import { FakeSocket, fakeSocketFactory } from "../test/fake-socket";
import { LiveStream, channelMatches } from "./stream";

const quote = {
  symbol: "AAPL",
  price: 347.73,
  ts: "2026-10-23T20:00:01+00:00",
  bid: null,
  ask: null,
  bid_size: null,
  ask_size: null,
  volume: 39496500,
  prev_close: 348.6,
  source: "yfinance",
  change: -0.87,
  change_pct: -0.25,
  kind: "quote",
  delayed: true,
  feed: "yfinance",
};

function makeStream() {
  return new LiveStream({ url: "ws://test/ws", socketFactory: fakeSocketFactory, baseBackoffMs: 1000 });
}

beforeEach(() => {
  FakeSocket.instances = [];
  vi.useFakeTimers();
  vi.spyOn(Math, "random").mockReturnValue(1); // backoff = its ceiling
});
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("channelMatches", () => {
  it("mirrors tradinglib.events.channel_matches", () => {
    expect(channelMatches("tape", "tape")).toBe(true);
    expect(channelMatches("tape", "tape.NVDA")).toBe(true);
    expect(channelMatches("tape.*", "tape")).toBe(true);
    expect(channelMatches("*", "status")).toBe(true);
    expect(channelMatches("tape.NVDA", "tape.AAPL")).toBe(false);
    expect(channelMatches("tape", "tapeworm")).toBe(false);
  });
});

describe("LiveStream", () => {
  it("sends wanted subscriptions on open and delivers validated payloads", () => {
    const stream = makeStream();
    const got: unknown[] = [];
    stream.listen("tape", TapeQuote, (payload, channel) => got.push([channel, payload.price]), {
      symbols: ["AAPL"],
    });
    stream.connect();
    expect(stream.snapshot().state).toBe("connecting");

    const socket = FakeSocket.latest();
    socket.open();
    expect(stream.snapshot().state).toBe("open");
    expect(socket.sent).toEqual([{ action: "subscribe", channel: "tape", params: { symbols: ["AAPL"] } }]);

    socket.event("tape.AAPL", quote);
    socket.event("status", { engine_health: "ok" });
    expect(got).toEqual([["tape.AAPL", 347.73]]);
    expect(stream.snapshot().received).toBe(2);
  });

  it("drops a payload that fails its schema and keeps dispatching", () => {
    const error = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const stream = makeStream();
    const got: number[] = [];
    stream.listen("tape", TapeQuote, (payload) => got.push(payload.price ?? 0));
    stream.connect();
    FakeSocket.latest().open();
    FakeSocket.latest().event("tape.AAPL", { ...quote, price: "347" });
    FakeSocket.latest().event("tape.AAPL", quote);
    expect(got).toEqual([347.73]);
    expect(stream.snapshot().rejected).toBe(1);
    expect(error).toHaveBeenCalledWith(expect.stringContaining("rejected"), expect.anything());
  });

  it("isolates a throwing handler", () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    const stream = makeStream();
    const got: string[] = [];
    stream.on("status", z.unknown(), () => {
      throw new Error("bad handler");
    });
    stream.on("status", z.unknown(), () => got.push("second"));
    stream.connect();
    FakeSocket.latest().open();
    FakeSocket.latest().event("status", {});
    expect(got).toEqual(["second"]);
  });

  it("reconnects with backoff, shows why, and resubscribes when it recovers", () => {
    const stream = makeStream();
    stream.subscribe("status");
    stream.connect();
    FakeSocket.latest().open();

    FakeSocket.latest().drop(1006);
    const down = stream.snapshot();
    expect(down.state).toBe("reconnecting");
    expect(down.lastError).toBe("socket closed (code 1006)");
    expect(down.nextRetryInMs).toBe(1000);

    vi.advanceTimersByTime(1000);
    FakeSocket.latest().drop(1006); // still down: the wait doubles
    expect(stream.snapshot().nextRetryInMs).toBe(2000);
    expect(stream.snapshot().attempts).toBe(2);

    vi.advanceTimersByTime(2000);
    const socket = FakeSocket.latest();
    socket.open();
    expect(stream.snapshot()).toMatchObject({ state: "open", attempts: 0, lastError: null });
    expect(socket.sent).toEqual([{ action: "subscribe", channel: "status", params: {} }]);
    expect(FakeSocket.instances).toHaveLength(3);
  });

  it("tears down an open socket that goes silent", () => {
    const stream = new LiveStream({
      url: "ws://test/ws",
      socketFactory: fakeSocketFactory,
      pingIntervalMs: 1000,
      idleTimeoutMs: 10_000,
    });
    stream.connect();
    const socket = FakeSocket.latest();
    socket.open();
    vi.advanceTimersByTime(1000);
    expect(socket.sent).toContainEqual({ action: "ping" });

    vi.advanceTimersByTime(10_000);
    expect(stream.snapshot().state).toBe("reconnecting");
    expect(stream.snapshot().lastError).toBe("no traffic for 10s");
  });

  it("unsubscribes when the last listener leaves, and close() stops retrying", () => {
    const stream = makeStream();
    stream.connect();
    const socket = FakeSocket.latest();
    socket.open();
    const offA = stream.listen("notifications", z.unknown(), () => undefined);
    const offB = stream.listen("notifications", z.unknown(), () => undefined);
    offA();
    expect(socket.sent.filter((f) => f.action === "unsubscribe")).toEqual([]);
    offB();
    expect(socket.sent.at(-1)).toEqual({ action: "unsubscribe", channel: "notifications" });

    stream.close();
    expect(stream.snapshot().state).toBe("closed");
    vi.advanceTimersByTime(60_000);
    expect(FakeSocket.instances).toHaveLength(1);
  });

  it("records the server's channels and its error frames", () => {
    vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const stream = makeStream();
    stream.connect();
    FakeSocket.latest().open();
    FakeSocket.latest().frame({ type: "welcome", channels: ["status", "tape"] });
    FakeSocket.latest().frame({ type: "error", error: "unknown_channel", detail: "bogus" });
    expect(stream.snapshot()).toMatchObject({ serverChannels: ["status", "tape"], lastError: "unknown_channel" });
  });
});
