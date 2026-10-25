import { useValuation } from "../../api/hooks";
import { num, pct, toneFor, toneText } from "../../ui/format";
import { cn } from "../../ui/lib/cn";
import { Badge } from "../../ui/primitives/badge";
import { Panel } from "../../ui/primitives/card";
import { MetricCell, Note } from "./parts";
import { reason } from "./rules";

export function ValuationTab({ symbol }: { symbol: string }) {
  const valuation = useValuation(symbol);
  if (valuation.isPending) return <Note>Loading valuation and peers…</Note>;
  if (valuation.isError) return <Note error>Valuation: {reason(valuation.error)}</Note>;
  const { multiples, peers, peer_basis } = valuation.data;

  return (
    <div className="mt-[11px] grid grid-cols-2 gap-[11px]">
      <Panel title="Valuation snapshot" accent={false} bodyClassName="p-0" actions={<Badge>TTM</Badge>}>
        <dl className="grid grid-cols-4">
          <MetricCell label="EV / SALES" metric={multiples.ev_to_sales} />
          <MetricCell label="EV / EBITDA" metric={multiples.ev_to_ebitda} />
          <MetricCell label="FCF YIELD" metric={multiples.fcf_yield} />
          <MetricCell label="REVERSE DCF GROWTH" metric={multiples.reverse_dcf_growth} />
        </dl>
      </Panel>
      <Panel title="Peer comparison" accent={false} bodyClassName="p-0" actions={<Badge>{peers.length} names</Badge>}>
        {peers.length ? (
          <table className="w-full">
            <thead className="text-[9px] text-text-muted">
              <tr>
                {["Company", "Growth", "EV/Sales", "FCF margin"].map((h, i) => (
                  <th key={h} className={cn("px-2.5 py-1.5 font-normal", i ? "text-right" : "text-left")}>
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {peers.map((row) => {
                const growth = toneFor(row.revenue_yoy);
                return (
                  <tr key={row.symbol} className="border-t border-border" aria-current={row.is_subject ? "true" : undefined}>
                    <td className={cn("px-2.5 py-1.5", row.is_subject && "text-accent")} title={row.name ?? ""}>
                      {row.symbol}
                    </td>
                    <td className={cn("mono px-2.5 py-1.5 text-right", growth && toneText[growth])}>
                      {pct(row.revenue_yoy, { fraction: true, sign: true })}
                    </td>
                    <td className="mono px-2.5 py-1.5 text-right">{row.ev_to_sales === null ? "—" : `${num(row.ev_to_sales, 1)}×`}</td>
                    <td className="mono px-2.5 py-1.5 text-right">{pct(row.fcf_margin, { fraction: true })}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        ) : (
          <Note>No peers could be resolved from the configured universes.</Note>
        )}
        <Note>{peer_basis}</Note>
      </Panel>
    </div>
  );
}
