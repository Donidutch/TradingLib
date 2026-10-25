/* Strategy Lab, frame, hypotheses and experiments: data, empty and error
 * states per panel, the request each write sends, and the live run log
 * (map: docs/UI_PORT.md, S4). */
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { openLegacy } from "../../shell/legacy";
import { fixture, json, renderApp, serve } from "../../test/render-app";

vi.mock("../../shell/legacy", () => ({ openLegacy: vi.fn() }));
afterEach(() => {
  vi.unstubAllGlobals();
  vi.mocked(openLegacy).mockClear();
});

interface Write {
  method: string;
  path: string;
  body: Record<string, unknown> | null;
}

const universe = fixture<{ universes: Record<string, unknown>[] }>("universes.list").universes[0];
const active = { ...fixture<Record<string, unknown>>("universes.active"), universe };

/** The lab's reads from fixtures; `routes` overrides. Records every write. */
function labServer(routes: Record<string, unknown> = {}) {
  const writes: Write[] = [];
  const reads = serve({
    "/api/lab": fixture("lab.summary"),
    "/api/lab/hypotheses": fixture("lab.hypothesesWithEvidence"),
    "/api/lab/experiments": fixture("lab.experiments"),
    "/api/lab/experiments/*": fixture("lab.experiment"),
    "/api/lab/backtests": fixture("lab.backtests"),
    "/api/lab/validation": fixture("lab.validation"),
    "/api/lab/validation/gates": fixture("lab.gates"),
    "/api/lab/validation/strategies": fixture("lab.strategies"),
    "/api/lab/evaluation": fixture("lab.evaluation"),
    "/api/lab/analyses": fixture("lab.analyses"),
    "/api/screens": fixture("screens.list"),
    "/api/screens/runs": fixture("screens.runs"),
    "/api/quality/summary": fixture("quality.summary"),
    "/api/agents/runs": fixture("agents.runs"),
    "/api/agents/definitions": fixture("agents.definitions"),
    "/api/universes/active": active,
    ...routes,
  });
  return {
    writes,
    handler: (url: URL, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (method !== "GET") {
        writes.push({ method, path: url.pathname, body: init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : null });
        const answer = routes[`${method} ${url.pathname}`];
        if (answer instanceof Response) return answer.clone();
        if (answer instanceof Promise) return answer;
        if (answer !== undefined) return json(200, answer);
        return json(404, { detail: "no fixture for this write" });
      }
      return reads(url, init);
    },
  };
}

const toastSays = (text: string | RegExp) =>
  waitFor(
    () => expect(screen.getAllByRole("status").some((s) => (typeof text === "string" ? s.textContent === text : text.test(s.textContent ?? "")))).toBe(true),
    { timeout: 9000 },
  );

const panel = () => screen.getByRole("tabpanel");

describe("the frame", () => {
  it("shows the crumb, the funnel and the tab counts, and sends unported tabs to the current console", async () => {
    const user = userEvent.setup();
    await renderApp("/app/lab", labServer().handler);
    await waitFor(() => expect(screen.getByLabelText("Breadcrumb")).toHaveTextContent(`/ lowvol_top4 · ${String(universe?.name)}`));
    const funnel = screen.getByRole("group", { name: "Funnel" });
    expect(within(funnel).getByRole("button", { name: /^RESEARCH/ })).toHaveTextContent("RESEARCH3experiments logged");
    expect(within(funnel).getByRole("button", { name: /^VALIDATED/ })).toHaveTextContent("VALIDATED00.0% of research");
    expect(within(funnel).getByRole("button", { name: /^LIVE/ })).toHaveTextContent("0 approved, none deployed");
    const tabs = screen.getByRole("tablist", { name: "Strategy Lab" });
    expect(within(tabs).getByRole("tab", { name: "HYPOTHESES 2" })).toBeInTheDocument();
    expect(within(tabs).getByRole("tab", { name: "EXPERIMENTS 3" })).toHaveAttribute("aria-selected", "true");
    expect(within(tabs).getByRole("tab", { name: "VALIDATION 1 ↗" })).toBeInTheDocument();
    await user.click(within(tabs).getByRole("tab", { name: /^BACKTESTS/ }));
    expect(openLegacy).toHaveBeenCalledWith("/#tl/lab/backtests");
  });

  it("filters the experiments to a funnel stage, and clears it", async () => {
    const user = userEvent.setup();
    const app = await renderApp("/app/lab/hypotheses", labServer().handler);
    await user.click(await within(screen.getByRole("group", { name: "Funnel" })).findByRole("button", { name: /^VALIDATED/ }));
    await waitFor(() => expect(app.router.state.location.pathname).toBe("/lab/experiments"));
    expect(await screen.findByText("Nothing at this filter")).toBeInTheDocument();
    expect(screen.getByText("0 of 3")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "reached validated ✕" }));
    expect(await screen.findByText("3 of 3")).toBeInTheDocument();
  });
});

describe("hypotheses", () => {
  it("lists hypotheses and opens one from the list into the URL", async () => {
    const user = userEvent.setup();
    const app = await renderApp("/app/lab/hypotheses", labServer().handler);
    const list = await screen.findByRole("navigation", { name: "Hypotheses" });
    expect(within(list).getAllByRole("button")).toHaveLength(2);
    const h1 = within(list).getByRole("button", { name: /^H-1 · Momentum/ });
    expect(h1).toHaveTextContent("2 experiments · 1 evidence · ACTIVE");
    expect(within(list).getByRole("button", { name: /^H-2/ })).toHaveTextContent("agent");
    // The first one is shown until one is chosen.
    expect(within(panel()).getByText("low-vol sharpe not above the equal-weight book")).toBeInTheDocument();

    await user.click(h1);
    await waitFor(() => expect(app.router.state.location.search).toEqual({ id: "H-1" }));
    expect(screen.getByText("top-decile momentum shows no excess return out of sample")).toBeInTheDocument();
    expect(screen.getByText("Classic cross-sectional momentum.")).toBeInTheDocument();
    // Evidence without a URL is a label, not a link.
    expect(screen.getByText("Jegadeesh & Titman").tagName).toBe("SPAN");
    expect(screen.getByRole("button", { name: "mom_12_1_top4" }).closest("div")).toHaveTextContent("RESEARCH · Sharpe 0.48 · 7/12 gates");
    expect(screen.getByRole("button", { name: "mom_6_1_top4" }).closest("div")).toHaveTextContent("RESEARCH · Sharpe 1.33");
    expect(screen.getByText(/→ ACTIVE · created/)).toBeInTheDocument();
    expect(screen.getByLabelText("Breadcrumb")).toHaveTextContent("/ H-1 · Momentum persists in large-cap tech");
  });

  it("creates a hypothesis, refusing one without a falsifier", { timeout: 30_000 }, async () => {
    const user = userEvent.setup();
    const created = { ...fixture<{ hypotheses: Record<string, unknown>[] }>("lab.hypothesesWithEvidence").hypotheses[0], id: "H-3", title: "Quality" };
    const server = labServer({ "POST /api/lab/hypotheses": created });
    const app = await renderApp("/app/lab/hypotheses", server.handler);
    await screen.findByRole("navigation", { name: "Hypotheses" });
    await user.click(within(panel()).getByRole("button", { name: "+ NEW HYPOTHESIS" }));
    const form = await screen.findByRole("form", { name: "New hypothesis" });
    expect(screen.getByLabelText("Breadcrumb")).toHaveTextContent("/ new hypothesis");
    await user.type(within(form).getByLabelText("TITLE"), "Quality");
    await user.type(within(form).getByLabelText("STATEMENT"), "Quality compounds.");
    await user.click(within(form).getByRole("button", { name: "SAVE AND OPEN" }));
    await toastSays("Title, statement and falsifier are all required.");
    expect(server.writes).toHaveLength(0);
    await user.type(within(form).getByLabelText("FALSIFIER · REQUIRED"), "no excess return");
    await user.type(within(form).getByLabelText("TAGS"), "quality, compounding");
    await user.click(within(form).getByRole("button", { name: "SAVE AND OPEN" }));
    await waitFor(() => expect(server.writes).toHaveLength(1));
    expect(server.writes[0]).toMatchObject({
      method: "POST",
      path: "/api/lab/hypotheses",
      body: { title: "Quality", statement: "Quality compounds.", falsifier: "no excess return", tags: ["quality", "compounding"], universe_id: universe?.id, status: "active", owner: null },
    });
    await toastSays("Created H-3");
    await waitFor(() => expect(app.router.state.location.search).toEqual({ id: "H-3" }));
  });

  it("edits, moves status, links and detaches evidence, and deletes after asking", { timeout: 40_000 }, async () => {
    const user = userEvent.setup();
    const h1 = fixture<{ hypotheses: Record<string, unknown>[] }>("lab.hypothesesWithEvidence").hypotheses[1];
    const server = labServer({
      "PUT /api/lab/hypotheses/H-1": h1,
      "POST /api/lab/hypotheses/H-1/status": h1,
      "POST /api/lab/hypotheses/H-1/evidence": { id: 2 },
      "DELETE /api/lab/hypotheses/H-1/evidence/1": { deleted: 1 },
      "DELETE /api/lab/hypotheses/H-1": { deleted: "H-1" },
    });
    await renderApp("/app/lab/hypotheses?id=H-1", server.handler);
    await user.click(await screen.findByRole("button", { name: "EDIT" }));
    const form = screen.getByRole("form", { name: "Edit hypothesis" });
    expect(within(form).getByLabelText("TITLE")).toHaveValue("Momentum persists in large-cap tech");
    expect(within(form).queryByLabelText("STATUS")).not.toBeInTheDocument();
    await user.clear(within(form).getByLabelText("OWNER"));
    await user.click(within(form).getByRole("button", { name: "SAVE CHANGES" }));
    await waitFor(() => expect(server.writes[0]).toMatchObject({ method: "PUT", path: "/api/lab/hypotheses/H-1", body: { owner: null, title: "Momentum persists in large-cap tech" } }));
    await toastSays("Saved H-1");

    await user.click(await screen.findByRole("button", { name: "MARK VALIDATED" }));
    expect(screen.queryByRole("button", { name: "MARK ACTIVE" })).not.toBeInTheDocument();
    await waitFor(() => expect(server.writes[1]).toMatchObject({ path: "/api/lab/hypotheses/H-1/status", body: { status: "validated" } }));
    await toastSays("H-1 → VALIDATED");

    await user.click(screen.getByRole("button", { name: "+ LINK" }));
    await toastSays("Evidence needs a reference.");
    await user.selectOptions(screen.getByLabelText("Evidence kind"), "backtest");
    await user.type(screen.getByLabelText("Evidence reference"), " bt-99c18832dbbd ");
    await user.click(screen.getByRole("button", { name: "+ LINK" }));
    await waitFor(() => expect(server.writes[2]).toMatchObject({ path: "/api/lab/hypotheses/H-1/evidence", body: { kind: "backtest", ref: "bt-99c18832dbbd", label: null } }));
    await toastSays("Evidence linked");
    await user.click(screen.getByRole("button", { name: "Detach Jegadeesh & Titman" }));
    await waitFor(() => expect(server.writes[3]).toMatchObject({ method: "DELETE", path: "/api/lab/hypotheses/H-1/evidence/1" }));

    await user.click(screen.getByRole("button", { name: "DELETE" }));
    const dialog = await screen.findByRole("dialog", { name: "Delete hypothesis" });
    expect(dialog).toHaveTextContent("Delete H-1? Its experiments keep their reference.");
    expect(server.writes).toHaveLength(4);
    await user.click(within(dialog).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(server.writes[4]).toMatchObject({ method: "DELETE", path: "/api/lab/hypotheses/H-1" }));
    await toastSays("Deleted H-1");
  });

  it("shows agent activity, or says there is none or that the Agent Center is down", async () => {
    const run = {
      id: "run-9",
      name: "Nightly research",
      pipeline_id: "nightly",
      status: "awaiting_approval",
      trigger: "schedule",
      started_at: "2026-10-25T14:00:00Z",
      stages: [{ stage_id: "r", name: "Review", status: "awaiting_approval", agent_id: "human-review" }],
    };
    const view = await renderApp("/app/lab/hypotheses", labServer({ "/api/agents/runs": { runs: [run], count: 1 } }).handler);
    const activity = await screen.findByLabelText("Agent activity");
    expect(await within(activity).findByText("Human Review")).toBeInTheDocument();
    expect(within(activity).getByText(/^Blocked on a human decision at Review in Nightly research/)).toBeInTheDocument();
    expect(screen.getByText("1 live")).toBeInTheDocument();
    await userEvent.setup().click(within(activity).getByRole("button", { name: /Human Review/ }));
    expect(openLegacy).toHaveBeenCalledWith("/#agents/runs?run=run-9");
    view.unmount();

    const empty = await renderApp("/app/lab/hypotheses", labServer().handler);
    expect(await screen.findByText("No agent runs yet")).toBeInTheDocument();
    expect(screen.getByText("none")).toBeInTheDocument();
    empty.unmount();

    await renderApp("/app/lab/hypotheses", labServer({ "/api/agents/runs": json(503, { detail: "agents offline" }) }).handler);
    expect(await screen.findByText("Agent Center unreachable")).toBeInTheDocument();
    expect(screen.getByText("agents offline")).toBeInTheDocument();
  });

  it("says so when the lab has no hypotheses, or they cannot load", async () => {
    const view = await renderApp("/app/lab/hypotheses", labServer({ "/api/lab/hypotheses": { hypotheses: [], count: 0, summary: { total: 0, by_status: {} } } }).handler);
    expect(await screen.findByText("No hypotheses yet")).toBeInTheDocument();
    expect(screen.getByText("Select a hypothesis on the left, or create one.")).toBeInTheDocument();
    view.unmount();
    await renderApp("/app/lab/hypotheses", labServer({ "/api/lab/hypotheses": json(500, { detail: "db locked" }) }).handler);
    expect(await screen.findByText("Hypotheses could not be loaded: db locked")).toBeInTheDocument();
  });
});

describe("experiments", () => {
  it("lists experiments and shows the selected one's figures, strategy, criteria, backtests and runs", async () => {
    await renderApp("/app/lab/experiments?id=exp-4327e88747", labServer().handler);
    const list = await screen.findByRole("list", { name: "Experiment list" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(3);
    expect(within(list).getByRole("button", { name: /^mom_12_1_top4/ })).toHaveTextContent("MANUAL H-1 · Sharpe 0.48 · gates 7/12");
    expect(within(list).getByRole("button", { name: /^lowvol_top4/ })).toHaveTextContent("Sharpe — · gates —");
    expect(screen.getByText("3 of 3")).toBeInTheDocument();

    const detail = panel();
    expect(within(detail).getByText("OOS SHARPE").nextSibling).toHaveTextContent("0.48");
    expect(within(detail).getByText("DEFLATED").nextSibling).toHaveTextContent("0.57");
    expect(within(detail).getByText("TRADES").nextSibling).toHaveTextContent("125");
    expect(within(detail).getByText("UNIVERSE", { selector: "small" }).nextSibling).toHaveTextContent("explicit");
    expect(within(detail).getByText(/exp-4327e88747 · 252d momentum/)).toBeInTheDocument();
    expect(within(detail).getByText("top n")).toBeInTheDocument();
    expect(within(detail).getByText("Min OOS Sharpe").nextSibling).toHaveTextContent("0.80");
    const backtests = within(detail).getByRole("table", { name: "Backtests" });
    expect(within(backtests).getByRole("button", { name: "bt-99c18832dbbd" }).closest("tr")).toHaveTextContent("0.480.57");
    const runs = await within(detail).findByRole("table", { name: "Experiment runs" });
    expect(within(runs).getByText("xr-cc4bc16a9b9f").closest("tr")).toHaveTextContent("0.3131");
    expect(within(runs).getByText("NOT MET")).toBeInTheDocument();
    expect(within(detail).getByText("No analyses yet — a single run is one observation.")).toBeInTheDocument();
  });

  it("filters by text and origin, and says when nothing matches", async () => {
    const user = userEvent.setup();
    await renderApp("/app/lab/experiments", labServer().handler);
    const list = await screen.findByRole("list", { name: "Experiment list" });
    await user.type(screen.getByLabelText("Filter experiments"), "mom_");
    await waitFor(() => expect(within(list).getAllByRole("listitem")).toHaveLength(2));
    expect(screen.getByText("2 of 3")).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Origin"), "agent");
    expect(await screen.findByText("Nothing at this filter")).toBeInTheDocument();
  });

  it("explains an experiment that never ran, and opens analyses and backtests in the current console", async () => {
    const user = userEvent.setup();
    await renderApp("/app/lab/experiments", labServer({ "/api/lab/experiments/*": { ...fixture<Record<string, unknown>>("lab.experiment"), runs: [], run_count: 0 } }).handler);
    expect(await screen.findByText(/^No completed backtest yet/)).toBeInTheDocument();
    expect(screen.getByText(/^Run a backtest first/)).toBeInTheDocument();
    expect(await screen.findByText("Never run.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "⑂ SEND TO PIPELINE ↗" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: /^mom_12_1_top4/ }));
    await user.click(await screen.findByRole("button", { name: "bt-99c18832dbbd" }));
    expect(openLegacy).toHaveBeenCalledWith("/#tl/lab/backtests?id=bt-99c18832dbbd");
    await user.click(screen.getByRole("button", { name: "+ SWEEP ↗" }));
    expect(openLegacy).toHaveBeenCalledWith("/#tl/lab/experiments?id=exp-4327e88747");
  });

  it("runs an experiment, streams its log live, then shows the stored log", { timeout: 30_000 }, async () => {
    const user = userEvent.setup();
    let release: (r: Response) => void = () => undefined;
    const pending = new Promise<Response>((resolve) => (release = resolve));
    const server = labServer({ "POST /api/lab/experiments/exp-4327e88747/run": pending });
    const app = await renderApp("/app/lab/experiments?id=exp-4327e88747", server.handler);
    act(() => app.socket().open());
    await user.click(await screen.findByRole("button", { name: "▷ RUN EXPERIMENT" }));
    const log = screen.getByRole("log", { name: "Run log" });
    expect(log).toHaveTextContent("requesting run of mom_12_1_top4…");
    expect(screen.getByRole("button", { name: "▷ RUNNING…" })).toBeDisabled();
    act(() => app.socket().event("lab", { kind: "experiment", phase: "log", id: "xr-1", experiment_id: "exp-4327e88747", ts: "2026-10-25T19:00:01Z", level: "info", message: "loaded 1255 bars" }));
    act(() => app.socket().event("lab", { kind: "experiment", phase: "log", id: "xr-1", experiment_id: "exp-4327e88747", ts: "2026-10-25T19:00:01Z", level: "info", message: "loaded 1255 bars" }));
    expect(within(log).getAllByText("loaded 1255 bars")).toHaveLength(1);
    act(() => app.socket().event("lab", { kind: "experiment", phase: "log", id: "xr-1", experiment_id: "exp-4327e88747", ts: "2026-10-25T19:00:02Z", level: "warn", message: "FAIL OOS Sharpe" }));
    expect(within(log).getByText("FAIL OOS Sharpe").previousSibling).toHaveClass("text-warn");

    act(() => release(json(200, fixture("lab.runExperiment"))));
    await toastSays(/^Experiment run xr-d0475b471990 complete · acceptance not met$/);
    await waitFor(() => expect(log).toHaveTextContent("experiment lowvol_top4 started (combined)"));
    expect(log).not.toHaveTextContent("requesting run");
  });

  it("runs a backtest with the stored config and deletes after asking", { timeout: 30_000 }, async () => {
    const user = userEvent.setup();
    const record = fixture<{ backtests: Record<string, unknown>[] }>("lab.backtests").backtests[1];
    const server = labServer({ "POST /api/lab/backtests/run": record, "DELETE /api/lab/experiments/exp-4327e88747": { deleted: "exp-4327e88747" } });
    await renderApp("/app/lab/experiments?id=exp-4327e88747", server.handler);
    await user.click(await screen.findByRole("button", { name: "▷ RUN BACKTEST WITH CURRENT CONFIG" }));
    await waitFor(() => expect(server.writes[0]).toMatchObject({ path: "/api/lab/backtests/run", body: { experiment_id: "exp-4327e88747", hypothesis_id: "H-1", source: "manual" } }));
    expect((server.writes[0]?.body?.config as { name: string }).name).toBe("mom_12_1_top4");
    await toastSays("Backtest bt-99c18832dbbd complete");

    await user.click(screen.getByRole("button", { name: "DELETE" }));
    const dialog = await screen.findByRole("dialog", { name: "Delete experiment" });
    expect(dialog).toHaveTextContent("Delete experiment mom_12_1_top4 and its runs?");
    await user.click(within(dialog).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(server.writes[1]).toMatchObject({ method: "DELETE", path: "/api/lab/experiments/exp-4327e88747" }));
    await toastSays("Deleted mom_12_1_top4");
  });

  it("creates an experiment from the form, checking name and scope first, then runs it", { timeout: 40_000 }, async () => {
    const user = userEvent.setup();
    const created = fixture<{ experiments: Record<string, unknown>[] }>("lab.experiments").experiments[0];
    const server = labServer({ "POST /api/lab/experiments": created, "POST /api/lab/experiments/exp-4b0d9c25e9/run": fixture("lab.runExperiment") });
    const app = await renderApp("/app/lab/hypotheses?id=H-1", server.handler);
    await user.click(await within(panel()).findByRole("button", { name: "+ NEW EXPERIMENT" }));
    await waitFor(() => expect(app.router.state.location.pathname).toBe("/lab/experiments"));
    const form = await screen.findByRole("form", { name: "New experiment" });
    expect(within(form).getByLabelText("HYPOTHESIS")).toHaveValue("H-1");
    expect(within(form).getByLabelText("UNIVERSE")).toHaveValue(String(universe?.id));
    expect(within(form).getByLabelText("Judged by")).toHaveTextContent("OOS Sharpe >= 0.8 · Max drawdown <= 15%");

    await user.click(within(form).getByRole("button", { name: "SAVE EXPERIMENT" }));
    await toastSays("The experiment needs a name.");
    await user.type(within(form).getByLabelText("NAME · REQUIRED"), "quality_mom");
    await user.selectOptions(within(form).getByLabelText("UNIVERSE"), "");
    await user.click(within(form).getByRole("button", { name: "SAVE EXPERIMENT" }));
    await toastSays("Choose a universe or list explicit symbols.");
    expect(server.writes).toHaveLength(0);

    await user.type(within(form).getByLabelText("SYMBOLS (when no universe)"), "aapl, msft");
    await user.clear(within(form).getByLabelText("TOP N"));
    await user.type(within(form).getByLabelText("TOP N"), "2");
    await user.click(within(form).getByRole("button", { name: "▷ SAVE AND RUN" }));
    await waitFor(() => expect(server.writes[0]).toMatchObject({ path: "/api/lab/experiments" }));
    expect(server.writes[0]?.body).toMatchObject({
      name: "quality_mom",
      hypothesis_id: "H-1",
      kind: "combined",
      horizon: 21,
      criteria: { min_oos_sharpe: 0.8, max_drawdown: 0.15, min_trades: 30 },
      config: { name: "quality_mom", universe_id: null, symbols: ["AAPL", "MSFT"], signal: { kind: "momentum", top_n: 2, lookback: 252 }, oos_fraction: 0.3 },
    });
    await waitFor(() => expect(server.writes[1]).toMatchObject({ path: "/api/lab/experiments/exp-4b0d9c25e9/run" }));
    await toastSays("Created experiment lowvol_top4");
    await waitFor(() => expect(app.router.state.location.search).toEqual({ id: "exp-4b0d9c25e9" }));
  });

  it("clones an experiment into the form", async () => {
    const user = userEvent.setup();
    await renderApp("/app/lab/experiments?id=exp-16f09805f7", labServer().handler);
    await user.click(await screen.findByRole("button", { name: "CLONE & EDIT" }));
    const form = screen.getByRole("form", { name: "New experiment" });
    expect(within(form).getByLabelText("NAME · REQUIRED")).toHaveValue("mom_6_1_top4 (clone)");
    expect(within(form).getByLabelText("LOOKBACK (bars)")).toHaveValue(126);
    expect(within(form).getByLabelText("UNIVERSE")).toHaveValue("");
    await user.click(within(form).getByRole("button", { name: "CANCEL" }));
    expect(screen.getByRole("button", { name: "CLONE & EDIT" })).toBeInTheDocument();
  });

  it("compares two to four experiments on their newest runs", async () => {
    const user = userEvent.setup();
    await renderApp("/app/lab/experiments?id=exp-4327e88747", labServer().handler);
    await user.click(await screen.findByRole("button", { name: "COMPARE WITH…" }));
    await toastSays("Tick a second experiment in the list to compare.");
    await user.click(screen.getByRole("checkbox", { name: "Compare mom_6_1_top4" }));
    expect(screen.getByRole("button", { name: "COMPARE SELECTED (2)" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "COMPARE SELECTED (2)" }));
    expect(await screen.findByText("Comparing 2 experiments")).toBeInTheDocument();
    expect(screen.getByLabelText("Breadcrumb")).toHaveTextContent("/ comparing 2 experiments");
    const metrics = screen.getByRole("table", { name: "Compared metrics" });
    const sharpe = within(metrics).getByText("OOS Sharpe").closest("tr") as HTMLElement;
    expect(sharpe).toHaveTextContent("OOS Sharpe0.481.33");
    expect(within(sharpe).getByText("1.33")).toHaveClass("text-up");
    const diff = screen.getByRole("table", { name: "Config diff" });
    expect(within(diff).getByText("signal.lookback").closest("tr")).toHaveTextContent("252126");
    expect(screen.getByText(/identical fields hidden\.$/)).toBeInTheDocument();
    expect(screen.getByText("7 / 12 · FAIL")).toBeInTheDocument();
    expect(screen.getByText("never validated")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "← BACK TO EXPERIMENTS" }));
    expect(await screen.findByRole("list", { name: "Experiment list" })).toBeInTheDocument();
  });

  it("runs a screen and queues a backtest from it", { timeout: 30_000 }, async () => {
    const user = userEvent.setup();
    const server = labServer({ "POST /api/screens/screen-t/run": fixture("screens.run"), "POST /api/screens/screen-t/queue-backtest": fixture("screens.queueBacktest") });
    await renderApp("/app/lab/experiments", server.handler);
    const screens = await screen.findByRole("table", { name: "Screens" });
    const row = within(screens).getByText("Screen t").closest("tr") as HTMLElement;
    expect(row).toHaveTextContent("Screen t");
    expect(row).toHaveTextContent("never");
    await user.click(within(row).getByRole("button", { name: "RUN" }));
    await waitFor(() => expect(server.writes[0]).toMatchObject({ path: "/api/screens/screen-t/run" }));
    await toastSays("Screen t: 18 of 50 names passed");
    await user.click(within(row).getByRole("button", { name: "→ BACKTEST ↗" }));
    await waitFor(() => expect(server.writes[1]).toMatchObject({ path: "/api/screens/screen-t/queue-backtest", body: {} }));
    await toastSays("Queued backtest bt-b9e12a4d0aff from Screen t");
    expect(openLegacy).toHaveBeenCalledWith("/#tl/lab/backtests?id=bt-b9e12a4d0aff");
  });

  it("shows the server's reason when experiments or screens cannot load", async () => {
    await renderApp("/app/lab/experiments", labServer({ "/api/lab/experiments": json(500, { detail: "db locked" }), "/api/screens": json(502, { detail: "screens down" }) }).handler);
    expect(await screen.findByText("Experiments could not be loaded: db locked")).toBeInTheDocument();
    expect(await screen.findByText("Screens could not be loaded: screens down")).toBeInTheDocument();
    expect(screen.getByText("Select an experiment on the left, or create one.")).toBeInTheDocument();
  });

  it("refetches what a lab frame touches", async () => {
    const app = await renderApp("/app/lab/experiments", labServer().handler);
    await screen.findByRole("list", { name: "Experiment list" });
    const reads = (path: string) => app.fetchMock.mock.calls.filter(([u]) => new URL(String(u), "http://x").pathname === path).length;
    const before = { reports: reads("/api/lab/validation"), backtests: reads("/api/lab/backtests") };
    act(() => app.socket().open());
    act(() => app.socket().event("lab", { kind: "validation", phase: "complete", id: "val-1" }));
    await waitFor(() => expect(reads("/api/lab/validation")).toBe(before.reports + 1));
    act(() => app.socket().event("lab", { kind: "backtest", phase: "complete", id: "bt-1" }));
    await waitFor(() => expect(reads("/api/lab/backtests")).toBeGreaterThan(before.backtests));
  });
});
