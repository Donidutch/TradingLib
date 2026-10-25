import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { json, renderApp, serve } from "../../test/render-app";
import { ARTICLES } from "./articles";

afterEach(() => vi.unstubAllGlobals());

describe("Docs page", () => {
  it("lists the five articles and opens the first", async () => {
    await renderApp("/app/docs");
    expect(await screen.findByRole("article", { name: "Hypotheses and experiments" })).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /Hypotheses and experiments|Backtest integrity|Validation gates|Keyboard shortcuts|Agent autonomy/ })).toHaveLength(5);
    expect(ARTICLES).toHaveLength(5);
  });

  it("opens an article in place and in the URL, with the build version", async () => {
    const user = userEvent.setup();
    const app = await renderApp("/app/docs");
    await user.click(await screen.findByRole("button", { name: /Keyboard shortcuts/ }));
    await waitFor(() => expect(app.router.state.location.pathname).toBe("/docs/shortcuts"));
    const article = screen.getByRole("article", { name: "Keyboard shortcuts" });
    expect(within(article).getByText(/\/app\/stock\/NVDA/)).toBeInTheDocument();
    await waitFor(() => expect(within(article).getByText("TradingLib 0.1.0")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: /Keyboard shortcuts/ })).toHaveAttribute("aria-current", "true");
  });

  it("deep-links an article, and falls back to the first for an unknown one", async () => {
    await renderApp("/app/docs/autonomy");
    expect(await screen.findByRole("article", { name: "Agent autonomy" })).toBeInTheDocument();
  });

  it("falls back to the first article for an unknown id, and keeps the footer when health fails", async () => {
    await renderApp("/app/docs/nope", serve({ "/api/health": json(503, { detail: "down" }) }));
    const article = await screen.findByRole("article", { name: "Hypotheses and experiments" });
    expect(within(article).getByText("TradingLib")).toBeInTheDocument();
  });
});
