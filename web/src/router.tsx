import { createRootRoute, createRoute, createRouter } from "@tanstack/react-router";
import { DEFAULT_SYMBOL } from "./pages/stock/rules";
import { StockPage } from "./pages/stock/StockPage";
import { OverviewPage } from "./pages/overview/OverviewPage";
import { Shell } from "./shell/Shell";

const rootRoute = createRootRoute({ component: Shell });

const overviewRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  validateSearch: (search: Record<string, unknown>): { symbol?: string } =>
    typeof search.symbol === "string" && search.symbol.trim() ? { symbol: search.symbol.trim().toUpperCase() } : {},
  component: function Overview() {
    const { symbol } = overviewRoute.useSearch();
    return <OverviewPage symbol={symbol} />;
  },
});

const stockIndexRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/stock",
  component: () => <StockPage key={DEFAULT_SYMBOL} symbol={DEFAULT_SYMBOL} />,
});

const stockRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/stock/$symbol",
  component: function Stock() {
    const symbol = stockRoute.useParams().symbol.trim().toUpperCase();
    return <StockPage key={symbol} symbol={symbol} />;
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
