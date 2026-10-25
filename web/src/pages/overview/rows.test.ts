import type { PriceHistory } from "../../api/schemas/research";
import type { Breadth } from "../../api/schemas/universes";
import type { TapeQuote } from "../../api/schemas/stream";
import { fixture } from "../../test/render-app";
import { advanceDecline, agentsHref, breadth200, regime, vix } from "./rows";

const history = (closes: number[]): PriceHistory => ({
  ...fixture<PriceHistory>("research.price"),
  points: closes.map((close, i) => ({ time: `2026-01-${String(i + 1).padStart(2, "0")}T00:00:00Z`, open: null, high: null, low: null, close, volume: null })),
  source: "store",
});

describe("regime", () => {
  it("needs 200 closes, and says how many it has", () => {
    expect(regime(history([1, 2, 3]))).toEqual({
      text: "—",
      title: "only 3 SPY closes available; 200 are needed for the moving average",
    });
  });

  it("is risk-on above the 200-day average and risk-off below it", () => {
    const rising = Array.from({ length: 250 }, (_, i) => 100 + i);
    expect(regime(history(rising))).toMatchObject({ text: "RISK-ON", tone: "up" });
    expect(regime(history(rising.reverse()))).toMatchObject({ text: "RISK-OFF", tone: "down" });
    expect(regime(history(rising)).title).toMatch(/^SPY \d+\.\d\d vs its 200-day average \d+\.\d\d \(source store\)$/);
  });
});

describe("breadth200", () => {
  const base = fixture<Breadth>("universes.breadth");
  it("shows the share above, and how many members were excluded", () => {
    const row = breadth200({ ...base, percent: 0.61, above: 61, covered: 100, requested: 503, window: 200, universe: "S&P 500", as_of: "2026-10-23T04:00:00+00:00" });
    expect(row).toEqual({
      text: "61%",
      tone: "up",
      title:
        "61 of 100 S&P 500 constituents close above their own 200-day average (403 of 503 have fewer than 200 stored sessions and are excluded) · as of 2026-10-23",
    });
  });

  it("passes on the server's reason when there is no reading", () => {
    expect(breadth200({ ...base, percent: null, reason: "only 3 of 503 constituents have 200 stored daily closes" })).toEqual({
      text: "—",
      title: "only 3 of 503 constituents have 200 stored daily closes",
    });
  });
});

describe("advanceDecline", () => {
  const quote = (change_pct: number | null) => ({ change_pct }) as TapeQuote;
  it("waits for the first tape cycle", () => {
    expect(advanceDecline(new Map())).toMatchObject({ text: "—", title: "waiting for the first tape cycle" });
  });

  it("is the ratio of advancing to declining quotes, ignoring unchanged and unknown", () => {
    const quotes = new Map([["A", quote(1)], ["B", quote(2)], ["C", quote(-1)], ["D", quote(0)], ["E", quote(null)]]);
    expect(advanceDecline(quotes)).toEqual({
      text: "2.00",
      tone: "up",
      title: "2 advancing, 1 declining of 3 quoted symbols on the live tape",
    });
    expect(advanceDecline(new Map([["A", quote(1)]]))).toMatchObject({ text: "1 / 0" });
  });
});

describe("vix", () => {
  it("shows the last close, green when it fell", () => {
    expect(vix(history([19.03, 18.5]))).toMatchObject({ text: "18.5", tone: "up" });
    expect(vix(history([18.5, 19.45]))).toMatchObject({ text: "19.5", tone: "down" });
  });

  it("says when the vendor returned nothing", () => {
    expect(vix(history([]))).toEqual({ text: "—", title: "the vendor returned no ^VIX closes" });
  });
});

it("links an inbox item to its Agent Center route, skipping empty context", () => {
  expect(agentsHref("findings", { findingId: "FND-1", runId: null })).toBe("/#agents/findings?findingId=FND-1");
  expect(agentsHref("", {})).toBe("/#agents/inbox");
});
