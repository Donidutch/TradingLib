import { type HTMLAttributes } from "react";
import { cn } from "../lib/cn";

/** Shimmering placeholder. Size it with width/height utilities. */
export function Skeleton({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        "rounded-[5px] bg-surface-2 animate-pulse motion-reduce:animate-none",
        className,
      )}
      {...props}
    />
  );
}

export function SkeletonText({ lines = 3, className }: { lines?: number; className?: string }) {
  return (
    <div className={cn("flex flex-col gap-2", className)}>
      {Array.from({ length: lines }).map((_, i) => (
        <Skeleton
          key={i}
          className="h-3"
          style={{ width: `${[92, 78, 64, 85, 70][i % 5]}%` }}
        />
      ))}
    </div>
  );
}

export function SkeletonCard({ className }: { className?: string }) {
  return (
    <div className={cn("bg-surface border border-border rounded-[8px] p-3.5 flex flex-col gap-2", className)}>
      <Skeleton className="h-2.5 w-1/3" />
      <Skeleton className="h-5 w-2/3" />
      <Skeleton className="h-2.5 w-1/4" />
    </div>
  );
}
