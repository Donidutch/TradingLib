import { render, screen } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";
import { createMemoryHistory } from "@tanstack/react-router";
import { App } from "./App";
import { makeRouter } from "./router";

describe("App", () => {
  it("renders the index route under the /app base", async () => {
    const router = makeRouter(createMemoryHistory({ initialEntries: ["/app/"] }));
    render(<App router={router} queryClient={new QueryClient()} />);
    expect(await screen.findByRole("heading", { name: "TradingLib" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "current console" })).toHaveAttribute("href", "/");
  });
});
