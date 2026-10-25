/* Each page (and the chart library they share) loads when first opened, so
 * the shell paints without waiting for code it may not need. */
import type { DataTab } from "./data/tabs";
import { lazy, Suspense } from "react";

const OverviewPage = lazy(() => import("./overview/OverviewPage").then((m) => ({ default: m.OverviewPage })));
const StockPage = lazy(() => import("./stock/StockPage").then((m) => ({ default: m.StockPage })));
const SettingsPage = lazy(() => import("./settings/SettingsPage").then((m) => ({ default: m.SettingsPage })));
const DataPage = lazy(() => import("./data/DataPage").then((m) => ({ default: m.DataPage })));
const DocsPage = lazy(() => import("./docs/DocsPage").then((m) => ({ default: m.DocsPage })));

const loading = <p className="py-6 text-text-muted">Loading…</p>;

export function LazyOverview({ symbol }: { symbol: string | undefined }) {
  return (
    <Suspense fallback={loading}>
      <OverviewPage symbol={symbol} />
    </Suspense>
  );
}

/** Keyed by symbol, so a new ticker starts with fresh tabs and range. */
export function LazyStock({ symbol }: { symbol: string }) {
  return (
    <Suspense fallback={loading}>
      <StockPage key={symbol} symbol={symbol} />
    </Suspense>
  );
}

export function LazySettings() {
  return (
    <Suspense fallback={loading}>
      <SettingsPage />
    </Suspense>
  );
}

export function LazyDocs({ article }: { article: string | undefined }) {
  return (
    <Suspense fallback={loading}>
      <DocsPage article={article} />
    </Suspense>
  );
}

export function LazyData({ tab }: { tab: DataTab }) {
  return (
    <Suspense fallback={loading}>
      <DataPage tab={tab} />
    </Suspense>
  );
}
