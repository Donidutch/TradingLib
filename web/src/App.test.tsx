import { screen } from "@testing-library/react";
import { renderApp } from "./test/render-app";

afterEach(() => vi.unstubAllGlobals());

describe("App", () => {
  it("renders the shell and the index route under the /app base", async () => {
    await renderApp("/app/");
    expect(screen.getByRole("navigation", { name: "Workspaces" })).toBeInTheDocument();
    expect(await screen.findByLabelText("Market strip")).toBeInTheDocument();
  });
});
