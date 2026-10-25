/* WebSocket payloads (tradinglib/api/ws.py, live.py). */
import { z } from "zod";
import { Quote, nstr } from "./common";

/** tape.<SYMBOL>: one quote; `delayed` is true for the Yahoo fallback. */
export const TapeQuote = Quote.extend({
  kind: z.string(),
  delayed: z.boolean(),
  feed: z.string(),
});

/** Bare `tape`: cycle reports and errors about the tape itself. */
export const TapeNotice = z.looseObject({
  event: z.string(),
  detail: nstr.optional(),
  retry_in_seconds: z.number().optional(),
  delayed: z.boolean().optional(),
  feed: z.string().optional(),
});

/** `notifications`: user-facing messages from agents, findings, pipelines. */
export const Notification = z.looseObject({
  kind: z.string(),
  message: z.string(),
  action: z.string().optional(),
});

export type TapeQuote = z.infer<typeof TapeQuote>;
export type TapeNotice = z.infer<typeof TapeNotice>;
export type Notification = z.infer<typeof Notification>;
