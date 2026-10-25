import { render, screen } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";
import { createMemoryHistory } from "@tanstack/react-router";
import { App } from "./App";
import { ApiClient } from "./api/client";
import { makeApi } from "./api/endpoints";
import { LiveStream } from "./live/stream";
import { makeRouter } from "./router";
import { fakeSocketFactory } from "./test/fake-socket";

describe("App", () => {
  it("renders the index route under the /app base", async () => {
    const router = makeRouter(createMemoryHistory({ initialEntries: ["/app/"] }));
    const stream = new LiveStream({ url: "ws://test/ws", socketFactory: fakeSocketFactory });
    render(<App router={router} queryClient={new QueryClient()} api={makeApi(new ApiClient())} stream={stream} />);
    expect(await screen.findByRole("heading", { name: "TradingLib" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "current console" })).toHaveAttribute("href", "/");
  });
});
