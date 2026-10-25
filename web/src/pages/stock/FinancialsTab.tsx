import { useMemo } from "react";
import { useFinancials } from "../../api/hooks";
import { LineChart } from "../../ui/charts/LineChart";
import { toneFor, toneText } from "../../ui/format";
import { cn } from "../../ui/lib/cn";
import { Badge } from "../../ui/primitives/badge";
import { Panel } from "../../ui/primitives/card";
import { Note } from "./parts";
import { millions, reason } from "./rules";

const ROWS = [
  ["Revenue", "revenue", false],
  ["Gross profit", "gross_profit", false],
  ["Adj. EBITDA", "ebitda", true],
  ["Free cash flow", "free_cash_flow", true],
] as const;

export function FinancialsTab({ symbol }: { symbol: string }) {
  const financials = useFinancials(symbol);
  const annual = useMemo(
    () => (financials.data?.annual ?? []).filter((row) => row.revenue !== null || row.ebitda !== null),
    [financials.data],
  );
  const lines = useMemo(() => {
    const series = (key: "gross_margin" | "ebitda_margin") =>
      annual.flatMap((row) => {
        const value = row[key];
        return value === null ? [] : [{ time: row.period_date || row.period, value: value * 100 }];
      });
    return [
      { label: "Gross margin", colour: "up" as const, points: series("gross_margin") },
      { label: "EBITDA margin", colour: "accent" as const, points: series("ebitda_margin") },
    ].filter((line) => line.points.length > 0);
  }, [annual]);

  if (financials.isPending) return <Note>Loading statements…</Note>;
  if (financials.isError) return <Note error>Financials: {reason(financials.error)}</Note>;
  const drawable = lines.some((line) => line.points.length >= 2);

  return (
    <div className="mt-[11px] grid grid-cols-2 gap-[11px]">
      <Panel title="Historical financials" accent={false} bodyClassName="p-0" actions={<Badge>{financials.data.currency ?? "USD"} millions</Badge>}>
        {annual.length ? (
          <table className="w-full">
            <thead className="text-[9px] text-text-muted">
              <tr>
                <th className="px-2.5 py-1.5 text-left font-normal">Metric</th>
                {annual.map((row) => (
                  <th key={row.period} className="px-2.5 py-1.5 text-right font-normal">
                    {(row.period_date || row.period).slice(0, 4)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {ROWS.map(([label, key, toned]) => (
                <tr key={key} className="border-t border-border">
                  <td className="px-2.5 py-1.5">{label}</td>
                  {annual.map((row) => {
                    const tone = toned ? toneFor(row[key]) : null;
                    return (
                      <td key={row.period} className={cn("mono px-2.5 py-1.5 text-right", tone && toneText[tone])}>
                        {millions(row[key])}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Note>The vendor published no annual statements for {symbol}.</Note>
        )}
      </Panel>
      <Panel title="Margin progression" accent={false} bodyClassName="p-0" actions={<Badge>gross · EBITDA</Badge>}>
        <div className="relative h-[260px]">
          {drawable ? (
            <LineChart className="absolute inset-[13px]" lines={lines} />
          ) : (
            <Note>Not enough reported margins to draw a progression.</Note>
          )}
        </div>
      </Panel>
    </div>
  );
}
