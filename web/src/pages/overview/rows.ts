/* The Overview's computed rows, kept apart from the components so each rule
 * is tested on its own: market context (panel 7) and inbox links (panel 8). */
import type { PriceHistory } from "../../api/schemas/research";
import type { Breadth } from "../../api/schemas/universes";
import { num, pct } from "../../ui/format";
import type { Quotes } from "./useTape";

export const REGIME_SYMBOL = "SPY";
export const REGIME_WINDOW = 200;
export const BREADTH_UNIVERSE = "sp500";
export const VIX_SYMBOL = "^VIX";

export interface Row {
  text: string;
  tone?: "up" | "down";
  title: string;
}

export function regime(history: PriceHistory): Row {
  const closes = history.points.map((p) => p.close).filter(Number.isFinite);
  if (closes.length < REGIME_WINDOW) {
    return {
      text: "—",
      title: `only ${closes.length} ${REGIME_SYMBOL} closes available; ${REGIME_WINDOW} are needed for the moving average`,
    };
  }
  const window = closes.slice(-REGIME_WINDOW);
  const average = window.reduce((a, b) => a + b, 0) / window.length;
  const last = closes[closes.length - 1] ?? average;
  const on = last >= average;
  return {
    text: on ? "RISK-ON" : "RISK-OFF",
    tone: on ? "up" : "down",
    title: `${REGIME_SYMBOL} ${num(last)} vs its ${REGIME_WINDOW}-day average ${num(average)} (source ${history.source})`,
  };
}

export function breadth200(payload: Breadth): Row {
  if (payload.percent === null) return { text: "—", title: payload.reason ?? "no breadth reading" };
  const excluded = payload.requested - payload.covered;
  return {
    text: pct(payload.percent, { fraction: true, decimals: 0 }),
    tone: payload.percent >= 0.5 ? "up" : "down",
    title:
      `${payload.above} of ${payload.covered} ${payload.universe} constituents close above their own ${payload.window}-day average` +
      (excluded > 0
        ? ` (${excluded} of ${payload.requested} have fewer than ${payload.window} stored sessions and are excluded)`
        : "") +
      (payload.as_of ? ` · as of ${payload.as_of.slice(0, 10)}` : ""),
  };
}

export function advanceDecline(quotes: Quotes): Row {
  let advancing = 0;
  let declining = 0;
  for (const quote of quotes.values()) {
    if (quote.change_pct == null) continue;
    if (quote.change_pct > 0) advancing += 1;
    else if (quote.change_pct < 0) declining += 1;
  }
  const counted = advancing + declining;
  if (!counted) return { text: "—", title: "waiting for the first tape cycle" };
  return {
    text: declining === 0 ? `${advancing} / 0` : num(advancing / declining),
    tone: advancing >= declining ? "up" : "down",
    title: `${advancing} advancing, ${declining} declining of ${counted} quoted symbols on the live tape`,
  };
}

export function vix(history: PriceHistory): Row {
  const points = history.points;
  const last = points.at(-1);
  if (!last || !Number.isFinite(last.close)) return { text: "—", title: `the vendor returned no ${VIX_SYMBOL} closes` };
  const previous = points.at(-2)?.close;
  return {
    text: num(last.close, 1),
    tone: previous === undefined ? undefined : last.close <= previous ? "up" : "down",
    title:
      `CBOE volatility index close ${num(last.close)}` +
      (previous === undefined ? "" : ` · previous ${num(previous)}`) +
      ` · source ${history.source} · ${last.time.slice(0, 10)}`,
  };
}

/** "#agents/<route>?k=v", skipping empty context values like the old router. */
export function agentsHref(route: string, context: Record<string, unknown>): string {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(context)) {
    if (value !== null && value !== undefined && value !== "") query.set(key, String(value));
  }
  const qs = query.toString();
  return `/#agents/${encodeURIComponent(route || "inbox")}${qs ? `?${qs}` : ""}`;
}
