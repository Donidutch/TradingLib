import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { fixture, json, renderApp, serve } from "../../test/render-app";

type Body = Record<string, unknown> & { keys: { provider: string; configured: boolean; source: string | null }[] };
const stored = () => fixture<Body>("settings");

afterEach(() => {
  vi.unstubAllGlobals();
  delete document.documentElement.dataset.theme;
  delete document.documentElement.dataset.density;
});

/** A settings API that applies PUT patches like the server does. */
function settingsServer(start: Body = stored(), answer?: (patch: Record<string, unknown>) => Response | undefined) {
  let current = start;
  const puts: Record<string, unknown>[] = [];
  const handler = serve({
    "/api/settings": () => json(200, current),
  });
  return {
    puts,
    handler: (url: URL, init?: RequestInit): Response | Promise<Response> | undefined => {
      if (url.pathname === "/api/settings" && init?.method === "PUT") {
        const patch = JSON.parse(String(init.body)) as Record<string, unknown>;
        puts.push(patch);
        const custom = answer?.(patch);
        if (custom) return custom;
        const { keys, ...fields } = patch as { keys?: Record<string, string> };
        current = {
          ...current,
          ...fields,
          keys: current.keys.map((k) =>
            keys && k.provider in keys ? { ...k, configured: Boolean(keys[k.provider]), source: keys[k.provider] ? "stored" : null } : k,
          ),
        };
        return json(200, current);
      }
      return handler(url, init);
    },
  };
}

const panel = async (name: string) => (await screen.findByRole("heading", { name })).closest("section") as HTMLElement;

describe("Settings page", () => {
  it("shows every stored preference, key status and provider", async () => {
    await renderApp("/app/settings", settingsServer().handler);
    const appearance = await panel("Appearance");
    expect(within(appearance).getByRole("button", { name: "Switch to light theme" })).toBeInTheDocument();
    expect(within(appearance).getByLabelText("Density")).toHaveValue("compact");
    expect(within(appearance).getByLabelText("Timezone")).toHaveValue("America/New_York");
    const defaults = await panel("Research defaults");
    expect(within(defaults).getByLabelText("Primary benchmark")).toHaveValue("SPY");
    expect(within(defaults).getByLabelText("Default universe")).toHaveValue("us-quality-50");
    const keys = await panel("API keys");
    expect(within(keys).getByText("0 / 3 configured")).toBeInTheDocument();
    expect(within(keys).getByText("not configured · reads ANTHROPIC_API_KEY")).toBeInTheDocument();
    expect(within(keys).getByText(/never returned by the API/)).toBeInTheDocument();
    const providers = await panel("Provider status");
    expect(within(providers).getByText(/per run \$12.00 · daily \$50.00 · monthly \$250.00/)).toBeInTheDocument();
  });

  it("switches the theme, density and clock app-wide, saving one field at a time", async () => {
    const user = userEvent.setup();
    const server = settingsServer();
    await renderApp("/app/settings", server.handler);
    await user.click(await screen.findByRole("button", { name: "Switch to light theme" }));
    await waitFor(() => expect(document.documentElement.dataset.theme).toBe("light"));
    expect(screen.getByRole("button", { name: "Switch to dark theme" })).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Density"), "comfortable");
    await waitFor(() => expect(document.documentElement.dataset.density).toBe("comfortable"));
    await user.selectOptions(screen.getByLabelText("Timezone"), "UTC");
    await waitFor(() => expect(screen.getByLabelText("Clock")).toHaveTextContent(/^\d\d:\d\d:\d\d UTC$/));
    expect(server.puts).toEqual([{ theme: "light" }, { density: "comfortable" }, { timezone: "UTC" }]);
    expect(screen.getAllByRole("status").some((s) => /Light theme enabled|Density: comfortable|Timezone: UTC/.test(s.textContent ?? ""))).toBe(true);
  });

  it("applies the stored theme on any page from the first load", async () => {
    await renderApp("/app/", settingsServer({ ...stored(), theme: "light", density: "comfortable" }).handler);
    await waitFor(() => expect(document.documentElement.dataset.theme).toBe("light"));
    expect(document.documentElement.dataset.density).toBe("comfortable");
  });

  it("moves the topbar universe with the default universe", async () => {
    const user = userEvent.setup();
    const server = settingsServer();
    const app = await renderApp("/app/settings", server.handler);
    await user.selectOptions(await screen.findByLabelText("Default universe"), "sp500");
    await waitFor(() => expect(server.puts).toEqual([{ default_universe: "sp500" }]));
    await waitFor(() =>
      expect(app.fetchMock.mock.calls.filter(([u]) => String(u) === "/api/universes").length).toBeGreaterThan(1),
    );
  });

  it("sends a key without keeping it, refuses an empty one, and clears a stored one", async () => {
    const user = userEvent.setup();
    const server = settingsServer();
    await renderApp("/app/settings", server.handler);
    const field = await screen.findByLabelText("Anthropic API key");
    const row = field.closest("div.border-b") as HTMLElement;
    expect(within(row).getByRole("button", { name: "Clear" })).toBeDisabled();
    await user.click(within(row).getByRole("button", { name: "Save" }));
    expect(server.puts).toEqual([]);
    expect(screen.getAllByRole("status").some((s) => s.textContent?.startsWith("Type a key first"))).toBe(true);

    await user.type(field, "sk-test-not-real");
    await user.click(within(row).getByRole("button", { name: "Save" }));
    expect(field).toHaveValue("");
    await waitFor(() => expect(within(row).getByText(/^configured · stored/)).toBeInTheDocument());
    expect(server.puts).toEqual([{ keys: { anthropic: "sk-test-not-real" } }]);
    expect(document.body.textContent).not.toContain("sk-test-not-real");

    await user.click(within(row).getByRole("button", { name: "Clear" }));
    await waitFor(() => expect(server.puts.at(-1)).toEqual({ keys: { anthropic: "" } }));
    await waitFor(() => expect(within(row).getByText("not configured · reads ANTHROPIC_API_KEY")).toBeInTheDocument());
  });

  it("keeps the stored value and says why when a save fails", async () => {
    const user = userEvent.setup();
    const server = settingsServer(stored(), () => json(400, { detail: "unknown timezone 'Mars/Olympus'" }));
    await renderApp("/app/settings", server.handler);
    await user.selectOptions(await screen.findByLabelText("Timezone"), "UTC");
    await waitFor(() =>
      expect(screen.getAllByRole("status").some((s) => s.textContent === "Could not save: unknown timezone 'Mars/Olympus'")).toBe(true),
    );
    expect(screen.getByLabelText("Timezone")).toHaveValue("America/New_York");
  });

  it("warns when a change needs a restart", async () => {
    const user = userEvent.setup();
    const server = settingsServer(stored(), (patch) => json(200, { ...stored(), ...patch, restart_detail: "takes effect after a restart" }));
    await renderApp("/app/settings", server.handler);
    await user.selectOptions(await screen.findByLabelText("Primary benchmark"), "QQQ");
    await waitFor(() => expect(screen.getAllByRole("status").some((s) => s.textContent === "takes effect after a restart")).toBe(true));
  });

  it("says when settings cannot be loaded", async () => {
    await renderApp("/app/settings", serve({ "/api/settings": json(500, { detail: "db locked" }) }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Settings could not be loaded: db locked");
  });
});
