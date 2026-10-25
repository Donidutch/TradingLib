import { useFilings, useFindings, useHypotheses, useMemory, useNews } from "../../api/hooks";
import { etDate, statusText } from "../../ui/format";
import { Badge } from "../../ui/primitives/badge";
import { Panel } from "../../ui/primitives/card";
import { ListRow, Note } from "./parts";
import { reason } from "./rules";

const FILINGS_SHOWN = 12;
const FINDINGS_SHOWN = 6;

export function ResearchTab({ symbol }: { symbol: string }) {
  const filings = useFilings(symbol);
  const news = useNews(symbol);
  const memory = useMemory(symbol);
  const hypotheses = useHypotheses(symbol);
  const findings = useFindings(symbol);

  const loading = filings.isPending || memory.isPending || hypotheses.isPending || findings.isPending;
  const failed = filings.error ?? memory.error ?? hypotheses.error ?? findings.error;
  if (failed) return <Note error>Research memory: {reason(failed)}</Note>;
  if (loading || !filings.data) return <Note>Loading research memory…</Note>;
  const filed = filings.data;

  const mem = memory.data;
  const counts = mem?.counts ?? {};
  const forms = Object.entries(filed.form_counts)
    .map(([form, n]) => `${n} × ${form}`)
    .join(" · ");
  const hypothesisCount = hypotheses.data?.hypotheses.length ?? 0;
  const sources = [
    `${filed.count} filings`,
    news.data ? `${news.data.count} news items` : "news unavailable",
    mem ? `${counts.knowledge ?? 0} knowledge entries` : null,
    forms || null,
  ]
    .filter(Boolean)
    .join(" · ");
  const findingRows = findings.data?.findings ?? [];
  const items = filed.items;

  return (
    <>
      <div className="mt-[11px] grid grid-cols-2 gap-[11px]">
        <Panel
          title="Research memory"
          accent={false}
          bodyClassName="p-0"
          actions={
            <Badge tone={mem ? "neutral" : "warn"}>
              {mem ? `${counts.hypotheses ?? 0} hypotheses · ${counts.findings ?? 0} findings` : `${hypothesisCount} hypotheses`}
            </Badge>
          }
        >
          {mem ? (
            <>
              <ListRow lead="Current thesis" detail={mem.thesis.text} title={mem.thesis.detail ?? mem.basis} />
              {mem.questions.length ? (
                mem.questions.map((q, i) => (
                  <ListRow key={q.text} lead={i === 0 ? "Open questions" : ""} detail={q.text} title={q.detail ?? q.kind} />
                ))
              ) : (
                <ListRow
                  lead="Open questions"
                  detail={counts.hypotheses ? `None — every recorded hypothesis naming ${symbol} is settled.` : "None recorded."}
                />
              )}
            </>
          ) : (
            <>
              <ListRow lead="Current thesis" detail="The research-memory endpoint is unavailable in this build." />
              <ListRow lead="Open questions" detail="—" />
            </>
          )}
          <ListRow lead="Sources" detail={sources} />
        </Panel>
        <Panel
          title="Linked agent findings"
          accent={false}
          bodyClassName="p-0"
          actions={<Badge tone={findings.data === null ? "warn" : "neutral"}>{findings.data === null ? "unavailable" : String(findingRows.length)}</Badge>}
        >
          {findings.data === null ? (
            <ListRow
              lead="Agent findings not served yet"
              detail="GET /api/agents/findings is not part of this build; nothing is shown rather than a placeholder finding."
            />
          ) : !findingRows.length ? (
            <Note>No agent has recorded a finding for {symbol} yet.</Note>
          ) : (
            findingRows.slice(0, FINDINGS_SHOWN).map((row) => (
              <ListRow key={row.id} lead={row.title || row.summary || row.id} detail={[row.agent_id ?? "", statusText(row.status)].filter(Boolean).join(" · ")} />
            ))
          )}
        </Panel>
      </div>
      <div className="mt-[11px]">
        <Panel title="SEC filings" accent={false} bodyClassName="p-0" actions={<Badge>{filed.source || "edgar"}</Badge>}>
          {items.length ? (
            <table className="w-full">
              <thead className="text-[9px] text-text-muted">
                <tr>
                  {["Filed", "Form", "Document", "Period"].map((h) => (
                    <th key={h} className="px-2.5 py-1.5 text-left font-normal">
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {items.slice(0, FILINGS_SHOWN).map((row) => (
                  <tr key={row.accession} className="border-t border-border">
                    <td className="px-2.5 py-1.5">{etDate(row.filed_at)}</td>
                    <td className="px-2.5 py-1.5">{row.form}</td>
                    <td className="px-2.5 py-1.5">
                      <a className="text-accent underline" href={row.url} target="_blank" rel="noopener noreferrer">
                        {row.title || row.accession}
                      </a>
                    </td>
                    <td className="px-2.5 py-1.5">{row.period_of_report ? etDate(row.period_of_report) : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <Note>{filed.error ?? `EDGAR returned no filings for ${symbol}.`}</Note>
          )}
        </Panel>
      </div>
    </>
  );
}
