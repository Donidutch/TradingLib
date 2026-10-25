import { fixture } from "../../test/render-app";
import type { MetricCatalogue } from "../../api/schemas/screens";
import type { UniverseList } from "../../api/schemas/universes";
import type { WatchlistList } from "../../api/schemas/watchlists";
import { apiRules, asOfFor, basisLabel, collectionRows, defaultRules, editableRules, inputValue, ranked, unitFor, withField, withOperator, withValue } from "./rules";

const metrics = fixture<MetricCatalogue>("screens.metrics").metrics;
const def = (key: string) => metrics.find((m) => m.key === key);

describe("collectionRows", () => {
  it("lists universes then watchlists with their construction and status", () => {
    const u = fixture<UniverseList>("universes.listWithMembers");
    const w = fixture<WatchlistList>("watchlists.list");
    const rows = collectionRows(u.universes, u.active, w.watchlists, w.default);
    expect(rows.map((r) => [r.kind, r.id, r.construction, r.symbols, r.refresh, r.status.text])).toEqual([
      ["Universe", "nasdaq100", "Published index constituents", 101, "On demand", "Ready"],
      ["Universe", "sp500", "Published index constituents", 503, "On demand", "Ready"],
      ["Universe", "us-quality-50", "Static built-in definition", 50, "On demand", "Active"],
      ["Watchlist", "primary-research", "Manual symbols", 2, "Manual", "Default"],
    ]);
  });

  it("marks an empty universe as not fetched", () => {
    const base = fixture<UniverseList>("universes.listWithMembers").universes[0];
    if (!base) throw new Error("fixture has no universe");
    const [row] = collectionRows([{ ...base, member_count: 0, symbol_count: 0, source: "user", description: "Mine" }], null, [], null);
    expect(row).toMatchObject({ construction: "Mine", status: { text: "Not fetched", tone: "warn" } });
  });
});

it("dates the as-of slider mid-year, and today as no date", () => {
  const now = new Date("2026-10-25T00:00:00Z");
  expect(asOfFor(2019, now)).toBe("2019-06-30");
  expect(asOfFor(2026, now)).toBeNull();
  expect(basisLabel("snapshot")).toEqual({ text: "PIT ✓ snapshot", tone: "up" });
  expect(basisLabel("membership-dates").tone).toBe("warn");
});

describe("rule editing", () => {
  it("starts from the reference's rules that this build can screen on", () => {
    expect(defaultRules(metrics).map((r) => r.field)).toEqual(["mkt_cap", "rev_growth", "fcf_margin", "country", "roic", "mom_12_1", "net_debt_ebitda"]);
    expect(defaultRules([]).length).toBe(0);
  });

  it("edits market cap in $bn and percentiles as plain numbers", () => {
    const cap = { field: "mkt_cap", operator: ">", value: 2e9 };
    expect(inputValue(cap, def("mkt_cap"))).toBe("2");
    expect(unitFor(cap, def("mkt_cap"))).toBe("B");
    expect(withValue(cap, "5", def("mkt_cap")).value).toBe(5e9);
    expect(withValue(cap, "abc", def("mkt_cap")).value).toBe(0);
    const pctl = withOperator(cap, "top_pctl");
    expect(pctl).toMatchObject({ value: 25, scope: "all" });
    expect(unitFor(pctl, def("mkt_cap"))).toBe("%");
    expect(withValue(pctl, "10", def("mkt_cap")).value).toBe(10);
  });

  it("switches operators to fit a categorical field", () => {
    const rule = { field: "roic", operator: ">", value: 10 };
    expect(withField(rule, "country", def("country"))).toEqual({ field: "country", operator: "in", value: "Any" });
    expect(withField({ field: "country", operator: "in", value: "US" }, "roic", def("roic"))).toEqual({ field: "roic", operator: ">", value: 0 });
    expect(withOperator(rule, "not_in").value).toBe("Any");
  });

  it("round-trips rules with the API", () => {
    const rules = [{ field: "roic", operator: "top_pctl", value: 30, scope: "sector", value2: null }];
    expect(apiRules(rules)).toEqual([{ field: "roic", operator: "top_pctl", value: 30, scope: "sector" }]);
    expect(editableRules([{ field: "country", operator: "in", value: ["US", "DK"] }])[0]?.value).toBe("US, DK");
  });
});

it("ranks survivors best first with missing values last", () => {
  const rows = [{ symbol: "A", composite: 50 }, { symbol: "B", composite: null }, { symbol: "C", composite: 90 }];
  expect(ranked(rows, "composite").map((r) => r.symbol)).toEqual(["C", "A", "B"]);
});
