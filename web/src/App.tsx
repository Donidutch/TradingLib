import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { ApiProvider } from "./api/ApiProvider";
import type { Api } from "./api/endpoints";
import { StreamProvider } from "./live/StreamProvider";
import type { LiveStream } from "./live/stream";
import type { makeRouter } from "./router";

export interface AppProps {
  router: ReturnType<typeof makeRouter>;
  queryClient: QueryClient;
  api: Api;
  stream: LiveStream;
}

export function App({ router, queryClient, api, stream }: AppProps) {
  return (
    <QueryClientProvider client={queryClient}>
      <ApiProvider api={api}>
        <StreamProvider stream={stream}>
          <RouterProvider router={router} />
        </StreamProvider>
      </ApiProvider>
    </QueryClientProvider>
  );
}
