/* Market & Data, source catalog, ingestion and quality: data, empty and error
 * states per panel, and the request each write sends (map: docs/UI_PORT.md, S3). */
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { fixture, json, renderApp, serve } from "../../test/render-app";

afterEach(() => vi.unstubAllGlobals());

interface Write {
  method: string;
  path: string;
  query: Record<string, string>;
  body: Record<string, unknown> | null;
}

/** The tabs' reads from fixtures; `routes` overrides. Records every write. */
function sourcesServer(routes: Record<string, unknown> = {}) {
  const writes: Write[] = [];
  const reads = serve({
    "/api/ingest/sources": fixture("ingest.sources"),
    "/api/ingest/subscriptions": fixture("ingest.subscriptions"),
    "/api/quality/summary": fixture("quality.summary"),
    "/api/quality/series": fixture("quality.series"),
    "/api/quality/incidents": fixture("quality.incidents"),
    "/api/quality/datasets": fixture("quality.datasets"),
    "/api/quality/outliers": fixture("quality.outliers"),
    "/api/quality/sla": fixture("quality.sla"),
    "/api/quality/tickets": fixture("quality.tickets"),
    ...routes,
  });
  return {
    writes,
    handler: (url: URL, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (method !== "GET") {
        writes.push({
          method,
          path: url.pathname,
          query: Object.fromEntries(url.searchParams),
          body: init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : null,
        });
        const answer = routes[`${method} ${url.pathname}`];
        if (answer instanceof Response) return answer.clone();
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

const catalogNav = () => screen.findByRole("navigation", { name: "Source catalog" });
async function pick(user: ReturnType<typeof userEvent.setup>, name: RegExp) {
  await user.click(await within(await catalogNav()).findByRole("button", { name }));
}

describe("source catalog", () => {
  it("lists the sources and shows the first one's detail, subscription panel and the active feeds", async () => {
    await renderApp("/app/data/source-catalog", sourcesServer().handler);
    const nav = await catalogNav();
    expect(await within(nav).findAllByRole("button")).toHaveLength(3);
    expect(screen.getByText("3 sources")).toBeInTheDocument();
    expect(within(nav).getByRole("button", { name: /FRED Macro/ })).toHaveTextContent("Connected");
    // Alpaca is first: a stream with no credentials and no history endpoint.
    expect(screen.getByRole("heading", { name: /Alpaca Market Stream/ })).toHaveTextContent("Not probed");
    expect(screen.getByText(/AUTH API KEY — NOT CONFIGURED/)).toBeInTheDocument();
    const protocols = screen.getByRole("group", { name: "Protocols supported" });
    expect(within(protocols).getByRole("button", { name: "WebSocket" })).toHaveAttribute("aria-pressed", "true");
    expect(within(protocols).getByRole("button", { name: "REST" })).toBeDisabled();
    expect(screen.getByText(/^Credentials are required before this connector can ingest/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "+ Add subscription" })).toBeDisabled();
    expect(screen.getByText(/^Not applicable — a WebSocket stream source pushes/)).toBeInTheDocument();
    expect(screen.getByText(/^No history endpoint/)).toBeInTheDocument();
    expect(screen.getByText(/Alpaca Market Stream streams over WebSocket stream\./)).toBeInTheDocument();

    const feeds = screen.getByRole("table", { name: "Active subscriptions" });
    expect(screen.getByText("1 feed")).toBeInTheDocument();
    const row = within(feeds).getByText("FRED Macro").closest("tr") as HTMLElement;
    expect(row).toHaveTextContent("series");
    expect(row).toHaveTextContent("POLL");
    expect(row).toHaveTextContent("universe:us-quality-50");
  });

  it("subscribes a source with the chosen channel, feed mode, point-in-time and resolution", { timeout: 30_000 }, async () => {
    const user = userEvent.setup();
    const server = sourcesServer({ "POST /api/ingest/subscriptions": fixture("ingest.subscribe") });
    await renderApp("/app/data/source-catalog", server.handler);
    await pick(user, /FRED Macro/);
    expect(await screen.findByRole("button", { name: /^series.*ACTIVE/ })).toBeInTheDocument();
    expect(screen.getByText("Connector is implemented and configured — it can be subscribed.")).toBeInTheDocument();
    await user.click(screen.getByLabelText("Preserve point-in-time values"));
    await user.click(within(screen.getByRole("group", { name: "Frequency" })).getByRole("button", { name: "1wk" }));
    await user.click(screen.getByRole("button", { name: "+ Add subscription" }));
    await waitFor(() => expect(server.writes).toHaveLength(1));
    expect(server.writes[0]).toMatchObject({
      method: "POST",
      path: "/api/ingest/subscriptions",
      body: { source_id: "fred", channel: "series", scope: [expect.stringMatching(/^universe:/)], active: true, feed_mode: "scheduled", preserve_pit: true, granularity: "1wk" },
    });
    await toastSays("Subscribed FRED Macro · series · scheduled · point-in-time");
  });

  it("rewrites the poll schedule from a preset, and refuses an interval it cannot parse", { timeout: 30_000 }, async () => {
    const user = userEvent.setup();
    const server = sourcesServer({ "PUT /api/ingest/sources/fred": fixture("ingest.update") });
    await renderApp("/app/data/source-catalog", server.handler);
    await pick(user, /FRED Macro/);
    const poll = await screen.findByRole("group", { name: "Poll interval" });
    expect(within(poll).getByRole("button", { name: "1h" })).toHaveAttribute("aria-pressed", "true");
    await user.click(within(poll).getByRole("button", { name: "15m" }));
    await waitFor(() => expect(server.writes).toHaveLength(1));
    expect(server.writes[0]).toMatchObject({ method: "PUT", path: "/api/ingest/sources/fred", body: { id: "fred", schedule: "15m", retry_policy: "exponential" } });
    await toastSays(/^FRED Macro now polls every 15m/);

    const custom = screen.getByLabelText("Custom poll interval");
    await user.type(custom, "soon");
    expect(screen.getByText("Unrecognised interval — use a value like 45s, 2m, 4h or 1d.")).toBeInTheDocument();
    await user.type(custom, "{Enter}");
    await toastSays("Unrecognised interval “soon” — use a value like 45s, 2m, 4h or 1d.");
    expect(server.writes).toHaveLength(1);
  });

  it("runs a backfill, follows its progress on the ingest channel, and says when one is already running", { timeout: 30_000 }, async () => {
    const user = userEvent.setup();
    const server = sourcesServer({ "POST /api/ingest/sources/fred/backfill": fixture("ingest.backfill") });
    const app = await renderApp("/app/data/source-catalog", server.handler);
    await pick(user, /FRED Macro/);
    const from = await screen.findByLabelText("From");
    await user.clear(from);
    await user.click(screen.getByRole("button", { name: "Run backfill" }));
    await toastSays("A backfill needs a FROM boundary");
    await user.type(from, "30d");
    await user.click(screen.getByRole("button", { name: "Run backfill" }));
    await waitFor(() => expect(server.writes).toHaveLength(1));
    expect(server.writes[0]).toMatchObject({ path: "/api/ingest/sources/fred/backfill", query: { range: "30d", end: "now" } });
    expect(await screen.findByText("Backfill started · 30d → now. Progress arrives on the ingest channel.")).toBeInTheDocument();
    await toastSays("Backfilling FRED Macro from 30d to now");

    act(() => app.socket().open());
    act(() => app.socket().event("ingest", { event: "backfill_progress", source_id: "fred", batch: 2, rows_written: 1200, dataset: "series:fred:UNRATE" }));
    expect(await screen.findByText("Batch 2 · 1,200 rows written into series:fred:UNRATE")).toBeInTheDocument();
    act(() => app.socket().event("ingest", { event: "backfill_finished", source_id: "fred", ok: true, batches: 3, rows_written: 1800 }));
    expect(await screen.findByText("Backfill complete · 3 batches · 1,800 rows written")).toBeInTheDocument();
    await toastSays("Backfill finished: 1,800 rows");
  });

  it("warns instead of failing when the source is already backfilling", async () => {
    const user = userEvent.setup();
    const busy = json(409, { detail: { error: "backfill_running", detail: "fred is backfilling" } });
    await renderApp("/app/data/source-catalog", sourcesServer({ "POST /api/ingest/sources/fred/backfill": busy }).handler);
    await pick(user, /FRED Macro/);
    await user.click(await screen.findByRole("button", { name: "Run backfill" }));
    await toastSays("FRED Macro is already backfilling; wait for it to finish.");
  });

  it("asks before removing a subscription", async () => {
    const user = userEvent.setup();
    const id = fixture<{ subscriptions: { id: string }[] }>("ingest.subscriptions").subscriptions[0]?.id ?? "";
    const server = sourcesServer({ [`DELETE /api/ingest/subscriptions/${id}`]: { deleted: id } });
    await renderApp("/app/data/source-catalog", server.handler);
    const feeds = await screen.findByRole("table", { name: "Active subscriptions" });
    await user.click(within(feeds).getByRole("button", { name: "Remove" }));
    const dialog = await screen.findByRole("dialog", { name: "Remove subscription" });
    expect(dialog).toHaveTextContent("Stop the series subscription on FRED Macro?");
    expect(server.writes).toHaveLength(0);
    await user.click(within(dialog).getByRole("button", { name: "Remove" }));
    await waitFor(() => expect(server.writes).toEqual([expect.objectContaining({ method: "DELETE", path: `/api/ingest/subscriptions/${id}` })]));
    await toastSays("Subscription removed");
  });

  it("sends missing credentials to Settings, and hands a source to the ingestion form", { timeout: 30_000 }, async () => {
    const user = userEvent.setup();
    const app = await renderApp("/app/data/source-catalog", sourcesServer().handler);
    await user.click(await screen.findByRole("button", { name: "Credentials" }));
    await toastSays("Add credentials for Alpaca Market Stream in Settings → API keys");
    await waitFor(() => expect(app.router.state.location.pathname).toBe("/settings"));

    await act(() => app.router.navigate({ to: "/data/$tab", params: { tab: "source-catalog" } }));
    await pick(user, /FRED Macro/);
    await user.click(await screen.findByRole("button", { name: "Use custom configuration" }));
    await waitFor(() => expect(app.router.state.location.pathname).toBe("/data/ingestion"));
    expect(await screen.findByLabelText("Source name")).toHaveValue("FRED Macro");
    expect(screen.getByText("editing fred")).toBeInTheDocument();
    await toastSays("FRED Macro copied into the ingestion form");
  });

  it("opens a blank form for a new source", async () => {
    const user = userEvent.setup();
    const app = await renderApp("/app/data/source-catalog", sourcesServer().handler);
    await user.click(await screen.findByRole("button", { name: "+ Request new source" }));
    await waitFor(() => expect(app.router.state.location.pathname).toBe("/data/ingestion"));
    expect(await screen.findByLabelText("Source name")).toHaveValue("");
    await toastSays("Describe the new source in the ingestion form and test it");
  });

  it("shows the server's reason when the catalog cannot load, and an empty catalog plainly", async () => {
    await renderApp("/app/data/source-catalog", sourcesServer({ "/api/ingest/sources": json(500, { detail: "registry offline" }) }).handler);
    expect(await screen.findByText("Source catalog: registry offline")).toBeInTheDocument();
  });

  it("says so when no source or subscription exists", async () => {
    await renderApp("/app/data/source-catalog", sourcesServer({ "/api/ingest/sources": { sources: [], count: 0 }, "/api/ingest/subscriptions": { subscriptions: [], count: 0 } }).handler);
    expect(await screen.findByText("No ingestion sources are registered in this build.")).toBeInTheDocument();
    expect(screen.getByText(/^No subscriptions configured\./)).toBeInTheDocument();
  });
});

describe("data ingestion", () => {
  it("lists the configured sources with their state", async () => {
    await renderApp("/app/data/ingestion", sourcesServer().handler);
    const table = await screen.findByRole("table", { name: "Configured ingestion sources" });
    await waitFor(() => expect(within(table).getAllByRole("row")).toHaveLength(4));
    expect(screen.getByText("4 / 3 connected")).toBeInTheDocument();
    const fred = within(table).getByText("FRED Macro").closest("tr") as HTMLElement;
    expect(fred).toHaveTextContent("Live");
    expect(within(fred).getByRole("button", { name: "Pause" })).toBeInTheDocument();
    // Built-in sources cannot be deleted.
    expect(within(table).queryByRole("button", { name: "Delete" })).not.toBeInTheDocument();
  });

  it("tests the form as filled in and shows what the vendor said", async () => {
    const user = userEvent.setup();
    const server = sourcesServer({ "POST /api/ingest/sources/test": fixture("ingest.test") });
    await renderApp("/app/data/ingestion", server.handler);
    await user.click(await screen.findByRole("button", { name: "Test connection" }));
    await toastSays("A source needs a name before it can be tested");
    expect(server.writes).toHaveLength(0);

    await user.type(screen.getByLabelText("Source name"), "Local file");
    await user.selectOptions(screen.getByLabelText("Protocol / connector"), "File");
    expect(screen.getByLabelText("File path / discovery pattern")).toHaveAttribute("placeholder", "/data/import/**/*.parquet");
    expect(screen.getByLabelText("Frequency / schedule")).toHaveValue("On file change");
    await user.selectOptions(screen.getByLabelText("Authentication"), "None");
    expect(screen.queryByLabelText("Key value")).not.toBeInTheDocument();
    await user.type(screen.getByLabelText("File path / discovery pattern"), "/nonexistent/*.parquet");
    await user.click(screen.getByRole("button", { name: "Test connection" }));
    await waitFor(() => expect(server.writes).toHaveLength(1));
    expect(server.writes[0]).toMatchObject({
      path: "/api/ingest/sources/test",
      body: { sourceName: "Local file", sourceProtocol: "file", sourceEndpoint: "/nonexistent/*.parquet", sourceAuth: "None", sourceFrequency: "On file change" },
    });
    const result = await screen.findByText("✗ Connection failed");
    const box = result.parentElement as HTMLElement;
    // One sentence as both message and error is shown once.
    expect(within(box).getAllByText("no files match '/nonexistent/*.parquet'")).toHaveLength(1);
    await toastSays("Connection failed · no files match '/nonexistent/*.parquet'");
  });

  it("saves a new source with the credential named for its auth kind, then clears the form", async () => {
    const user = userEvent.setup();
    const server = sourcesServer({ "POST /api/ingest/sources": fixture("ingest.create") });
    await renderApp("/app/data/ingestion", server.handler);
    await user.type(await screen.findByLabelText("Source name"), "Local file");
    await user.type(screen.getByLabelText("WebSocket endpoint"), "wss://x");
    await user.selectOptions(screen.getByLabelText("Authentication"), "Basic auth");
    await user.type(screen.getByLabelText("Username"), "me");
    await user.type(screen.getByLabelText("Password"), "pw");
    await user.click(screen.getByRole("button", { name: "Save data source" }));
    await waitFor(() => expect(server.writes).toHaveLength(1));
    expect(server.writes[0]).toMatchObject({ method: "POST", path: "/api/ingest/sources", body: { sourceName: "Local file", auth_username: "me", auth_password: "pw" } });
    await toastSays(/^Data source “Local file” saved/);
    await waitFor(() => expect(screen.getByLabelText("Source name")).toHaveValue(""));
  });

  it("edits a source in place with a PUT, warning that a stored key must be re-entered", async () => {
    const user = userEvent.setup();
    const sources = fixture<{ sources: Record<string, unknown>[] }>("ingest.sources");
    const keyed = { ...sources, sources: sources.sources.map((s) => (s.id === "fred" ? { ...s, auth: { ...(s.auth as object), type: "api_key", label: "API key", key_name: "api_key", key_set: true } } : s)) };
    const server = sourcesServer({ "/api/ingest/sources": keyed, "PUT /api/ingest/sources/fred": fixture("ingest.update") });
    await renderApp("/app/data/ingestion", server.handler);
    const table = await screen.findByRole("table", { name: "Configured ingestion sources" });
    await user.click(within((await within(table).findByText("FRED Macro")).closest("tr") as HTMLElement).getByRole("button", { name: "Edit" }));
    expect(screen.getByText("editing fred")).toBeInTheDocument();
    expect(screen.getByLabelText("Source name")).toHaveValue("FRED Macro");
    expect(screen.getByLabelText("Key value")).toHaveAttribute("placeholder", "a key is stored — re-enter it or saving will clear it");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(server.writes).toHaveLength(1));
    expect(server.writes[0]).toMatchObject({ method: "PUT", path: "/api/ingest/sources/fred", body: { id: "fred", sourceName: "FRED Macro", sourceProtocol: "polling" } });
    await toastSays(/^Data source “.*” updated/);
  });

  it("pauses, probes, and deletes a user source only after asking", { timeout: 30_000 }, async () => {
    const user = userEvent.setup();
    const sources = fixture<{ sources: Record<string, unknown>[] }>("ingest.sources");
    const mine = { ...sources, sources: sources.sources.map((s) => (s.id === "trends" ? { ...s, builtin: false } : s)) };
    const server = sourcesServer({
      "/api/ingest/sources": mine,
      "POST /api/ingest/sources/fred/enabled": fixture("ingest.setEnabled"),
      "POST /api/ingest/sources/fred/test": fixture("ingest.probe"),
      "DELETE /api/ingest/sources/trends": { deleted: "trends" },
    });
    await renderApp("/app/data/ingestion", server.handler);
    const table = await screen.findByRole("table", { name: "Configured ingestion sources" });
    const fred = (await within(table).findByText("FRED Macro")).closest("tr") as HTMLElement;
    await user.click(within(fred).getByRole("button", { name: "Pause" }));
    await waitFor(() => expect(server.writes[0]).toMatchObject({ path: "/api/ingest/sources/fred/enabled", body: { enabled: false } }));
    await toastSays("FRED Macro paused");
    await user.click(within(fred).getByRole("button", { name: "Test" }));
    await waitFor(() => expect(server.writes[1]).toMatchObject({ path: "/api/ingest/sources/fred/test" }));
    await toastSays(/^FRED Macro · /);

    const trends = within(table).getByText("Google Trends").closest("tr") as HTMLElement;
    await user.click(within(trends).getByRole("button", { name: "Delete" }));
    const dialog = await screen.findByRole("dialog", { name: "Delete data source" });
    expect(server.writes).toHaveLength(2);
    await user.click(within(dialog).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(server.writes[2]).toMatchObject({ method: "DELETE", path: "/api/ingest/sources/trends" }));
    await toastSays("Google Trends deleted");
  });

  it("shows the server's reason when the sources cannot load", async () => {
    await renderApp("/app/data/ingestion", sourcesServer({ "/api/ingest/sources": json(503, { detail: "registry offline" }) }).handler);
    expect(await screen.findByText("registry offline")).toBeInTheDocument();
  });
});

describe("data quality", () => {
  it("shows the cards, incidents, check suite, SLA rows, tickets and the outlier queue", async () => {
    await renderApp("/app/data/quality", sourcesServer().handler);
    expect(await screen.findByText("96.6%")).toBeInTheDocument();
    expect(screen.getByText("0.14%")).toBeInTheDocument();
    expect(screen.getByText("71")).toBeInTheDocument();
    expect(screen.getByText("3 open")).toBeInTheDocument();

    const suite = screen.getByRole("table", { name: "Check suite" });
    const row = (name: string) => within(suite).getByText(name).closest("tr") as HTMLElement;
    expect(row("Gap scan")).toHaveTextContent("all stored series · 3 checkedCLEAN");
    expect(row("Stale tickers")).toHaveTextContent("NOT RUN");
    expect(row("SLA · fred")).toHaveTextContent("BREACHED 3.7H");
    expect(row("SLA · trends")).toHaveTextContent("PENDING");
    expect(screen.getByText(/^1 open quality ticket\(s\): SLA breach · FRED\./)).toBeInTheDocument();
    expect(screen.getByText("3 dataset(s) in the store · 3 scored on the last run")).toBeInTheDocument();

    expect(screen.getByText("157 open")).toBeInTheDocument();
    const queue = screen.getByLabelText("Outlier review queue");
    expect(within(queue).getAllByRole("button", { name: "Escalate" })).toHaveLength(3);
    // The detector's "human" suggestion highlights Escalate.
    expect(within(queue).getAllByRole("button", { name: "Escalate" })[0]).toHaveClass("bg-accent");
    expect(screen.getAllByText("⚠ 71 open incident(s). Flagged observations stay in the data until somebody quarantines them in the queue.").length).toBeGreaterThan(0);
  });

  it("resolves an incident, opens an SLA ticket, decides an outlier and runs the suite", { timeout: 30_000 }, async () => {
    const user = userEvent.setup();
    const incident = fixture<{ incidents: { id: string }[] }>("quality.incidents").incidents[0]?.id ?? "";
    const outlier = fixture<{ outliers: { id: string }[] }>("quality.outliers").outliers[0]?.id ?? "";
    const server = sourcesServer({
      [`POST /api/quality/incidents/${incident}/resolve`]: fixture("quality.resolve"),
      "POST /api/quality/tickets": fixture("quality.ticket"),
      [`POST /api/quality/outliers/${outlier}/decide`]: fixture("quality.decide"),
      "POST /api/quality/run": fixture("quality.run"),
    });
    await renderApp("/app/data/quality", server.handler);
    const incidents = await screen.findByRole("table", { name: "Quality incidents" });
    await user.click(within(incidents).getAllByRole("button", { name: "Resolve" })[0] as HTMLElement);
    await waitFor(() => expect(server.writes[0]).toMatchObject({ path: `/api/quality/incidents/${incident}/resolve` }));
    await toastSays("Incident resolved");

    const suite = screen.getByRole("table", { name: "Check suite" });
    await user.click(within(within(suite).getByText("SLA · fred").closest("tr") as HTMLElement).getByRole("button", { name: "Ticket" }));
    await waitFor(() =>
      expect(server.writes[1]).toMatchObject({
        path: "/api/quality/tickets",
        body: { title: "SLA breach · FRED Macro", check: "sla", dataset: "", severity: "error", body: "3.7h since the last ingest, tolerance 2.0h (schedule: Hourly)" },
      }),
    );
    await toastSays("Ticket ec3d4e47 opened · tradinglib workspace (no external tracker configured)");

    const queue = screen.getByLabelText("Outlier review queue");
    await user.click(within(queue).getAllByRole("button", { name: "Escalate" })[0] as HTMLElement);
    await waitFor(() => expect(server.writes[2]).toMatchObject({ path: `/api/quality/outliers/${outlier}/decide`, body: { decision: "escalate", by: "user" } }));
    await toastSays("fred/UNRATE · escalated for human review — still in the queue, value untouched");

    await user.click(screen.getByRole("button", { name: "Run checks" }));
    await waitFor(() => expect(server.writes[3]).toMatchObject({ path: "/api/quality/run" }));
    await toastSays(/^Quality suite ran over \d+ dataset\(s\)$/);
  });

  it("lists a failing check's datasets on Review", async () => {
    const user = userEvent.setup();
    const series = { datasets: [{ dataset: "bars:A:1d", label: "A 1d", checks: [{ check: "completeness", passed: false, severity: "warn", detail: "2 gaps" }] }], count: 1 };
    await renderApp("/app/data/quality", sourcesServer({ "/api/quality/series": series }).handler);
    const suite = await screen.findByRole("table", { name: "Check suite" });
    const gap = within(suite).getByText("Gap scan").closest("tr") as HTMLElement;
    expect(gap).toHaveTextContent("1 GAP");
    await user.click(within(gap).getByRole("button", { name: "Review" }));
    expect(screen.getByLabelText("Check detail")).toHaveTextContent("Gap scan · 1 dataset(s)A 1d — 2 gaps");
  });

  it("is honest before any check has run, and refetches when the quality channel fires", async () => {
    const empty = { summary: { overall_quality_pct: null, stale_series: 0, missing_values_pct: null, failed_checks: 0, last_run: null } };
    const server = sourcesServer({
      "/api/quality/summary": empty.summary,
      "/api/quality/series": { datasets: [], count: 0 },
      "/api/quality/incidents": { incidents: [], count: 0 },
      "/api/quality/outliers": { outliers: [], count: 0, counts: { open: 0, quarantined: 0, accepted: 0, escalated: 0, awaiting_review: 0 } },
      "/api/quality/sla": { sla: [], count: 0 },
      "/api/quality/tickets": { tickets: [], count: 0 },
    });
    const app = await renderApp("/app/data/quality", server.handler);
    expect(await screen.findByText("no run yet")).toBeInTheDocument();
    expect(screen.getByText("never run")).toBeInTheDocument();
    expect(screen.getByText("No checks have run yet. Run the suite below to record real results.")).toBeInTheDocument();
    expect(screen.getByText(/^No check has been recorded yet\./)).toBeInTheDocument();
    expect(screen.getByText("NO SOURCES")).toBeInTheDocument();
    expect(screen.getByText("cleared")).toBeInTheDocument();
    expect(screen.getByText(/^Nothing flagged\./)).toBeInTheDocument();

    const summaryReads = () => app.fetchMock.mock.calls.filter(([u]) => String(u).includes("/api/quality/summary")).length;
    const before = summaryReads();
    act(() => app.socket().open());
    act(() => app.socket().event("quality", { type: "run_finished" }));
    await waitFor(() => expect(summaryReads()).toBe(before + 1));
  });

  it("shows the server's reason when quality cannot load", async () => {
    await renderApp("/app/data/quality", sourcesServer({ "/api/quality/sla": json(500, { detail: "sla table missing" }) }).handler);
    expect(await screen.findByText("Data quality: sla table missing")).toBeInTheDocument();
  });
});
