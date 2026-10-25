import { ApiError } from "./client";

/**
 * The server's own reason for a failure, for showing to a person: FastAPI's
 * `detail` when it is a string, `detail.detail` for TradingLib's structured
 * errors, the validation messages for a 422, and the error message otherwise.
 */
export function errorReason(err: unknown): string {
  if (err instanceof ApiError && err.payload && typeof err.payload === "object") {
    const detail = (err.payload as { detail?: unknown }).detail;
    if (typeof detail === "string" && detail) return detail;
    if (detail && typeof detail === "object" && "detail" in detail && typeof detail.detail === "string") {
      return detail.detail;
    }
    if (Array.isArray(detail)) {
      const messages = detail.map((d: { msg?: unknown }) => (typeof d.msg === "string" ? d.msg : "")).filter(Boolean);
      if (messages.length) return messages.join("; ");
    }
  }
  return err instanceof Error ? err.message : String(err);
}
