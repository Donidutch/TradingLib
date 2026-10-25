/* Each page (and the chart library they share) loads when first opened, so
 * the shell paints without waiting for code it may not need. */
import { lazy, Suspense } from "react";

const OverviewPage = lazy(() => import("./overview/OverviewPage").then((m) => ({ default: m.OverviewPage })));
const StockPage = lazy(() => import("./stock/StockPage").then((m) => ({ default: m.StockPage })));

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
