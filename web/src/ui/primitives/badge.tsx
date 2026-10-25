import { cva, type VariantProps } from "class-variance-authority";
import { type HTMLAttributes, type ReactNode } from "react";
import { cn } from "../lib/cn";

const badge = cva(
  "inline-flex items-center gap-1 rounded-[4px] font-medium uppercase tracking-[0.06em] whitespace-nowrap",
  {
    variants: {
      tone: {
        neutral: "bg-surface-2 text-text-muted",
        accent: "bg-accent-dim text-accent",
        up: "bg-up-dim text-up",
        down: "bg-down-dim text-down",
        warn: "bg-warn-dim text-warn",
        info: "bg-stream-dim text-stream",
      },
      size: {
        sm: "px-1.5 py-0.5 text-[9px]",
        md: "px-2 py-0.5 text-[10px]",
      },
    },
    defaultVariants: { tone: "neutral", size: "md" },
  },
);

export interface BadgeProps
  extends HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badge> {}

export function Badge({ className, tone, size, ...props }: BadgeProps) {
  return <span className={cn(badge({ tone, size }), className)} {...props} />;
}

const dotTone = {
  neutral: "bg-text-subtle",
  accent: "bg-accent",
  up: "bg-up",
  down: "bg-down",
  warn: "bg-warn",
  info: "bg-stream",
} as const;

/** Small status dot, optionally pulsing for "live" state. */
export function StatusDot({
  tone = "neutral",
  pulse = false,
  className,
}: {
  tone?: keyof typeof dotTone;
  pulse?: boolean;
  className?: string;
}) {
  return (
    <span
      className={cn(
        "inline-block h-1.5 w-1.5 rounded-full shrink-0",
        dotTone[tone],
        pulse && "animate-pulse",
        className,
      )}
    />
  );
}

/** Dot + label, e.g. "● Connected". */
export function StatusBadge({
  tone = "neutral",
  pulse,
  children,
  className,
}: {
  tone?: keyof typeof dotTone;
  pulse?: boolean;
  children: ReactNode;
  className?: string;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 text-[11px] font-medium text-text-muted",
        className,
      )}
    >
      <StatusDot tone={tone} pulse={pulse} />
      {children}
    </span>
  );
}
