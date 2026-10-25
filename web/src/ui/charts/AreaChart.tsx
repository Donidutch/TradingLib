/* An area chart of closes on lightweight-charts, with one live point that
 * moves the newest bar instead of growing an intraday tail on daily data. */
import { AreaSeries, ColorType, createChart, type IChartApi, type ISeriesApi, type UTCTimestamp } from "lightweight-charts";
import { useEffect, useRef } from "react";
import { chartColours, toSeconds } from "./theme";

export interface ChartPoint {
  time: string;
  close: number;
}

const DAY = 86_400;

export function AreaChart({
  points,
  live,
  className,
}: {
  points: readonly ChartPoint[];
  /** The latest quote; pinned to its own day, never before the newest bar. */
  live?: { ts: string | null; price: number | null } | null;
  className?: string;
}) {
  const box = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const series = useRef<ISeriesApi<"Area"> | null>(null);
  const lastTime = useRef<number | null>(null);

  useEffect(() => {
    if (!box.current) return;
    const c = chartColours();
    const api = createChart(box.current, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: "transparent" }, textColor: c.text, fontSize: 10 },
      grid: { vertLines: { visible: false }, horzLines: { color: c.grid } },
      rightPriceScale: { borderColor: c.border },
      timeScale: { borderColor: c.border },
      crosshair: { vertLine: { color: c.crosshair }, horzLine: { color: c.crosshair } },
    });
    series.current = api.addSeries(AreaSeries, {
      lineColor: c.up,
      topColor: `${c.up}38`,
      bottomColor: `${c.up}00`,
      lineWidth: 2,
    });
    chart.current = api;
    return () => {
      api.remove();
      chart.current = null;
      series.current = null;
    };
  }, []);

  useEffect(() => {
    const data = points
      .map((p) => ({ time: toSeconds(p.time), value: p.close }))
      .filter((p): p is { time: number; value: number } => p.time !== null)
      .map((p) => ({ time: p.time as UTCTimestamp, value: p.value }));
    series.current?.setData(data);
    lastTime.current = data.at(-1)?.time ?? null;
    chart.current?.timeScale().fitContent();
  }, [points]);

  useEffect(() => {
    if (!live?.ts || live.price == null || lastTime.current === null) return;
    const seconds = toSeconds(live.ts);
    if (seconds === null) return;
    const day = Math.max(Math.floor(seconds / DAY) * DAY, lastTime.current);
    series.current?.update({ time: day as UTCTimestamp, value: live.price });
  }, [live?.ts, live?.price]);

  return <div ref={box} className={className} data-testid="area-chart" />;
}
