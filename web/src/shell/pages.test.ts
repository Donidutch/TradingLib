import { targetForHash } from "./pages";

describe("targetForHash", () => {
  it.each([
    ["#overview", { kind: "app", to: "/" }],
    ["", { kind: "app", to: "/" }],
    ["#overview/nvda", { kind: "app", to: "/", symbol: "NVDA" }],
    ["#stock", { kind: "app", to: "/stock" }],
    ["#stock/NVDA", { kind: "app", to: "/stock/$symbol", symbol: "NVDA" }],
    ["stock/BRK.B", { kind: "app", to: "/stock/$symbol", symbol: "BRK.B" }],
    ["#tl/lab/backtests?id=BT-3", { kind: "legacy", href: "/#tl/lab/backtests?id=BT-3" }],
    ["#tl/lab", { kind: "app", to: "/lab" }],
    ["#tl/lab/hypotheses?id=H-1", { kind: "app", to: "/lab/$tab", tab: "hypotheses", id: "H-1" }],
    ["#tl/lab/experiments", { kind: "app", to: "/lab/$tab", tab: "experiments" }],
    ["#tl/lab/pipeline", { kind: "legacy", href: "/#tl/lab/pipeline" }],
    ["#data/screener", { kind: "app", to: "/data/$tab", tab: "screener" }],
    ["#data/quality", { kind: "app", to: "/data/$tab", tab: "quality" }],
    ["#data/source-catalog", { kind: "app", to: "/data/$tab", tab: "source-catalog" }],
    ["#data/nope", { kind: "legacy", href: "/#data/nope" }],
    ["#settings", { kind: "app", to: "/settings" }],
    ["#docs", { kind: "app", to: "/docs" }],
    ["#docs/shortcuts", { kind: "app", to: "/docs/$article", article: "shortcuts" }],
    ["#agents/inbox", { kind: "legacy", href: "/#agents/inbox" }],
  ])("%s", (hash, target) => {
    expect(targetForHash(hash)).toEqual(target);
  });
});
