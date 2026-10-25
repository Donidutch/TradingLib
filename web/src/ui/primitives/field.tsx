import { cva, type VariantProps } from "class-variance-authority";
import {
  forwardRef,
  type InputHTMLAttributes,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
  type LabelHTMLAttributes,
  type ReactNode,
} from "react";
import { ChevronDown } from "lucide-react";
import { cn } from "../lib/cn";

const fieldBase = cva(
  "w-full bg-surface-3 text-text border border-border rounded-[var(--ds-radius-ctrl)] transition-colors placeholder:text-text-subtle " +
    "hover:border-border-strong focus:border-accent focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40 " +
    "disabled:opacity-50 disabled:pointer-events-none",
  {
    variants: {
      size: {
        sm: "h-[var(--ds-ctrl-h-sm)] px-2 text-[12px]",
        md: "h-[var(--ds-ctrl-h-md)] px-2.5 text-[13px]",
        lg: "h-[var(--ds-ctrl-h-lg)] px-3 text-sm",
      },
      invalid: { true: "border-down focus:border-down focus-visible:ring-down/40", false: "" },
    },
    defaultVariants: { size: "md", invalid: false },
  },
);

export interface InputProps
  extends Omit<InputHTMLAttributes<HTMLInputElement>, "size">,
    VariantProps<typeof fieldBase> {}

export const Input = forwardRef<HTMLInputElement, InputProps>(
  ({ className, size, invalid, ...props }, ref) => (
    <input ref={ref} className={cn(fieldBase({ size, invalid }), className)} {...props} />
  ),
);
Input.displayName = "Input";

export interface TextareaProps
  extends Omit<TextareaHTMLAttributes<HTMLTextAreaElement>, "size">,
    VariantProps<typeof fieldBase> {}

export const Textarea = forwardRef<HTMLTextAreaElement, TextareaProps>(
  ({ className, size, invalid, ...props }, ref) => (
    <textarea
      ref={ref}
      className={cn(fieldBase({ size, invalid }), "h-auto py-2 leading-relaxed resize-y", className)}
      {...props}
    />
  ),
);
Textarea.displayName = "Textarea";

export interface SelectProps
  extends Omit<SelectHTMLAttributes<HTMLSelectElement>, "size">,
    VariantProps<typeof fieldBase> {
  /** Sizes the select's box; it fills its container by default. */
  wrapperClassName?: string;
}

/** Native select with a custom chevron and consistent field styling. */
export const Select = forwardRef<HTMLSelectElement, SelectProps>(
  ({ className, size, invalid, wrapperClassName, children, ...props }, ref) => (
    <div className={cn("relative inline-flex w-full items-center", wrapperClassName)}>
      <select
        ref={ref}
        className={cn(fieldBase({ size, invalid }), "appearance-none pr-7 cursor-pointer", className)}
        {...props}
      >
        {children}
      </select>
      <ChevronDown
        size={14}
        aria-hidden
        className="pointer-events-none absolute right-2 text-text-muted"
      />
    </div>
  ),
);
Select.displayName = "Select";

/** Uppercase micro-label that pairs with form fields. */
export function Label({
  className,
  children,
  ...props
}: LabelHTMLAttributes<HTMLLabelElement> & { children: ReactNode }) {
  return (
    <label
      className={cn(
        "block text-[10px] font-medium uppercase tracking-[0.12em] text-text-muted",
        className,
      )}
      {...props}
    >
      {children}
    </label>
  );
}

/** Label + control + optional hint/error, stacked. */
export function Field({
  label,
  hint,
  error,
  htmlFor,
  className,
  children,
}: {
  label?: ReactNode;
  hint?: ReactNode;
  error?: ReactNode;
  htmlFor?: string;
  className?: string;
  children: ReactNode;
}) {
  return (
    <div className={cn("flex flex-col gap-1.5", className)}>
      {label && <Label htmlFor={htmlFor}>{label}</Label>}
      {children}
      {error ? (
        <p className="text-[11px] text-down">{error}</p>
      ) : hint ? (
        <p className="text-[11px] text-text-subtle">{hint}</p>
      ) : null}
    </div>
  );
}
