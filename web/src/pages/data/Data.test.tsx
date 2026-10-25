/* Market & Data, collections and screener: data, empty and error states per
 * panel, and the request each write sends (map: docs/UI_PORT.md, S2). */
import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { fixture, json, renderApp, serve } from "../../test/render-app";
import { openLegacy } from "../../shell/legacy";

vi.mock("../../shell/legacy", () => ({ openLegacy: vi.fn() }));
afterEach(() => {
  vi.unstubAllGlobals();
  vi.mocked(openLegacy).mockClear();
});

type Body = Record<string, unknown>;

/** The page's reads from fixtures; `routes` overrides. Records every write. */
function dataServer(routes: Record<string, unknown> = {}) {
  const writes: { method: string; path: string; body: unknown }[] = [];
  const reads = serve({
    "/api/universes": fixture("universes.listWithMembers"),
    "/api/universes/*": (url: URL) =>
      url.pathname.endsWith("/changelog")
        ? json(200, fixture("universes.changelog"))
        : json(200, url.searchParams.get("as_of") ? fixture("universes.membersAsOf") : fixture("universes.members")),
    "/api/watchlists/primary-research": fixture("watchlists.get"),
    "/api/screens/metrics": fixture("screens.metrics"),
    "/api/screens": fixture("screens.list"),
    "/api/screens/alerts": fixture("screens.alerts"),
    "/api/screens/preview": () => json(200, fixture("screens.preview")),
    "/api/quality/datasets": fixture("quality.datasets"),
    ...routes,
  });
  return {
    writes,
    handler: (url: URL, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (method !== "GET" && url.pathname !== "/api/screens/preview") {
        writes.push({ method, path: url.pathname, body: init?.body ? JSON.parse(String(init.body)) : null });
        const answer = routes[`${method} ${url.pathname}`];
        if (answer instanceof Response) return answer.clone();
        if (answer !== undefined) return json(200, answer);
      }
      return reads(url, init);
    },
  };
}

/** Toasts queue one after another (2.6 s each), so a later one can take a while. */
const toastSays = (text: string | RegExp) =>
  waitFor(
    () => expect(screen.getAllByRole("status").some((s) => (typeof text === "string" ? s.textContent === text : text.test(s.textContent ?? "")))).toBe(true),
    { timeout: 9000 },
  );

describe("collections table and membership", () => {
  it("lists universes and watchlists, and shows the first one's members, change log and coverage", async () => {
    await renderApp("/app/data", dataServer().handler);
    const table = await screen.findByRole("table", { name: "Collections" });
    await waitFor(() => expect(within(table).getAllByRole("row")).toHaveLength(5));
    expect(screen.getByText("4 collections")).toBeInTheDocument();
    expect(within(table).getByText("US Quality 50").closest("tr")).toHaveTextContent("Static built-in definition50On demandActive");
    expect(within(table).getByText("Primary research").closest("tr")).toHaveTextContent("Default");

    const pit = screen.getByRole("region", { name: "Point-in-time membership" });
    expect(await within(pit).findByText("Nasdaq-100 · membership @ today")).toBeInTheDocument();
    expect(within(pit).getByText("current membership")).toBeInTheDocument();
    expect(within(pit).getAllByRole("row").length).toBeGreaterThan(1);
    expect(await within(pit).findByText(/baseline snapshot/)).toBeInTheDocument();
    expect(within(pit).getByText(/Rule def v1 · /)).toBeInTheDocument();
    expect(await within(pit).findByText("Requested")).toBeInTheDocument();
    expect(within(pit).getByText(/Source: parquet store/)).toBeInTheDocument();
  });

  it("selects a watchlist from the keyboard", async () => {
    await renderApp("/app/data", dataServer().handler);
    const row = (await screen.findByText("Primary research")).closest("tr") as HTMLElement;
    row.focus();
    fireEvent.keyDown(row, { key: "Enter" });
    const pit = screen.getByRole("region", { name: "Point-in-time membership" });
    expect(await within(pit).findByText("Primary research · membership @ today")).toBeInTheDocument();
    expect(within(pit).getByText("Watchlists carry no snapshot history.")).toBeInTheDocument();
    expect(within(pit).getByText("Coverage is computed for universes.")).toBeInTheDocument();
    expect(within(pit).getByText("AAPL")).toBeInTheDocument();
    expect(within(pit).queryByLabelText("As-of year")).not.toBeInTheDocument();
  });

  it("reloads membership at the as-of date when the slider is released", async () => {
    const app = await renderApp("/app/data", dataServer().handler);
    const slider = await screen.findByLabelText("As-of year");
    fireEvent.change(slider, { target: { value: "2019" } });
    fireEvent.keyUp(slider);
    await waitFor(() => expect(app.fetchMock.mock.calls.some(([u]) => String(u).includes("as_of=2019-06-30"))).toBe(true));
    // A built-in universe with no stored members at that date says how to fetch them.
    expect(await screen.findByText(/No members stored yet — use “Refresh members”/)).toBeInTheDocument();
  });

  it("says when there are no collections, and when they cannot be loaded", async () => {
    await renderApp("/app/data", dataServer({ "/api/universes": { universes: [], count: 0, active: null }, "/api/watchlists": { watchlists: [], count: 0, default: null } }).handler);
    expect(await screen.findByText("No universes or watchlists yet — create one with “+ New universe”.")).toBeInTheDocument();
  });

  it("shows a load failure in the table", async () => {
    await renderApp("/app/data", dataServer({ "/api/universes": json(500, { detail: "registry locked" }) }).handler);
    expect(await screen.findByText("registry locked")).toBeInTheDocument();
    expect(screen.getByText("failed")).toBeInTheDocument();
  });
});

describe("collection actions", () => {
  it("activates, refreshes and snapshots a universe", { timeout: 30_000 }, async () => {
    const user = userEvent.setup();
    const server = dataServer({
      "POST /api/universes/active": fixture("universes.active"),
      "POST /api/universes/nasdaq100/refresh": { ...fixture<Body>("universes.create"), id: "nasdaq100", member_count: 101 },
      "POST /api/universes/nasdaq100/snapshot": fixture("universes.snapshot"),
    });
    await renderApp("/app/data", server.handler);
    const pit = await screen.findByRole("region", { name: "Point-in-time membership" });
    await user.click(within(pit).getByRole("button", { name: "Set active" }));
    await toastSays("Active universe: Nasdaq-100");
    await user.click(within(pit).getByRole("button", { name: "Refresh members" }));
    await toastSays("Nasdaq-100 · 101 members fetched");
    await user.click(within(pit).getByRole("button", { name: "Freeze snapshot" }));
    await toastSays("Snapshot 2026-10-25 · 2 symbols");
    expect(server.writes.map((w) => `${w.method} ${w.path}`)).toEqual([
      "POST /api/universes/active",
      "POST /api/universes/nasdaq100/refresh",
      "POST /api/universes/nasdaq100/snapshot",
    ]);
    expect(server.writes[0]?.body).toEqual({ universe_id: "nasdaq100" });
  });

  it("deletes only after confirming", async () => {
    const user = userEvent.setup();
    const server = dataServer({ "DELETE /api/watchlists/primary-research": { deleted: "primary-research", default: null } });
    await renderApp("/app/data", server.handler);
    await user.click(await screen.findByText("Primary research"));
    const pit = screen.getByRole("region", { name: "Point-in-time membership" });
    await user.click(await within(pit).findByRole("button", { name: "Delete" }));
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveTextContent("Delete watchlist “Primary research”?");
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(server.writes).toEqual([]);
    await user.click(within(pit).getByRole("button", { name: "Delete" }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Delete" }));
    await toastSays("Primary research deleted");
    expect(server.writes).toEqual([{ method: "DELETE", path: "/api/watchlists/primary-research", body: null }]);
  });

  it("built-in universes cannot be deleted", async () => {
    await renderApp("/app/data", dataServer().handler);
    const pit = await screen.findByRole("region", { name: "Point-in-time membership" });
    await within(pit).findByRole("button", { name: "Freeze snapshot" });
    expect(within(pit).queryByRole("button", { name: "Delete" })).not.toBeInTheDocument();
  });

  it("creates a watchlist from the dialog and passes on fields the server ignored", { timeout: 30_000 }, async () => {
    const user = userEvent.setup();
    const server = dataServer({
      "POST /api/watchlists": { ...fixture<Body>("watchlists.get"), id: "semis", name: "Semis", ignored_detail: "a watchlist stores a name and symbols only" },
    });
    await renderApp("/app/data", server.handler);
    await user.click(await screen.findByRole("button", { name: "+ New watchlist" }));
    const form = screen.getByRole("form", { name: "Collection" });
    await user.type(within(form).getByLabelText("Name"), "Semis");
    await user.type(within(form).getByLabelText("Description"), "Chip makers");
    await user.type(within(form).getByLabelText("Symbols or rules"), "NVDA, AVGO");
    await user.click(within(form).getByRole("button", { name: "Create collection" }));
    await toastSays("Watchlist “Semis” created");
    await toastSays("a watchlist stores a name and symbols only");
    expect(server.writes[0]).toEqual({
      method: "POST",
      path: "/api/watchlists",
      body: { name: "Semis", symbols: "NVDA, AVGO", description: "Chip makers", construction_method: "Manual symbols", refresh: "Manual", is_default: false },
    });
  });

  it("edits a watchlist with its kind fixed", async () => {
    const user = userEvent.setup();
    const server = dataServer({ "PUT /api/watchlists/primary-research": fixture("watchlists.get") });
    await renderApp("/app/data", server.handler);
    await user.click(await screen.findByText("Primary research"));
    await user.click(await screen.findByRole("button", { name: "Edit" }));
    const form = screen.getByRole("form", { name: "Collection" });
    expect(within(form).getByRole("radio", { name: "Universe" })).toBeDisabled();
    expect(within(form).getByLabelText("Symbols or rules")).toHaveValue("AAPL, NVDA");
    await user.clear(within(form).getByLabelText("Name"));
    await user.type(within(form).getByLabelText("Name"), "Core");
    await user.click(within(form).getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(server.writes[0]).toEqual({ method: "PUT", path: "/api/watchlists/primary-research", body: { name: "Core", symbols: "AAPL, NVDA" } }));
  });

  it("reports a failed save and keeps the dialog open", async () => {
    const user = userEvent.setup();
    const server = dataServer({ "POST /api/universes": json(409, { detail: "a universe called Semis exists" }) });
    await renderApp("/app/data", server.handler);
    await user.click(await screen.findByRole("button", { name: "+ New universe" }));
    await user.type(screen.getByLabelText("Name"), "Semis");
    await user.click(screen.getByRole("button", { name: "Create collection" }));
    await toastSays("Could not save Semis: a universe called Semis exists");
    expect(screen.getByRole("form", { name: "Collection" })).toBeInTheDocument();
  });
});

describe("screener", () => {
  it("starts from the default rules with real funnel counts", async () => {
    const app = await renderApp("/app/data/screener", dataServer().handler);
    const rules = await screen.findAllByRole("group", { name: /^Rule \d$/ });
    expect(rules).toHaveLength(7);
    expect(within(rules[0] as HTMLElement).getByLabelText("Value")).toHaveValue("2");
    await waitFor(() => expect(within(rules[0] as HTMLElement).getByLabelText("Funnel")).toHaveTextContent("50 → 50"));
    const previews = app.fetchMock.mock.calls.filter(([u]) => String(u) === "/api/screens/preview");
    expect(JSON.parse(String(previews.at(-1)?.[1]?.body)).rules).toHaveLength(7);
    expect(screen.getByText("50 names · built-in")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Screen t/ })).toBeInTheDocument();
  });

  it("edits rules: $bn values, percentile scope, removal, and keeps at least one", async () => {
    const user = userEvent.setup();
    const app = await renderApp("/app/data/screener", dataServer().handler);
    const [first] = await screen.findAllByRole("group", { name: /^Rule \d$/ });
    const value = within(first as HTMLElement).getByLabelText("Value");
    await user.clear(value);
    await user.type(value, "5");
    await waitFor(() => {
      const body = JSON.parse(String(app.fetchMock.mock.calls.filter(([u]) => String(u) === "/api/screens/preview").at(-1)?.[1]?.body));
      expect(body.rules[0]).toEqual({ field: "mkt_cap", operator: ">", value: 5e9 });
    });
    await user.selectOptions(within(first as HTMLElement).getByLabelText("Operator"), "top_pctl");
    expect(within(first as HTMLElement).getByLabelText("Scope")).toHaveValue("all");
    for (let i = 0; i < 6; i++) await user.click(within(screen.getAllByRole("group", { name: /^Rule \d$/ })[0] as HTMLElement).getByRole("button", { name: "Remove rule" }));
    expect(screen.getAllByRole("group", { name: /^Rule \d$/ })).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "Remove rule" }));
    await toastSays("A screen needs at least one rule");
  });

  it("runs a screen: saves it, then shows ranked results, the profile and the run's own funnel", async () => {
    const user = userEvent.setup();
    const server = dataServer({ "POST /api/screens": fixture<{ screens: Body[] }>("screens.list").screens[0], "POST /api/screens/screen-t/run": fixture("screens.run") });
    await renderApp("/app/data/screener", server.handler);
    await user.click(await screen.findByRole("button", { name: "Run screen" }));
    await toastSays("Screen executed · 18 of 50 names pass");
    expect(server.writes[0]).toMatchObject({ method: "POST", path: "/api/screens", body: { universe_id: "us-quality-50", rank_by: "composite", description: "Created from the Market & Data screener." } });
    expect(server.writes[1]).toEqual({ method: "POST", path: "/api/screens/screen-t/run", body: { limit: 200 } });
    const results = screen.getByRole("table", { name: "Results" });
    const rows = within(results).getAllByRole("row").slice(1);
    expect(rows[0]).toHaveTextContent("1NVDA91.3");
    expect(rows[0]?.querySelector("td:nth-child(3)")).toHaveClass("text-up");
    expect(screen.getByText("18 symbols")).toBeInTheDocument();
    expect(screen.getByText(/Financial Services 17% · Healthcare 17% · Technology 17%/)).toBeInTheDocument();
    expect(screen.getByText("$118.6B")).toBeInTheDocument();
    expect(screen.getByText("first run — nothing to compare against")).toBeInTheDocument();
    expect(within(screen.getAllByRole("group", { name: /^Rule \d$/ })[1] as HTMLElement).getByLabelText("Funnel")).toHaveTextContent("50 → 18");
  });

  it("queues a backtest with the Settings benchmark and opens it in the lab", async () => {
    const user = userEvent.setup();
    const server = dataServer({
      "POST /api/screens": fixture<{ screens: Body[] }>("screens.list").screens[0],
      "POST /api/screens/screen-t/run": fixture("screens.run"),
      "POST /api/screens/screen-t/queue-backtest": fixture("screens.queueBacktest"),
    });
    await renderApp("/app/data/screener", server.handler);
    await user.click(await screen.findByRole("button", { name: "→ Backtest this screen" }));
    await toastSays("Backtest bt-b9e12a4d0aff · 18 names vs SPY");
    expect(server.writes.at(-1)).toEqual({ method: "POST", path: "/api/screens/screen-t/queue-backtest", body: { run_id: "scr-a23503244c", benchmark: "SPY" } });
    expect(openLegacy).toHaveBeenCalledWith("/#tl/lab/backtests?id=bt-b9e12a4d0aff");
  });

  it("saves survivors as a universe only after a run", { timeout: 30_000 }, async () => {
    const user = userEvent.setup();
    const server = dataServer({
      "POST /api/screens": fixture<{ screens: Body[] }>("screens.list").screens[0],
      "POST /api/screens/screen-t/run": fixture("screens.run"),
      "POST /api/universes": fixture("universes.create"),
    });
    await renderApp("/app/data/screener", server.handler);
    await user.click(await screen.findByRole("button", { name: "Save as universe" }));
    await toastSays("Run the screen first — there are no survivors to save yet");
    expect(server.writes).toEqual([]);
    await user.click(screen.getByRole("button", { name: "Run screen" }));
    await toastSays(/Screen executed/);
    await user.click(screen.getByRole("button", { name: "Save as universe" }));
    await waitFor(() => expect(server.writes.at(-1)).toMatchObject({ path: "/api/universes", body: { construction_method: "Screener result", symbols: "NVDA, AAPL, V" } }));
  });

  it("pins a screen, records the baseline and says how often it is re-run", async () => {
    const user = userEvent.setup();
    const saved = fixture<{ screens: Body[] }>("screens.list").screens[0] as Body;
    const server = dataServer({
      "POST /api/screens": saved,
      "PUT /api/screens/screen-t": { ...saved, tags: ["pinned", "alert"] },
      "POST /api/screens/alerts/check": { alerts: [], count: 0 },
    });
    await renderApp("/app/data/screener", server.handler);
    await user.click(await screen.findByRole("button", { name: "Pin + alert on changes" }));
    await toastSays("“Screen t” pinned and watched — it is re-run every 15 min, and any symbol entering or leaving raises an alert.");
    expect(server.writes.map((w) => `${w.method} ${w.path}`)).toEqual(["POST /api/screens", "PUT /api/screens/screen-t", "POST /api/screens/alerts/check"]);
    expect(server.writes[1]?.body).toEqual({ tags: ["pinned", "alert"] });
  });

  it("loads a saved screen", async () => {
    const user = userEvent.setup();
    await renderApp("/app/data/screener", dataServer().handler);
    await user.click(await screen.findByRole("button", { name: /Screen t/ }));
    expect(screen.getAllByRole("group", { name: /^Rule \d$/ })).toHaveLength(2);
    await toastSays("Loaded screen “Screen t”");
  });

  it("shows alerts, acknowledges one and refreshes on a screen_alert notification", async () => {
    const user = userEvent.setup();
    const alert = { id: "al-1", screen_id: "screen-t", screen_name: "Screen t", headline: "Screen t: +TSLA −NKE", entered: ["TSLA"], exited: ["NKE"], pass_count: 18, previous_count: 18, detected_at: new Date().toISOString(), acknowledged_at: null };
    const server = dataServer({
      "/api/screens/alerts": { alerts: [alert], count: 1, monitor: { running: false, interval_seconds: 900, last_cycle_at: null, last_error: null, watched: ["screen-t"] } },
      "POST /api/screens/alerts/al-1/acknowledge": { ...alert, acknowledged_at: new Date().toISOString() },
    });
    const app = await renderApp("/app/data/screener", server.handler);
    expect(await screen.findByText("Screen t: +TSLA −NKE")).toBeInTheDocument();
    expect(screen.getByText("1 screen watched")).toBeInTheDocument();
    expect(screen.getByText(/alert monitor is not running/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Acknowledge" }));
    await waitFor(() => expect(server.writes).toEqual([{ method: "POST", path: "/api/screens/alerts/al-1/acknowledge", body: {} }]));
    const before = app.fetchMock.mock.calls.filter(([u]) => String(u).startsWith("/api/screens/alerts?")).length;
    act(() => app.socket().open());
    act(() => app.socket().event("notifications", { event: "screen_alert", level: "info", message: "Screen t changed" }));
    await waitFor(() => expect(app.fetchMock.mock.calls.filter(([u]) => String(u).startsWith("/api/screens/alerts?")).length).toBeGreaterThan(before));
  });

  it("says when the catalogue cannot be loaded, and when a preview fails", async () => {
    await renderApp("/app/data/screener", dataServer({ "/api/screens/metrics": json(500, { detail: "catalogue missing" }) }).handler);
    expect(await screen.findByRole("alert")).toHaveTextContent("Screener: catalogue missing");
  });

  it("marks the funnel failed when previews fail", async () => {
    await renderApp("/app/data/screener", dataServer({ "/api/screens/preview": json(502, { detail: "vendor down" }) }).handler);
    await waitFor(() => expect(screen.getAllByLabelText("Funnel")[0]).toHaveTextContent("preview failed"), { timeout: 3000 });
    await toastSays("Rule preview failed: vendor down");
  });
});

describe("unported tabs", () => {
  it("open in the current console", async () => {
    const user = userEvent.setup();
    await renderApp("/app/data", dataServer().handler);
    await user.click(await screen.findByRole("tab", { name: "Data Quality ↗" }));
    expect(openLegacy).toHaveBeenCalledWith("/#data/quality");
  });
});
