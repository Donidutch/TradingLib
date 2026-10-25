import { useNews } from "../../api/hooks";
import type { News } from "../../api/schemas/research";
import { ago, num } from "../../ui/format";
import { Badge } from "../../ui/primitives/badge";
import { Panel } from "../../ui/primitives/card";
import { cn } from "../../ui/lib/cn";
import { Note } from "./parts";
import { reason, sentimentTone } from "./rules";

/** One news item, toned by its sentiment label; opens the article. */
export function NewsRow({ item }: { item: News["items"][number] }) {
  const tone = sentimentTone(item.sentiment_label);
  const label = item.sentiment_label ? item.sentiment_label[0]?.toUpperCase() + item.sentiment_label.slice(1) : "";
  return (
    <a
      href={item.url}
      target="_blank"
      rel="noopener noreferrer"
      title={`${item.url}\ncompound ${num(item.sentiment, 3)}`}
      className="block border-t border-border px-2.5 py-2 first:border-t-0 hover:bg-surface-2"
    >
      <b className="block font-medium text-text-strong">{item.headline}</b>
      <span className={cn("text-[10px] text-text-muted", tone === "up" && "text-up", tone === "down" && "text-down")}>
        {[label, item.publisher ?? item.source, ago(item.published_at)].filter(Boolean).join(" · ")}
      </span>
    </a>
  );
}

export function NewsTab({ symbol }: { symbol: string }) {
  const news = useNews(symbol);
  if (news.isPending) return <Note>Loading news…</Note>;
  if (news.isError) return <Note error>News: {reason(news.error)}</Note>;
  const { items, aggregate, scorer, count } = news.data;
  const counts = aggregate.counts;
  return (
    <div className="mt-[11px]">
      <Panel
        title="Ticker news & sentiment"
        accent={false}
        bodyClassName="p-0"
        actions={<Badge tone={sentimentTone(aggregate.label) ?? "neutral"}>{`${symbol} · ${aggregate.label ?? "—"}`}</Badge>}
      >
        {items.length ? (
          <>
            <div className="border-b border-border px-2.5 py-2">
              <b className="block font-medium text-text-strong">
                {count} items scored by {scorer}
              </b>
              <span className="text-[10px] text-text-muted">
                {counts.positive ?? 0} positive · {counts.neutral ?? 0} neutral · {counts.negative ?? 0} negative · recency-weighted compound{" "}
                {num(aggregate.recency_weighted, 3)}
              </span>
            </div>
            {items.map((item) => (
              <NewsRow key={item.url} item={item} />
            ))}
          </>
        ) : (
          <Note>The vendor returned no news for {symbol}.</Note>
        )}
      </Panel>
    </div>
  );
}
