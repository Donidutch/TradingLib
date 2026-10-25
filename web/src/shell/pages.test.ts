import { targetForHash } from "./pages";

describe("targetForHash", () => {
  it.each([
    ["#overview", { kind: "app", to: "/" }],
    ["", { kind: "app", to: "/" }],
    ["#stock", { kind: "app", to: "/stock" }],
    ["#stock/NVDA", { kind: "app", to: "/stock/$symbol", symbol: "NVDA" }],
    ["stock/BRK.B", { kind: "app", to: "/stock/$symbol", symbol: "BRK.B" }],
    ["#tl/lab/backtests?id=BT-3", { kind: "legacy", href: "/#tl/lab/backtests?id=BT-3" }],
    ["#data/screener", { kind: "legacy", href: "/#data/screener" }],
    ["#settings", { kind: "legacy", href: "/#settings" }],
  ])("%s", (hash, target) => {
    expect(targetForHash(hash)).toEqual(target);
  });
});
