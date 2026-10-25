import { useEstimates } from "../../api/hooks";
import { etDate, num, pct, toneFor, toneText, usd } from "../../ui/format";
import { cn } from "../../ui/lib/cn";
import { Badge } from "../../ui/primitives/badge";
import { Panel } from "../../ui/primitives/card";
import { Cell, Note } from "./parts";
import { reason } from "./rules";

function Signed({ value, title }: { value: number | null; title?: string }) {
  if (value === null) return <>—</>;
  const tone = toneFor(value);
  return (
    <span className={cn(tone && toneText[tone])} title={title}>
      {pct(value, { sign: true })}
    </span>
  );
}

export function EarningsTab({ symbol }: { symbol: string }) {
  const estimates = useEstimates(symbol);
  if (estimates.isPending) return <Note>Loading earnings history…</Note>;
  if (estimates.isError) return <Note error>Estimates: {reason(estimates.error)}</Note>;
  const history = [...estimates.data.history].reverse();
  const c = estimates.data.consensus;
  const revision = toneFor(c.revision_30d);

  return (
    <div className="mt-[11px] grid grid-cols-2 gap-[11px]">
      <Panel title="Earnings history" accent={false} bodyClassName="p-0" actions={<Badge>{history.length} quarters</Badge>}>
        {history.length ? (
          <table className="w-full">
            <thead className="text-[9px] text-text-muted">
              <tr>
                {["Quarter", "EPS est.", "EPS actual", "Surprise", "Reaction"].map((h, i) => (
                  <th key={h} className={cn("px-2.5 py-1.5 font-normal", i === 0 || i === 4 ? "text-left" : "text-right")}>
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {history.map((row) => (
                <tr key={row.date} className="border-t border-border">
                  <td className="px-2.5 py-1.5">{row.calendar_quarter ?? row.fiscal_period ?? "—"}</td>
                  <td className="mono px-2.5 py-1.5 text-right">{num(row.eps_estimate)}</td>
                  <td className="mono px-2.5 py-1.5 text-right">{num(row.eps_reported)}</td>
                  <td className="mono px-2.5 py-1.5 text-right">
                    <Signed value={row.surprise_pct} />
                  </td>
                  <td className="mono px-2.5 py-1.5">
                    <Signed value={row.reaction_pct} title={`close-to-close move on ${etDate(row.date)}`} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Note>The vendor published no earnings history for {symbol}.</Note>
        )}
      </Panel>
      <Panel title="Consensus estimates" accent={false} bodyClassName="p-0" actions={<Badge>current fiscal year</Badge>}>
        <dl className="grid grid-cols-4">
          <Cell label="FY REVENUE" value={usd(c.fy_revenue, { compact: true, decimals: 2 })} title={c.basis} />
          <Cell label="FY EPS" value={usd(c.fy_eps, { decimals: 2 })} title={`${c.analysts ?? "?"} analysts`} />
          <Cell
            label="30D REVISION"
            value={pct(c.revision_30d, { fraction: true, sign: true })}
            tone={revision}
            title="30-day change in the mean EPS estimate"
          />
          <Cell label="DISPERSION" value={pct(c.dispersion, { fraction: true })} title="(high − low) / |mean| EPS" />
        </dl>
      </Panel>
    </div>
  );
}
