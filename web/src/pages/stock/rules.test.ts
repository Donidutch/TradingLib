import { ApiError } from "../../api/client";
import type { AltData, Company } from "../../api/schemas/research";
import { barHeights, freshnessSummary, millions, reason, sentimentTone, signalText, signalTitle } from "./rules";

type Signal = AltData["signals"][string];
const signal = (patch: Partial<Signal>): Signal => ({ unit: "index", value: null, reason: null, ...patch });

describe("reason", () => {
  it("prefers the server's own detail", () => {
    const err = new ApiError("GET x → 404", { status: 404, payload: { detail: { error: "not_found", detail: "Yahoo returned no company profile" } } });
    expect(reason(err)).toBe("Yahoo returned no company profile");
    expect(reason(new ApiError("GET x → 500 boom", { status: 500, payload: { detail: "boom" } }))).toBe("GET x → 500 boom");
    expect(reason("plain")).toBe("plain");
  });
});

it("shows figures in millions", () => {
  expect(millions(394_328_000_000)).toBe("394,328");
  expect(millions(null)).toBe("—");
});

describe("freshnessSummary", () => {
  const report = (stale: boolean) => ({
    component: "quote", source: "yfinance", fetched_at: new Date().toISOString(), age_seconds: 1, stale, max_age_seconds: 120, detail: null,
  });
  it("is CURRENT unless a component is stale, and lists each age", () => {
    expect(freshnessSummary({ quote: report(false) } as Company["freshness"])).toMatchObject({ text: "CURRENT", tone: "up", title: "quote 0s (yfinance)" });
    expect(freshnessSummary({ quote: report(false), news: report(true) } as Company["freshness"])).toMatchObject({ text: "STALE", tone: "warn" });
    expect(freshnessSummary({})).toEqual({ text: "—", title: "no freshness report" });
  });
});

describe("signalText", () => {
  it("says why a signal has no value", () => {
    expect(signalText(signal({ reason: "no public job board", basis: "Greenhouse, Ashby, Lever" }))).toEqual({
      text: "—", tone: null, title: "Greenhouse, Ashby, Lever · no public job board · index",
    });
    expect(signalText(undefined).title).toBe("no source configured");
  });

  it("formats rank moves, changes and levels", () => {
    expect(signalText(signal({ value: 16.67 }), { arrow: true })).toMatchObject({ text: "↑ 17", tone: "up" });
    expect(signalText(signal({ value: -3 }), { arrow: true })).toMatchObject({ text: "↓ 3", tone: "down" });
    expect(signalText(signal({ value: 0.067 }), { percent: true, fraction: true })).toMatchObject({ text: "+6.7%", tone: "up" });
    expect(signalText(signal({ value: 12 }), { percent: true, level: true })).toMatchObject({ text: "12.0%", tone: null });
    expect(signalText(signal({ value: 0.25 }), { decimals: 2, sign: true })).toMatchObject({ text: "+0.25", tone: "up" });
    expect(signalText(signal({ value: 70, change_90d_pct: 4 }), { decimals: 0 }).title).toBe("index · 90d +4.0%");
  });

  it("titles a signal with its derivation, units and source", () => {
    expect(signalTitle(signal({ basis: "Google Trends", source: "pytrends", as_of: "2026-10-23T00:00:00Z" }))).toBe(
      "Google Trends · index · source pytrends · as of 2026-10-23",
    );
  });
});

it("scales bars to the largest figure, keeping small ones visible", () => {
  expect(barHeights([{ revenue: 200, ebitda: 1 }, { revenue: 100, ebitda: null }])).toEqual([
    { revenue: 100, ebitda: 1 },
    { revenue: 50, ebitda: null },
  ]);
});

it("tones sentiment labels", () => {
  expect([sentimentTone("positive"), sentimentTone("negative"), sentimentTone("neutral")]).toEqual(["up", "down", null]);
});
