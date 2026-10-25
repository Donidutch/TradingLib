import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient } from "@tanstack/react-query";
import { App } from "./App";
import { ApiClient } from "./api/client";
import { makeApi } from "./api/endpoints";
import { LiveStream } from "./live/stream";
import { makeRouter } from "./router";
import "./ui/theme.css";

const root = document.getElementById("root");
if (!root) throw new Error("#root missing from index.html");

const queryClient = new QueryClient({
  defaultOptions: {
    // A failed request is shown, not hidden behind silent retries; live data
    // arrives over the stream, so refetching on focus is not needed.
    queries: { retry: 1, refetchOnWindowFocus: false, staleTime: 30_000 },
  },
});

createRoot(root).render(
  <StrictMode>
    <App
      router={makeRouter()}
      queryClient={queryClient}
      api={makeApi(new ApiClient())}
      stream={new LiveStream()}
    />
  </StrictMode>,
);
