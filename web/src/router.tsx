import { createRootRoute, createRoute, createRouter, Outlet } from "@tanstack/react-router";

// Pages arrive slice by slice (docs/UI_PORT.md); P0 only proves the app is
// built and served at /app.
const rootRoute = createRootRoute({ component: Outlet });

const indexRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  component: () => (
    <main className="placeholder">
      <h1>TradingLib</h1>
      <p>
        The React console is being ported. Use the <a href="/">current console</a> meanwhile.
      </p>
    </main>
  ),
});

export const routeTree = rootRoute.addChildren([indexRoute]);

export function makeRouter(history?: Parameters<typeof createRouter>[0]["history"]) {
  return createRouter({ routeTree, basepath: "/app", history });
}

declare module "@tanstack/react-router" {
  interface Register {
    router: ReturnType<typeof makeRouter>;
  }
}
