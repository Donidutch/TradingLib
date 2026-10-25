import { type ReactNode } from "react";
import { cn } from "../lib/cn";

/**
 * Lightweight CSS-only tooltip (hover + focus), no portal. Wrap a trigger and
 * pass the label via `content`. For dense terminals, keep labels short.
 */
export function Tooltip({
  content,
  side = "top",
  children,
  className,
}: {
  content: ReactNode;
  side?: "top" | "bottom";
  children: ReactNode;
  className?: string;
}) {
  return (
    <span className={cn("relative inline-flex group", className)}>
      {children}
      <span
        role="tooltip"
        className={cn(
          "pointer-events-none absolute left-1/2 -translate-x-1/2 z-50 whitespace-nowrap",
          "rounded-[6px] border border-border bg-elevated px-2 py-1 text-[11px] text-text shadow-lg",
          "opacity-0 transition-opacity duration-100 group-hover:opacity-100 group-focus-within:opacity-100",
          side === "top" ? "bottom-full mb-1.5" : "top-full mt-1.5",
        )}
      >
        {content}
      </span>
    </span>
  );
}
