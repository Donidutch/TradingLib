import { useEffect, useId, type ReactNode } from "react";
import { X } from "lucide-react";
import { cn } from "../lib/cn";
import { IconButton } from "./button";

/**
 * Accessible modal dialog with backdrop, Escape-to-close, and scroll lock.
 * Render conditionally: {open && <Modal …/>} or pass `open`.
 */
export function Modal({
  open,
  onClose,
  title,
  description,
  footer,
  className,
  children,
}: {
  open: boolean;
  onClose: () => void;
  title?: ReactNode;
  description?: ReactNode;
  footer?: ReactNode;
  className?: string;
  children?: ReactNode;
}) {
  const titleId = useId();
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prev;
    };
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-[100] flex items-center justify-center p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby={title ? titleId : undefined}
    >
      <div
        className="absolute inset-0 bg-backdrop backdrop-blur-sm"
        onClick={onClose}
        aria-hidden
      />
      <div
        className={cn(
          "relative z-10 w-full max-w-md bg-elevated border border-border rounded-[12px] shadow-2xl",
          "flex flex-col max-h-[85vh] animate-in",
          className,
        )}
      >
        {(title || description) && (
          <div className="flex items-start justify-between gap-3 px-5 pt-4 pb-3 border-b border-border-subtle">
            <div className="min-w-0">
              {title && (
                <h2 id={titleId} className="text-sm font-medium text-text">
                  {title}
                </h2>
              )}
              {description && (
                <p className="mt-0.5 text-[12px] text-text-muted">{description}</p>
              )}
            </div>
            <IconButton aria-label="Close" onClick={onClose} size="sm">
              <X size={15} aria-hidden />
            </IconButton>
          </div>
        )}
        {children && <div className="px-5 py-4 overflow-auto">{children}</div>}
        {footer && (
          <div className="flex items-center justify-end gap-2 px-5 py-3 border-t border-border-subtle">
            {footer}
          </div>
        )}
      </div>
    </div>
  );
}
