import { useEffect, type ReactNode } from "react";
import { StreamContext } from "./context";
import type { LiveStream } from "./stream";

/** Provides the stream and keeps it connected while the app is mounted. */
export function StreamProvider({ stream, children }: { stream: LiveStream; children: ReactNode }) {
  useEffect(() => {
    stream.connect();
    return () => stream.close();
  }, [stream]);
  return <StreamContext.Provider value={stream}>{children}</StreamContext.Provider>;
}
