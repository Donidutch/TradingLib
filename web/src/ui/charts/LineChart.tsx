/* Line series on lightweight-charts: the Stock Research price chart (one
 * series, with a live point) and the margin progression (two series). */
import { ColorType, createChart, LineSeries, type IChartApi, type ISeriesApi, type UTCTimestamp } from "lightweight-charts";
import { useEffect, useRef } from "react";
import { chartColours, toSeconds } from "./theme";

export interface LinePoint {
  time: string;
  value: number;
}

export interface Line {
  label: string;
  points: readonly LinePoint[];
  colour: "accent" | "up" | "down";
}

const DAY = 86_400;

export function LineChart({
  lines,
  live,
  intraday = false,
  className,
}: {
  lines: readonly Line[];
  /** A live quote for the first line: appended on intraday charts, pinned to its day otherwise. */
  live?: { ts: string | null; price: number | null } | null;
  intraday?: boolean;
  className?: string;
}) {
  const box = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const series = useRef<ISeriesApi<"Line">[]>([]);
  const lastTime = useRef<number | null>(null);

  useEffect(() => {
    if (!box.current) return;
    const c = chartColours();
    const api = createChart(box.current, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: "transparent" }, textColor: c.text, fontSize: 10 },
      grid: { vertLines: { visible: false }, horzLines: { color: c.grid } },
      rightPriceScale: { borderColor: c.border },
      timeScale: { borderColor: c.border, timeVisible: intraday },
      crosshair: { vertLine: { color: c.crosshair }, horzLine: { color: c.crosshair } },
    });
    chart.current = api;
    return () => {
      api.remove();
      chart.current = null;
      series.current = [];
    };
  }, [intraday]);

  useEffect(() => {
    const api = chart.current;
    if (!api) return;
    const c = chartColours();
    for (const old of series.current) api.removeSeries(old);
    series.current = lines.map((line) => {
      const s = api.addSeries(LineSeries, { color: c[line.colour], lineWidth: 2, title: lines.length > 1 ? line.label : "" });
      const data = line.points
        .map((p) => ({ time: toSeconds(p.time), value: p.value }))
        .filter((p): p is { time: number; value: number } => p.time !== null)
        .map((p) => ({ time: p.time as UTCTimestamp, value: p.value }));
      s.setData(data);
      return s;
    });
    const first = lines[0]?.points.at(-1);
    lastTime.current = first ? toSeconds(first.time) : null;
    api.timeScale().fitContent();
  }, [lines, intraday]);

  useEffect(() => {
    const target = series.current[0];
    if (!target || !live?.ts || live.price == null || lastTime.current === null) return;
    const seconds = toSeconds(live.ts);
    if (seconds === null) return;
    const time = intraday
      ? Math.max(seconds, lastTime.current)
      : Math.max(Math.floor(seconds / DAY) * DAY, lastTime.current);
    target.update({ time: time as UTCTimestamp, value: live.price });
    if (intraday) lastTime.current = time;
  }, [live?.ts, live?.price, intraday]);

  return <div ref={box} className={className} data-testid="line-chart" />;
}
