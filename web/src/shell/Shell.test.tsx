import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { fixture, json, renderApp } from "../test/render-app";
import { openLegacy } from "./legacy";
import { PICK_STORE_KEY } from "./Pick";

vi.mock("./legacy", () => ({ openLegacy: vi.fn() }));

afterEach(() => {
  vi.unstubAllGlobals();
  vi.mocked(openLegacy).mockClear();
  localStorage.clear();
});

const strip = () => screen.getByLabelText("Datastrip");
const engine = () => screen.getByLabelText("Engine health");
const toast = () => screen.getByRole("status");
const statusCalls = (fetchMock: Awaited<ReturnType<typeof renderApp>>["fetchMock"]) =>
  fetchMock.mock.calls.filter(([url]) => String(url) === "/api/status").length;

describe("datastrip and topbar", () => {
  it("shows the real status figures and the active scope", async () => {
    await renderApp();
    await waitFor(() => expect(strip()).toHaveTextContent("4 SOURCES CONNECTED"));
    expect(strip()).toHaveTextContent("GATEWAY 1069MS");
    expect(strip()).toHaveTextContent("1 DELAYED SERIES");
    expect(strip()).toHaveTextContent("SPEND $0.00 / $50.00");
    expect(strip()).toHaveTextContent("TRIALS LOGGED 0");
    expect(engine()).toHaveTextContent("● ENGINE DEGRADED");
    const select = await screen.findByRole("combobox", { name: "Universe" });
    await waitFor(() => expect(select).toHaveValue("watchlist:primary-research"));
    expect(within(select).getByRole("option", { name: "Watchlist: Primary research (2)" })).toBeInTheDocument();
    expect(screen.getByLabelText("New York time")).toHaveTextContent(/^\d\d:\d\d:\d\d ET$/);
  });

  it("follows the status channel", async () => {
    const app = await renderApp();
    act(() => app.socket().open());
    await waitFor(() => expect(strip()).toHaveTextContent("4 SOURCES CONNECTED"));
    act(() =>
      app.socket().event("status", {
        ...fixture<Record<string, unknown>>("status"),
        event: "datastrip",
        sources_connected: 7,
        engine_health: "healthy",
        data_live: true,
      }),
    );
    await waitFor(() => expect(strip()).toHaveTextContent("7 SOURCES CONNECTED"));
    expect(strip()).toHaveTextContent("● DATA LIVE");
    expect(engine()).toHaveTextContent("● ENGINE HEALTHY");
  });

  it("toasts the backend's notifications", async () => {
    const app = await renderApp();
    act(() => app.socket().open());
    act(() => app.socket().event("notifications", { kind: "finding", message: "Finding created: AAPL" }));
    expect(toast()).toHaveTextContent("Finding created: AAPL");
  });
});

describe("degraded and recovered", () => {
  it("announces a lost socket and its recovery, and refreshes status", async () => {
    const app = await renderApp();
    act(() => app.socket().open());
    await waitFor(() => expect(strip()).toHaveTextContent("4 SOURCES CONNECTED"));
    const first = app.socket();

    act(() => first.drop());
    expect(strip()).toHaveTextContent("● LIVE FEED LOST");
    expect(toast()).toHaveTextContent("Live connection lost — retrying");

    await waitFor(() => expect(app.socket()).not.toBe(first), { timeout: 2000 });
    const before = statusCalls(app.fetchMock);
    act(() => app.socket().open());
    expect(strip()).toHaveTextContent("● DATA IDLE");
    await waitFor(() => expect(statusCalls(app.fetchMock)).toBeGreaterThan(before));
    await waitFor(() => expect(toast()).toHaveTextContent("Live connection restored"), { timeout: 6000 });
  }, 10_000);

  it("shows an unreachable backend, then the real state once it answers again", async () => {
    let down = false;
    const app = await renderApp("/app/", (url) => {
      if (down && url.pathname === "/api/status") throw new TypeError("Failed to fetch");
      return undefined;
    });
    await waitFor(() => expect(strip()).toHaveTextContent("4 SOURCES CONNECTED"));

    down = true;
    await act(() => app.queryClient.refetchQueries({ queryKey: ["status"] }));
    expect(engine()).toHaveTextContent("● ENGINE UNREACHABLE");
    expect(engine()).toHaveAttribute("title", expect.stringContaining("cannot reach GET /api/status"));
    expect(strip()).toHaveTextContent("● BACKEND UNREACHABLE");

    down = false;
    await act(() => app.queryClient.refetchQueries({ queryKey: ["status"] }));
    expect(engine()).toHaveTextContent("● ENGINE DEGRADED");
    expect(strip()).not.toHaveTextContent("UNREACHABLE");
  });
});

describe("navigation", () => {
  it("routes ported workspaces in the app and sends the rest to the current console", async () => {
    const app = await renderApp();
    const rail = screen.getByRole("navigation", { name: "Workspaces" });
    expect(within(rail).getByRole("link", { name: /Overview/ })).toHaveAttribute("aria-current", "page");
    expect(within(rail).getByRole("link", { name: /Strategy Lab/ })).toHaveAttribute("href", "/#tl/lab");
    expect(within(rail).getByRole("link", { name: /Settings/ })).toHaveAttribute("href", "/#settings");

    await userEvent.click(within(rail).getByRole("link", { name: /Stock Research/ }));
    await waitFor(() => expect(app.router.state.location.pathname).toBe("/stock"));
    expect(within(rail).getByRole("link", { name: /Stock Research/ })).toHaveAttribute("aria-current", "page");
    expect(await screen.findByRole("heading", { name: "STOCK RESEARCH" })).toBeInTheDocument();
  });

  it("opens a deep link to a ticker", async () => {
    await renderApp("/app/stock/nvda");
    expect(await screen.findByRole("heading", { name: "STOCK RESEARCH · NVDA" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "current console" })).toHaveAttribute("href", "/#stock/NVDA");
  });
});

describe("command palette", () => {
  it("opens with ⌘K and jumps to any typed ticker", async () => {
    const user = userEvent.setup();
    const app = await renderApp();
    await user.keyboard("{Control>}k{/Control}");
    const dialog = screen.getByRole("dialog", { name: "Command palette" });
    await user.type(within(dialog).getByRole("textbox"), "msft");
    expect(within(dialog).getByRole("option", { name: /Research MSFT/ })).toHaveAttribute("aria-selected", "true");
    await user.keyboard("{Enter}");
    await waitFor(() => expect(app.router.state.location.pathname).toBe("/stock/MSFT"));
    expect(screen.queryByRole("dialog", { name: "Command palette" })).not.toBeInTheDocument();
  });

  it("lists search results and opens them where they live", async () => {
    const user = userEvent.setup();
    const app = await renderApp();
    await user.click(screen.getByRole("button", { name: "Search the research app" }));
    const dialog = screen.getByRole("dialog", { name: "Command palette" });
    await user.type(within(dialog).getByRole("textbox"), "aa");
    const result = await within(dialog).findByRole("option", { name: /^AAPL/ });
    await user.click(result);
    await waitFor(() => expect(app.router.state.location.pathname).toBe("/stock/AAPL"));
  });

  it("filters workspaces, and opens unported ones in the current console", async () => {
    const user = userEvent.setup();
    await renderApp();
    await user.keyboard("{Control>}k{/Control}");
    const dialog = screen.getByRole("dialog", { name: "Command palette" });
    await user.type(within(dialog).getByRole("textbox"), "settings");
    await user.click(within(dialog).getByRole("option", { name: /Open Settings/ }));
    expect(openLegacy).toHaveBeenCalledWith("/#settings");
  });

  it("opens the backtests tab, and says so, when there is no active backtest", async () => {
    const user = userEvent.setup();
    await renderApp();
    await user.keyboard("{Control>}k{/Control}");
    await user.click(screen.getByRole("option", { name: /Open active backtest/ }));
    await waitFor(() => expect(openLegacy).toHaveBeenCalledWith("/#tl/lab/backtests"));
    expect(toast()).toHaveTextContent("No backtests yet");
  });

  it("shows a failed search instead of an empty list", async () => {
    const user = userEvent.setup();
    await renderApp("/app/", (url) => (url.pathname === "/api/search" ? json(500, { detail: "index offline" }) : undefined));
    await user.keyboard("{Control>}k{/Control}");
    const dialog = screen.getByRole("dialog", { name: "Command palette" });
    await user.type(within(dialog).getByRole("textbox"), "apple");
    expect(await within(dialog).findByRole("option", { name: /Search failed: .*index offline/ })).toBeDisabled();
  });

  it("closes with Escape and on the backdrop", async () => {
    const user = userEvent.setup();
    await renderApp();
    await user.keyboard("{Control>}k{/Control}");
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});

describe("universe selector", () => {
  it("sets the active scope on the server and confirms it", async () => {
    const user = userEvent.setup();
    const active = fixture<Record<string, unknown>>("universes.active");
    const switched = {
      ...active,
      active: "nasdaq100",
      scope: { kind: "universe", id: "nasdaq100", name: "Nasdaq-100", ref: "universe:nasdaq100", symbols: [], symbol_count: 0 },
    };
    let current = active;
    const app = await renderApp("/app/", (url, init) => {
      if (url.pathname !== "/api/universes/active") return undefined;
      if (init?.method === "POST") current = switched;
      return json(200, current);
    });
    const select = await screen.findByRole("combobox", { name: "Universe" });
    await waitFor(() => expect(select).toBeEnabled());
    await user.selectOptions(select, "universe:nasdaq100");
    const post = app.fetchMock.mock.calls.find(([, init]) => init?.method === "POST");
    expect(post?.[1]?.body).toBe('{"universe_id":"universe:nasdaq100"}');
    await waitFor(() => expect(toast()).toHaveTextContent("Active universe: Nasdaq-100"));
    expect(select).toHaveValue("universe:nasdaq100");
  });

  it("keeps the old scope and says why when the switch fails", async () => {
    const user = userEvent.setup();
    await renderApp("/app/", (url, init) =>
      url.pathname === "/api/universes/active" && init?.method === "POST"
        ? json(409, { detail: "universe has no members" })
        : undefined,
    );
    const select = await screen.findByRole("combobox", { name: "Universe" });
    await waitFor(() => expect(select).toBeEnabled());
    await user.selectOptions(select, "universe:sp500");
    await waitFor(() => expect(toast()).toHaveTextContent(/Could not switch scope: .*universe has no members/));
    expect(select).toHaveValue("watchlist:primary-research");
  });
});

describe("PICK mode", () => {
  it("marks panels, keeps them across reloads and exports them", async () => {
    const user = userEvent.setup();
    // A page with no panels of its own, so the injected one is p0.
    await renderApp("/app/stock");
    const main = screen.getByRole("main");
    const panel = document.createElement("section");
    panel.setAttribute("data-panel", "");
    panel.innerHTML = "<h3>Quotes</h3><button>inside</button>";
    act(() => main.append(panel));

    await user.click(screen.getByRole("button", { name: "PICK" }));
    expect(document.body).toHaveClass("pickmode");
    fireEvent.click(within(panel).getByRole("button", { name: "inside" }));
    await waitFor(() => expect(panel).toHaveAttribute("data-picked", "1"));
    expect(screen.getByLabelText("Panels selected")).toHaveTextContent("1");
    expect(JSON.parse(localStorage.getItem(PICK_STORE_KEY) ?? "{}")).toEqual({
      "stock.p0": { page: "stock", sub: "", id: "stock.p0", title: "Quotes" },
    });

    await user.click(screen.getByRole("button", { name: "PICK" }));
    await user.click(screen.getByRole("button", { name: "Export" }));
    const exported = screen.getByLabelText("Picked panels as JSON") as HTMLTextAreaElement;
    expect(JSON.parse(exported.value)).toEqual([
      { page: "stock", sub: "", id: "stock.p0", title: "Quotes" },
    ]);
    await user.click(screen.getByRole("button", { name: "Clear all" }));
    expect(JSON.parse(exported.value)).toEqual([]);
    expect(panel).not.toHaveAttribute("data-picked");
  });
});
