/* The page's live quotes: one `tape` subscription carrying the watchlist, the
 * centre symbol and the first scope symbols (the server caps a cycle at 30). */
import { useCallback, useState } from "react";
import { z } from "zod";
import { TapeNotice, TapeQuote } from "../../api/schemas/stream";
import { useChannel } from "../../live/context";

export const TAPE_SYMBOLS = 14;
const TAPE_CAP = 30;
const TapeFrame = z.union([TapeQuote, TapeNotice]);

export type Quotes = ReadonlyMap<string, TapeQuote>;

export function tapeSymbols(watchlist: readonly string[], centre: string | null, scope: readonly string[]): string[] {
  const out: string[] = [];
  for (const raw of [...watchlist, ...(centre ? [centre] : []), ...scope.slice(0, TAPE_SYMBOLS)]) {
    const symbol = raw.toUpperCase();
    if (symbol && !out.includes(symbol)) out.push(symbol);
  }
  return out.slice(0, TAPE_CAP);
}

export function useTape(symbols: readonly string[]) {
  const [quotes, setQuotes] = useState<Quotes>(new Map());
  const [notice, setNotice] = useState<TapeNotice | null>(null);

  const onFrame = useCallback((payload: z.output<typeof TapeFrame>, channel: string) => {
    if (channel === "tape") {
      if ("event" in payload && typeof payload.event === "string") setNotice(payload as TapeNotice);
      return;
    }
    const parsed = TapeQuote.safeParse(payload);
    if (!parsed.success) return;
    const symbol = channel.includes(".") ? (channel.split(".")[1] ?? parsed.data.symbol) : parsed.data.symbol;
    setQuotes((current) => new Map(current).set(symbol, parsed.data));
  }, []);

  useChannel("tape", TapeFrame, onFrame, { symbols: [...symbols] });
  return { quotes, notice };
}
