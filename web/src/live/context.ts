import { createContext, useContext, useEffect, useRef, useSyncExternalStore } from "react";
import type { z } from "zod";
import type { LiveStream, StreamSnapshot } from "./stream";

export const StreamContext = createContext<LiveStream | null>(null);

export function useStream(): LiveStream {
  const stream = useContext(StreamContext);
  if (!stream) throw new Error("useStream needs a <StreamProvider>");
  return stream;
}

/** Connection state for the shell: live, reconnecting (with why), closed. */
export function useStreamSnapshot(): StreamSnapshot {
  const stream = useStream();
  return useSyncExternalStore(
    (notify) => stream.onChange(notify),
    () => stream.snapshot(),
  );
}

/**
 * Subscribe to a channel for the component's lifetime. The handler may change
 * between renders without resubscribing; `params` changes do resubscribe.
 */
export function useChannel<S extends z.ZodType>(
  channel: string | null,
  schema: S,
  handler: (payload: z.output<S>, channel: string) => void,
  params?: Record<string, unknown>,
): void {
  const stream = useStream();
  const latest = useRef(handler);
  useEffect(() => {
    latest.current = handler;
  });
  const paramsKey = JSON.stringify(params ?? {});
  useEffect(() => {
    if (!channel) return;
    return stream.listen(
      channel,
      schema,
      (payload, from) => latest.current(payload, from),
      JSON.parse(paramsKey) as Record<string, unknown>,
    );
  }, [stream, channel, schema, paramsKey]);
}
