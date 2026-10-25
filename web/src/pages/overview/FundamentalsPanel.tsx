import { useCompany } from "../../api/hooks";
import { metricText, toneText } from "../../ui/format";
import { cn } from "../../ui/lib/cn";
import { Badge } from "../../ui/primitives/badge";
import { Panel } from "../../ui/primitives/card";

const CELLS = [
  ["MARKET CAP", "market_cap", false],
  ["FWD P/E", "forward_pe", false],
  ["REVENUE GROWTH", "revenue_yoy", true],
  ["FCF MARGIN", "fcf_margin", true],
  ["ROIC", "return_on_invested_capital", false],
  ["NET CASH", "net_cash", false],
] as const;

/** Panel 6: derived metrics for the centre symbol, each with its basis. */
export function FundamentalsPanel({ symbol }: { symbol: string }) {
  const company = useCompany(symbol);
  const metrics = company.data?.metrics;
  return (
    <Panel
      title={`${symbol} Fundamentals`}
      accent={false}
      bodyClassName="p-0"
      actions={<Badge>TTM</Badge>}
    >
      <div className="flex items-end justify-between p-[11px]">
        <strong className="font-medium text-up">QUALITY SCORE</strong>
        <span title="no composite quality score is exposed by /api/research/company/{symbol}; the screener computes one per universe run (/api/screens)">
          —
        </span>
      </div>
      <dl className="grid grid-cols-4">
        {CELLS.map(([label, key, sign]) => {
          const out = metricText(metrics?.[key], { sign });
          return (
            <div key={key} className="border-t border-border p-2.5">
              <dt className="text-[9px] text-text-muted">{label}</dt>
              <dd className={cn("mt-1 font-bold", out.tone && toneText[out.tone])} title={out.title}>
                {out.text}
              </dd>
            </div>
          );
        })}
      </dl>
    </Panel>
  );
}
