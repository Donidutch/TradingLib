import { forwardRef, type HTMLAttributes, type ReactNode } from "react";
import { cn } from "../lib/cn";

/** Surface card — the default bounded container. */
export const Card = forwardRef<HTMLDivElement, HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div
      ref={ref}
      className={cn(
        "bg-surface border border-border rounded-[var(--ds-radius-card)] overflow-hidden",
        className,
      )}
      {...props}
    />
  ),
);
Card.displayName = "Card";

/**
 * Panel — denser container with a titled header bar and an accent left
 * edge. The workhorse layout block; `data-panel` makes it markable in PICK mode.
 */
export function Panel({
  title,
  actions,
  icon,
  className,
  bodyClassName,
  accent = true,
  children,
}: {
  title?: ReactNode;
  actions?: ReactNode;
  icon?: ReactNode;
  className?: string;
  bodyClassName?: string;
  accent?: boolean;
  children: ReactNode;
}) {
  return (
    <section
      data-panel=""
      className={cn(
        "bg-surface border border-border rounded-[var(--ds-radius-card)] overflow-hidden flex flex-col min-h-0",
        className,
      )}
    >
      {title != null && (
        <header
          className={cn(
            "flex items-center justify-between gap-2 px-[var(--ds-card-pad-x)] py-2 bg-surface-3 border-b border-border-subtle",
            accent && "border-l-2 border-l-accent",
          )}
        >
          <div className="flex items-center gap-2 min-w-0">
            {icon && <span className="text-text-muted shrink-0">{icon}</span>}
            <h3 className="text-[11px] font-medium uppercase tracking-[0.14em] text-text-muted truncate">
              {title}
            </h3>
          </div>
          {actions && <div className="flex items-center gap-1.5 shrink-0">{actions}</div>}
        </header>
      )}
      <div className={cn("p-[var(--ds-panel-pad)] flex-1 min-h-0", bodyClassName)}>{children}</div>
    </section>
  );
}

export function CardHeader({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        "flex items-center justify-between gap-2 px-[var(--ds-card-pad-x)] py-[var(--ds-card-pad-y)] border-b border-border-subtle",
        className,
      )}
      {...props}
    />
  );
}

export function CardBody({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("p-[var(--ds-panel-pad)]", className)} {...props} />;
}
