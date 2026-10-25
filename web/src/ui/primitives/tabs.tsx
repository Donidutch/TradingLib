import { cva } from "class-variance-authority";
import { type ReactNode } from "react";
import { cn } from "../lib/cn";

export interface TabItem<T extends string = string> {
  value: T;
  label: ReactNode;
  icon?: ReactNode;
}

const tab = cva(
  "inline-flex items-center gap-1.5 font-medium transition-colors cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50",
  {
    variants: {
      variant: {
        underline: "-mb-px border-b-2 px-3 py-2 text-[12px]",
        pill: "rounded-[var(--ds-pill-radius)] px-3 py-1.5 text-[12px]",
      },
      active: { true: "", false: "" },
    },
    compoundVariants: [
      { variant: "underline", active: true, className: "border-accent text-text" },
      {
        variant: "underline",
        active: false,
        className: "border-transparent text-text-muted hover:text-text",
      },
      { variant: "pill", active: true, className: "bg-accent-dim text-accent" },
      {
        variant: "pill",
        active: false,
        className: "text-text-muted hover:text-text hover:bg-surface-2",
      },
    ],
    defaultVariants: { variant: "underline", active: false },
  },
);

/**
 * Controlled tab strip with roving keyboard focus. `variant="pill"` for a
 * compact segmented control, `"underline"` for a section bar.
 */
export function Tabs<T extends string>({
  tabs,
  value,
  onChange,
  variant = "underline",
  className,
}: {
  tabs: TabItem<T>[];
  value: T;
  onChange: (value: T) => void;
  variant?: "underline" | "pill";
  className?: string;
}) {
  return (
    <div
      role="tablist"
      className={cn(
        "flex items-center gap-1",
        variant === "underline" && "border-b border-border-subtle gap-0",
        className,
      )}
    >
      {tabs.map((t) => {
        const active = t.value === value;
        return (
          <button
            key={t.value}
            type="button"
            role="tab"
            aria-selected={active}
            tabIndex={active ? 0 : -1}
            className={tab({ variant, active })}
            onClick={() => onChange(t.value)}
          >
            {t.icon && <span aria-hidden>{t.icon}</span>}
            {t.label}
          </button>
        );
      })}
    </div>
  );
}
