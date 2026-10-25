import { cva, type VariantProps } from "class-variance-authority";
import { forwardRef, type ButtonHTMLAttributes } from "react";
import { cn } from "../lib/cn";

export const buttonVariants = cva(
  "inline-flex items-center justify-center gap-2 whitespace-nowrap font-medium select-none transition-colors " +
    "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/55 focus-visible:ring-offset-1 focus-visible:ring-offset-bg " +
    "disabled:opacity-50 disabled:pointer-events-none",
  {
    variants: {
      variant: {
        primary: "bg-accent text-accent-contrast hover:bg-accent-bright",
        secondary:
          "bg-surface-2 text-text border border-border hover:border-border-strong hover:bg-elevated",
        ghost: "text-text-muted hover:text-text hover:bg-surface-2",
        accent: "bg-accent-dim text-accent border border-transparent hover:bg-accent/20",
        danger: "bg-down-dim text-down border border-down/30 hover:bg-down/20",
        success: "bg-up-dim text-up border border-up/30 hover:bg-up/20",
      },
      size: {
        sm: "h-[var(--ds-ctrl-h-sm)] px-2.5 text-[12px] rounded-[var(--ds-radius-ctrl)]",
        md: "h-[var(--ds-ctrl-h-md)] px-3 text-[13px] rounded-[var(--ds-radius-ctrl)]",
        lg: "h-[var(--ds-ctrl-h-lg)] px-4 text-sm rounded-[var(--ds-radius-ctrl)]",
        icon: "h-[var(--ds-ctrl-h-md)] w-[var(--ds-ctrl-h-md)] rounded-[var(--ds-radius-ctrl)]",
      },
    },
    defaultVariants: { variant: "secondary", size: "md" },
  },
);

export interface ButtonProps
  extends ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, type = "button", ...props }, ref) => (
    <button
      ref={ref}
      type={type}
      className={cn(buttonVariants({ variant, size }), className)}
      {...props}
    />
  ),
);
Button.displayName = "Button";

export interface IconButtonProps extends ButtonProps {
  "aria-label": string;
}

/** Square button for a single icon. `aria-label` is required. */
export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(
  ({ size = "icon", variant = "ghost", className, ...props }, ref) => (
    <Button ref={ref} size={size} variant={variant} className={className} {...props} />
  ),
);
IconButton.displayName = "IconButton";
