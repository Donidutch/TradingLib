/* Panel 9: News, Earnings calendar, FRED macro and Google Trends. A tab loads
 * the first time it is shown; the page re-keys this panel on scope change. */
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useApi } from "../../api/context";
import { keys, useMacro } from "../../api/hooks";
import type { Estimates } from "../../api/schemas/research";
import { ago, etDate, num, usd } from "../../ui/format";
import { cn } from "../../ui/lib/cn";
import { Badge } from "../../ui/primitives/badge";
import { Tabs } from "../../ui/primitives/tabs";
import { sessionTiming } from "./names";

export const NEWS_SYMBOL = "SPY";
export const EARNINGS_SYMBOLS = 6;
export const TRENDS_SYMBOLS = 4;

type TabKey = "news" | "earnings" | "fred" | "trends";
const TABS: { value: TabKey; label: string }[] = [
  { value: "news", label: "News" },
  { value: "earnings", label: "Earnings calendar" },
  { value: "fred", label: "FRED macro" },
  { value: "trends", label: "Google Trends" },
];

const note = "px-2.5 py-2.5";
const why = (err: unknown) => (err instanceof Error ? err.message : String(err));

/** Run `fn` over `items` with at most `limit` in flight. */
async function mapLimited<T, R>(items: readonly T[], limit: number, fn: (item: T) => Promise<R>): Promise<R[]> {
  const out: R[] = new Array(items.length);
  let cursor = 0;
  const worker = async () => {
    for (let i = cursor++; i < items.length; i = cursor++) out[i] = await fn(items[i] as T);
  };
  await Promise.all(Array.from({ length: Math.max(1, Math.min(limit, items.length)) }, worker));
  return out;
}

function Cell({ label, value, title, tone }: { label: string; value: string; title?: string; tone?: "up" | "down" }) {
  return (
    <div className="border-t border-border p-2.5">
      <dt className="text-[9px] text-text-muted">{label}</dt>
      <dd className={cn("mt-1 font-bold", tone === "up" && "text-up", tone === "down" && "text-down")} title={title}>
        {value}
      </dd>
    </div>
  );
}

function NewsTab() {
  const api = useApi();
  const news = useQuery({
    queryKey: keys.research(NEWS_SYMBOL, "news", "9"),
    queryFn: ({ signal }) => api.research.news(NEWS_SYMBOL, { limit: 9 }, signal),
  });
  if (news.isPending) return <p className={note}>Loading market news…</p>;
  if (news.isError) return <p className={cn(note, "text-down")}>Market news unavailable: {news.error.message}</p>;
  const [lead, ...rest] = news.data.items;
  if (!lead) return <p className={note}>The vendor returned no market news.</p>;
  const columns = [rest.slice(0, 2), rest.slice(2, 4)];
  return (
    <div className="grid grid-cols-[1.2fr_1fr_1fr]" title={`news for ${NEWS_SYMBOL} via ${news.data.source}; sentiment by ${news.data.scorer}`}>
      <a href={lead.url} target="_blank" rel="noopener noreferrer" className="border-r border-border p-[13px]" title={lead.url}>
        <span className="text-[7px] text-accent">TOP MARKET STORY</span>
        <h3 className="my-[7px] text-[13px] font-semibold leading-[1.45] text-text-strong">{lead.headline}</h3>
        <p className="text-[11px] text-text-muted">{lead.summary ?? ""}</p>
      </a>
      {columns.map((items, index) => (
        <div key={index}>
          {items.map((item) => (
            <a
              key={item.url}
              href={item.url}
              target="_blank"
              rel="noopener noreferrer"
              title={item.url}
              className="block border-b border-border px-2.5 py-2 hover:bg-surface-2"
            >
              <b className="block font-medium text-text">{item.headline}</b>
              <span
                className={cn(
                  "text-[10px] text-text-muted",
                  item.sentiment_label === "positive" && "text-up",
                  item.sentiment_label === "negative" && "text-down",
                )}
              >
                {[`${ago(item.published_at)} ago`, item.publisher ?? item.source, `${item.sentiment_label} ${num(item.sentiment)}`]
                  .filter(Boolean)
                  .join(" · ")}
              </span>
            </a>
          ))}
        </div>
      ))}
    </div>
  );
}

/** The current-quarter consensus revenue from an estimates payload. */
function revenueEstimate(payload: Estimates): number | null {
  const row = payload.revenue_estimate.find((r) => r.period === "0q");
  return typeof row?.avg === "number" ? row.avg : null;
}

function EarningsTab({ symbols }: { symbols: readonly string[] }) {
  const api = useApi();
  const picked = symbols.slice(0, EARNINGS_SYMBOLS);
  const calendar = useQuery({
    queryKey: ["overview", "earnings", ...picked],
    enabled: picked.length > 0,
    queryFn: async ({ signal }) => {
      const failures: string[] = [];
      const rows = await mapLimited(picked, 2, async (symbol) => {
        try {
          const payload = await api.research.estimates(symbol, signal);
          const next = payload.next_earnings;
          return next ? { symbol, date: next.date, eps: next.eps_estimate, revenue: revenueEstimate(payload) } : null;
        } catch (err) {
          failures.push(`${symbol}: ${why(err)}`);
          return null;
        }
      });
      return {
        rows: rows.filter((r) => r !== null).sort((a, b) => a.date.localeCompare(b.date)),
        failures,
      };
    },
  });
  if (!picked.length) return <p className={note}>No universe members to look up.</p>;
  if (calendar.isPending) return <p className={note}>Loading the next reporting dates for {picked.join(", ")}…</p>;
  if (calendar.isError) return <p className={cn(note, "text-down")}>{calendar.error.message}</p>;
  const { rows, failures } = calendar.data;
  return (
    <>
      {rows.length ? (
        <table className="w-full text-left">
          <thead className="text-[9px] text-text-muted">
            <tr>
              {["Date", "Company", "Timing", "Est. EPS", "Est. Revenue"].map((h, i) => (
                <th key={h} className={cn("px-2.5 py-1.5 font-normal", i > 2 && "text-right")}>
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const timing = sessionTiming(row.date);
              return (
                <tr key={row.symbol} className="border-t border-border">
                  <td className="px-2.5 py-1.5">{etDate(row.date)}</td>
                  <td className="px-2.5 py-1.5">{row.symbol}</td>
                  <td className="px-2.5 py-1.5" title={timing.title}>
                    {timing.text}
                  </td>
                  <td className="mono px-2.5 py-1.5 text-right">{usd(row.eps, { decimals: 2 })}</td>
                  <td className="mono px-2.5 py-1.5 text-right">{usd(row.revenue, { compact: true, decimals: 1 })}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      ) : (
        <p className={note}>The vendor published no upcoming reporting dates for these symbols.</p>
      )}
      {failures.length > 0 && <p className={cn(note, "text-warn")}>{failures.join(" · ")}</p>}
    </>
  );
}

function FredTab() {
  const macro = useMacro();
  if (macro.isPending) return <p className={note}>Loading FRED macro series…</p>;
  if (macro.isError) return <p className={note}>FRED macro unavailable: {macro.error.message}</p>;
  const notes: string[] = [];
  return (
    <>
      <dl className="grid grid-cols-4">
        {macro.data.series.map((row) => {
          const label = (row.label || row.series_id).toUpperCase();
          if (row.value === null) {
            notes.push(`${row.series_id}: ${row.reason ?? "no observations"}`);
            return <Cell key={row.series_id} label={label} value="—" title={row.reason ?? `${row.series_id}: no observations`} />;
          }
          const digits = Math.abs(row.value) >= 10 ? 1 : 2;
          if (row.stale && row.reason) notes.push(`${row.series_id}: ${row.reason}`);
          const bits = [`${row.series_id} · ${row.description ?? ""}`];
          if (row.as_of) bits.push(`as of ${row.as_of}`);
          if (row.change !== null && row.previous_as_of) bits.push(`previous ${num(row.previous, digits)} (${row.previous_as_of})`);
          if (row.change_1y !== null) bits.push(`year ago ${num(row.year_ago, digits)}`);
          bits.push(`source ${row.source} via ${row.origin ?? "store"}`);
          return (
            <Cell
              key={row.series_id}
              label={label}
              value={`${num(row.value, digits)}${row.unit === "percent" ? "%" : ""}`}
              tone={row.change === null ? undefined : row.change >= 0 ? "up" : "down"}
              title={bits.join(" · ")}
            />
          );
        })}
      </dl>
      <p className={note}>
        {macro.data.available} of {macro.data.total} series carry a current level
        {notes.length ? ` · ${notes.join(" · ")}` : ""}.
      </p>
    </>
  );
}

function TrendsTab({ symbols }: { symbols: readonly string[] }) {
  const api = useApi();
  const picked = symbols.slice(0, TRENDS_SYMBOLS);
  const trends = useQuery({
    queryKey: ["overview", "trends", ...picked],
    enabled: picked.length > 0,
    // Sequential: Google Trends rate-limits hard and a burst only yields 429s.
    queryFn: async ({ signal }) => {
      const cells: { symbol: string; label: string; value: string; title: string; failed: boolean }[] = [];
      for (const symbol of picked) {
        try {
          const payload = await api.research.altdata(symbol, signal);
          const interest = payload.signals.google_trends;
          const label = (interest?.keyword ?? symbol).toUpperCase();
          cells.push(
            interest?.value == null
              ? { symbol, label, value: "—", title: interest?.reason ?? "no Google Trends value", failed: false }
              : {
                  symbol,
                  label,
                  value: num(interest.value, 0),
                  title: [interest.unit, interest.geo, interest.timeframe].filter(Boolean).join(" · "),
                  failed: false,
                },
          );
        } catch (err) {
          cells.push({ symbol, label: symbol, value: "—", title: why(err), failed: true });
        }
      }
      return cells;
    },
  });
  if (!picked.length) return <p className={note}>No universe members to look up.</p>;
  if (trends.isPending) return <p className={note}>Loading Google Trends for {picked.join(", ")}…</p>;
  if (trends.isError) return <p className={cn(note, "text-down")}>{trends.error.message}</p>;
  const failures = trends.data.filter((c) => c.failed);
  return (
    <>
      <dl className="grid grid-cols-4">
        {trends.data.map((cell) => (
          <Cell key={cell.symbol} label={cell.label} value={cell.value} title={cell.title} />
        ))}
      </dl>
      {failures.length > 0 && (
        <p className={cn(note, "text-warn")}>{failures.map((c) => `${c.symbol}: ${c.title}`).join(" · ")}</p>
      )}
    </>
  );
}

export function IntelPanel({ symbols }: { symbols: readonly string[] }) {
  const [tab, setTab] = useState<TabKey>("news");
  const [opened, setOpened] = useState<ReadonlySet<TabKey>>(new Set(["news"]));
  const show = (next: TabKey) => {
    setTab(next);
    setOpened((current) => new Set(current).add(next));
  };
  const body = (key: TabKey) => {
    if (key === "news") return <NewsTab />;
    if (key === "earnings") return <EarningsTab symbols={symbols} />;
    if (key === "fred") return <FredTab />;
    return <TrendsTab symbols={symbols} />;
  };
  return (
    <section data-panel="" aria-label="Market intel" className="col-[2/4] overflow-hidden rounded-[var(--ds-radius-card)] border border-border bg-surface">
      <div className="flex items-center justify-between px-2.5 py-2">
        <Tabs tabs={TABS} value={tab} onChange={show} />
        <Badge tone="info">SCOPE: GENERAL MARKET</Badge>
      </div>
      {TABS.filter((t) => opened.has(t.value)).map((t) => (
        <div key={t.value} role="tabpanel" aria-label={t.label} hidden={t.value !== tab}>
          {body(t.value)}
        </div>
      ))}
    </section>
  );
}
