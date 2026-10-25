/* The shell's live wiring, ported from app.js: the `status` channel feeds the
 * datastrip, `notifications` become toasts, and losing or regaining the socket
 * or the REST API is announced and triggers a status refresh. */
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import { useReachability } from "../api/context";
import { keys, useStatus } from "../api/hooks";
import { Status } from "../api/schemas/status";
import { Notification } from "../api/schemas/stream";
import { useChannel, useStreamSnapshot } from "../live/context";
import { useToast } from "./toast-context";

/** REST fallback while the socket is down, and a slow re-poll while it is up. */
const POLL_DOWN_MS = 15_000;
const POLL_UP_MS = 60_000;

export function useShellLive() {
  const queryClient = useQueryClient();
  const toast = useToast();
  const stream = useStreamSnapshot();
  const apiOnline = useReachability();
  const status = useStatus(stream.state === "open" ? POLL_UP_MS : POLL_DOWN_MS);

  // The server republishes the same payload /api/status builds.
  useChannel("status", Status, (payload) => queryClient.setQueryData(keys.status, payload));

  useChannel("notifications", Notification, (note) => {
    const message = note.message ?? note.detail ?? note.title;
    if (!message) return;
    const level = (note.level ?? note.severity ?? "").toLowerCase();
    if (level === "error" || level === "critical") toast.error(message);
    else if (level === "warning" || level === "warn") toast.warn(message);
    else toast.show(message);
  });

  const wasOpen = useRef(false);
  const announcedLoss = useRef(false);
  useEffect(() => {
    if (stream.state === "open") {
      if (announcedLoss.current) toast.ok("Live connection restored");
      announcedLoss.current = false;
      if (wasOpen.current) void queryClient.invalidateQueries({ queryKey: keys.status });
      wasOpen.current = true;
    } else if (wasOpen.current && !announcedLoss.current && stream.state !== "connecting") {
      toast.error("Live connection lost — retrying");
      announcedLoss.current = true;
    }
  }, [stream.state, toast, queryClient]);

  // REST came back: the strip may be stale.
  const wasOffline = useRef(false);
  useEffect(() => {
    if (apiOnline === false) wasOffline.current = true;
    else if (apiOnline && wasOffline.current) {
      wasOffline.current = false;
      void queryClient.invalidateQueries({ queryKey: keys.status });
    }
  }, [apiOnline, queryClient]);

  return { status: status.data, stream, apiOnline };
}
