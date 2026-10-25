import { type ReactNode } from "react";
import { AlertTriangle } from "lucide-react";
import { cn } from "../lib/cn";

/** Centered zero-data / empty placeholder with optional icon and action. */
export function EmptyState({
  icon,
  title,
  description,
  action,
  className,
}: {
  icon?: ReactNode;
  title: ReactNode;
  description?: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center text-center gap-2 py-10 px-6",
        className,
      )}
    >
      {icon && (
        <div className="flex h-10 w-10 items-center justify-center rounded-full bg-accent-dim text-accent">
          {icon}
        </div>
      )}
      <p className="text-sm font-medium text-text">{title}</p>
      {description && (
        <p className="text-[12px] text-text-muted max-w-xs leading-relaxed">{description}</p>
      )}
      {action && <div className="mt-1">{action}</div>}
    </div>
  );
}

/** Error variant of EmptyState. */
export function ErrorState({
  title = "Something went wrong",
  description,
  action,
  className,
}: {
  title?: ReactNode;
  description?: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <EmptyState
      className={className}
      icon={<AlertTriangle size={18} className="text-down" aria-hidden />}
      title={title}
      description={description}
      action={action}
    />
  );
}
