/* Panel 8: the agent inbox (SPEC §6 "agent inbox merge"). Items open in the
 * Agent Center, which still lives in the current console. */
import { useInbox } from "../../api/hooks";
import { openLegacy } from "../../shell/legacy";
import { ago } from "../../ui/format";
import { cn } from "../../ui/lib/cn";
import { Badge } from "../../ui/primitives/badge";
import { Panel } from "../../ui/primitives/card";
import { agentsHref } from "./rows";

const SHOWN = 6;

export function InboxPanel() {
  const inbox = useInbox();
  const items = inbox.data?.items ?? [];

  let badge: { text: string; tone: "neutral" | "up" | "down" | "warn"; title?: string } = { text: "—", tone: "neutral" };
  if (inbox.isError) badge = { text: "error", tone: "down" };
  else if (inbox.data === null)
    badge = { text: "unavailable", tone: "warn", title: "GET /api/agents/inbox answered 404 in this build" };
  else if (inbox.data) badge = { text: `${items.length} ${items.length === 1 ? "item" : "items"}`, tone: items.length ? "up" : "neutral" };

  return (
    <Panel
      title="Research inbox"
      accent={false}
      bodyClassName="p-0"
      actions={
        <Badge tone={badge.tone} title={badge.title}>
          {badge.text}
        </Badge>
      }
    >
      {inbox.isPending ? (
        <p className="px-2.5 py-2.5 text-text-muted">Loading the agent inbox…</p>
      ) : inbox.isError ? (
        <p className="px-2.5 py-2.5 text-down">{inbox.error.message}</p>
      ) : inbox.data === null ? (
        <div className="px-2.5 py-2.5">
          <b className="block font-medium text-text-strong">Agent inbox not served yet</b>
          <span className="text-text-muted">
            GET /api/agents/inbox is not part of this build; nothing is shown rather than a placeholder queue.
          </span>
        </div>
      ) : !items.length ? (
        <div className="px-2.5 py-2.5">
          <b className="block font-medium text-text-strong">Nothing needs a decision</b>
          <span className="text-text-muted">The agent inbox is empty.</span>
        </div>
      ) : (
        <ul>
          {items.slice(0, SHOWN).map((item) => {
            const urgency = item.urgency.toLowerCase();
            const detail = [item.type, item.symbol, item.source ?? "", item.created_at ? `${ago(item.created_at)} ago` : ""]
              .filter(Boolean)
              .join(" · ");
            return (
              <li key={item.id} className="border-t border-border first:border-t-0">
                <button
                  type="button"
                  title={item.reason}
                  onClick={() => openLegacy(agentsHref(item.route, item.context))}
                  className="w-full px-2.5 py-2 text-left hover:bg-surface-2"
                >
                  <b
                    className={cn(
                      "block font-medium text-text-strong",
                      urgency === "critical" && "text-down",
                      urgency === "high" && "text-warn",
                    )}
                  >
                    {item.title}
                  </b>
                  <span className="text-[10px] text-text-muted">{detail}</span>
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </Panel>
  );
}
