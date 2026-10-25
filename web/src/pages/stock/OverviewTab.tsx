/* The Overview tab: price & events, fundamental snapshot and research thesis
 * above; revenue & EBITDA, news and alternative signals below. */
import { useMemo, useState } from "react";
import { useAltData, useCompany, useHypotheses, useNews, usePrice, useThesis } from "../../api/hooks";
import type { TapeQuote } from "../../api/schemas/stream";
import { ago, num, statusText, usd } from "../../ui/format";
import { LineChart } from "../../ui/charts/LineChart";
import { cn } from "../../ui/lib/cn";
import { Badge } from "../../ui/primitives/badge";
import { Panel } from "../../ui/primitives/card";
import { InfoRow, ListRow, MetricCell, Note } from "./parts";
import { barHeights, reason, sentimentTone, signalText } from "./rules";
import { NewsRow } from "./NewsTab";

const RANGES = [
  ["1d", "1D"],
  ["5d", "5D"],
  ["1m", "1M"],
  ["3m", "3M"],
  ["6m", "6M"],
  ["1y", "1Y"],
  ["5y", "5Y"],
  ["max", "MAX"],
] as const;
const INTRADAY = new Set(["1d", "5d"]);

function PricePanel({ symbol, live }: { symbol: string; live: TapeQuote | undefined }) {
  const [range, setRange] = useState("1y");
  const price = usePrice(symbol, range);
  const points = useMemo(() => price.data?.points ?? [], [price.data]);
  // Stable between live ticks, so the chart only redraws when the bars change.
  const lines = useMemo(
    () => [{ label: symbol, colour: "up" as const, points: points.map((p) => ({ time: p.time, value: p.close })) }],
    [points, symbol],
  );
  const note = price.isError
    ? `price history (${range}): ${reason(price.error)}`
    : price.data && !points.length
      ? `no bars for ${symbol} over ${range}${price.data.error ? ` — ${price.data.error}` : ""}`
      : null;
  return (
    <Panel
      title="Price & events"
      accent={false}
      bodyClassName="p-0"
      actions={
        <span role="group" aria-label="Chart range" className="flex gap-1">
          {RANGES.map(([key, label]) => (
            <button key={key} type="button" aria-pressed={key === range} onClick={() => setRange(key)}>
              <Badge tone={key === range ? "info" : "neutral"} className="cursor-pointer">
                {label}
              </Badge>
            </button>
          ))}
        </span>
      }
    >
      <div className="relative h-[280px]">
        {points.length > 0 && (
          <LineChart className="absolute inset-[13px]" lines={lines} live={live} intraday={INTRADAY.has(range)} />
        )}
      </div>
      {note && (
        <p role="alert" className="border-t border-border px-2.5 py-2 text-down">
          {note}
        </p>
      )}
    </Panel>
  );
}

function SnapshotPanel({ symbol }: { symbol: string }) {
  const company = useCompany(symbol);
  const m = company.data?.metrics;
  return (
    <Panel title="Fundamental snapshot" accent={false} bodyClassName="p-0" actions={<Badge>TTM</Badge>}>
      {company.isError ? (
        <Note error>{reason(company.error)}</Note>
      ) : (
        <dl className="grid grid-cols-2">
          <MetricCell label="REVENUE" metric={m?.revenue_ttm} />
          <MetricCell label="YOY GROWTH" metric={m?.revenue_yoy} sign />
          <MetricCell label="GROSS MARGIN" metric={m?.gross_margin} />
          <MetricCell label="FCF MARGIN" metric={m?.fcf_margin} sign />
          <MetricCell label="EV / SALES" metric={m?.ev_to_sales} />
          <MetricCell label="NET CASH" metric={m?.net_cash} />
        </dl>
      )}
    </Panel>
  );
}

function ThesisPanel({ symbol }: { symbol: string }) {
  const hypotheses = useHypotheses(symbol);
  const thesis = useThesis(symbol);
  const failed = hypotheses.isError ? hypotheses.error : thesis.isError ? thesis.error : null;
  const rows = hypotheses.data?.hypotheses ?? [];
  const catalysts = thesis.data?.catalysts ?? [];
  const risks = thesis.data?.risks ?? [];
  const loading = hypotheses.isPending || thesis.isPending;
  const sources = rows.length + (catalysts.length || risks.length ? 1 : 0);
  const empty = thesis.data ? "nothing above the thresholds" : "not loaded";
  const lead = rows[0];

  const points = (label: string, tone: "up" | "down", items: typeof catalysts) => (
    <div className="border-t border-border px-2.5 py-2">
      <b className={cn("mr-2", tone === "up" ? "text-up" : "text-down")}>{label}</b>
      {items.length ? (
        items.map((item, i) => (
          <span key={item.label}>
            {i > 0 && " · "}
            <span title={item.basis}>{item.label}</span>
          </span>
        ))
      ) : (
        <span className="text-text-muted">{empty}</span>
      )}
    </div>
  );

  return (
    <Panel
      title="Research thesis"
      accent={false}
      bodyClassName="p-0"
      actions={<Badge tone={failed ? "down" : "neutral"}>{failed ? "error" : loading ? "—" : `${sources} ${sources === 1 ? "source" : "sources"}`}</Badge>}
    >
      {failed ? (
        <Note error>{reason(failed)}</Note>
      ) : loading ? (
        <Note>Loading recorded research…</Note>
      ) : (
        <>
          <div className="p-[11px]">
            <h3 className="mb-[7px] text-[11px] font-semibold text-text-strong">
              {lead ? lead.title || lead.statement : `No hypothesis recorded for ${symbol}`}
            </h3>
            <p className="text-[11px] leading-[1.55] text-text-muted">
              {lead
                ? lead.statement || lead.rationale || ""
                : "A written thesis appears here once a Strategy Lab hypothesis or an agent finding names this symbol. TradingLib does not write one on your behalf — the two rows below are measured from this company's own numbers."}
            </p>
          </div>
          {rows.slice(0, 3).map((row) => (
            <ListRow
              key={row.id}
              lead={row.title || row.id}
              detail={[statusText(row.status), row.owner ?? "", row.updated_at ? `${ago(row.updated_at)} ago` : ""].filter(Boolean).join(" · ")}
            />
          ))}
          {points("CATALYSTS", "up", catalysts)}
          {points("KEY RISKS", "down", risks)}
        </>
      )}
    </Panel>
  );
}

function BarsPanel({ symbol }: { symbol: string }) {
  const company = useCompany(symbol);
  const rows = (company.data?.revenue_ebitda ?? []).filter((r) => r.revenue !== null || r.ebitda !== null);
  const heights = barHeights(rows);
  const badge = company.isError ? "error" : rows.length ? `${rows[0]?.year}–${rows.at(-1)?.year}` : company.data ? "no data" : "—";
  return (
    <Panel title="Revenue & adjusted EBITDA" accent={false} bodyClassName="p-0" actions={<Badge title="annual revenue and EBITDA, vendor statements">{badge}</Badge>}>
      {company.isError ? (
        <Note error>No statements: {reason(company.error)}</Note>
      ) : company.data && !rows.length ? (
        <Note error>The vendor published no annual revenue or EBITDA for {symbol}.</Note>
      ) : (
        <div className="flex h-[170px] items-end gap-[18px] px-4 pb-[9px] pt-3" role="img" aria-label="Revenue and EBITDA by year">
          {rows.map((row, i) => (
            <div key={row.year} className="flex-1 text-center">
              <div className="flex h-[125px] items-end justify-center gap-1">
                {(["revenue", "ebitda"] as const).map((key) => {
                  const value = row[key];
                  const height = heights[i]?.[key];
                  return (
                    <i
                      key={key}
                      data-bar={key}
                      className={cn("w-4", value !== null && value < 0 ? "bg-down" : key === "revenue" ? "bg-accent" : "bg-up")}
                      style={{ height: `${(height ?? 0).toFixed(1)}%` }}
                      title={value === null ? `${key}: not reported` : `${key === "revenue" ? "Revenue" : "EBITDA"} ${usd(value, { compact: true })}`}
                    />
                  );
                })}
              </div>
              <span className="mt-[5px] block text-[9px] text-text-muted">{row.year}</span>
            </div>
          ))}
        </div>
      )}
    </Panel>
  );
}

function NewsBrief({ symbol }: { symbol: string }) {
  const news = useNews(symbol);
  const aggregate = news.data?.aggregate;
  const tone = sentimentTone(aggregate?.label);
  return (
    <Panel
      title="News & sentiment"
      accent={false}
      bodyClassName="p-0"
      actions={
        <Badge
          tone={news.isError ? "down" : tone ?? "neutral"}
          title={news.data ? `recency-weighted VADER compound ${num(aggregate?.recency_weighted)} over ${news.data.count} items` : undefined}
        >
          {news.isError ? "error" : aggregate?.label ?? "—"}
        </Badge>
      }
    >
      {news.isPending ? (
        <Note>Loading news…</Note>
      ) : news.isError ? (
        <Note error>{reason(news.error)}</Note>
      ) : !news.data.items.length ? (
        <Note error>The vendor returned no news for {symbol}.</Note>
      ) : (
        news.data.items.slice(0, 4).map((item) => <NewsRow key={item.url} item={item} />)
      )}
    </Panel>
  );
}

function AltBrief({ symbol }: { symbol: string }) {
  const alt = useAltData(symbol);
  const signals = alt.data?.signals ?? {};
  const rows = [
    ["Google Trends", signalText(signals.google_trends, { decimals: 0 })],
    ["App rank composite", signalText(signals.app_rank_composite, { arrow: true })],
    ["News sentiment", signalText(signals.news_sentiment, { decimals: 2, sign: true })],
  ] as const;
  return (
    <Panel
      title="Alternative signals"
      accent={false}
      bodyClassName="p-0"
      actions={<Badge tone={alt.isError ? "down" : "neutral"}>{alt.isError ? "error" : signals.google_trends?.timeframe ?? "—"}</Badge>}
    >
      {alt.isPending ? (
        <Note>Loading alternative signals…</Note>
      ) : alt.isError ? (
        <Note error>{reason(alt.error)}</Note>
      ) : (
        <dl>
          {rows.map(([label, out]) => (
            <InfoRow key={label} label={label} value={out.text} tone={out.tone} title={out.title} />
          ))}
        </dl>
      )}
    </Panel>
  );
}

export function OverviewTab({ symbol, live }: { symbol: string; live: TapeQuote | undefined }) {
  return (
    <>
      <div className="mt-[11px] grid grid-cols-[1.45fr_.72fr_.68fr] gap-[11px]">
        <PricePanel symbol={symbol} live={live} />
        <SnapshotPanel symbol={symbol} />
        <ThesisPanel symbol={symbol} />
      </div>
      <div className="mt-[11px] grid grid-cols-[1.2fr_.8fr_.8fr] gap-[11px]">
        <BarsPanel symbol={symbol} />
        <NewsBrief symbol={symbol} />
        <AltBrief symbol={symbol} />
      </div>
    </>
  );
}
