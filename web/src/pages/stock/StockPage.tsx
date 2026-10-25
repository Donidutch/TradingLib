/* Stock Research, ported from ui/js/pages/stock.js. Map: docs/UI_PORT.md, P4.
 * Any ticker loads; an unknown one shows the server's reason. Tabs load the
 * first time they are shown and share payloads through the query cache. */
import { useNavigate } from "@tanstack/react-router";
import { useEffect, useRef, useState } from "react";
import { useCompany } from "../../api/hooks";
import { TapeQuote } from "../../api/schemas/stream";
import { useChannel } from "../../live/context";
import { useToast } from "../../shell/toast-context";
import { Tabs } from "../../ui/primitives/tabs";
import { AltTab } from "./AltTab";
import { EarningsTab } from "./EarningsTab";
import { FinancialsTab } from "./FinancialsTab";
import { Head } from "./Head";
import { NewsTab } from "./NewsTab";
import { OverviewTab } from "./OverviewTab";
import { ResearchTab } from "./ResearchTab";
import { reason } from "./rules";
import { ValuationTab } from "./ValuationTab";

type TabKey = "overview" | "financials" | "earnings" | "valuation" | "news" | "alt" | "research";
const TABS: { value: TabKey; label: string }[] = [
  { value: "overview", label: "Overview" },
  { value: "financials", label: "Financials" },
  { value: "earnings", label: "Earnings & Estimates" },
  { value: "valuation", label: "Valuation & Peers" },
  { value: "news", label: "News & Sentiment" },
  { value: "alt", label: "Alternative Data" },
  { value: "research", label: "Research" },
];

/** Mounted per symbol (the route keys it), so a new ticker starts fresh. */
export function StockPage({ symbol }: { symbol: string }) {
  const navigate = useNavigate();
  const toast = useToast();
  const company = useCompany(symbol);
  const [tab, setTab] = useState<TabKey>("overview");
  const [opened, setOpened] = useState<ReadonlySet<TabKey>>(new Set(["overview"]));
  const [live, setLive] = useState<TapeQuote | undefined>(undefined);

  // The tape publisher polls exactly the symbols browsers ask for, so this
  // channel brings live quotes without touching the Overview's subscription.
  useChannel(`tape.${symbol}`, TapeQuote, (quote) => {
    if (quote.price != null) setLive(quote);
  });

  const announced = useRef(false);
  useEffect(() => {
    if (company.isError && !announced.current) {
      announced.current = true;
      toast.error(`${symbol}: ${reason(company.error)}`);
    }
  }, [company.isError, company.error, symbol, toast]);

  const show = (next: TabKey) => {
    setTab(next);
    setOpened((current) => new Set(current).add(next));
  };

  const body = (key: TabKey) => {
    switch (key) {
      case "overview":
        return <OverviewTab symbol={symbol} live={live} />;
      case "financials":
        return <FinancialsTab symbol={symbol} />;
      case "earnings":
        return <EarningsTab symbol={symbol} />;
      case "valuation":
        return <ValuationTab symbol={symbol} />;
      case "news":
        return <NewsTab symbol={symbol} />;
      case "alt":
        return <AltTab symbol={symbol} />;
      case "research":
        return <ResearchTab symbol={symbol} />;
    }
  };

  return (
    <div>
      <Head symbol={symbol} live={live} onSubmit={(next) => void navigate({ to: "/stock/$symbol", params: { symbol: next } })} />
      <Tabs tabs={TABS} value={tab} onChange={show} className="mt-2" />
      {TABS.filter((t) => opened.has(t.value)).map((t) => (
        <div key={t.value} role="tabpanel" aria-label={t.label} hidden={t.value !== tab}>
          {body(t.value)}
        </div>
      ))}
    </div>
  );
}
