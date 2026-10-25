/* Small pieces the lab's panels share: labelled metrics, key-value rows,
 * stage and status pills, entity links and the strategy's rule rows. */
import type { ReactNode } from "react";
import { cn } from "../../ui/lib/cn";
import { Badge } from "../../ui/primitives/badge";
import { toneText, type Tone } from "./rules";

const badgeTone = (tone: Tone) => (tone === "up" ? "up" : tone === "down" ? "down" : tone === "warn" ? "warn" : tone === "accent" ? "accent" : "neutral");

export function Pill({ tone, children, className }: { tone: Tone; children: ReactNode; className?: string }) {
  return (
    <Badge size="sm" tone={badgeTone(tone)} className={className}>
      {children}
    </Badge>
  );
}

export function Metric({ label, value, tone }: { label: string; value: string; tone?: Tone }) {
  return (
    <div>
      <small className="block text-[9px] tracking-[1px] text-text-subtle">{label}</small>
      <span className={cn("text-[16px]", toneText(tone) ?? "text-text-strong")}>{value}</span>
    </div>
  );
}

export function Kv({ k, children }: { k: ReactNode; children: ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-b border-border-subtle py-1 text-[11px] last:border-b-0">
      <span className="text-text-muted">{k}</span>
      <span className="text-right">{children}</span>
    </div>
  );
}

export function Section({ label, children, className, actions }: { label: ReactNode; children: ReactNode; className?: string; actions?: ReactNode }) {
  return (
    <section className={cn("mb-2.5 rounded-[4px] border border-border-subtle p-2.5", className)}>
      <div className="mb-1.5 flex items-center gap-2">
        <h4 className="text-[9px] tracking-[1px] text-text-subtle uppercase">{label}</h4>
        {actions && <div className="ml-auto flex items-center gap-1">{actions}</div>}
      </div>
      {children}
    </section>
  );
}

export const Foot = ({ children, className }: { children: ReactNode; className?: string }) => <p className={cn("text-[10px] text-text-muted", className)}>{children}</p>;

/** An entity reference that opens it. */
export function Ref({ children, onClick, label }: { children: ReactNode; onClick: () => void; label?: string }) {
  return (
    <button type="button" className="text-accent hover:underline" onClick={onClick} aria-label={label}>
      {children}
    </button>
  );
}

export function RuleRow({ parts }: { parts: [string, string, string, string] }) {
  return (
    <div className="mb-1 flex flex-wrap gap-1 text-[11px]">
      {parts.map((part, i) =>
        part ? (
          <span key={i} className={cn("rounded-[3px] px-1.5 py-0.5", i === 2 ? "text-text-muted" : "bg-surface-2 text-text-strong")}>
            {part}
          </span>
        ) : null,
      )}
    </div>
  );
}

export function EmptyBox({ headline, detail, action }: { headline: string; detail: string; action?: ReactNode }) {
  return (
    <div className="rounded-[4px] border border-dashed border-border-subtle p-4 text-center">
      <div className="mb-1.5 text-text-strong">{headline}</div>
      <Foot className="mb-3">{detail}</Foot>
      {action}
    </div>
  );
}
