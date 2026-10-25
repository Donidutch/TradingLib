/* Documentation, ported from ui/js/pages/docs.js: two panels of articles,
 * the chosen one below, in the URL. The version is read from the server. */
import { useNavigate } from "@tanstack/react-router";
import { useHealth } from "../../api/hooks";
import { cn } from "../../ui/lib/cn";
import { Panel } from "../../ui/primitives/card";
import { ARTICLES, type Article } from "./articles";

const GROUPS = ["Methodology", "Product guide"] as const;

function ArticleView({ article }: { article: Article }) {
  const health = useHealth();
  return (
    <article aria-label={article.title} className="mt-[11px] rounded-[var(--ds-radius-card)] border border-border bg-surface px-[18px] py-4 leading-[1.65]" data-panel="">
      <h2 className="mb-1 text-[15px] text-text-strong">{article.title}</h2>
      <p className="text-text">{article.lede}</p>
      {article.body.map((block, i) => {
        const [kind, content] = block;
        if (kind === "ul") {
          return (
            <ul key={i} className="my-2 ml-[18px] list-disc text-text-muted">
              {content.map((item) => (
                <li key={item} className="my-[5px]">
                  {item}
                </li>
              ))}
            </ul>
          );
        }
        if (kind === "h3") {
          return (
            <h3 key={i} className="mb-1.5 mt-4 text-[12px] uppercase tracking-[.04em] text-text">
              {content}
            </h3>
          );
        }
        return (
          <p key={i} className="my-2 text-text-muted">
            {content}
          </p>
        );
      })}
      <footer className="mt-[18px] border-t border-border pt-2.5 text-[11px] text-text-subtle">
        <span>TradingLib{health.data ? ` ${health.data.version}` : ""}</span> · {article.group} · this page describes what this
        build implements; anything not implemented is named as such where it appears.
      </footer>
    </article>
  );
}

export function DocsPage({ article: requested }: { article: string | undefined }) {
  const navigate = useNavigate();
  const fallback = ARTICLES[0] as Article;
  const current = ARTICLES.find((a) => a.id === requested) ?? fallback;
  return (
    <div>
      <header className="mb-3">
        <h1 className="text-[14px] tracking-[1.5px] text-text-strong">DOCUMENTATION</h1>
        <p className="text-text-muted">Research methodology and product guide</p>
      </header>
      <div className="grid grid-cols-2 gap-[11px]">
        {GROUPS.map((group) => (
          <Panel key={group} title={group} accent={false} bodyClassName="p-0">
            <ul>
              {ARTICLES.filter((a) => a.group === group).map((a) => (
                <li key={a.id}>
                  <button
                    type="button"
                    aria-current={a.id === current.id ? "true" : undefined}
                    onClick={() => void navigate({ to: "/docs/$article", params: { article: a.id } })}
                    className={cn(
                      "w-full border-t border-border px-2.5 py-2 text-left hover:bg-surface-2",
                      a.id === current.id && "bg-active shadow-[inset_2px_0_var(--ds-accent)]",
                    )}
                  >
                    <b className="block font-medium text-text-strong">{a.lead}</b>
                    <span className="text-[10px] text-text-muted">{a.detail}</span>
                  </button>
                </li>
              ))}
            </ul>
          </Panel>
        ))}
      </div>
      <ArticleView article={current} />
    </div>
  );
}
