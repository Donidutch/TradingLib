import { createRootRoute, createRoute, createRouter } from "@tanstack/react-router";
import { LazyData, LazyDocs, LazyLab, LazyOverview, LazySettings, LazyStock } from "./pages/lazy";
import { DEFAULT_SYMBOL } from "./pages/stock/rules";
import { isDataTab } from "./pages/data/tabs";
import { isPortedLabTab, legacyLab } from "./pages/lab/tabs";
import { openLegacy } from "./shell/legacy";
import { Shell } from "./shell/Shell";

const rootRoute = createRootRoute({ component: Shell });

const overviewRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  validateSearch: (search: Record<string, unknown>): { symbol?: string } =>
    typeof search.symbol === "string" && search.symbol.trim() ? { symbol: search.symbol.trim().toUpperCase() } : {},
  component: function Overview() {
    const { symbol } = overviewRoute.useSearch();
    return <LazyOverview symbol={symbol} />;
  },
});

const stockIndexRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/stock",
  component: () => <LazyStock symbol={DEFAULT_SYMBOL} />,
});

const stockRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/stock/$symbol",
  component: function Stock() {
    const symbol = stockRoute.useParams().symbol.trim().toUpperCase();
    return <LazyStock symbol={symbol} />;
  },
});

const settingsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/settings",
  component: LazySettings,
});

const docsIndexRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/docs",
  component: () => <LazyDocs article={undefined} />,
});

const docsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/docs/$article",
  component: function Docs() {
    return <LazyDocs article={docsRoute.useParams().article} />;
  },
});

const dataIndexRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/data",
  component: () => <LazyData tab="collections" />,
});

const dataRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/data/$tab",
  component: function Data() {
    const tab = dataRoute.useParams().tab;
    return <LazyData tab={isDataTab(tab) ? tab : "collections"} />;
  },
});

const labSearch = (search: Record<string, unknown>): { id?: string } =>
  typeof search.id === "string" && search.id.trim() ? { id: search.id.trim() } : {};

const labIndexRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/lab",
  validateSearch: labSearch,
  component: function Lab() {
    return <LazyLab tab="experiments" id={labIndexRoute.useSearch().id} />;
  },
});

const labRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/lab/$tab",
  validateSearch: labSearch,
  component: function Lab() {
    const tab = labRoute.useParams().tab;
    const { id } = labRoute.useSearch();
    // A tab not ported yet opens in the current console; the lab stays on experiments.
    if (!isPortedLabTab(tab)) {
      openLegacy(legacyLab(tab, id));
      return <LazyLab tab="experiments" id={undefined} />;
    }
    return <LazyLab tab={tab} id={id} />;
  },
});

export const routeTree = rootRoute.addChildren([
  labIndexRoute,
  labRoute,
  dataIndexRoute,
  dataRoute,
  overviewRoute,
  stockIndexRoute,
  stockRoute,
  settingsRoute,
  docsIndexRoute,
  docsRoute,
]);

export function makeRouter(history?: Parameters<typeof createRouter>[0]["history"]) {
  return createRouter({ routeTree, basepath: "/app", history });
}

declare module "@tanstack/react-router" {
  interface Register {
    router: ReturnType<typeof makeRouter>;
  }
}
