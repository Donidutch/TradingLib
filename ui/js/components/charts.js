/* TradingLib — chart helpers (window.AlgoCharts).
 *
 * Thin wrappers over the two self-hosted vendor libraries (SPEC D2):
 *   - TradingView lightweight-charts v5 → candles, lines, equity curves
 *   - uPlot v1.6 → sparklines and small dense series
 *
 * Why wrappers: the vendor APIs differ (v5 replaced addCandlestickSeries with
 * addSeries(CandlestickSeries, …)), both need the same theme colours pulled
 * from the CSS tokens, and every chart in the app needs the same three things —
 * resize handling, a destroy() a page can call from unmount(), and tolerant
 * input normalisation for whatever shape the API returns.
 *
 * Every builder throws if its vendor library is missing, rather than silently
 * rendering nothing: a chart that cannot be drawn is a real failure.
 */
(function (global) {
  "use strict";

  /** Read a CSS custom property off :root, with a fallback. */
  function token(name, fallback) {
    const value = getComputedStyle(document.documentElement).getPropertyValue(name);
    const trimmed = (value || "").trim();
    return trimmed || fallback;
  }

  /** The palette every chart shares, resolved from the stylesheet tokens. */
  function theme() {
    return {
      background: token("--panel", "#0a0f1a"),
      text: token("--mid", "#8b96ad"),
      grid: token("--line", "#1c2739"),
      border: token("--line2", "#2a3550"),
      up: token("--grn", "#2ee6a8"),
      down: token("--red", "#ff5c5c"),
      accent: token("--blu", "#7ea6ff"),
      amber: token("--amb", "#ffb020"),
    };
  }

  /** Which vendor libraries actually loaded. */
  function available() {
    return {
      lightweightCharts: typeof global.LightweightCharts !== "undefined",
      uplot: typeof global.uPlot !== "undefined",
    };
  }

  /**
   * Normalise a timestamp to the seconds lightweight-charts expects.
   * Accepts ISO strings, Date objects, seconds and milliseconds.
   * @param {any} value
   * @returns {number|null}
   */
  function toSeconds(value) {
    if (value === undefined || value === null || value === "") return null;
    if (typeof value === "number") return value > 1e11 ? Math.floor(value / 1000) : Math.floor(value);
    if (value instanceof Date) {
      const ms = value.getTime();
      return isNaN(ms) ? null : Math.floor(ms / 1000);
    }
    const ms = Date.parse(String(value));
    return isNaN(ms) ? null : Math.floor(ms / 1000);
  }

  /**
   * Normalise OHLC bars from `/api/research/company/{symbol}/price`.
   * Points missing a time or a close are dropped, and the result is sorted and
   * de-duplicated — lightweight-charts throws on unordered data.
   * @param {Array<object>} points
   * @returns {Array<{time:number, open:number, high:number, low:number, close:number}>}
   */
  function toCandles(points) {
    const out = [];
    (points || []).forEach(function (point) {
      if (!point) return;
      const time = toSeconds(point.time !== undefined ? point.time : point.t);
      const close = Number(point.close !== undefined ? point.close : point.c);
      if (time === null || !isFinite(close)) return;
      const open = Number(point.open !== undefined ? point.open : close);
      const high = Number(point.high !== undefined ? point.high : close);
      const low = Number(point.low !== undefined ? point.low : close);
      out.push({
        time: time,
        open: isFinite(open) ? open : close,
        high: isFinite(high) ? high : close,
        low: isFinite(low) ? low : close,
        close: close,
      });
    });
    return dedupe(out);
  }

  /**
   * Normalise a value series to {time, value}.
   * @param {Array<object|Array>} points objects ({time,value}/{time,close}/
   *   {date,equity}) or [time, value] pairs.
   * @param {string} [valueKey] explicit value field
   */
  function toLine(points, valueKey) {
    const out = [];
    (points || []).forEach(function (point) {
      if (!point) return;
      let time;
      let value;
      if (Array.isArray(point)) {
        time = toSeconds(point[0]);
        value = Number(point[1]);
      } else {
        time = toSeconds(point.time !== undefined ? point.time
          : (point.date !== undefined ? point.date : point.t));
        const raw = valueKey ? point[valueKey]
          : (point.value !== undefined ? point.value
            : (point.equity !== undefined ? point.equity
              : (point.close !== undefined ? point.close : point.v)));
        value = Number(raw);
      }
      if (time === null || !isFinite(value)) return;
      out.push({ time: time, value: value });
    });
    return dedupe(out);
  }

  /** Sort ascending by time and keep the last point per timestamp. */
  function dedupe(points) {
    points.sort(function (a, b) { return a.time - b.time; });
    const out = [];
    for (let i = 0; i < points.length; i += 1) {
      if (out.length && out[out.length - 1].time === points[i].time) out[out.length - 1] = points[i];
      else out.push(points[i]);
    }
    return out;
  }

  /** Shared chart options for both lightweight-charts flavours. */
  function baseOptions(colors, options) {
    const opts = options || {};
    return {
      autoSize: true,
      layout: {
        background: { type: global.LightweightCharts.ColorType.Solid, color: "transparent" },
        textColor: colors.text,
        fontSize: 10,
        fontFamily: "ui-monospace, 'SF Mono', Menlo, Consolas, monospace",
        attributionLogo: false,
      },
      grid: {
        vertLines: { color: colors.grid },
        horzLines: { color: colors.grid },
      },
      rightPriceScale: { borderColor: colors.border, scaleMargins: { top: 0.12, bottom: 0.08 } },
      timeScale: {
        borderColor: colors.border,
        timeVisible: !!opts.intraday,
        secondsVisible: false,
        rightOffset: 2,
      },
      crosshair: { mode: global.LightweightCharts.CrosshairMode.Normal },
      handleScale: opts.interactive === false ? false : undefined,
      handleScroll: opts.interactive === false ? false : undefined,
    };
  }

  /**
   * A price chart (candles by default, line/area on request).
   *
   * @param {HTMLElement} container element to draw into; it is emptied first
   * @param {{type?: "candles"|"line"|"area", intraday?: boolean,
   *          interactive?: boolean, color?: string, height?: number,
   *          volume?: boolean}} [options]
   * @returns {{chart: object, series: object, setCandles: Function,
   *            setLine: Function, update: Function, setVolume: Function,
   *            fit: Function, destroy: Function}}
   */
  function priceChart(container, options) {
    if (!available().lightweightCharts) {
      throw new Error("lightweight-charts is not loaded (ui/vendor/lightweight-charts.standalone.production.js)");
    }
    if (!container) throw new Error("priceChart: no container element");
    const opts = options || {};
    const colors = theme();
    const LWC = global.LightweightCharts;

    container.innerHTML = "";
    if (opts.height) container.style.height = opts.height + "px";

    const chart = LWC.createChart(container, baseOptions(colors, opts));

    let series;
    const kind = opts.type || "candles";
    if (kind === "candles") {
      series = chart.addSeries(LWC.CandlestickSeries, {
        upColor: colors.up,
        downColor: colors.down,
        borderUpColor: colors.up,
        borderDownColor: colors.down,
        wickUpColor: colors.up,
        wickDownColor: colors.down,
      });
    } else if (kind === "area") {
      series = chart.addSeries(LWC.AreaSeries, {
        lineColor: opts.color || colors.up,
        topColor: (opts.color || colors.up) + "38",
        bottomColor: (opts.color || colors.up) + "00",
        lineWidth: 2,
      });
    } else {
      series = chart.addSeries(LWC.LineSeries, {
        color: opts.color || colors.accent,
        lineWidth: 2,
      });
    }

    let volumeSeries = null;
    if (opts.volume) {
      volumeSeries = chart.addSeries(LWC.HistogramSeries, {
        priceFormat: { type: "volume" },
        priceScaleId: "volume",
      });
      chart.priceScale("volume").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    }

    const controller = {
      chart: chart,
      series: series,
      volumeSeries: volumeSeries,

      /** Replace the OHLC data. @param {Array<object>} points */
      setCandles: function (points) {
        series.setData(toCandles(points));
        return controller;
      },
      /** Replace a value series. @param {Array<object>} points */
      setLine: function (points, valueKey) {
        series.setData(toLine(points, valueKey));
        return controller;
      },
      /** Push one live point (a tick or the newest bar). */
      update: function (point) {
        if (!point) return controller;
        if (kind === "candles" && point.open !== undefined) {
          const candles = toCandles([point]);
          if (candles.length) series.update(candles[0]);
        } else {
          const line = toLine([point]);
          if (line.length) series.update(line[0]);
        }
        return controller;
      },
      /** Replace the volume histogram (colour-coded by bar direction). */
      setVolume: function (points) {
        if (!volumeSeries) return controller;
        const bars = toCandles(points);
        volumeSeries.setData(bars.map(function (bar, index) {
          const previous = index > 0 ? bars[index - 1].close : bar.open;
          const source = (points[index] && points[index].volume) || 0;
          return {
            time: bar.time,
            value: Number(source) || 0,
            color: bar.close >= previous ? colors.up + "55" : colors.down + "55",
          };
        }));
        return controller;
      },
      /** Fit every point into view. */
      fit: function () {
        chart.timeScale().fitContent();
        return controller;
      },
      /** Remove the chart and free its resources. */
      destroy: function () {
        try {
          chart.remove();
        } catch (err) {
          console.warn("chart destroy failed", err);
        }
      },
    };
    return controller;
  }

  /**
   * An equity-curve chart: a line plus an optional dashed benchmark.
   * @param {HTMLElement} container
   * @param {{height?: number, color?: string, benchmarkColor?: string}} [options]
   */
  function equityChart(container, options) {
    const opts = options || {};
    const colors = theme();
    const controller = priceChart(container, {
      type: "line",
      color: opts.color || colors.up,
      height: opts.height,
      interactive: opts.interactive,
    });
    const LWC = global.LightweightCharts;
    let benchmark = null;

    /** Draw (or redraw) the benchmark series. */
    controller.setBenchmark = function (points, valueKey) {
      if (!benchmark) {
        benchmark = controller.chart.addSeries(LWC.LineSeries, {
          color: opts.benchmarkColor || colors.accent,
          lineWidth: 1,
          lineStyle: LWC.LineStyle.Dashed,
        });
      }
      benchmark.setData(toLine(points, valueKey));
      return controller;
    };
    return controller;
  }

  /**
   * A sparkline drawn with uPlot: no axes, no legend, one stroke plus fill.
   * @param {HTMLElement} container
   * @param {Array<number>|Array<object>} values numbers, or points carrying a
   *   time and a value (see toLine)
   * @param {{height?: number, width?: number, color?: string, fill?: boolean,
   *          valueKey?: string}} [options]
   * @returns {{plot: object, setData: Function, destroy: Function}}
   */
  function sparkline(container, values, options) {
    if (!available().uplot) throw new Error("uPlot is not loaded (ui/vendor/uPlot.iife.min.js)");
    if (!container) throw new Error("sparkline: no container element");
    const opts = options || {};
    const colors = theme();
    const stroke = opts.color || colors.up;

    /** @returns {[number[], number[]]} uPlot's column-major data */
    function toSeries(input) {
      const xs = [];
      const ys = [];
      if (!input || !input.length) return [xs, ys];
      if (typeof input[0] === "number") {
        input.forEach(function (value, index) {
          if (!isFinite(value)) return;
          xs.push(index);
          ys.push(Number(value));
        });
        return [xs, ys];
      }
      toLine(input, opts.valueKey).forEach(function (point) {
        xs.push(point.time);
        ys.push(point.value);
      });
      return [xs, ys];
    }

    container.innerHTML = "";
    const width = opts.width || container.clientWidth || 160;
    const height = opts.height || container.clientHeight || 36;

    const config = {
      width: width,
      height: height,
      padding: [2, 1, 2, 1],
      cursor: { show: false },
      legend: { show: false },
      axes: [{ show: false }, { show: false }],
      scales: { x: { time: typeof values[0] !== "number" } },
      series: [
        {},
        {
          stroke: stroke,
          width: 1.4,
          fill: opts.fill === false ? undefined : stroke + "22",
          points: { show: false },
        },
      ],
    };

    const plot = new global.uPlot(config, toSeries(values), container);

    // Keep the sparkline the width of its box; uPlot has no autoSize.
    let observer = null;
    if (typeof ResizeObserver !== "undefined") {
      observer = new ResizeObserver(function () {
        const w = container.clientWidth;
        if (w > 0) plot.setSize({ width: w, height: opts.height || height });
      });
      observer.observe(container);
    }

    return {
      plot: plot,
      /** Replace the series data. */
      setData: function (next) {
        plot.setData(toSeries(next));
      },
      destroy: function () {
        if (observer) observer.disconnect();
        try {
          plot.destroy();
        } catch (err) {
          console.warn("sparkline destroy failed", err);
        }
      },
    };
  }

  global.AlgoCharts = {
    available: available,
    theme: theme,
    toSeconds: toSeconds,
    toCandles: toCandles,
    toLine: toLine,
    priceChart: priceChart,
    equityChart: equityChart,
    sparkline: sparkline,
  };
})(window);
