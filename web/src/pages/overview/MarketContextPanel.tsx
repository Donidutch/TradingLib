/* Panel 7: regime, constituent breadth, tape advance/decline and VIX. Each
 * row is computed from real closes or quotes, or says why it cannot be. */
import { useBreadth, usePrice } from "../../api/hooks";
import { cn } from "../../ui/lib/cn";
import { Badge } from "../../ui/primitives/badge";
import { Panel } from "../../ui/primitives/card";
import { advanceDecline, BREADTH_UNIVERSE, breadth200, REGIME_SYMBOL, REGIME_WINDOW, regime, vix, VIX_SYMBOL, type Row } from "./rows";
import type { Quotes } from "./useTape";

const why = (err: Error | null) => err?.message ?? "unknown error";

export function MarketContextPanel({ quotes }: { quotes: Quotes }) {
  const spy = usePrice(REGIME_SYMBOL, "2y");
  const sp500 = useBreadth(BREADTH_UNIVERSE, REGIME_WINDOW);
  const volatility = usePrice(VIX_SYMBOL, "1m");
  const pending = { text: "—", title: "loading" };

  const rows: [string, Row][] = [
    [
      "Regime",
      spy.isError
        ? { text: "—", title: `${REGIME_SYMBOL} price history unavailable: ${why(spy.error)}` }
        : spy.data
          ? regime(spy.data)
          : pending,
    ],
    [
      "S&P above 200D",
      sp500.isError
        ? { text: "—", title: `constituent breadth unavailable: ${why(sp500.error)}` }
        : sp500.data
          ? breadth200(sp500.data)
          : pending,
    ],
    ["Advance / decline", advanceDecline(quotes)],
    [
      "VIX",
      volatility.isError
        ? { text: "—", title: `${VIX_SYMBOL} unavailable: ${why(volatility.error)}` }
        : volatility.data
          ? vix(volatility.data)
          : pending,
    ],
  ];

  return (
    <Panel title="Market context" accent={false} bodyClassName="p-0" actions={<Badge>US equities</Badge>}>
      <dl>
        {rows.map(([label, row]) => (
          <div key={label} className="flex justify-between border-b border-border px-2.5 py-2 text-[10px] last:border-b-0">
            <dt>{label}</dt>
            <dd
              className={cn("font-bold", row.tone === "up" && "text-up", row.tone === "down" && "text-down")}
              title={row.title}
            >
              {row.text}
            </dd>
          </div>
        ))}
      </dl>
    </Panel>
  );
}
