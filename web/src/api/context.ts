import { createContext, useContext, useSyncExternalStore } from "react";
import type { Api } from "./endpoints";

export const ApiContext = createContext<Api | null>(null);

export function useApi(): Api {
  const api = useContext(ApiContext);
  if (!api) throw new Error("useApi needs an <ApiProvider>");
  return api;
}

/** Whether the REST API answered the last request; null before the first. */
export function useReachability(): boolean | null {
  const { client } = useApi();
  return useSyncExternalStore(
    (notify) => client.onReachability(notify),
    () => client.online,
  );
}
