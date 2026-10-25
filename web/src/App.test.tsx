import { screen } from "@testing-library/react";
import { renderApp } from "./test/render-app";

afterEach(() => vi.unstubAllGlobals());

describe("App", () => {
  it("renders the shell and the index route under the /app base", async () => {
    await renderApp("/app/");
    expect(await screen.findByRole("heading", { name: "OVERVIEW" })).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Workspaces" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "current console" })).toHaveAttribute("href", "/#overview");
  });
});
