import { forwardRef, type InputHTMLAttributes } from "react";
import { cn } from "../lib/cn";

export interface SwitchProps
  extends Omit<InputHTMLAttributes<HTMLInputElement>, "type" | "size"> {
  label?: string;
}

/** Accessible toggle switch built on a native checkbox. */
export const Switch = forwardRef<HTMLInputElement, SwitchProps>(
  ({ className, label, checked, ...props }, ref) => (
    <label className={cn("inline-flex items-center gap-2 cursor-pointer select-none", className)}>
      <span className="relative inline-flex">
        <input
          ref={ref}
          type="checkbox"
          role="switch"
          checked={checked}
          className="peer sr-only"
          {...props}
        />
        <span
          className={cn(
            "h-4 w-7 rounded-full bg-surface-2 border border-border transition-colors",
            "peer-checked:bg-accent peer-checked:border-accent",
            "peer-focus-visible:ring-2 peer-focus-visible:ring-accent/55",
          )}
        />
        <span
          className={cn(
            "absolute left-0.5 top-0.5 h-3 w-3 rounded-full bg-text-muted transition-transform",
            "peer-checked:translate-x-3 peer-checked:bg-accent-contrast",
          )}
        />
      </span>
      {label && <span className="text-[12px] text-text">{label}</span>}
    </label>
  ),
);
Switch.displayName = "Switch";
