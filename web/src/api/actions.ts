/* The one way the app writes. An action runs its request, refreshes the
 * queries it changed, confirms in a toast, and on failure shows the server's
 * reason in a toast and on `action.error` for the form that asked. */
import { useMutation, useQueryClient, type QueryClient, type QueryKey } from "@tanstack/react-query";
import { useToast } from "../shell/toast-context";
import { errorReason } from "./errors";

export interface ActionOptions<A, R> {
  run: (args: A) => Promise<R>;
  /** Queries to refetch once the write has landed. */
  invalidate?: (result: R, args: A) => readonly QueryKey[];
  /** Cache writes that cannot wait for a refetch (the server's copy wins). */
  apply?: (client: QueryClient, result: R, args: A) => void;
  /** Success toast; null for none. */
  done?: (result: R, args: A) => string | null;
  /** What failed, e.g. "Could not add NVDA"; the server's reason follows. */
  failed: (args: A) => string;
}

export function useAction<A, R>(options: ActionOptions<A, R>) {
  const client = useQueryClient();
  const toast = useToast();
  return useMutation<R, Error, A>({
    mutationFn: options.run,
    onSuccess: async (result, args) => {
      options.apply?.(client, result, args);
      await Promise.all((options.invalidate?.(result, args) ?? []).map((queryKey) => client.invalidateQueries({ queryKey })));
      const message = options.done?.(result, args);
      if (message) toast.ok(message);
    },
    onError: (err, args) => toast.error(`${options.failed(args)}: ${errorReason(err)}`),
  });
}
