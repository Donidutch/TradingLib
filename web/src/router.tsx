import { createRootRoute, createRoute, createRouter } from "@tanstack/react-router";
import { NotPorted } from "./pages/NotPorted";
import { Shell } from "./shell/Shell";

const rootRoute = createRootRoute({ component: Shell });

const overviewRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  component: () => <NotPorted title="Overview" slice="P3" href="/#overview" />,
});

const stockIndexRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/stock",
  component: () => <NotPorted title="Stock Research" slice="P4" href="/#stock" />,
});

const stockRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/stock/$symbol",
  component: function StockPage() {
    const { symbol } = stockRoute.useParams();
    return (
      <NotPorted
        title={`Stock Research · ${symbol.toUpperCase()}`}
        slice="P4"
        href={`/#stock/${encodeURIComponent(symbol.toUpperCase())}`}
      />
    );
  },
});

export const routeTree = rootRoute.addChildren([overviewRoute, stockIndexRoute, stockRoute]);

export function makeRouter(history?: Parameters<typeof createRouter>[0]["history"]) {
  return createRouter({ routeTree, basepath: "/app", history });
}

declare module "@tanstack/react-router" {
  interface Register {
    router: ReturnType<typeof makeRouter>;
  }
}
