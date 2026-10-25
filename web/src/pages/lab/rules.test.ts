/* Strategy Lab rules, against the cases the old page handled (lab.js). */
import type { AgentRun } from "../../api/schemas/agents";
import type { Backtest, Experiment, ValidationReport } from "../../api/schemas/lab";
import { fixture } from "../../test/render-app";
import {
  activityActor,
  activityBadge,
  activitySentence,
  appendLog,
  compareRows,
  configDiff,
  configForm,
  deflatedOf,
  filterExperiments,
  flattenConfig,
  funnelCards,
  judgedWindow,
  newestByExperiment,
  originOf,
  reachedStage,
  readConfig,
  reportsByBacktest,
  strategyRules,
  tellingStage,
} from "./rules";

const experiments = fixture<{ experiments: Experiment[] }>("lab.experiments").experiments;
const backtests = fixture<{ backtests: Backtest[] }>("lab.backtests").backtests;
const reports = fixture<{ reports: ValidationReport[] }>("lab.validation").reports;
const exp = (over: Partial<Experiment>): Experiment => ({ ...(experiments[0] as Experiment), ...over });

describe("lookups", () => {
  it("finds each experiment's newest completed run, and each run's report", () => {
    const newest = newestByExperiment(backtests);
    expect(newest["exp-4327e88747"]?.id).toBe("bt-99c18832dbbd");
    expect(newest["exp-4b0d9c25e9"]).toBeUndefined();
    const queued = { ...(backtests[0] as Backtest), id: "bt-q", status: "queued" };
    expect(newestByExperiment([queued, ...backtests])["exp-16f09805f7"]?.id).toBe("bt-5bdfc7837dcc");
    const byBacktest = reportsByBacktest(reports);
    expect(byBacktest["bt-99c18832dbbd"]?.gates_passed).toBe(7);
    expect(deflatedOf(byBacktest["bt-99c18832dbbd"])).toBeCloseTo(0.5725, 3);
    expect(deflatedOf(null)).toBeNull();
  });

  it("judges the out-of-sample window when there is one", () => {
    const record = backtests[0] as Backtest;
    expect(judgedWindow(record)).toBe(record.out_of_sample);
    expect(judgedWindow({ ...record, out_of_sample: null })).toBe(record.stats);
    expect(judgedWindow(null)).toBeNull();
  });

  it("derives an experiment's origin from its runs", () => {
    const e = experiments[0] as Experiment;
    expect(originOf(e, backtests)).toBe("manual");
    expect(originOf({ ...e, owner: "Agent" }, [])).toBe("agent");
    expect(originOf(e, [{ ...(backtests[0] as Backtest), experiment_id: e.id, screen_id: "s" }])).toBe("screen");
    expect(originOf(e, [{ ...(backtests[0] as Backtest), experiment_id: e.id, source: "agent" }])).toBe("agent");
  });
});

describe("the funnel", () => {
  it("counts experiments that reached each stage, killed ones as research", () => {
    const cards = funnelCards(
      [
        { stage: "research", count: 4 },
        { stage: "validated", count: 2 },
        { stage: "paper", count: 1 },
        { stage: "approved", count: 0 },
        { stage: "live", count: 1 },
        { stage: "killed", count: 2 },
      ],
      [],
    );
    expect(cards.map((c) => c.count)).toEqual([10, 4, 2, 1, 1]);
    expect(cards.map((c) => c.caption)).toEqual(["experiments logged", "40.0% of research", "50.0% of validated", "awaiting deploy", "1 approved, 1 deployed"]);
  });

  it("falls back to the experiments' own stages, and has captions for an empty lab", () => {
    expect(funnelCards(null, experiments).map((c) => c.count)).toEqual([3, 0, 0, 0, 0]);
    expect(funnelCards(null, []).map((c) => c.caption)).toEqual(["experiments logged", "gates passed", "promoted to paper", "awaiting deploy", "0 approved, none deployed"]);
  });

  it("filters on reaching a stage, origin and text", () => {
    expect(reachedStage(exp({ stage: "paper" }), "validated")).toBe(true);
    expect(reachedStage(exp({ stage: "research" }), "validated")).toBe(false);
    expect(reachedStage(exp({ stage: "killed" }), "research")).toBe(true);
    expect(reachedStage(exp({ stage: "killed" }), "killed")).toBe(true);
    const all = { stage: null, query: "", origin: "all" as const };
    expect(filterExperiments(experiments, backtests, { ...all, query: "MOM_" }).map((e) => e.name)).toEqual(["mom_6_1_top4", "mom_12_1_top4"]);
    expect(filterExperiments(experiments, backtests, { ...all, query: "h-2" }).map((e) => e.name)).toEqual(["lowvol_top4"]);
    expect(filterExperiments(experiments, backtests, { ...all, origin: "agent" })).toEqual([]);
    expect(filterExperiments(experiments, backtests, { ...all, stage: "validated" })).toEqual([]);
  });
});

describe("strategy and compare", () => {
  it("reads the stored config as the old page's rule rows", () => {
    const rules = strategyRules((experiments[2] as Experiment).config);
    expect(rules).toEqual([
      ["signal", "momentum", "lookback", "252d"],
      ["signal", "skip", "=", "21d"],
      ["rank", "top n", "=", "4"],
      ["weight", "", "", "equal"],
      ["rebalance", "", "", "monthly"],
      ["universe", "", "", "3 symbols"],
      ["benchmark", "", "", "SPY"],
      ["costs", "bps", "=", "6 one-way"],
      ["split", "OOS", "=", "30%"],
    ]);
    expect(strategyRules({ signal: { kind: "ma_cross", fast: 50, slow: 200, min_signal: 0 }, universe_id: "sp500", oos_start: "2024-01-01" })).toContainEqual(["signal", "sma", "=", "50/200"]);
  });

  it("flattens nested config and diffs only what differs", () => {
    expect(flattenConfig({ a: 1, b: { c: 2 }, s: ["x", "y"] })).toEqual({ a: 1, "b.c": 2, s: "2 items" });
    const items = experiments.slice(1).map((experiment) => ({ experiment, record: null, report: null }));
    const diff = configDiff(items);
    expect(diff.keys).toEqual(["name", "signal.lookback"]);
    expect(diff.identical).toBeGreaterThan(20);
  });

  it("marks the best value where higher is better, never on trades or turnover", () => {
    const newest = newestByExperiment(backtests);
    const items = ["exp-16f09805f7", "exp-4327e88747"].map((id) => {
      const experiment = experiments.find((e) => e.id === id) as Experiment;
      const record = newest[id] ?? null;
      return { experiment, record, report: record ? (reportsByBacktest(reports)[record.id] ?? null) : null };
    });
    const rows = Object.fromEntries(compareRows(items).map((r) => [r.label, r]));
    expect(rows["OOS Sharpe"]).toMatchObject({ text: ["1.33", "0.48"], bestIndex: [0] });
    expect(rows["Deflated Sharpe"]).toMatchObject({ text: ["—", "0.57"], bestIndex: [1] });
    expect(rows.Trades).toMatchObject({ text: ["158", "125"], bestIndex: [] });
  });
});

describe("the config editor", () => {
  it("seeds from a stored config, or from the defaults and the active universe", () => {
    expect(configForm(null, "us-quality-50")).toMatchObject({ universe: "us-quality-50", kind: "momentum", lookback: "252", topN: "10", benchmark: "SPY", oosFraction: "0.3", longOnly: true });
    const seeded = configForm((experiments[0] as Experiment).config, "us-quality-50");
    expect(seeded).toMatchObject({ universe: "", kind: "low_volatility", lookback: "63", topN: "4", symbols: "AAPL, MSFT, NVDA" });
  });

  it("reads back a BacktestConfig with the old page's fallbacks", () => {
    const form = { ...configForm(null, ""), symbols: " aapl, msft ,", lookback: "", topN: "", maxPositions: "", benchmark: " ", name: "" };
    expect(readConfig(form)).toMatchObject({
      name: "backtest",
      universe_id: null,
      symbols: ["AAPL", "MSFT"],
      signal: { lookback: 252, top_n: null, expression: null },
      benchmark: null,
      max_positions: null,
      costs: { commission_bps: 1, slippage_bps: 5, borrow_bps_annual: 0 },
    });
  });
});

describe("agent activity", () => {
  const run = (over: Partial<AgentRun>): AgentRun => ({
    id: "run-1",
    name: "Nightly research",
    status: "running",
    stages: [
      { stage_id: "a", name: "Scan", status: "succeeded", agent_id: "scanner" },
      { stage_id: "b", name: "Review", status: "running", agent_id: "human-review" },
    ],
    progress: { completed: 1, total: 2 },
    started_at: "2026-10-25T14:00:00Z",
    ...over,
  });

  it("names the agent on the stage that explains the run", () => {
    expect(tellingStage(run({}))?.stage_id).toBe("b");
    expect(activityActor(run({}), [{ id: "human-review", name: "Human Review" }])).toBe("Human Review");
    expect(activityActor(run({ stages: [] }), [])).toBe("Nightly research");
  });

  it("says what the run is doing in the old page's words", () => {
    expect(activitySentence(run({ cost_usd: 0.42 }))).toMatch(/^Running Review in Nightly research · 1 of 2 stages · \$0\.42 · Oct 25 /);
    expect(activitySentence(run({ status: "awaiting_approval" }))).toMatch(/^Blocked on a human decision at Review in Nightly research · /);
    expect(activitySentence(run({ status: "failed", error: "boom", stages: [{ stage_id: "a", name: "Scan", status: "failed" }] }))).toMatch(/^Failed at Scan in Nightly research — boom · /);
    expect(activitySentence(run({ status: "succeeded" }))).toMatch(/^Completed Nightly research · 1 of 2 stages · /);
  });

  it("badges live, autonomous, idle or none", () => {
    expect(activityBadge([])).toEqual({ text: "none", live: false });
    expect(activityBadge([run({})])).toEqual({ text: "1 live", live: true });
    expect(activityBadge([run({ status: "succeeded", trigger: "schedule" })]).text).toBe("1 autonomous");
    expect(activityBadge([run({ status: "succeeded" })]).text).toBe("idle");
  });
});

describe("the run log", () => {
  it("drops a line already shown and keeps the last 400", () => {
    const one = appendLog([], { ts: "t1", message: "a" });
    expect(appendLog(one, { ts: "t1", message: "a" })).toBe(one);
    expect(appendLog(one, { ts: "t2", message: "a", level: "warn" })).toEqual([...one, { ts: "t2", level: "warn", message: "a" }]);
    let lines = one;
    for (let i = 0; i < 450; i++) lines = appendLog(lines, { ts: `x${i}`, message: String(i) });
    expect(lines).toHaveLength(400);
    expect(lines.at(-1)?.message).toBe("449");
  });
});
