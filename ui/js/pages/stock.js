/* TradingLib — Stock Research workspace.
 *
 * REFERENCE's Stock Research page, rebuilt against the real backend. The DOM
 * mirrors REFERENCE structure and classes exactly (SPEC D1a); the dead symbol
 * form is fixed — ANY ticker the vendor knows loads, and one it does not know
 * shows the server's own reason instead of silently keeping the old page.
 *
 *   head              /api/research/company/{symbol}  (profile, quote, freshness)
 *   Overview tab      price chart (lightweight-charts + range switcher, live
 *                     last price from the `tape` channel), derived fundamentals,
 *                     revenue/EBITDA bars, news+sentiment, alternative signals
 *   Financials        /financials  (normalised annual statements + margins)
 *   Earnings          /estimates   (history with real price reactions, consensus)
 *   Valuation         /valuation   (multiples + peer table)
 *   News & Sentiment  /news        (VADER-scored items)
 *   Alternative Data  /altdata     (Trends, news sentiment; unsourced signals
 *                                   render as "—" with the backend's reason)
 *   Research          /filings + /api/lab/hypotheses + /api/agents/findings
 *
 * Every metric with no value shows "—" and carries the backend's reason in its
 * title attribute. Nothing on this page is invented.
 *
 * See ui/js/router.js for the mount/unmount/route contract; the symbol travels
 * in ctx.sub ("#stock/NVDA").
 */
(function (global) {
  "use strict";

  const ui = global.AlgoUI;
  const el = ui.el;
  const fmt = ui.fmt;

  /** Chart ranges offered by the switcher (keys accepted by /price?range=). */
  const RANGES = [
    { key: "1d", label: "1D" },
    { key: "5d", label: "5D" },
    { key: "1m", label: "1M" },
    { key: "3m", label: "3M" },
    { key: "6m", label: "6M" },
    { key: "1y", label: "1Y" },
    { key: "5y", label: "5Y" },
    { key: "max", label: "MAX" },
  ];
  const DEFAULT_RANGE = "1y";
  /** Symbol used when the hash carries none. */
  const DEFAULT_SYMBOL = "GRAB";
  /** Ranges whose bars are intraday, so the chart shows a time axis. */
  const INTRADAY = { "1d": true, "5d": true };

  /** @type {object|null} */
  let s = null;

  // ---------------------------------------------------------------- helpers

  function cancelled(err) {
    return !!(err && (err.cancelled || err.name === "AbortError"));
  }

  function reason(err) {
    if (!err) return "unknown error";
    if (err.payload && err.payload.detail && err.payload.detail.detail) return err.payload.detail.detail;
    return err.message ? err.message : String(err);
  }

  /** Format one derived metric object from the research payloads. */
  function metricText(metric, options) {
    const opts = options || {};
    if (!metric) return { text: "—", tone: "", title: "not part of this payload" };
    const value = metric.value;
    if (value === null || value === undefined) {
      return { text: "—", tone: "", title: metric.reason || metric.basis || "no value" };
    }
    const unit = metric.unit || "num";
    let text;
    if (unit === "usd") text = fmt.usd(value, { compact: true, decimals: 2 });
    else if (unit === "ratio") text = fmt.pct(value, { fraction: true, sign: opts.sign });
    else if (unit === "x") text = fmt.num(value, 1) + "×";
    else text = fmt.num(value, opts.decimals === undefined ? 2 : opts.decimals);
    return { text: text, tone: opts.sign ? fmt.toneFor(value) : "", title: metric.basis || "" };
  }

  /** A `.metric` cell built from a metric object. */
  function metricCell(label, metric, options) {
    const out = metricText(metric, options);
    const value = el("b", { text: out.text, title: out.title });
    if (out.tone) value.classList.add(out.tone);
    return el("div.metric", el("small", { text: label }), value);
  }

  /** A `.metric` cell built from an already-formatted value. */
  function plainCell(label, text, options) {
    const opts = options || {};
    const value = el("b", { text: text === undefined || text === null ? "—" : String(text) });
    if (opts.tone) value.classList.add(opts.tone);
    if (opts.title) value.title = opts.title;
    return el("div.metric", el("small", { text: label }), value);
  }

  /** Number formatted in millions, the unit REFERENCE's tables use. */
  function millions(value) {
    if (value === null || value === undefined || !isFinite(Number(value))) return "—";
    return fmt.int(Number(value) / 1e6);
  }

  /** Wire a `.tabs` group against its `.tab-panel`s. */
  function wireTabs(tabs, scope, onActivate) {
    const group = tabs.dataset.tabs;
    tabs.addEventListener("click", function (event) {
      const button = event.target.closest(".tab");
      if (!button || !tabs.contains(button)) return;
      const key = button.dataset.tab;
      tabs.querySelectorAll(".tab").forEach(function (tab) {
        tab.classList.toggle("active", tab === button);
      });
      scope.querySelectorAll('[data-panel-group="' + group + '"]').forEach(function (panel) {
        panel.classList.toggle("active", panel.dataset.panel === key);
      });
      if (onActivate) onActivate(key);
    });
  }

  /**
   * Memoise one API call per symbol so the seven tabs share their payloads and
   * a tab revisit costs nothing.
   * @param {string} key cache key
   * @param {() => Promise<any>} fn
   */
  function once(key, fn) {
    const cache = s.cache;
    if (!cache[key]) cache[key] = fn();
    return cache[key];
  }

  /** Replace a panel body with a one-line error, keeping the page usable. */
  function failPanel(node, message) {
    ui.render(node, el("div.list-row", el("span.red", { text: message })));
  }

  // ------------------------------------------------------------------ build

  function build(root) {
    const n = s.nodes;

    // --- identity head
    n.symbolInput = el("input.input.stock-search", {
      id: "stockSymbolInput", "aria-label": "Research ticker",
      autocomplete: "off", spellcheck: "false", value: s.symbol,
    });
    n.companyName = el("span", { id: "stockCompanyName", text: "Loading…" });
    n.companyMeta = el("small", { id: "stockCompanyMeta", text: "" });
    n.price = el("span.stock-price", { id: "stockPrice", text: "—" });
    const form = el("form.stock-identity", {
      id: "stockSymbolForm",
      on: {
        submit: function (event) {
          event.preventDefault();
          const value = n.symbolInput.value.trim().toUpperCase();
          if (value) s.ctx.router.go("stock", { sub: value });
        },
      },
    }, n.symbolInput, el("button.btn", { type: "submit", text: "Load" }),
      el("div.stock-name", n.companyName, n.companyMeta), n.price);

    n.marketCap = el("b", { id: "stockMarketCap", text: "—" });
    n.nextEarnings = el("b", { id: "stockNextEarnings", text: "—" });
    n.freshness = el("b", { text: "—" });
    const head = el("div.stock-head", form, el("div.stock-metrics",
      el("div", el("small", { text: "MARKET CAP" }), n.marketCap),
      el("div", el("small", { text: "NEXT EARNINGS" }), n.nextEarnings),
      el("div", el("small", { text: "DATA FRESHNESS" }), n.freshness)));

    // --- tabs, exactly the seven REFERENCE offers
    const tabDefs = [
      ["overview", "Overview"], ["financials", "Financials"],
      ["earnings", "Earnings & Estimates"], ["valuation", "Valuation & Peers"],
      ["news", "News & Sentiment"], ["alt", "Alternative Data"], ["research", "Research"],
    ];
    const tabs = el("div.tabs", { dataset: { tabs: "stock-tabs" } },
      tabDefs.map(function (def, index) {
        return el("button.tab" + (index === 0 ? ".active" : ""), {
          type: "button", dataset: { tab: def[0] }, text: def[1],
        });
      }));

    n.panels = {};
    const panels = tabDefs.map(function (def, index) {
      const panel = el("div.tab-panel" + (index === 0 ? ".active" : ""), {
        dataset: { panelGroup: "stock-tabs", panel: def[0] },
      });
      n.panels[def[0]] = panel;
      return panel;
    });

    buildOverviewTab();
    ui.render(root, [head, tabs, panels]);
    wireTabs(tabs, root, function (key) { loadTab(key); });
  }

  /** The Overview tab: three panels above, three below (REFERENCE layout). */
  function buildOverviewTab() {
    const n = s.nodes;

    n.rangeButtons = RANGES.map(function (range) {
      const button = el("button.badge", {
        type: "button",
        dataset: { range: range.key },
        text: range.label,
        style: "cursor:pointer;margin-left:4px",
        "aria-pressed": range.key === s.range ? "true" : "false",
      });
      if (range.key === s.range) button.classList.add("cyan");
      button.addEventListener("click", function () { setRange(range.key); });
      return button;
    });
    n.chartMount = el("div", { style: "position:absolute;inset:13px" });
    n.chartNote = el("div.list-row", { style: "display:none" }, el("span.red", { text: "" }));
    const pricePanel = el("section.panel",
      el("div.panel-title", "Price & events ", el("span", n.rangeButtons)),
      el("div.chart-box", { style: "height:280px" }, el("div.chart-grid"), n.chartMount),
      n.chartNote);

    n.snapshot = el("div.metric-grid");
    const snapshotPanel = el("section.panel",
      el("div.panel-title", "Fundamental snapshot ", ui.badge("TTM")), n.snapshot);

    n.thesisBadge = ui.badge("—");
    n.thesisBody = el("div", el("div.list-row", el("span", { text: "Loading recorded research…" })));
    const thesisPanel = el("section.panel",
      el("div.panel-title", "Research thesis ", n.thesisBadge), n.thesisBody);

    n.bars = el("div.bar-chart");
    n.barsBadge = ui.badge("—");
    const barsPanel = el("section.panel",
      el("div.panel-title", "Revenue & adjusted EBITDA ", n.barsBadge), n.bars);

    n.newsBrief = el("div", el("div.list-row", el("span", { text: "Loading news…" })));
    n.newsBriefBadge = ui.badge("—");
    const newsPanel = el("section.panel",
      el("div.panel-title", "News & sentiment ", n.newsBriefBadge), n.newsBrief);

    n.altBrief = el("div", el("div.list-row", el("span", { text: "Loading alternative signals…" })));
    n.altBriefBadge = ui.badge("—");
    const altPanel = el("section.panel",
      el("div.panel-title", "Alternative signals ", n.altBriefBadge), n.altBrief);

    ui.render(s.nodes.panels.overview, [
      el("div.stock-layout", pricePanel, snapshotPanel, thesisPanel),
      el("div.stock-bottom", barsPanel, newsPanel, altPanel),
    ]);
  }

  // ------------------------------------------------------------- data loads

  /** Load everything the head and the currently visible tab need. */
  function loadSymbol() {
    s.cache = {};
    loadCompany();
    loadTab(s.tab);
  }

  function companyPayload() {
    return once("company", function () {
      return s.ctx.api.research.company(s.symbol, null, { signal: s.ctx.signal });
    });
  }

  /** Head, fundamental snapshot and the revenue/EBITDA bars. */
  async function loadCompany() {
    const n = s.nodes;
    let payload;
    try {
      payload = await companyPayload();
    } catch (err) {
      if (cancelled(err)) return;
      n.companyName.textContent = s.symbol + " could not be loaded";
      n.companyMeta.textContent = reason(err);
      n.companyMeta.className = "red";
      n.price.textContent = "—";
      n.marketCap.textContent = "—";
      n.nextEarnings.textContent = "—";
      n.freshness.textContent = "—";
      n.freshness.className = "";
      ui.render(n.snapshot, el("div.list-row", el("span.red", { text: reason(err) })));
      failPanel(n.bars, "No statements: " + reason(err));
      s.ctx.toast.error(s.symbol + ": " + reason(err));
      return;
    }
    if (!s) return;
    const profile = payload.profile || {};
    const quote = payload.quote || {};
    const metrics = payload.metrics || {};

    n.companyName.textContent = (profile.name || s.symbol).toUpperCase();
    n.companyMeta.className = "";
    n.companyMeta.textContent = profile.meta_line
      || [profile.exchange, profile.sector, profile.region].filter(Boolean).join(" · ")
      || "—";
    if (quote.price !== null && quote.price !== undefined) {
      n.price.textContent = fmt.usd(quote.price, { decimals: 2 });
      n.price.title = "quote " + (quote.ts || "") + " · " + (quote.source || "");
      n.price.classList.toggle("red", Number(quote.change_pct) < 0);
    }

    const cap = metricText(metrics.market_cap);
    n.marketCap.textContent = cap.text;
    n.marketCap.title = cap.title;

    const next = payload.next_earnings;
    if (next && next.days_away !== null && next.days_away !== undefined) {
      n.nextEarnings.textContent = next.days_away + (next.days_away === 1 ? " DAY" : " DAYS");
      n.nextEarnings.title = fmt.etDate(next.date) + " · " + (next.basis || next.source || "");
    } else {
      n.nextEarnings.textContent = "—";
      n.nextEarnings.title = "the vendor published no upcoming reporting date";
    }
    paintFreshness(payload.freshness);
    paintSnapshot(metrics);
    paintBars(payload.revenue_ebitda || []);
    (payload.warnings || []).forEach(function (warning) {
      console.warn("research warning for " + s.symbol + ":", warning);
    });
  }

  /** DATA FRESHNESS: the worst component age in the company payload. */
  function paintFreshness(freshness) {
    const n = s.nodes;
    const components = freshness ? Object.keys(freshness) : [];
    if (!components.length) {
      n.freshness.textContent = "—";
      n.freshness.className = "";
      n.freshness.title = "no freshness report";
      return;
    }
    let stale = 0;
    let oldest = null;
    components.forEach(function (key) {
      const report = freshness[key];
      if (!report) return;
      if (report.stale) stale += 1;
      if (!oldest || (report.age_seconds || 0) > (oldest.age_seconds || 0)) oldest = report;
    });
    n.freshness.textContent = stale ? "STALE" : "CURRENT";
    n.freshness.className = stale ? "yellow" : "green";
    n.freshness.title = components.map(function (key) {
      const report = freshness[key] || {};
      return key + " " + fmt.ago(report.fetched_at) + " (" + (report.source || "?") + ")";
    }).join(" · ");
  }

  /** The fundamental snapshot grid, straight off the derived metrics. */
  function paintSnapshot(metrics) {
    ui.render(s.nodes.snapshot, [
      metricCell("REVENUE", metrics.revenue_ttm),
      metricCell("YOY GROWTH", metrics.revenue_yoy, { sign: true }),
      metricCell("GROSS MARGIN", metrics.gross_margin),
      metricCell("FCF MARGIN", metrics.fcf_margin, { sign: true }),
      metricCell("EV / SALES", metrics.ev_to_sales),
      metricCell("NET CASH", metrics.net_cash),
    ]);
  }

  /** Revenue and EBITDA bars, scaled against the largest absolute figure. */
  function paintBars(rows) {
    const n = s.nodes;
    const usable = rows.filter(function (row) {
      return isFinite(Number(row.revenue)) || isFinite(Number(row.ebitda));
    });
    if (!usable.length) {
      n.barsBadge.textContent = "no data";
      failPanel(n.bars, "The vendor published no annual revenue or EBITDA for " + s.symbol + ".");
      return;
    }
    let max = 0;
    usable.forEach(function (row) {
      max = Math.max(max, Math.abs(Number(row.revenue) || 0), Math.abs(Number(row.ebitda) || 0));
    });
    n.barsBadge.textContent = usable[0].year + "–" + usable[usable.length - 1].year;
    n.barsBadge.title = "annual revenue and EBITDA, vendor statements";

    ui.clear(n.bars);
    usable.forEach(function (row) {
      const pair = el("div.bar-pair");
      [["rev", row.revenue], ["ebitda", row.ebitda]].forEach(function (entry) {
        const value = Number(entry[1]);
        const bar = el("i." + entry[0]);
        if (!isFinite(value)) {
          bar.style.height = "0%";
          bar.title = entry[0] + ": not reported";
        } else {
          bar.style.height = (max ? Math.max(1, Math.abs(value) / max * 100) : 0).toFixed(1) + "%";
          bar.title = (entry[0] === "rev" ? "Revenue " : "EBITDA ") + fmt.usd(value, { compact: true });
          if (value < 0) bar.style.background = "var(--red)";
        }
        pair.appendChild(bar);
      });
      n.bars.appendChild(el("div.bar-year", pair, el("label", { text: String(row.year) })));
    });
  }

  // ------------------------------------------------------------ price chart

  /** Switch the chart range and reload the bars. */
  function setRange(range) {
    if (s.range === range) return;
    s.range = range;
    s.nodes.rangeButtons.forEach(function (button) {
      const on = button.dataset.range === range;
      button.setAttribute("aria-pressed", on ? "true" : "false");
      button.classList.toggle("cyan", on);
    });
    loadPrice();
  }

  /** Draw the price chart for the selected range. */
  async function loadPrice() {
    const n = s.nodes;
    const range = s.range;
    let payload;
    try {
      payload = await s.ctx.api.research.price(s.symbol, range, { signal: s.ctx.signal });
    } catch (err) {
      if (cancelled(err)) return;
      showChartNote("price history (" + range + "): " + reason(err));
      return;
    }
    if (!s || s.range !== range) return;
    const points = payload.points || [];
    if (!points.length) {
      showChartNote("no bars for " + s.symbol + " over " + range
        + (payload.error ? " — " + payload.error : ""));
      return;
    }
    hideChartNote();
    if (s.chart) s.chart.destroy();
    s.chart = global.AlgoCharts.priceChart(n.chartMount, {
      type: "line", intraday: !!INTRADAY[range],
    });
    // REFERENCE paints the grid itself (.chart-grid behind the plot).
    s.chart.chart.applyOptions({
      grid: { vertLines: { visible: false }, horzLines: { visible: false } },
    });
    s.chart.setLine(points, "close").fit();
    s.lastPointTime = global.AlgoCharts.toSeconds(points[points.length - 1].time);
    s.intradayChart = !!INTRADAY[range];
  }

  function showChartNote(message) {
    s.nodes.chartNote.style.display = "";
    s.nodes.chartNote.firstChild.textContent = message;
  }

  function hideChartNote() {
    s.nodes.chartNote.style.display = "none";
  }

  /** A live quote for this symbol: repaint the price and nudge the chart. */
  function onQuote(payload) {
    if (!s || !payload || payload.price === null || payload.price === undefined) return;
    const n = s.nodes;
    n.price.textContent = fmt.usd(payload.price, { decimals: 2 });
    n.price.title = "live quote " + (payload.ts || "") + " · " + (payload.source || "")
      + (payload.delayed === false ? " (realtime)" : " (delayed)");
    n.price.classList.toggle("red", Number(payload.change_pct) < 0);
    if (!s.chart || s.lastPointTime === null) return;
    const seconds = global.AlgoCharts.toSeconds(payload.ts);
    if (seconds === null) return;
    // On a daily chart the live point is pinned to the quote's own day so the
    // series does not grow an intraday tail; intraday charts just append.
    const time = s.intradayChart
      ? Math.max(seconds, s.lastPointTime)
      : Math.max(Math.floor(seconds / 86400) * 86400, s.lastPointTime);
    s.chart.update({ time: time, value: payload.price });
    if (s.intradayChart) s.lastPointTime = time;
  }

  // -------------------------------------------------------------- tab loads

  /** Load a tab's payloads the first time it is shown. */
  function loadTab(key) {
    if (!s) return;
    s.tab = key;
    if (s.tabsLoaded[key]) return;
    s.tabsLoaded[key] = true;
    if (key === "overview") {
      loadPrice();
      loadNewsBrief();
      loadAltBrief();
      loadThesis();
    } else if (key === "financials") loadFinancials();
    else if (key === "earnings") loadEarnings();
    else if (key === "valuation") loadValuation();
    else if (key === "news") loadNewsTab();
    else if (key === "alt") loadAltTab();
    else if (key === "research") loadResearchTab();
  }

  function newsPayload() {
    return once("news", function () {
      return s.ctx.api.research.news(s.symbol, { limit: 25 }, { signal: s.ctx.signal });
    });
  }

  function altPayload() {
    return once("altdata", function () {
      return s.ctx.api.research.altdata(s.symbol, null, { signal: s.ctx.signal });
    });
  }

  /** Overview tab: the two-row news brief. */
  async function loadNewsBrief() {
    const n = s.nodes;
    let payload;
    try {
      payload = await newsPayload();
    } catch (err) {
      if (cancelled(err)) return;
      n.newsBriefBadge.textContent = "error";
      failPanel(n.newsBrief, reason(err));
      return;
    }
    if (!s) return;
    const aggregate = payload.aggregate || {};
    n.newsBriefBadge.textContent = aggregate.label || "—";
    const badgeTone = toneForLabel(aggregate.label);
    n.newsBriefBadge.classList.remove("green", "red");
    if (badgeTone) n.newsBriefBadge.classList.add(badgeTone);
    n.newsBriefBadge.title = "recency-weighted VADER compound "
      + fmt.num(aggregate.recency_weighted, 2) + " over " + (payload.count || 0) + " items";
    const items = payload.items || [];
    if (!items.length) {
      failPanel(n.newsBrief, "The vendor returned no news for " + s.symbol + ".");
      return;
    }
    const body = el("div");
    items.slice(0, 4).forEach(function (item) { body.appendChild(newsRow(item)); });
    ui.render(n.newsBrief, body);
  }

  /** Overview tab: the alternative-signals health rows. */
  async function loadAltBrief() {
    const n = s.nodes;
    let payload;
    try {
      payload = await altPayload();
    } catch (err) {
      if (cancelled(err)) return;
      n.altBriefBadge.textContent = "error";
      failPanel(n.altBrief, reason(err));
      return;
    }
    if (!s) return;
    const signals = payload.signals || {};
    n.altBriefBadge.textContent = (signals.google_trends && signals.google_trends.timeframe) || "—";
    ui.render(n.altBrief, [
      signalRow("Google Trends", signals.google_trends, { decimals: 0 }),
      signalRow("App rank composite", signals.app_rank_composite, { arrow: true }),
      signalRow("News sentiment", signals.news_sentiment, { decimals: 2, sign: true }),
    ]);
  }

  /** One `.health-row` for an alternative-data signal (null carries its reason). */
  function signalRow(label, signal, options) {
    const opts = options || {};
    const value = el("b");
    if (!signal || signal.value === null || signal.value === undefined) {
      value.textContent = "—";
      value.title = signalTitle(signal) || "no source configured";
    } else {
      const n = Number(signal.value);
      if (opts.arrow) {
        value.textContent = (n > 0 ? "↑ " : n < 0 ? "↓ " : "· ")
          + fmt.num(Math.abs(n), opts.decimals === undefined ? 0 : opts.decimals);
      } else {
        value.textContent = fmt.num(n, opts.decimals === undefined ? 1 : opts.decimals);
        if (opts.sign && n > 0) value.textContent = "+" + value.textContent;
      }
      value.title = [signalTitle(signal),
        signal.change_90d_pct !== undefined && signal.change_90d_pct !== null
          ? "90d " + fmt.pct(signal.change_90d_pct, { sign: true }) : null,
      ].filter(Boolean).join(" · ");
      if (opts.sign || opts.arrow) {
        const tone = fmt.toneFor(signal.value);
        if (tone) value.classList.add(tone);
      }
    }
    return el("div.health-row", el("span", { text: label }), value);
  }

  /**
   * Overview tab: the Research thesis card.
   *
   * Two independent things land here, exactly as REFERENCE lays them out:
   * the recorded research (Strategy Lab hypotheses naming this symbol) as the
   * headline, and the CATALYSTS / KEY RISKS rows. The rows come from
   * GET /api/research/company/{symbol}/thesis — each entry is measured from
   * this company's own consensus, statements, balance sheet, news sentiment
   * and coverage warnings, and carries that measurement in its tooltip. They
   * are never written prose, and they are never "—" when the numbers are
   * there: a company with nothing to flag says so in words.
   */
  async function loadThesis() {
    const n = s.nodes;
    let hypotheses = null;
    let thesis = null;
    try {
      const results = await Promise.all([
        s.ctx.api.optional(s.ctx.api.lab.hypotheses({ q: s.symbol }, { signal: s.ctx.signal })),
        s.ctx.api.optional(s.ctx.api.research.thesis(s.symbol, null, { signal: s.ctx.signal })),
      ]);
      hypotheses = results[0];
      thesis = results[1];
    } catch (err) {
      if (cancelled(err)) return;
      n.thesisBadge.textContent = "error";
      failPanel(n.thesisBody, reason(err));
      return;
    }
    if (!s) return;
    const rows = hypotheses ? (hypotheses.hypotheses || hypotheses.items || []) : [];
    const catalysts = (thesis && thesis.catalysts) || [];
    const risks = (thesis && thesis.risks) || [];
    const sources = rows.length + (catalysts.length || risks.length ? 1 : 0);
    n.thesisBadge.textContent = sources + (sources === 1 ? " source" : " sources");

    const body = el("div");
    if (rows.length) {
      const lead = rows[0];
      body.appendChild(el("div.research-card",
        el("h3", { text: lead.title || lead.statement || lead.id || "—" }),
        el("p", { text: lead.statement || lead.rationale || lead.description || "" })));
      rows.slice(0, 3).forEach(function (row) {
        body.appendChild(ui.listRow(row.title || row.id || "—", [
          fmt.statusText(row.status),
          row.owner || "",
          row.updated_at ? fmt.ago(row.updated_at) + " ago" : "",
        ].filter(Boolean).join(" · ")));
      });
    } else {
      body.appendChild(el("div.research-card",
        el("h3", { text: "No hypothesis recorded for " + s.symbol }),
        el("p", { text: "A written thesis appears here once a Strategy Lab hypothesis or an agent finding names this symbol. TradingLib does not write one on your behalf — the two rows below are measured from this company's own numbers." })));
    }
    body.appendChild(thesisRow("CATALYSTS", "green", catalysts,
      thesis ? "nothing above the thresholds" : "not loaded"));
    body.appendChild(thesisRow("KEY RISKS", "red", risks,
      thesis ? "nothing above the thresholds" : "not loaded"));
    ui.render(n.thesisBody, body);
  }

  /**
   * One CATALYSTS / KEY RISKS row: the labels joined with "·" as REFERENCE
   * does, each carrying its measurement as a tooltip.
   * @param {string} label row label
   * @param {string} tone "green" or "red"
   * @param {Array<{label: string, basis: string}>} items derived entries
   * @param {string} empty text shown when nothing fired
   */
  function thesisRow(label, tone, items, empty) {
    const value = el("span");
    if (!items.length) {
      value.textContent = empty;
      value.classList.add("muted");
    } else {
      items.forEach(function (item, index) {
        if (index) value.appendChild(document.createTextNode(" · "));
        value.appendChild(el("span", { text: item.label, title: item.basis }));
      });
    }
    return el("div.list-row", el("b", { class: tone, text: label }), value);
  }

  // --------------------------------------------------------- financials tab

  async function loadFinancials() {
    const panel = s.nodes.panels.financials;
    ui.render(panel, el("div.list-row", el("span", { text: "Loading statements…" })));
    let payload;
    try {
      payload = await once("financials", function () {
        return s.ctx.api.research.financials(s.symbol, null, { signal: s.ctx.signal });
      });
    } catch (err) {
      if (cancelled(err)) return;
      s.tabsLoaded.financials = false;
      failPanel(panel, "Financials: " + reason(err));
      return;
    }
    if (!s) return;
    const annual = (payload.annual || []).filter(function (row) {
      return isFinite(Number(row.revenue)) || isFinite(Number(row.ebitda));
    });
    const years = annual.map(function (row) { return String(row.period_date || row.period).slice(0, 4); });

    const metrics = [
      ["Revenue", "revenue"], ["Gross profit", "gross_profit"],
      ["Adj. EBITDA", "ebitda"], ["Free cash flow", "free_cash_flow"],
    ];
    const table = el("table.data-table");
    const headRow = el("tr", el("th", { text: "Metric" }));
    years.forEach(function (year) { headRow.appendChild(el("th.num", { text: year })); });
    table.appendChild(el("thead", headRow));
    const body = el("tbody");
    metrics.forEach(function (metric) {
      const tr = el("tr", el("td", { text: metric[0] }));
      annual.forEach(function (row) {
        const value = row[metric[1]];
        const td = el("td.num", { text: millions(value) });
        const tone = fmt.toneFor(value);
        if (tone && (metric[1] === "ebitda" || metric[1] === "free_cash_flow")) td.classList.add(tone);
        tr.appendChild(td);
      });
      body.appendChild(tr);
    });
    table.appendChild(body);

    const marginMount = el("div", { style: "position:absolute;inset:13px" });
    ui.render(panel, el("div.split-table",
      el("section.panel",
        el("div.panel-title", "Historical financials ",
          ui.badge((payload.currency || "USD") + " millions")),
        annual.length ? table : el("div.list-row", el("span", {
          text: "The vendor published no annual statements for " + s.symbol + ".",
        }))),
      el("section.panel",
        el("div.panel-title", "Margin progression ", ui.badge("gross · EBITDA")),
        el("div.chart-box", { style: "height:260px" }, el("div.chart-grid"), marginMount))));

    drawMargins(marginMount, annual);
  }

  /** Gross-margin and EBITDA-margin lines over the annual periods. */
  function drawMargins(mount, annual) {
    const gross = [];
    const ebitda = [];
    annual.forEach(function (row) {
      const time = row.period_date || row.period;
      if (isFinite(Number(row.gross_margin))) gross.push({ time: time, value: Number(row.gross_margin) * 100 });
      if (isFinite(Number(row.ebitda_margin))) ebitda.push({ time: time, value: Number(row.ebitda_margin) * 100 });
    });
    if (gross.length < 2 && ebitda.length < 2) {
      mount.appendChild(el("div.list-row", el("span", {
        text: "Not enough reported margins to draw a progression.",
      })));
      return;
    }
    if (s.marginChart) s.marginChart.destroy();
    s.marginChart = global.AlgoCharts.equityChart(mount, {});
    s.marginChart.chart.applyOptions({
      grid: { vertLines: { visible: false }, horzLines: { visible: false } },
    });
    s.marginChart.setLine(gross);
    if (ebitda.length) s.marginChart.setBenchmark(ebitda);
    s.marginChart.fit();
  }

  // ----------------------------------------------------------- earnings tab

  async function loadEarnings() {
    const panel = s.nodes.panels.earnings;
    ui.render(panel, el("div.list-row", el("span", { text: "Loading earnings history…" })));
    let payload;
    try {
      payload = await once("estimates", function () {
        return s.ctx.api.research.estimates(s.symbol, null, { signal: s.ctx.signal });
      });
    } catch (err) {
      if (cancelled(err)) return;
      s.tabsLoaded.earnings = false;
      failPanel(panel, "Estimates: " + reason(err));
      return;
    }
    if (!s) return;
    const history = (payload.history || []).slice().reverse();
    const consensus = payload.consensus || {};

    const table = ui.table({
      columns: [
        { label: "Quarter", render: function (row) { return row.calendar_quarter || row.fiscal_period || "—"; } },
        { label: "EPS est.", num: true, render: function (row) { return fmt.num(row.eps_estimate, 2); } },
        { label: "EPS actual", num: true, render: function (row) { return fmt.num(row.eps_reported, 2); } },
        {
          label: "Surprise", num: true,
          render: function (row) {
            if (row.surprise_pct === null || row.surprise_pct === undefined) return "—";
            const node = el("span", { text: fmt.pct(row.surprise_pct, { sign: true }) });
            const tone = fmt.toneFor(row.surprise_pct);
            if (tone) node.classList.add(tone);
            return node;
          },
        },
        {
          label: "Reaction",
          render: function (row) {
            if (row.reaction_pct === null || row.reaction_pct === undefined) return "—";
            const node = el("span", { text: fmt.pct(row.reaction_pct, { sign: true }) });
            const tone = fmt.toneFor(row.reaction_pct);
            if (tone) node.classList.add(tone);
            node.title = "close-to-close move on " + fmt.etDate(row.date);
            return node;
          },
        },
      ],
      rows: history,
      empty: "The vendor published no earnings history for " + s.symbol + ".",
    });

    const grid = el("div.metric-grid",
      plainCell("FY REVENUE", consensus.fy_revenue === null || consensus.fy_revenue === undefined
        ? "—" : fmt.usd(consensus.fy_revenue, { compact: true, decimals: 2 }),
      { title: consensus.basis || "" }),
      plainCell("FY EPS", consensus.fy_eps === null || consensus.fy_eps === undefined
        ? "—" : fmt.usd(consensus.fy_eps, { decimals: 2 }),
      { title: (consensus.analysts || "?") + " analysts" }),
      plainCell("30D REVISION", consensus.revision_30d === null || consensus.revision_30d === undefined
        ? "—" : fmt.pct(consensus.revision_30d, { fraction: true, sign: true }),
      { tone: fmt.toneFor(consensus.revision_30d), title: "30-day change in the mean EPS estimate" }),
      plainCell("DISPERSION", consensus.dispersion === null || consensus.dispersion === undefined
        ? "—" : fmt.pct(consensus.dispersion, { fraction: true }),
      { title: "(high − low) / |mean| EPS" }));

    ui.render(panel, el("div.split-table",
      el("section.panel", el("div.panel-title", "Earnings history ",
        ui.badge(history.length + " quarters")), table),
      el("section.panel", el("div.panel-title", "Consensus estimates ",
        ui.badge("current fiscal year")), grid)));
  }

  // ---------------------------------------------------------- valuation tab

  async function loadValuation() {
    const panel = s.nodes.panels.valuation;
    ui.render(panel, el("div.list-row", el("span", { text: "Loading valuation and peers…" })));
    let payload;
    try {
      payload = await once("valuation", function () {
        return s.ctx.api.research.valuation(s.symbol, null, { signal: s.ctx.signal });
      });
    } catch (err) {
      if (cancelled(err)) return;
      s.tabsLoaded.valuation = false;
      failPanel(panel, "Valuation: " + reason(err));
      return;
    }
    if (!s) return;
    const multiples = payload.multiples || {};
    const grid = el("div.metric-grid",
      metricCell("EV / SALES", multiples.ev_to_sales),
      metricCell("EV / EBITDA", multiples.ev_to_ebitda),
      metricCell("FCF YIELD", multiples.fcf_yield),
      metricCell("REVERSE DCF GROWTH", multiples.reverse_dcf_growth));

    const peers = payload.peers || [];
    const table = ui.table({
      columns: [
        {
          label: "Company",
          render: function (row) {
            const node = el("span", { text: row.symbol });
            node.title = row.name || "";
            if (row.is_subject) node.classList.add("cyan");
            return node;
          },
        },
        {
          label: "Growth", num: true,
          render: function (row) {
            const node = el("span", { text: fmt.pct(row.revenue_yoy, { fraction: true, sign: true }) });
            const tone = fmt.toneFor(row.revenue_yoy);
            if (tone) node.classList.add(tone);
            return node;
          },
        },
        { label: "EV/Sales", num: true, render: function (row) { return row.ev_to_sales === null || row.ev_to_sales === undefined ? "—" : fmt.num(row.ev_to_sales, 1) + "×"; } },
        { label: "FCF margin", num: true, render: function (row) { return fmt.pct(row.fcf_margin, { fraction: true }); } },
      ],
      rows: peers,
      empty: "No peers could be resolved from the configured universes.",
    });

    ui.render(panel, el("div.split-table",
      el("section.panel", el("div.panel-title", "Valuation snapshot ", ui.badge("TTM")), grid),
      el("section.panel",
        el("div.panel-title", "Peer comparison ", ui.badge(peers.length + " names")),
        table,
        el("div.list-row", el("span", { text: payload.peer_basis || "" })))));
  }

  // --------------------------------------------------------------- news tab

  function toneForLabel(label) {
    if (label === "positive") return "green";
    if (label === "negative") return "red";
    return "";
  }

  /** One `.list-row` for a news item, toned by its sentiment. */
  function newsRow(item) {
    const tone = toneForLabel(item.sentiment_label);
    const detail = [
      item.sentiment_label ? item.sentiment_label.charAt(0).toUpperCase() + item.sentiment_label.slice(1) : "",
      item.publisher || item.source || "",
      item.published_at ? fmt.ago(item.published_at) : "",
    ].filter(Boolean).join(" · ");
    const row = ui.listRow(item.headline || "—", detail, { detailTone: tone });
    row.title = (item.url || "") + (item.sentiment !== undefined && item.sentiment !== null
      ? "\ncompound " + fmt.num(item.sentiment, 3) : "");
    if (item.url) {
      row.style.cursor = "pointer";
      row.addEventListener("click", function () { global.open(item.url, "_blank", "noopener"); });
    }
    return row;
  }

  async function loadNewsTab() {
    const panel = s.nodes.panels.news;
    ui.render(panel, el("div.list-row", el("span", { text: "Loading news…" })));
    let payload;
    try {
      payload = await newsPayload();
    } catch (err) {
      if (cancelled(err)) return;
      s.tabsLoaded.news = false;
      failPanel(panel, "News: " + reason(err));
      return;
    }
    if (!s) return;
    const items = payload.items || [];
    const aggregate = payload.aggregate || {};
    const section = el("section.panel", { style: "margin-top:11px" },
      el("div.panel-title", "Ticker news & sentiment ",
        ui.badge(s.symbol + " · " + (aggregate.label || "—"), { tone: toneForLabel(aggregate.label) })));
    if (!items.length) {
      section.appendChild(el("div.list-row", el("span", {
        text: "The vendor returned no news for " + s.symbol + ".",
      })));
    } else {
      const counts = aggregate.counts || {};
      section.appendChild(el("div.list-row",
        el("b", { text: (payload.count || items.length) + " items scored by " + (payload.scorer || "?") }),
        el("span", {
          text: (counts.positive || 0) + " positive · " + (counts.neutral || 0) + " neutral · "
            + (counts.negative || 0) + " negative · recency-weighted compound "
            + fmt.num(aggregate.recency_weighted, 3),
        })));
      items.forEach(function (item) { section.appendChild(newsRow(item)); });
    }
    ui.render(panel, section);
  }

  // ---------------------------------------------------- alternative data tab

  async function loadAltTab() {
    const panel = s.nodes.panels.alt;
    ui.render(panel, el("div.list-row", el("span", { text: "Loading alternative data…" })));
    let payload;
    try {
      payload = await altPayload();
    } catch (err) {
      if (cancelled(err)) return;
      s.tabsLoaded.alt = false;
      failPanel(panel, "Alternative data: " + reason(err));
      return;
    }
    if (!s) return;
    const signals = payload.signals || {};
    const cards = el("div.dashboard-cards", { style: "margin-top:11px" },
      statCard("GOOGLE TRENDS · " + s.symbol, signals.google_trends, { decimals: 0 }),
      statCard("APP RANK COMPOSITE", signals.app_rank_composite, { arrow: true }),
      statCard("WEB TRAFFIC YOY", signals.web_traffic_yoy, { percent: true, fraction: true }),
      // A level, not a change: no leading "+" and no red/green, because a
      // hiring rate is not directionally good or bad on its own.
      statCard("JOB POSTINGS 90D", signals.job_postings_90d, { percent: true, level: true }));

    const trends = signals.google_trends || {};
    const jobs = signals.job_postings_90d || {};
    const freshness = payload.freshness || {};
    const change = el("b", {
      text: trends.change_90d_pct === null || trends.change_90d_pct === undefined
        ? "—" : fmt.pct(trends.change_90d_pct, { sign: true }),
    });
    const changeTone = fmt.toneFor(trends.change_90d_pct);
    if (changeTone) change.classList.add(changeTone);
    const extra = el("section.panel",
      el("div.panel-title", "Signal detail ", ui.badge(trends.timeframe || "—")),
      el("div.health-row", el("span", { text: "Google Trends keyword" }),
        el("b", { text: trends.keyword || "—", title: "geo " + (trends.geo || "—") })),
      el("div.health-row", el("span", { text: "Trends 90d change" }), change),
      signalRow("News sentiment", signals.news_sentiment, { decimals: 3, sign: true }),
      el("div.health-row", el("span", { text: "Job board" }),
        el("b", {
          text: jobs.board
            ? jobs.board + " · " + jobs.slug + " · " + fmt.num(jobs.open_roles, 0) + " open roles"
            : "—",
          title: jobs.basis || jobs.reason || "",
        })),
      el("div.health-row", el("span", { text: "Trends fetched" }),
        el("b", {
          text: freshness.trends && freshness.trends.fetched_at
            ? fmt.ago(freshness.trends.fetched_at) + " ago" : "—",
          title: freshness.trends ? "source " + (freshness.trends.source || "?") : "no freshness report",
        })));

    ui.render(panel, [cards, extra]);
  }

  /**
   * A `.stat-card` for one alternative-data signal.
   * `arrow: true` renders a rank move as REFERENCE does ("↑ 4"); `fraction`
   * says the percentage arrives as 0.118 rather than 11.8; `level: true` marks
   * a percentage that is a level rather than a change, which drops the leading
   * sign and the red/green tone. The signal's `basis` — how the number was
   * derived — always lands in the tooltip, so a proxy is never mistaken for a
   * direct measurement.
   */
  function statCard(label, signal, options) {
    const opts = options || {};
    const value = el("b");
    if (!signal || signal.value === null || signal.value === undefined) {
      value.textContent = "—";
      value.title = signalTitle(signal) || "no source configured";
    } else {
      const n = Number(signal.value);
      if (opts.arrow) {
        const magnitude = fmt.num(Math.abs(n), opts.decimals === undefined ? 0 : opts.decimals);
        value.textContent = (n > 0 ? "↑ " : n < 0 ? "↓ " : "· ") + magnitude;
      } else if (opts.percent) {
        value.textContent = fmt.pct(n, { sign: !opts.level, fraction: !!opts.fraction });
      } else {
        value.textContent = fmt.num(n, opts.decimals === undefined ? 1 : opts.decimals);
      }
      value.title = signalTitle(signal);
      const tone = (opts.percent || opts.arrow) && !opts.level ? fmt.toneFor(n) : "";
      if (tone) value.classList.add(tone);
    }
    const card = el("section.panel.stat-card", el("small", { text: label }), value);
    const title = signalTitle(signal);
    if (title) card.title = title;
    return card;
  }

  /** Tooltip text for a signal: its derivation first, then units and source. */
  function signalTitle(signal) {
    if (!signal) return "";
    return [signal.basis, signal.reason, signal.unit, signal.label,
      signal.source ? "source " + signal.source : null,
      signal.as_of ? "as of " + String(signal.as_of).slice(0, 10) : null,
    ].filter(Boolean).join(" · ");
  }

  // ----------------------------------------------------------- research tab

  async function loadResearchTab() {
    const panel = s.nodes.panels.research;
    ui.render(panel, el("div.list-row", el("span", { text: "Loading research memory…" })));

    let filings = null;
    let news = null;
    let hypotheses = null;
    let findings = null;
    let memory = null;
    try {
      filings = await once("filings", function () {
        return s.ctx.api.research.filings(s.symbol, null, { signal: s.ctx.signal });
      });
      news = await newsPayload().catch(function () { return null; });
      memory = await s.ctx.api.optional(
        s.ctx.api.research.memory(s.symbol, null, { signal: s.ctx.signal })
      );
      hypotheses = await s.ctx.api.optional(
        s.ctx.api.lab.hypotheses({ q: s.symbol }, { signal: s.ctx.signal })
      );
      findings = await s.ctx.api.optional(
        s.ctx.api.agents.findings({ symbol: s.symbol }, { signal: s.ctx.signal })
      );
    } catch (err) {
      if (cancelled(err)) return;
      s.tabsLoaded.research = false;
      failPanel(panel, "Research memory: " + reason(err));
      return;
    }
    if (!s) return;

    const hypothesisRows = hypotheses ? (hypotheses.hypotheses || hypotheses.items || []) : [];
    const formCounts = (filings && filings.form_counts) || {};
    const forms = Object.keys(formCounts).map(function (form) {
      return formCounts[form] + " × " + form;
    }).join(" · ");

    // Current thesis / open questions come from the research-memory composer
    // (GET /api/research/company/{symbol}/memory): recorded hypotheses,
    // findings and knowledge entries, never model-written prose.
    const memoryCounts = (memory && memory.counts) || {};
    const questions = (memory && memory.questions) || [];
    const memoryPanel = el("section.panel",
      el("div.panel-title", "Research memory ",
        ui.badge(memory
          ? (memoryCounts.hypotheses || 0) + " hypotheses · " + (memoryCounts.findings || 0) + " findings"
          : hypothesisRows.length + " hypotheses",
        { tone: memory ? "" : "yellow" })));
    if (memory) {
      memoryPanel.appendChild(el("div.list-row",
        el("b", { text: "Current thesis" }),
        el("span", { text: memory.thesis.text, title: memory.thesis.detail || memory.basis || "" })));
      if (questions.length) {
        questions.forEach(function (question, index) {
          memoryPanel.appendChild(el("div.list-row",
            el("b", { text: index === 0 ? "Open questions" : "" }),
            el("span", { text: question.text, title: question.detail || question.kind })));
        });
      } else {
        memoryPanel.appendChild(ui.listRow("Open questions",
          memoryCounts.hypotheses
            ? "None — every recorded hypothesis naming " + s.symbol + " is settled."
            : "None recorded."));
      }
    } else {
      memoryPanel.appendChild(ui.listRow("Current thesis",
        "The research-memory endpoint is unavailable in this build."));
      memoryPanel.appendChild(ui.listRow("Open questions", "—"));
    }
    memoryPanel.appendChild(ui.listRow("Sources", [
      filings ? (filings.count || 0) + " filings" : "filings unavailable",
      news ? (news.count || 0) + " news items" : "news unavailable",
      memory ? (memoryCounts.knowledge || 0) + " knowledge entries" : null,
      forms || null,
    ].filter(Boolean).join(" · ")));

    const findingsPanel = el("section.panel",
      el("div.panel-title", "Linked agent findings ",
        ui.badge(findings === null ? "unavailable" : String((findings.findings || findings.items || []).length),
          { tone: findings === null ? "yellow" : "" })));
    if (findings === null) {
      findingsPanel.appendChild(el("div.list-row",
        el("b", { text: "Agent findings not served yet" }),
        el("span", { text: "GET /api/agents/findings is not part of this build; nothing is shown rather than a placeholder finding." })));
    } else {
      const rows = findings.findings || findings.items || [];
      if (!rows.length) {
        findingsPanel.appendChild(el("div.list-row",
          el("span", { text: "No agent has recorded a finding for " + s.symbol + " yet." })));
      } else {
        rows.slice(0, 6).forEach(function (row) {
          const lead = el("span.finding-link", { text: row.title || row.summary || row.id || "—" });
          findingsPanel.appendChild(ui.listRow(lead, [
            row.agent || row.author || "",
            row.status ? fmt.statusText(row.status) : "",
            row.evidence_score !== undefined && row.evidence_score !== null
              ? fmt.pct(row.evidence_score, { fraction: true }) + " evidence" : "",
          ].filter(Boolean).join(" · ")));
        });
      }
    }

    const filingsPanel = el("section.panel",
      el("div.panel-title", "SEC filings ",
        ui.badge(filings ? (filings.source || "edgar") : "unavailable")));
    const items = (filings && filings.items) || [];
    if (!items.length) {
      filingsPanel.appendChild(el("div.list-row", el("span", {
        text: (filings && filings.error) || "EDGAR returned no filings for " + s.symbol + ".",
      })));
    } else {
      filingsPanel.appendChild(ui.table({
        columns: [
          { label: "Filed", render: function (row) { return fmt.etDate(row.filed_at); } },
          { label: "Form", key: "form" },
          {
            label: "Document",
            render: function (row) {
              if (!row.url) return row.title || "—";
              return el("a.finding-link", {
                href: row.url, target: "_blank", rel: "noopener",
                text: row.title || row.accession || "filing",
              });
            },
          },
          { label: "Period", render: function (row) { return row.period_of_report ? fmt.etDate(row.period_of_report) : "—"; } },
        ],
        rows: items.slice(0, 12),
        empty: "No filings.",
      }));
    }

    ui.render(panel, [
      el("div.split-table", memoryPanel, findingsPanel),
      el("div.split-table", filingsPanel),
    ]);
  }

  // --------------------------------------------------------------- module

  global.AlgoPages.register("stock", {
    mount: function (ctx) {
      s = {
        ctx: ctx,
        nodes: {},
        symbol: (ctx.sub ? String(ctx.sub) : (ctx.params && ctx.params.symbol) || DEFAULT_SYMBOL)
          .trim().toUpperCase(),
        range: DEFAULT_RANGE,
        tab: "overview",
        tabsLoaded: {},
        cache: {},
        chart: null,
        marginChart: null,
        lastPointTime: null,
        intradayChart: false,
        offs: [],
      };
      build(ctx.root);
      // The tape publisher polls exactly the symbols browsers ask for, so a
      // per-symbol channel gets this page live quotes without disturbing the
      // Overview's own tape subscription.
      s.offs.push(ctx.stream.listen("tape." + s.symbol, onQuote));
      loadSymbol();
    },

    /** "#stock/<SYMBOL>" changed while mounted: reload in place. */
    route: function (ctx) {
      if (!s) return;
      const next = (ctx.sub ? String(ctx.sub) : DEFAULT_SYMBOL).trim().toUpperCase();
      if (next === s.symbol) return;
      s.offs.forEach(function (off) { off(); });
      s.offs = [];
      // The Overview tab is rebuilt below, so the charts drawn into its old
      // mounts have to go with it rather than leaking a live chart instance.
      if (s.chart) s.chart.destroy();
      if (s.marginChart) s.marginChart.destroy();
      s.chart = null;
      s.marginChart = null;
      s.range = DEFAULT_RANGE;
      s.symbol = next;
      s.tabsLoaded = {};
      s.cache = {};
      s.lastPointTime = null;
      s.nodes.symbolInput.value = next;
      s.nodes.companyName.textContent = "Loading…";
      s.nodes.companyMeta.textContent = "";
      s.nodes.companyMeta.className = "";
      s.nodes.price.textContent = "—";
      s.offs.push(ctx.stream.listen("tape." + next, onQuote));
      buildOverviewTab();
      loadSymbol();
    },

    unmount: function (ctx) {
      if (!s) return;
      s.offs.forEach(function (off) {
        try {
          off();
        } catch (err) {
          console.warn("stock: teardown failed", err);
        }
      });
      if (s.chart) s.chart.destroy();
      if (s.marginChart) s.marginChart.destroy();
      s = null;
    },
  });
})(window);
