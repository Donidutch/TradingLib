/* Overview panels against saved real responses: data, empty and error
 * states for each (panel map: docs/UI_PORT.md, P3). */
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { fixture, json, renderApp, serve } from "../../test/render-app";
import { openLegacy } from "../../shell/legacy";

vi.mock("../../shell/legacy", () => ({ openLegacy: vi.fn() }));
// jsdom has no canvas; the chart's own drawing is lightweight-charts' business.
vi.mock("../../ui/charts/AreaChart", () => ({
  AreaChart: (props: { points: unknown[]; live?: { price: number | null } | null }): ReactNode => (
    <div data-testid="area-chart" data-points={props.points.length} data-live={props.live?.price ?? ""} />
  ),
}));

afterEach(() => {
  vi.unstubAllGlobals();
  vi.mocked(openLegacy).mockClear();
});

type Body = Record<string, unknown>;
const active = fixture<Body>("universes.active");
const members = fixture<Body & { members: { symbol: string }[] }>("universes.members");
/** The active scope as a universe (MSFT, AAPL, NVDA) instead of the fixture's watchlist. */
const universeScope = {
  ...active,
  active: "us-quality-50",
  scope: { kind: "universe", id: "us-quality-50", name: "US Quality 50", ref: "universe:us-quality-50", symbols: ["MSFT", "AAPL", "NVDA"], symbol_count: 3 },
  symbols: ["MSFT", "AAPL", "NVDA"],
};

const news = {
  ...fixture<Body>("research.news"),
  items: [1, 2, 3, 4, 5].map((n) => ({
    symbol: "SPY",
    headline: `Headline ${n}`,
    url: `https://example.com/${n}`,
    published_at: "2026-10-25T10:00:00+00:00",
    publisher: `Desk ${n}`,
    summary: n === 1 ? "The lead story summary." : null,
    source: "yfinance",
    sentiment: n === 2 ? -0.4 : 0.3,
    sentiment_label: n === 2 ? "negative" : "positive",
    age_hours: 5,
  })),
  count: 5,
};

/** Every Overview request answered from fixtures; `routes` overrides any of them. */
function overview(routes: Record<string, unknown> = {}, path = "/app/") {
  return renderApp(
    path,
    serve({
      "/api/universes/active": universeScope,
      "/api/universes/us-quality-50/members": members,
      "/api/universes/sp500/breadth": fixture("universes.breadth"),
      "/api/research/company/SPY/news": news,
      "/api/research/company/*": (url: URL) => {
        const part = url.pathname.split("/")[5] ?? "";
        return json(200, fixture(part ? `research.${part}` : "research.company"));
      },
      "/api/research/resolve": fixture("research.resolve"),
      "/api/macro/series": fixture("macro.panel"),
      "/api/agents/inbox": fixture("agents.inbox"),
      ...routes,
    }),
  );
}

const panel = async (name: RegExp | string) =>
  (await screen.findByRole("heading", { name })).closest("section") as HTMLElement;

describe("market strip and health", () => {
  it("shows the status figures in the reference's wording", async () => {
    await overview();
    const strip = screen.getByLabelText("Market strip");
    await waitFor(() => expect(strip).toHaveTextContent("4 of 7 sources connected"));
    expect(strip).toHaveTextContent("Research gateway 1069ms");
    expect(strip).toHaveTextContent("1 delayed series");
    const health = await panel("System & data health");
    expect(within(health).getByText("DEGRADED")).toBeInTheDocument();
    expect(within(health).getByText("DELAYED")).toHaveAttribute("title", "ALPACA_API_KEY / ALPACA_SECRET_KEY are not configured");
    expect(within(health).getByText("2 / 2")).toHaveAttribute("title", expect.stringContaining("Primary research: 100.0% of members"));
  });

  it("says MEASURING before coverage exists, and shows dashes before any status", async () => {
    await overview({ "/api/status": { ...fixture<Body>("status"), fundamentals_coverage: null } });
    const health = await panel("System & data health");
    await waitFor(() => expect(within(health).getByText("MEASURING")).toBeInTheDocument());
  });

  it("keeps dashes when status cannot be loaded", async () => {
    await overview({ "/api/status": json(500, { detail: "boom" }) });
    await waitFor(() => expect(screen.getByLabelText("Engine health")).toHaveTextContent("ENGINE …"));
    const strip = screen.getByLabelText("Market strip");
    for (const text of ["● DATA …", "Research gateway —", "— sources connected", "Last ingest —", "— delayed series"]) {
      expect(strip).toHaveTextContent(text);
    }
    expect(within(await panel("System & data health")).getAllByText("—")).toHaveLength(4);
  });
});

describe("ticker tape", () => {
  it("lists the scope and fills prices from the tape channel", async () => {
    const app = await overview();
    const tape = screen.getByLabelText("Ticker tape");
    await waitFor(() => expect(tape).toHaveTextContent("● US QUALITY 50"));
    expect(within(tape).getByText("MSFT").parentElement).toHaveAttribute("title", "Microsoft Corporation");
    act(() => app.socket().open());
    expect(app.socket().sent).toContainEqual({
      action: "subscribe",
      channel: "tape",
      params: { symbols: ["AAPL", "NVDA", "MSFT"] },
    });
    act(() =>
      app.socket().event("tape.MSFT", {
        symbol: "MSFT", price: 524.51, ts: "2026-10-23T20:00:01+00:00", bid: null, ask: null, bid_size: null, ask_size: null,
        volume: 1, prev_close: 522, source: "yfinance", change: 2.51, change_pct: 0.48, kind: "quote", delayed: true, feed: "yfinance",
      }),
    );
    const msft = within(tape).getByText("MSFT").parentElement as HTMLElement;
    expect(msft).toHaveTextContent("MSFT524.51▲ 0.5%");
    act(() => app.socket().event("tape", { event: "tape_error", detail: "vendor timeout", retry_in_seconds: 30 }));
    expect(within(tape).getByText("● US QUALITY 50")).toHaveAttribute("title", "tape degraded: vendor timeout");
  });

  it("says when the scope has no members", async () => {
    await overview({ "/api/universes/active": { ...universeScope, symbols: [], scope: { ...universeScope.scope, symbols: [], symbol_count: 0 } }, "/api/universes/us-quality-50/members": { ...members, members: [], count: 0 } });
    await waitFor(() => expect(screen.getByLabelText("Ticker tape")).toHaveTextContent("no universe members"));
  });

  it("says when the scope cannot be loaded", async () => {
    await overview({ "/api/universes/active": json(503, { detail: "registry down" }) });
    const label = await within(screen.getByLabelText("Ticker tape")).findByText("● UNIVERSE UNAVAILABLE");
    expect(label).toHaveAttribute("title", expect.stringContaining("registry down"));
  });
});

describe("watchlist", () => {
  it("shows watched symbols with names, live prices and a filter, and opens one in the centre", async () => {
    const user = userEvent.setup();
    const app = await overview();
    const list = await screen.findByRole("list", { name: "Watched symbols" });
    await waitFor(() => expect(within(list).getByText("Apple · Technology")).toHaveAttribute("title", "Apple Inc. · Information Technology"));
    expect(within(await panel("Watchlist")).getByText("2 / 3")).toBeInTheDocument();
    act(() => app.socket().open());
    act(() =>
      app.socket().event("tape.NVDA", {
        symbol: "NVDA", price: 210.96, ts: null, bid: null, ask: null, bid_size: null, ask_size: null,
        volume: null, prev_close: 212.38, source: "yfinance", change: -1.42, change_pct: -0.67, kind: "quote", delayed: true, feed: "yfinance",
      }),
    );
    expect(within(list).getAllByRole("listitem")[1]).toHaveTextContent("210.96");
    expect(within(list).getAllByRole("listitem")[1]).toHaveTextContent("−0.7%");

    await user.type(screen.getByLabelText("Filter watchlist symbols"), "nv");
    expect(within(list).getAllByRole("listitem")).toHaveLength(1);
    await user.click(within(list).getByRole("button", { name: "Open NVDA" }));
    await waitFor(() => expect(app.router.state.location.search).toEqual({ symbol: "NVDA" }));
    expect(screen.getByLabelText("Chart ticker")).toHaveValue("NVDA");
    await user.type(screen.getByLabelText("Filter watchlist symbols"), "x");
    expect(within(list).getByText("No symbol matches “NVX”.")).toBeInTheDocument();
  });

  it("creates “Primary research” when there is no watchlist yet", async () => {
    const user = userEvent.setup();
    const created = { ...(fixture<{ watchlists: Body[] }>("watchlists.list").watchlists[0] as Body & { id: string }), symbols: ["TSLA"], symbol_count: 1 };
    let lists: Body = { watchlists: [], count: 0, default: null };
    const app = await overview({
      "/api/watchlists": () => json(200, lists),
    });
    expect(await screen.findByText("No watchlist yet")).toBeInTheDocument();
    app.fetchMock.mockImplementationOnce(async () => {
      lists = { watchlists: [created], count: 1, default: created.id };
      return json(201, created);
    });
    await user.type(screen.getByLabelText("Symbol to add to the watchlist"), "tsla");
    await user.click(screen.getByRole("button", { name: "Add" }));
    const post = app.fetchMock.mock.calls.find(([u, i]) => u === "/api/watchlists" && i?.method === "POST");
    expect(JSON.parse(String(post?.[1]?.body))).toEqual({
      name: "Primary research",
      symbols: ["TSLA"],
      is_default: true,
    });
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("TSLA added to Primary research"));
    expect(await screen.findByRole("button", { name: "Open TSLA" })).toBeInTheDocument();
  });

  it("removes a symbol, and reports a failed add as a toast", async () => {
    const user = userEvent.setup();
    const wl = fixture<{ watchlists: Body[] }>("watchlists.list").watchlists[0] as Body;
    const app = await overview({
      "/api/watchlists/primary-research/symbols/AAPL": { ...wl, symbols: ["NVDA"], symbol_count: 1 },
      "/api/watchlists/primary-research/symbols": json(422, { detail: "unknown symbol ZZZZQ" }),
    });
    await user.click(await screen.findByRole("button", { name: "Remove AAPL" }));
    expect(app.fetchMock.mock.calls.some(([u, i]) => u === "/api/watchlists/primary-research/symbols/AAPL" && i?.method === "DELETE")).toBe(true);
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("AAPL removed from Primary research"));

    await user.type(screen.getByLabelText("Symbol to add to the watchlist"), "zzzzq");
    await user.click(screen.getByRole("button", { name: "Add" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent(/Could not add ZZZZQ: .*unknown symbol ZZZZQ/), { timeout: 4000 });
  });

  it("shows a load error in the panel", async () => {
    await overview({ "/api/watchlists": json(500, { detail: "db locked" }) });
    expect(await within(await panel("Watchlist")).findByText(/db locked/)).toBeInTheDocument();
    expect(within(await panel("Watchlist")).getByText("error")).toBeInTheDocument();
  });
});

describe("quote, chart and fundamentals", () => {
  it("shows the first scope symbol's price, newest bar, chart and metrics", async () => {
    await overview();
    const quote = await screen.findByLabelText("Quote");
    await waitFor(() => expect(within(quote).getByLabelText("Price")).toHaveTextContent("347.73"));
    expect(screen.getByLabelText("Chart ticker")).toHaveValue("MSFT");
    expect(within(quote).getByText("OPEN").nextSibling).toHaveTextContent("269.28");
    expect(within(quote).getByText("VOL").nextSibling).toHaveTextContent("51.1M");
    expect(within(quote).getByTestId("area-chart")).toHaveAttribute("data-points", "3");
    const fundamentals = await panel("MSFT Fundamentals");
    expect(within(fundamentals).getByText("MARKET CAP").nextSibling).toHaveTextContent("$5.07T");
    expect(within(fundamentals).getByText("REVENUE GROWTH").nextSibling).toHaveClass("text-up");
    expect(within(fundamentals).getByText("QUALITY SCORE").nextSibling).toHaveAttribute("title", expect.stringContaining("no composite quality score"));
  });

  it("deep-links a symbol and moves the price with its live quote", async () => {
    const app = await overview({}, "/app/?symbol=nvda");
    expect(screen.getByLabelText("Chart ticker")).toHaveValue("NVDA");
    await waitFor(() => expect(screen.getByTestId("area-chart")).toBeInTheDocument());
    act(() => app.socket().open());
    act(() =>
      app.socket().event("tape.NVDA", {
        symbol: "NVDA", price: 211.5, ts: "2026-10-26T14:00:00+00:00", bid: null, ask: null, bid_size: null, ask_size: null,
        volume: null, prev_close: 210, source: "yfinance", change: 1.5, change_pct: 0.7, kind: "quote", delayed: true, feed: "yfinance",
      }),
    );
    expect(screen.getByLabelText("Price")).toHaveTextContent("211.50");
    expect(screen.getByTestId("area-chart")).toHaveAttribute("data-live", "211.5");
  });

  it("explains a metric without a value", async () => {
    const company = fixture<Body & { metrics: Record<string, Body> }>("research.company");
    await overview({
      "/api/research/company/MSFT": {
        ...company,
        metrics: { ...company.metrics, forward_pe: { ...company.metrics.forward_pe, value: null, reason: "no forward EPS estimate" } },
      },
    });
    const cell = await within(await panel("MSFT Fundamentals")).findByText("FWD P/E");
    await waitFor(() => expect(cell.nextSibling).toHaveAttribute("title", "no forward EPS estimate"));
    expect(cell.nextSibling).toHaveTextContent("—");
  });

  it("says when the ticker is unknown or has no history", async () => {
    await overview({
      "/api/research/company/ZZZZQ": json(404, { detail: { error: "not_found" } }),
      "/api/research/company/ZZZZQ/price": { ...fixture<Body>("research.price"), symbol: "ZZZZQ", points: [], count: 0 },
    }, "/app/?symbol=ZZZZQ");
    const note = await screen.findByRole("alert");
    await waitFor(() => expect(note).toHaveTextContent("ZZZZQ: not found · no price history for ZZZZQ"));
    expect(screen.getByLabelText("Price")).toHaveTextContent("—");
    expect(within(await panel("ZZZZQ Fundamentals")).getByText("MARKET CAP").nextSibling).toHaveTextContent("—");
    expect(screen.queryByTestId("area-chart")).not.toBeInTheDocument();
  });
});

describe("market context", () => {
  it("shows each row's reading or its reason", async () => {
    await overview({ "/api/research/company/%5EVIX/price": json(502, { detail: "vendor timeout" }) });
    const context = await panel("Market context");
    await waitFor(() => expect(within(context).getByText("S&P above 200D").nextSibling).toHaveAttribute("title", expect.stringContaining("constituents have 200 stored daily closes")));
    expect(within(context).getByText("Regime").nextSibling).toHaveAttribute("title", "only 3 SPY closes available; 200 are needed for the moving average");
    expect(within(context).getByText("Advance / decline").nextSibling).toHaveAttribute("title", "waiting for the first tape cycle");
    await waitFor(() => expect(within(context).getByText("VIX").nextSibling).toHaveAttribute("title", expect.stringMatching(/\^VIX unavailable: .*vendor timeout/)));
  });
});

describe("research inbox", () => {
  it("lists items and opens them in the Agent Center", async () => {
    const user = userEvent.setup();
    await overview();
    const inbox = await panel("Research inbox");
    const item = await within(inbox).findByRole("button", { name: /AAPL services growth/ });
    expect(item).toHaveTextContent("Agent · AAPL");
    expect(within(inbox).getByText("1 item")).toBeInTheDocument();
    await user.click(item);
    expect(openLegacy).toHaveBeenCalledWith("/#agents/findings?findingId=FND-577C4D");
  });

  it.each([
    [{ ...fixture<Body>("agents.inbox"), items: [] }, "Nothing needs a decision"],
    [json(404, { detail: "Not Found" }), "Agent inbox not served yet"],
    [json(500, { detail: "runner crashed" }), /runner crashed/],
  ])("shows the empty, unserved and failed states", async (body, text) => {
    await overview({ "/api/agents/inbox": body });
    expect(await within(await panel("Research inbox")).findByText(text)).toBeInTheDocument();
  });
});

describe("intel tabs", () => {
  it("shows the lead story and four more with their sentiment", async () => {
    await overview();
    const tab = await screen.findByRole("tabpanel", { name: "News" });
    expect(await within(tab).findByRole("heading", { name: "Headline 1" })).toBeInTheDocument();
    expect(within(tab).getByText("The lead story summary.")).toBeInTheDocument();
    expect(within(tab).getAllByRole("link")).toHaveLength(5);
    expect(within(tab).getByText(/Desk 2 · negative −0.40|Desk 2 · negative -0.40/)).toHaveClass("text-down");
  });

  it.each([
    [{ ...news, items: [], count: 0 }, "The vendor returned no market news."],
    [json(502, { detail: "vendor down" }), /Market news unavailable: .*vendor down/],
  ])("says when there is no news or it failed", async (body, text) => {
    await overview({ "/api/research/company/SPY/news": body });
    expect(await within(await screen.findByRole("tabpanel", { name: "News" })).findByText(text)).toBeInTheDocument();
  });

  it("loads the earnings calendar on first view and names the symbols that failed", async () => {
    const user = userEvent.setup();
    const app = await overview({ "/api/research/company/NVDA/estimates": json(502, { detail: "vendor timeout" }) });
    const estimatesCalls = () => app.fetchMock.mock.calls.filter(([u]) => String(u).endsWith("/estimates")).length;
    expect(estimatesCalls()).toBe(0);
    await user.click(await screen.findByRole("tab", { name: "Earnings calendar" }));
    const tab = screen.getByRole("tabpanel", { name: "Earnings calendar" });
    const rows = await within(tab).findAllByRole("row");
    expect(rows).toHaveLength(3); // header, MSFT, AAPL
    expect(rows[1]).toHaveTextContent("Nov 02MSFT15:00 ET$1.98");
    expect(within(tab).getByText(/NVDA: .*vendor timeout/)).toBeInTheDocument();
  });

  it("shows FRED levels and how many are current", async () => {
    const user = userEvent.setup();
    await overview();
    await user.click(await screen.findByRole("tab", { name: "FRED macro" }));
    const tab = screen.getByRole("tabpanel", { name: "FRED macro" });
    expect(await within(tab).findByText("10Y TREASURY")).toBeInTheDocument();
    expect(within(tab).getByText("4.96%")).toHaveClass("text-down");
    expect(within(tab).getByText("4 of 4 series carry a current level.")).toBeInTheDocument();
  });

  it("shows Google Trends per symbol and names failures", async () => {
    const user = userEvent.setup();
    await overview({ "/api/research/company/AAPL/altdata": json(429, { detail: "rate limited" }) });
    await user.click(await screen.findByRole("tab", { name: "Google Trends" }));
    const tab = screen.getByRole("tabpanel", { name: "Google Trends" });
    expect(await within(tab).findByText(/AAPL: .*rate limited/, {}, { timeout: 3000 })).toBeInTheDocument();
    expect(within(tab).getAllByText("APPLE")).toHaveLength(2); // MSFT and NVDA answer with the AAPL fixture
  });
});
