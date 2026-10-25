import { createContext, useContext } from "react";

export type ToastTone = "info" | "ok" | "warn" | "error";

export interface Toaster {
  show: (message: string, tone?: ToastTone, ms?: number) => void;
  ok: (message: string, ms?: number) => void;
  warn: (message: string, ms?: number) => void;
  error: (message: string, ms?: number) => void;
}

export const ToastContext = createContext<Toaster | null>(null);

export function useToast(): Toaster {
  const toast = useContext(ToastContext);
  if (!toast) throw new Error("useToast needs a <ToastProvider>");
  return toast;
}
