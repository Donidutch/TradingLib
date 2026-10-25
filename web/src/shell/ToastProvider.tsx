/* Toasts, ported from ui/js/components/toast.js: one at a time, rapid calls
 * queue instead of overwriting, and the backlog is capped at four. */
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { cn } from "../ui/lib/cn";
import { ToastContext, type ToastTone, type Toaster } from "./toast-context";

const DEFAULT_MS = 2600;
const ERROR_MS = 4200;
const MAX_QUEUED = 4;

interface Item {
  id: number;
  message: string;
  tone: ToastTone;
  ms: number;
}

const toneClass: Record<ToastTone, string> = {
  info: "border-border text-text",
  ok: "border-up/40 text-up",
  warn: "border-warn/40 text-warn",
  error: "border-down/40 text-down",
};

export function ToastProvider({ children }: { children: ReactNode }) {
  const [current, setCurrent] = useState<Item | null>(null);
  const queue = useRef<Item[]>([]);
  const nextId = useRef(0);

  const show = useCallback((message: string, tone: ToastTone = "info", ms?: number) => {
    if (!message) return;
    const item = { id: nextId.current++, message, tone, ms: ms ?? (tone === "error" ? ERROR_MS : DEFAULT_MS) };
    queue.current.push(item);
    while (queue.current.length > MAX_QUEUED) queue.current.shift();
    setCurrent((visible) => visible ?? queue.current.shift() ?? null);
  }, []);

  useEffect(() => {
    if (!current) return;
    const timer = setTimeout(() => setCurrent(queue.current.shift() ?? null), current.ms);
    return () => clearTimeout(timer);
  }, [current]);

  const toast = useMemo<Toaster>(
    () => ({
      show,
      ok: (message, ms) => show(message, "ok", ms),
      warn: (message, ms) => show(message, "warn", ms),
      error: (message, ms) => show(message, "error", ms),
    }),
    [show],
  );

  return (
    <ToastContext.Provider value={toast}>
      {children}
      <div role="status" aria-live="polite" className="pointer-events-none fixed bottom-4 right-4 z-[200]">
        {current && (
          <div
            key={current.id}
            data-tone={current.tone}
            className={cn(
              "animate-in max-w-sm rounded-[var(--ds-radius-card)] border bg-elevated px-3 py-2 shadow-lg",
              toneClass[current.tone],
            )}
          >
            {current.message}
          </div>
        )}
      </div>
    </ToastContext.Provider>
  );
}
