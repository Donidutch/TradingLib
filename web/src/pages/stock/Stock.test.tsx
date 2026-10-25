/* Stock Research against saved real responses (AAPL): data, empty and error
 * states for the head and each tab (map: docs/UI_PORT.md, P4). */
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { fixture, json, renderApp, serve } from "../../test/render-app";

vi.mock("../../ui/charts/LineChart", () => ({
  LineChart: (props: { lines: { label: string; points: unknown[] }[]; live?: { price: number | null } | null; intraday?: boolean }): ReactNode => (
    <div
      data-testid="line-chart"
      data-lines={props.lines.map((l) => `${l.label}:${l.points.length}`).join(",")}
      data-live={props.live?.price ?? ""}
      data-intraday={String(Boolean(props.intraday))}
    />
  ),
}));

afterEach(() => vi.unstubAllGlobals());

type Body = Record<string, unknown>;
const research = (part: string) => fixture<Body>(part ? `research.${part}` : "research.company");

const news = {
  ...research("news"),
  items: [1, 2, 3, 4, 5].map((n) => ({
    symbol: "AAPL", headline: `Story ${n}`, url: `https://example.com/${n}`, published_at: "2026-10-25T10:00:00+00:00",
    publisher: `Desk ${n}`, summary: null, source: "yfinance", sentiment: n === 1 ? 0.6 : -0.3,
    sentiment_label: n === 1 ? "positive" : "negative", age_hours: 5,
  })),
  count: 5,
  aggregate: { mean_compound: -0.12, recency_weighted: -0.1, label: "negative", counts: { positive: 1, neutral: 0, negative: 4 }, half_life_days: 3 },
};

/** Every request answered from the AAPL fixtures, whatever the symbol; `routes` overrides. */
function stock(path = "/app/stock/AAPL", routes: Record<string, unknown> = {}) {
  return renderApp(
    path,
    serve({
      "/api/research/company/*": (url: URL) => json(200, research(url.pathname.split("/")[5] ?? "")),
      "/api/lab/hypotheses": fixture("lab.hypotheses"),
      "/api/agents/findings": fixture("agents.findings"),
      ...routes,
    }),
  );
}

const tab = async (name: string) => {
  await userEvent.click(await screen.findByRole("tab", { name }));
  return screen.getByRole("tabpanel", { name });
};
const panel = async (scope: HTMLElement, name: string) =>
  (await within(scope).findByRole("heading", { name })).closest("section") as HTMLElement;
const valueOf = (scope: HTMLElement, label: string) => within(scope).getByText(label).nextElementSibling as HTMLElement;

describe("head", () => {
  it("shows the company, price, market cap, next earnings and freshness", async () => {
    await stock();
    expect(await screen.findByRole("heading", { name: "APPLE INC." })).toBeInTheDocument();
    expect(screen.getByText("NMS · Technology · North America")).toBeInTheDocument();
    const price = screen.getByLabelText("Price");
    expect(price).toHaveTextContent("$347.73");
    expect(price).toHaveClass("text-down");
    expect(valueOf(document.body, "MARKET CAP")).toHaveTextContent("$5.07T");
    expect(valueOf(document.body, "NEXT EARNINGS")).toHaveTextContent("8 DAYS");
    expect(valueOf(document.body, "NEXT EARNINGS")).toHaveAttribute("title", "Nov 02 · vendor earnings calendar");
    expect(valueOf(document.body, "DATA FRESHNESS")).toHaveTextContent("CURRENT");
  });

  it("takes its live price from the tape", async () => {
    const app = await stock();
    await screen.findByRole("heading", { name: "APPLE INC." });
    act(() => app.socket().open());
    expect(app.socket().sent).toContainEqual({ action: "subscribe", channel: "tape.AAPL", params: {} });
    act(() =>
      app.socket().event("tape.AAPL", {
        symbol: "AAPL", price: 351.2, ts: "2026-10-26T14:00:00+00:00", bid: null, ask: null, bid_size: null, ask_size: null,
        volume: null, prev_close: 348.6, source: "yfinance", change: 2.6, change_pct: 0.75, kind: "quote", delayed: true, feed: "yfinance",
      }),
    );
    const price = screen.getByLabelText("Price");
    expect(price).toHaveTextContent("$351.20");
    expect(price).toHaveClass("text-up");
    expect(price).toHaveAttribute("title", "live quote 2026-10-26T14:00:00+00:00 · yfinance (delayed)");
    expect(screen.getByTestId("line-chart")).toHaveAttribute("data-live", "351.2");
  });

  it("says why an unknown ticker could not be loaded", async () => {
    await stock("/app/stock/zzzzq", {
      "/api/research/company/ZZZZQ": json(404, { detail: { error: "not_found", entity: "symbol", detail: "Yahoo returned no company profile for ZZZZQ" } }),
    });
    expect(await screen.findByRole("heading", { name: "ZZZZQ could not be loaded" })).toBeInTheDocument();
    expect(screen.getAllByText("Yahoo returned no company profile for ZZZZQ").length).toBeGreaterThan(0);
    expect(screen.getByLabelText("Price")).toHaveTextContent("—");
    expect(valueOf(document.body, "MARKET CAP")).toHaveTextContent("—");
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("ZZZZQ: Yahoo returned no company profile for ZZZZQ"));
  });

  it("opens GRAB without a symbol and loads any ticker typed in", async () => {
    const user = userEvent.setup();
    const app = await stock("/app/stock");
    const input = screen.getByLabelText("Research ticker");
    expect(input).toHaveValue("GRAB");
    await user.clear(input);
    await user.type(input, "msft{Enter}");
    await waitFor(() => expect(app.router.state.location.pathname).toBe("/stock/MSFT"));
  });
});

describe("overview tab", () => {
  it("shows the snapshot, the thesis, the bars and the signals", async () => {
    await stock();
    const overview = screen.getByRole("tabpanel", { name: "Overview" });
    const snapshot = await panel(overview, "Fundamental snapshot");
    await waitFor(() => expect(valueOf(snapshot, "YOY GROWTH")).toHaveTextContent("+6.4%"));
    expect(valueOf(snapshot, "YOY GROWTH")).toHaveClass("text-up");
    expect(valueOf(snapshot, "NET CASH")).toHaveTextContent("-$21.95B");

    const thesis = await panel(overview, "Research thesis");
    expect(await within(thesis).findByRole("heading", { name: "AAPL margin expansion" })).toBeInTheDocument();
    expect(within(thesis).getByText("2 sources")).toBeInTheDocument();
    expect(within(thesis).getByText("Results in 8d")).toHaveAttribute("title", expect.any(String));
    expect(within(thesis).getByText("KEY RISKS").parentElement).toHaveTextContent("Valuation");

    const bars = await panel(overview, "Revenue & adjusted EBITDA");
    expect(within(bars).getByText("2022–2024")).toBeInTheDocument();
    expect(within(bars).getAllByTitle(/^Revenue \$/)).toHaveLength(3);

    const signals = await panel(overview, "Alternative signals");
    expect(valueOf(signals, "App rank composite")).toHaveTextContent("↑ 17");
    expect(valueOf(signals, "News sentiment")).toHaveTextContent("—");
  });

  it("switches the chart range and marks intraday ranges", async () => {
    const user = userEvent.setup();
    const app = await stock();
    await screen.findByTestId("line-chart");
    await user.click(screen.getByRole("button", { name: "1D" }));
    await waitFor(() => expect(app.fetchMock.mock.calls.some(([u]) => String(u).endsWith("/price?range=1d"))).toBe(true));
    expect(screen.getByRole("button", { name: "1D" })).toHaveAttribute("aria-pressed", "true");
    await waitFor(() => expect(screen.getByTestId("line-chart")).toHaveAttribute("data-intraday", "true"));
  });

  it("says when there are no bars, no thesis, no news and no statements", async () => {
    await stock("/app/stock/AAPL", {
      "/api/research/company/AAPL/price": { ...research("price"), points: [], count: 0, error: "vendor returned nothing" },
      "/api/research/company/AAPL": { ...research(""), revenue_ebitda: [] },
      "/api/lab/hypotheses": { ...fixture<Body>("lab.hypotheses"), hypotheses: [], count: 0 },
      "/api/research/company/AAPL/thesis": json(404, { detail: "Not Found" }),
    });
    const overview = screen.getByRole("tabpanel", { name: "Overview" });
    expect(await within(overview).findByRole("alert")).toHaveTextContent("no bars for AAPL over 1y — vendor returned nothing");
    expect(await within(overview).findByRole("heading", { name: "No hypothesis recorded for AAPL" })).toBeInTheDocument();
    expect(within(overview).getAllByText("not loaded")).toHaveLength(2);
    expect(await within(overview).findByText("The vendor published no annual revenue or EBITDA for AAPL.")).toBeInTheDocument();
    expect(await within(overview).findByText("The vendor returned no news for AAPL.")).toBeInTheDocument();
  });

  it("shows the news brief with its aggregate", async () => {
    await stock("/app/stock/AAPL", { "/api/research/company/AAPL/news": news });
    const brief = await panel(screen.getByRole("tabpanel", { name: "Overview" }), "News & sentiment");
    expect(await within(brief).findAllByRole("link")).toHaveLength(4);
    expect(within(brief).getByText("negative")).toHaveAttribute("title", "recency-weighted VADER compound -0.10 over 5 items");
  });

  it("reports failures inside each panel", async () => {
    await stock("/app/stock/AAPL", {
      "/api/research/company/AAPL/price": json(502, { detail: "chart vendor down" }),
      "/api/research/company/AAPL/news": json(502, { detail: "news vendor down" }),
      "/api/research/company/AAPL/altdata": json(502, { detail: "trends vendor down" }),
    });
    const overview = screen.getByRole("tabpanel", { name: "Overview" });
    expect(await within(overview).findByRole("alert")).toHaveTextContent(/price history \(1y\): .*chart vendor down/);
    expect(await within(overview).findByText(/news vendor down/)).toBeInTheDocument();
    expect(await within(overview).findByText(/trends vendor down/)).toBeInTheDocument();
  });
});

describe("financials tab", () => {
  it("shows the annual table in millions and the margin lines", async () => {
    await stock();
    const fin = await tab("Financials");
    expect(await within(fin).findByText("394,328")).toBeInTheDocument();
    expect(within(fin).getByText("USD millions")).toBeInTheDocument();
    expect(within(fin).getByTestId("line-chart")).toHaveAttribute("data-lines", "Gross margin:3,EBITDA margin:3");
  });

  it("says when there are no statements, and when the call fails", async () => {
    await stock("/app/stock/AAPL", { "/api/research/company/AAPL/financials": { ...research("financials"), annual: [] } });
    const fin = await tab("Financials");
    expect(await within(fin).findByText("The vendor published no annual statements for AAPL.")).toBeInTheDocument();
    expect(within(fin).getByText("Not enough reported margins to draw a progression.")).toBeInTheDocument();
  });

  it("shows the server's reason on failure", async () => {
    await stock("/app/stock/AAPL", { "/api/research/company/AAPL/financials": json(502, { detail: "statements vendor down" }) });
    expect(await within(await tab("Financials")).findByText(/Financials: .*statements vendor down/)).toBeInTheDocument();
  });
});

describe("earnings tab", () => {
  it("lists quarters newest first with surprises, and the consensus", async () => {
    await stock();
    const earnings = await tab("Earnings & Estimates");
    const rows = await within(earnings).findAllByRole("row");
    expect(rows[1]).toHaveTextContent("2025 Q4");
    expect(within(earnings).getByText("3 quarters")).toBeInTheDocument();
    expect(valueOf(earnings, "FY EPS")).toHaveTextContent("$8.83");
  });

  it.each([
    [{ ...research("estimates"), history: [] }, "The vendor published no earnings history for AAPL."],
    [json(502, { detail: "estimates down" }), /Estimates: .*estimates down/],
  ])("handles empty and failed estimates", async (body, text) => {
    await stock("/app/stock/AAPL", { "/api/research/company/AAPL/estimates": body });
    expect(await within(await tab("Earnings & Estimates")).findByText(text)).toBeInTheDocument();
  });
});

describe("valuation tab", () => {
  it("shows multiples and peers with the subject highlighted", async () => {
    await stock();
    const valuation = await tab("Valuation & Peers");
    await waitFor(() => expect(valueOf(valuation, "EV / SALES")).toHaveTextContent("10.9×"));
    expect(within(valuation).getByText("AAPL").closest("tr")).toHaveAttribute("aria-current", "true");
    expect(within(valuation).getByText("3 names")).toBeInTheDocument();
  });

  it.each([
    [{ ...research("valuation"), peers: [] }, "No peers could be resolved from the configured universes."],
    [json(502, { detail: "peers down" }), /Valuation: .*peers down/],
  ])("handles no peers and failure", async (body, text) => {
    await stock("/app/stock/AAPL", { "/api/research/company/AAPL/valuation": body });
    expect(await within(await tab("Valuation & Peers")).findByText(text)).toBeInTheDocument();
  });
});

describe("news tab", () => {
  it("lists every item with the label counts", async () => {
    await stock("/app/stock/AAPL", { "/api/research/company/AAPL/news": news });
    const tabpanel = await tab("News & Sentiment");
    expect(await within(tabpanel).findByText("5 items scored by vaderSentiment")).toBeInTheDocument();
    expect(within(tabpanel).getByText(/1 positive · 0 neutral · 4 negative/)).toBeInTheDocument();
    expect(within(tabpanel).getAllByRole("link")).toHaveLength(5);
    expect(within(tabpanel).getByText("AAPL · negative")).toBeInTheDocument();
  });

  it.each([
    [research("news"), "The vendor returned no news for AAPL."],
    [json(502, { detail: "news down" }), /News: .*news down/],
  ])("handles no news and failure", async (body, text) => {
    await stock("/app/stock/AAPL", { "/api/research/company/AAPL/news": body });
    expect(await within(await tab("News & Sentiment")).findByText(text)).toBeInTheDocument();
  });
});

describe("alternative data tab", () => {
  it("shows each signal or the reason it has none", async () => {
    await stock();
    const alt = await tab("Alternative Data");
    expect(await within(alt).findByText("GOOGLE TRENDS · AAPL")).toBeInTheDocument();
    expect(valueOf(alt, "WEB TRAFFIC YOY")).toHaveTextContent("+6.7%");
    expect(valueOf(alt, "JOB POSTINGS 90D")).toHaveAttribute("title", expect.stringContaining("no public Greenhouse, Ashby or Lever job board"));
    expect(valueOf(alt, "Google Trends keyword")).toHaveTextContent("Apple");
  });

  it("shows the server's reason on failure", async () => {
    await stock("/app/stock/AAPL", { "/api/research/company/AAPL/altdata": json(502, { detail: "trends down" }) });
    expect(await within(await tab("Alternative Data")).findByText(/Alternative data: .*trends down/)).toBeInTheDocument();
  });
});

describe("research tab", () => {
  it("shows memory, findings and filings", async () => {
    await stock();
    const tabpanel = await tab("Research");
    expect(await within(tabpanel).findByText("1 hypotheses · 1 findings")).toBeInTheDocument();
    expect(within(tabpanel).getByText("AAPL gross margin keeps rising")).toBeInTheDocument();
    expect(within(tabpanel).getByText(/^40 filings · 0 news items · 0 knowledge entries · 1 × 3 · 28 × 4/)).toBeInTheDocument();
    expect(within(tabpanel).getByText("AAPL services growth")).toBeInTheDocument();
    expect(within(tabpanel).getAllByRole("link", { name: "FORM 4" })).toHaveLength(3);
  });

  it("says when findings are not served and memory is missing", async () => {
    await stock("/app/stock/AAPL", {
      "/api/agents/findings": json(404, { detail: "Not Found" }),
      "/api/research/company/AAPL/memory": json(404, { detail: "Not Found" }),
      "/api/research/company/AAPL/filings": { ...research("filings"), items: [], count: 0, error: "EDGAR rate limit" },
    });
    const tabpanel = await tab("Research");
    expect(await within(tabpanel).findByText("Agent findings not served yet")).toBeInTheDocument();
    expect(within(tabpanel).getByText("The research-memory endpoint is unavailable in this build.")).toBeInTheDocument();
    expect(within(tabpanel).getByText("EDGAR rate limit")).toBeInTheDocument();
  });

  it("shows the server's reason on failure", async () => {
    await stock("/app/stock/AAPL", { "/api/research/company/AAPL/filings": json(503, { detail: "EDGAR down" }) });
    expect(await within(await tab("Research")).findByText(/Research memory: .*EDGAR down/)).toBeInTheDocument();
  });
});
