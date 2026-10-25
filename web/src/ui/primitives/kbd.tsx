import { type ReactNode } from "react";
import { cn } from "../lib/cn";

/** Keyboard key hint, e.g. <Kbd>K</Kbd>. */
export function Kbd({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <kbd
      className={cn(
        "inline-flex min-w-4 items-center justify-center rounded-[4px] border border-border bg-surface-2 px-1 py-0.5",
        "mono text-[10px] leading-none text-text-muted",
        className,
      )}
    >
      {children}
    </kbd>
  );
}
