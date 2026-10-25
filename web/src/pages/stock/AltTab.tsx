import { useAltData } from "../../api/hooks";
import { ago, num, pct, toneFor } from "../../ui/format";
import { cn } from "../../ui/lib/cn";
import { Badge } from "../../ui/primitives/badge";
import { Panel } from "../../ui/primitives/card";
import { InfoRow, Note } from "./parts";
import { reason, signalText, signalTitle, type SignalStyle } from "./rules";

export function AltTab({ symbol }: { symbol: string }) {
  const alt = useAltData(symbol);
  if (alt.isPending) return <Note>Loading alternative data…</Note>;
  if (alt.isError) return <Note error>Alternative data: {reason(alt.error)}</Note>;
  const { signals, freshness } = alt.data;
  const trends = signals.google_trends;
  const jobs = signals.job_postings_90d;
  const news = signalText(signals.news_sentiment, { decimals: 3, sign: true });
  const change = trends?.change_90d_pct ?? null;
  const changeTone = toneFor(change);

  const cards: [string, keyof typeof signals, SignalStyle][] = [
    [`GOOGLE TRENDS · ${symbol}`, "google_trends", { decimals: 0 }],
    ["APP RANK COMPOSITE", "app_rank_composite", { arrow: true }],
    ["WEB TRAFFIC YOY", "web_traffic_yoy", { percent: true, fraction: true }],
    // A hiring rate is a level, not good or bad on its own: no sign, no tone.
    ["JOB POSTINGS 90D", "job_postings_90d", { percent: true, level: true }],
  ];

  return (
    <>
      <div className="mb-[11px] mt-[11px] grid grid-cols-4 gap-[11px]">
        {cards.map(([label, key, style]) => {
          const out = signalText(signals[key], style);
          return (
            <section key={key} data-panel="" className="rounded-[var(--ds-radius-card)] border border-border bg-surface p-[13px]" title={signalTitle(signals[key]) || undefined}>
              <small className="mb-[7px] block text-[10px] text-text-muted">{label}</small>
              <b className={cn("text-[18px]", out.tone === "up" && "text-up", out.tone === "down" && "text-down")} title={out.title}>
                {out.text}
              </b>
            </section>
          );
        })}
      </div>
      <Panel title="Signal detail" accent={false} bodyClassName="p-0" actions={<Badge>{trends?.timeframe ?? "—"}</Badge>}>
        <dl>
          <InfoRow label="Google Trends keyword" value={trends?.keyword ?? "—"} title={`geo ${trends?.geo ?? "—"}`} />
          <InfoRow
            label="Trends 90d change"
            value={change === null ? "—" : pct(change, { sign: true })}
            tone={changeTone}
          />
          <InfoRow label="News sentiment" value={news.text} tone={news.tone} title={news.title} />
          <InfoRow
            label="Job board"
            value={jobs?.board ? `${jobs.board} · ${jobs.slug ?? ""} · ${num(jobs.open_roles, 0)} open roles` : "—"}
            title={jobs?.basis ?? jobs?.reason ?? ""}
          />
          <InfoRow
            label="Trends fetched"
            value={freshness.trends?.fetched_at ? `${ago(freshness.trends.fetched_at)} ago` : "—"}
            title={freshness.trends ? `source ${freshness.trends.source}` : "no freshness report"}
          />
        </dl>
      </Panel>
    </>
  );
}
