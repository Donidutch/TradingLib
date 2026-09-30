/* TradingLib — Overview workspace.
 *
 * The REFERENCE Overview page, rebuilt against the real backend. The DOM mirrors
 * REFERENCE structure and classes exactly (SPEC D1a); every value below comes
 * from an endpoint or a WebSocket frame, and anything TradingLib has no source for
 * renders as "—" carrying the reason in its title attribute rather than a
 * plausible-looking number.
 *
 *   market strip      /api/status  (+ the `status` channel, via AlgoShell)
 *   ticker tape       active universe members + the `tape` channel (live quotes)
 *   watchlist         /api/watchlists (+ live prices from the same tape)
 *   system health     /api/status subsystems
 *   quote + chart     /api/research/company/{symbol} (+ /price), lightweight-charts
 *   fundamentals      derived metrics from the company payload
 *   market context    SPY vs its 200-day average, breadth from the live tape
 *   intel tabs        SPY news, universe earnings calendar, FRED, Google Trends
 *   research inbox    /api/agents/inbox  (SPEC section 6 "agent inbox merge")
 *
 * See ui/js/router.js for the mount/unmount/route contract.
 */
(function (global) {
  "use strict";

  const ui = global.AlgoUI;
  const el = ui.el;
  const fmt = ui.fmt;

  /** Tape items rendered; the strip clips what does not fit, as REFERENCE does. */
  const TAPE_SYMBOLS = 14;
  /** Universe symbols whose earnings calendar the intel tab loads. */
  const EARNINGS_SYMBOLS = 6;
  /** Universe symbols whose Google Trends the intel tab loads. */
  const TRENDS_SYMBOLS = 4;
  /** Fallback centre symbol when no watchlist and no universe answers. */
  const FALLBACK_SYMBOL = "SPY";
  /** Trading days in the moving average behind the regime row. */
  const REGIME_WINDOW = 200;
  /** Universe whose constituent breadth the "S&P above 200D" row reports. */
  const BREADTH_UNIVERSE = "sp500";
  /** Index ticker behind the VIX row (the price endpoint accepts ^-prefixed indices). */
  const VIX_SYMBOL = "^VIX";

  /** @type {object|null} state of the current mount; null while unmounted. */
  let s = null;

  // ---------------------------------------------------------------- helpers

  /** True when an error is just this page being torn down mid-request. */
  function cancelled(err) {
    return !!(err && (err.cancelled || err.name === "AbortError"));
  }

  /** A one-line reason for a failed call, safe to show in the UI. */
  function reason(err) {
    if (!err) return "unknown error";
    if (err.status === 404) return "not found";
    return err.message ? err.message : String(err);
  }

  /**
   * Format one derived metric from the research payload.
   * @param {{value: any, unit?: string, reason?: string, basis?: string}|null} metric
   * @param {{sign?: boolean, decimals?: number}} [options]
   * @returns {{text: string, tone: string, title: string}}
   */
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
    return {
      text: text,
      tone: opts.sign ? fmt.toneFor(value) : "",
      title: metric.basis || "",
    };
  }

  /** Apply a formatted metric to a <b> node. */
  function paintMetric(node, metric, options) {
    if (!node) return;
    const out = metricText(metric, options);
    node.textContent = out.text;
    node.classList.remove("green", "red", "yellow", "cyan");
    if (out.tone) node.classList.add(out.tone);
    node.title = out.title;
  }

  /** A `.metric` cell: label above value. Returns the <b> for later updates. */
  function metricCell(label, id) {
    const value = el("b", { text: "—" });
    if (id) value.id = id;
    const cell = el("div.metric", el("small", { text: label }), value);
    return { cell: cell, value: value };
  }

  /** Run `fn` over `items` with at most `limit` in flight. */
  async function mapLimited(items, limit, fn) {
    const out = new Array(items.length);
    let cursor = 0;
    const workers = [];
    const size = Math.max(1, Math.min(limit, items.length));
    for (let i = 0; i < size; i += 1) {
      workers.push((async function () {
        for (;;) {
          const index = cursor;
          cursor += 1;
          if (index >= items.length) return;
          out[index] = await fn(items[index], index);
        }
      })());
    }
    await Promise.all(workers);
    return out;
  }

  /**
   * Wire a REFERENCE `.tabs` group: one `.tab` per `.tab-panel` of the group.
   * @param {HTMLElement} tabs the `.tabs` element
   * @param {HTMLElement} scope element containing the panels
   * @param {(key: string) => void} [onActivate]
   */
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

  // ------------------------------------------------------------------ build

  /** Build the whole page skeleton with placeholder values. */
  function build(root) {
    const n = s.nodes;

    // --- market strip (mirrors the datastrip, in REFERENCE's own wording)
    n.stripLive = el("strong", { text: "● DATA …" });
    n.stripGateway = el("span", { text: "Research gateway —" });
    n.stripSources = el("span", { text: "— sources connected" });
    n.stripIngest = el("span", { text: "Last ingest —" });
    n.stripDelayed = el("span", { text: "— delayed series" });
    const strip = el("div.market-strip",
      n.stripLive, n.stripGateway, n.stripSources, n.stripIngest, n.stripDelayed);

    // --- ticker tape
    n.tapeLabel = el("span.tape-label", { text: "● …" });
    n.tape = el("div.ticker-tape", n.tapeLabel);

    // --- watchlist
    n.watchBadge = ui.badge("—");
    n.watchFilter = el("input.input.watch-filter", {
      placeholder: "Filter symbols…", "aria-label": "Filter watchlist symbols",
      on: { input: function () { s.filter = n.watchFilter.value.trim().toUpperCase(); renderWatchRows(); } },
    });
    n.watchAddInput = el("input.input", {
      placeholder: "TICKER", "aria-label": "Symbol to add to the watchlist",
    });
    n.watchAddForm = el("form.quote-search", {
      style: "padding:0 9px 9px",
      on: { submit: function (event) { event.preventDefault(); addWatchSymbol(); } },
    }, n.watchAddInput, el("button.btn", { type: "submit", text: "Add" }));
    n.watchRows = el("div", { id: "watchRows" });
    const watchPanel = el("section.panel",
      el("div.panel-title", "Watchlist ", n.watchBadge),
      n.watchFilter, n.watchAddForm, n.watchRows);

    // --- system & data health
    n.healthEngine = el("b", { text: "—" });
    n.healthPrices = el("b", { text: "—" });
    n.healthFundamentals = el("b", { text: "—" });
    n.healthDelayed = el("b", { text: "—" });
    n.healthBadge = ui.badge("—");
    // Four rows, in the reference's order. "Data sources n/m" is not repeated
    // here: the datastrip already carries it.
    const healthPanel = el("section.panel",
      el("div.panel-title", "System & data health ", n.healthBadge),
      el("div.health-row", el("span", { text: "Research engine" }), n.healthEngine),
      el("div.health-row", el("span", { text: "Market prices" }), n.healthPrices),
      el("div.health-row", el("span", { text: "Fundamentals" }), n.healthFundamentals),
      el("div.health-row", el("span", { text: "Delayed series" }), n.healthDelayed));

    // --- centre: quote head + chart
    n.symbolInput = el("input.input", {
      id: "overviewSymbolInput", "aria-label": "Chart ticker", autocomplete: "off", spellcheck: "false",
    });
    n.symbolForm = el("form.quote-search", {
      id: "overviewSymbolForm",
      on: {
        submit: function (event) {
          event.preventDefault();
          const value = n.symbolInput.value.trim().toUpperCase();
          if (value) selectSymbol(value);
        },
      },
    }, el("span.muted", { text: "⌕ TICKER" }), n.symbolInput,
      el("button.btn", { type: "submit", text: "Load" }));

    n.price = el("span.price", { id: "overviewPrice", text: "—" });
    n.ohlcOpen = el("b", { text: "—" });
    n.ohlcHigh = el("b", { text: "—" });
    n.ohlcLow = el("b", { text: "—" });
    n.ohlcVol = el("b", { text: "—" });
    const ohlc = el("div.ohlc",
      el("div", el("small", { text: "OPEN" }), n.ohlcOpen),
      el("div", el("small", { text: "HIGH" }), n.ohlcHigh),
      el("div", el("small", { text: "LOW" }), n.ohlcLow),
      el("div", el("small", { text: "VOL" }), n.ohlcVol));

    n.chartMount = el("div", { style: "position:absolute;inset:13px" });
    n.chartNote = el("div.list-row", { style: "display:none" }, el("span.red", { text: "" }));
    const quotePanel = el("section.panel",
      el("div.quote-head", n.symbolForm, n.price, ohlc),
      el("div.chart-box", { style: "height:285px" }, el("div.chart-grid"), n.chartMount),
      n.chartNote);

    // --- fundamentals
    n.fundSymbol = el("span", { id: "fundSymbol", text: "—" });
    n.qualityLabel = el("strong", { text: "QUALITY SCORE" });
    n.qualityValue = el("span", {
      text: "—",
      title: "no composite quality score is exposed by /api/research/company/{symbol}; "
        + "the screener computes one per universe run (/api/screens)",
    });
    const marketCap = metricCell("MARKET CAP", "fundMarketCap");
    const pe = metricCell("FWD P/E", "fundForwardPe");
    const growth = metricCell("REVENUE GROWTH", "fundRevenueGrowth");
    const fcf = metricCell("FCF MARGIN");
    const roic = metricCell("ROIC", "fundRoic");
    const netCash = metricCell("NET CASH", "fundNetCash");
    n.fund = {
      market_cap: marketCap.value,
      forward_pe: pe.value,
      revenue_yoy: growth.value,
      fcf_margin: fcf.value,
      return_on_invested_capital: roic.value,
      net_cash: netCash.value,
    };
    const fundPanel = el("section.panel",
      el("div.panel-title", n.fundSymbol, " Fundamentals ", ui.badge("TTM")),
      el("div.quality", n.qualityLabel, n.qualityValue),
      el("div.metric-grid", marketCap.cell, pe.cell, growth.cell,
        fcf.cell, roic.cell, netCash.cell));

    // --- market context
    n.ctxRegime = el("b", { text: "—" });
    n.ctxVs200 = el("b", { text: "—" });
    n.ctxBreadth = el("b", { text: "—" });
    n.ctxVix = el("b", { text: "—" });
    n.ctxBadge = ui.badge("US equities");
    const contextPanel = el("section.panel",
      el("div.panel-title", "Market context ", n.ctxBadge),
      el("div.health-row", el("span", { text: "Regime" }), n.ctxRegime),
      el("div.health-row", el("span", { text: "S&P above 200D" }), n.ctxVs200),
      el("div.health-row", el("span", { text: "Advance / decline" }), n.ctxBreadth),
      el("div.health-row", el("span", { text: "VIX" }), n.ctxVix));

    // --- research inbox (SPEC section 6: the agent inbox merge)
    n.inboxBadge = ui.badge("—");
    n.inboxBody = el("div", el("div.list-row", el("span", { text: "Loading the agent inbox…" })));
    const inboxPanel = el("section.panel",
      el("div.panel-title", "Research inbox ", n.inboxBadge), n.inboxBody);

    // --- intel panel
    n.intelBadge = ui.badge("SCOPE: GENERAL MARKET", { tone: "cyan" });
    const intelTabs = el("div.tabs", { dataset: { tabs: "overview-intel" } },
      el("button.tab.active", { type: "button", dataset: { tab: "news" }, text: "News" }),
      el("button.tab", { type: "button", dataset: { tab: "earnings" }, text: "Earnings calendar" }),
      el("button.tab", { type: "button", dataset: { tab: "fred" }, text: "FRED macro" }),
      el("button.tab", { type: "button", dataset: { tab: "trends" }, text: "Google Trends" }));

    n.panelNews = el("div.tab-panel.active", { dataset: { panelGroup: "overview-intel", panel: "news" } },
      el("div.intel-grid", el("article.news-lead", el("span.cyan", { style: "font-size:7px", text: "TOP MARKET STORY" }),
        el("h3", { text: "Loading market news…" }), el("p", { text: "" })), el("div"), el("div")));
    n.panelEarnings = el("div.tab-panel", { dataset: { panelGroup: "overview-intel", panel: "earnings" } });
    n.panelFred = el("div.tab-panel", { dataset: { panelGroup: "overview-intel", panel: "fred" } });
    n.panelTrends = el("div.tab-panel", { dataset: { panelGroup: "overview-intel", panel: "trends" } });

    const intelPanel = el("section.panel.intel",
      el("div.intel-head", intelTabs, n.intelBadge),
      n.panelNews, n.panelEarnings, n.panelFred, n.panelTrends);

    // The grid: REFERENCE's three columns, then the inbox in the empty cell the
    // intel panel (grid-column 2/4) leaves under the left column.
    const grid = el("div.overview-grid",
      el("div.overview-left", watchPanel, healthPanel),
      el("div.overview-center", quotePanel),
      el("div.overview-right", fundPanel, contextPanel),
      inboxPanel,
      intelPanel);

    ui.render(root, [strip, n.tape, grid]);

    wireTabs(intelTabs, intelPanel, function (key) { loadIntel(key); });
    n.watchRows.addEventListener("click", onWatchClick);
    n.watchRows.addEventListener("keydown", function (event) {
      if (event.key !== "Enter" && event.key !== " ") return;
      const row = event.target.closest(".watch-row");
      if (!row) return;
      event.preventDefault();
      selectSymbol(row.dataset.symbol);
    });
  }

  // ------------------------------------------------------- status / health

  /** Paint the market strip and the health panel from a /api/status payload. */
  function applyStatus(status) {
    if (!s || !status) return;
    const n = s.nodes;
    const online = global.api.online !== false;
    const live = global.stream.connected;

    if (!online) n.stripLive.textContent = "● BACKEND UNREACHABLE";
    else if (!live) n.stripLive.textContent = "● LIVE FEED LOST";
    else if (status.data_live) n.stripLive.textContent = "● DATA LIVE";
    else n.stripLive.textContent = "● DATA IDLE";
    n.stripLive.classList.toggle("red", !online || !live || !status.data_live);

    n.stripGateway.textContent = status.gateway_ms === null || status.gateway_ms === undefined
      ? "Research gateway —"
      : "Research gateway " + Math.round(status.gateway_ms) + "ms";
    n.stripSources.textContent = status.sources_total
      ? status.sources_connected + " of " + status.sources_total + " sources connected"
      : status.sources_connected + " sources connected";
    n.stripIngest.textContent = status.last_ingest
      ? "Last ingest " + fmt.ago(status.last_ingest) + " ago"
      : "Last ingest —";
    const delayed = Number(status.delayed_series || 0);
    n.stripDelayed.textContent = delayed + " delayed series";
    n.stripDelayed.classList.toggle("yellow", delayed > 0);

    const healthy = status.engine_health === "healthy";
    n.healthEngine.textContent = fmt.statusText(status.engine_health);
    n.healthEngine.className = healthy ? "green" : "yellow";
    n.healthEngine.title = status.engine_detail || "";

    const marketStream = status.market_stream || {};
    if (marketStream.running) {
      n.healthPrices.textContent = "LIVE";
      n.healthPrices.className = "green";
    } else {
      n.healthPrices.textContent = "DELAYED";
      n.healthPrices.className = "yellow";
    }
    n.healthPrices.title = marketStream.detail
      || "quotes are polled from the delayed vendor feed; no push stream is configured";

    // Fundamentals coverage across the active universe. `null` means the
    // background measurement has not completed yet, which is said plainly
    // rather than shown as a zero.
    const coverage = status.fundamentals_coverage;
    if (!coverage) {
      n.healthFundamentals.textContent = "MEASURING";
      n.healthFundamentals.className = "";
      n.healthFundamentals.title =
        "Coverage across the active universe is measured in the background; "
        + "the first result appears once every member has been resolved.";
    } else if (!coverage.total) {
      n.healthFundamentals.textContent = "NO UNIVERSE";
      n.healthFundamentals.className = "yellow";
      n.healthFundamentals.title = "No active universe has members to measure.";
    } else {
      n.healthFundamentals.textContent = coverage.covered + " / " + coverage.total;
      n.healthFundamentals.className = coverage.covered < coverage.total ? "yellow" : "green";
      const missing = coverage.missing || [];
      n.healthFundamentals.title =
        (coverage.universe_name || coverage.universe_id || "active universe")
        + ": " + fmt.pct(coverage.pct) + " of members have a market cap and trailing revenue"
        + (missing.length ? ". Missing: " + missing.join(", ")
          + (coverage.missing_total > missing.length
            ? " (+" + (coverage.missing_total - missing.length) + " more)" : "") : "")
        + ". Measured " + fmt.ago(coverage.measured_at) + " ago.";
    }

    n.healthDelayed.textContent = delayed ? fmt.int(delayed) : "0";
    n.healthDelayed.className = delayed ? "yellow" : "green";

    n.healthBadge.textContent = live ? "live" : "offline";
    n.healthBadge.classList.toggle("green", live);
    n.healthBadge.classList.toggle("red", !live);
  }

  // ------------------------------------------------------------ ticker tape

  /** Rebuild the tape row from the active universe members. */
  function renderTape(universeName) {
    const n = s.nodes;
    n.tapeLabel.textContent = "● " + String(universeName || "UNIVERSE").toUpperCase();
    while (n.tape.childNodes.length > 1) n.tape.removeChild(n.tape.lastChild);
    s.tapeItems = new Map();

    const symbols = s.members.slice(0, TAPE_SYMBOLS);
    if (!symbols.length) {
      n.tape.appendChild(el("span.tape-item", el("span.muted", { text: "no universe members" })));
      return;
    }
    symbols.forEach(function (member) {
      const price = el("span", { text: "—" });
      const change = el("i", { text: "…" });
      const item = el("span.tape-item", el("b", { text: member.symbol }), price, change);
      item.title = member.name || member.symbol;
      n.tape.appendChild(item);
      s.tapeItems.set(member.symbol, { price: price, change: change });
      const quote = s.quotes.get(member.symbol);
      if (quote) paintTapeItem(member.symbol, quote);
    });
  }

  /** Paint one tape item from a quote payload. */
  function paintTapeItem(symbol, quote) {
    const item = s.tapeItems.get(symbol);
    if (!item) return;
    item.price.textContent = " " + fmt.num(quote.price, 2) + " ";
    const pct = quote.change_pct;
    if (pct === null || pct === undefined) {
      item.change.textContent = "—";
      item.change.className = "";
      item.change.title = "the quote carries no previous close";
      return;
    }
    item.change.textContent = (pct >= 0 ? "▲ " : "▼ ") + fmt.pct(Math.abs(pct));
    item.change.className = pct >= 0 ? "green" : "red";
    item.change.title = (quote.delayed === false ? "live" : "delayed") + " · " + (quote.source || "");
  }

  // ------------------------------------------------------------- watchlist

  /** Load the watchlists and render the default one. */
  async function loadWatchlist() {
    let payload;
    try {
      payload = await s.ctx.api.watchlists.list(null, { signal: s.ctx.signal });
    } catch (err) {
      if (cancelled(err)) return;
      s.nodes.watchBadge.textContent = "error";
      s.nodes.watchBadge.classList.add("red");
      ui.render(s.nodes.watchRows, el("div.list-row", el("span.red", { text: reason(err) })));
      return;
    }
    const lists = (payload && payload.watchlists) || [];
    const defaultId = payload && payload.default;
    s.watchlist = lists.filter(function (w) { return w.id === defaultId; })[0] || lists[0] || null;
    renderWatchRows();
    resubscribeTape();
  }

  /** Render the watch rows from the loaded watchlist, honouring the filter. */
  function renderWatchRows() {
    const n = s.nodes;
    const list = s.watchlist;
    const symbols = list ? (list.symbols || []) : [];
    // REFERENCE reads "6 / 50": watched symbols against the active universe.
    const universeSize = s.members ? s.members.length : 0;
    n.watchBadge.textContent = list
      ? (universeSize ? symbols.length + " / " + universeSize : String(symbols.length))
      : "none";
    n.watchBadge.title = list
      ? list.name + (universeSize
        ? " · " + symbols.length + " watched of " + universeSize + " in the active universe" : "")
      : "no watchlist exists yet";

    ui.clear(n.watchRows);
    s.watchNodes = new Map();

    if (!list) {
      n.watchRows.appendChild(el("div.list-row",
        el("b", { text: "No watchlist yet" }),
        el("span", { text: "Add a ticker above and TradingLib creates “Primary research” for it." })));
      return;
    }
    const shown = symbols.filter(function (symbol) {
      return !s.filter || symbol.indexOf(s.filter) === 0;
    });
    if (!shown.length) {
      n.watchRows.appendChild(el("div.list-row",
        el("span", { text: symbols.length ? "No symbol matches “" + s.filter + "”." : "This watchlist is empty." })));
      return;
    }
    shown.forEach(function (symbol) {
      const priceText = document.createTextNode("—");
      const change = el("span", { text: "…" });
      const remove = el("button", {
        type: "button",
        title: "Remove " + symbol + " from " + list.name,
        "aria-label": "Remove " + symbol,
        dataset: { remove: symbol },
        style: "background:none;border:0;color:var(--muted);cursor:pointer;padding:0 0 0 8px;font-size:11px",
        text: "✕",
      });
      const quote = el("span.watch-quote", priceText, remove, change);
      const row = el("div.watch-row", {
        role: "button", tabindex: "0", dataset: { symbol: symbol },
        "aria-label": "Open " + symbol,
      }, el("span", el("b", { text: symbol }),
        el("small", { text: sublabelFor(symbol), title: sublabelTitle(symbol) })), quote);
      if (symbol === s.symbol) row.classList.add("active");
      n.watchRows.appendChild(row);
      s.watchNodes.set(symbol, { price: priceText, change: change, row: row });
      const known = s.quotes.get(symbol);
      if (known) paintWatchRow(symbol, known);
    });
    fillNames(shown);
  }

  /** The company name for a symbol, when the universe members carry one. */
  function nameFor(symbol) {
    const name = s.memberNames.get(symbol);
    return name || "—";
  }

  /** Legal-form noise that a display name does not need. */
  const NAME_NOISE = new RegExp(
    "(,?\\s+(incorporated|corporation|company|holdings?|group|limited|"
    + "inc|corp|co|plc|nv|sa|ag|llc|lp|ltd|s\\.a|n\\.v)\\.?)+$", "i");

  /**
   * GICS sector names shortened to the forms the reference prints. Only the
   * label is shortened - the stored sector is untouched, and the full text is
   * still on the row's tooltip.
   */
  const SECTOR_SHORT = {
    "information technology": "Technology",
    "consumer discretionary": "Consumer Disc.",
    "consumer staples": "Staples",
    "communication services": "Communications",
    "health care": "Healthcare",
    "healthcare": "Healthcare",
    "real estate": "Real Estate",
    "financials": "Financials",
    "industrials": "Industrials",
    "materials": "Materials",
    "energy": "Energy",
    "utilities": "Utilities",
  };

  /** "NVIDIA Corporation" → "NVIDIA"; anything unrecognised is left alone. */
  function shortName(name) {
    if (!name || name === "—") return name;
    const trimmed = String(name)
      .replace(/^The\s+/i, "")
      .replace(NAME_NOISE, "")
      // "JPMorgan Chase & Co." and "Eli Lilly and Company" leave a dangling
      // conjunction once the legal form goes; drop it and any trailing comma.
      .replace(/\s*(&|and)$/i, "")
      .replace(/[\s,]+$/, "")
      .trim();
    return trimmed || name;
  }

  /** "Information Technology" → "Technology". */
  function shortSector(sector) {
    if (!sector) return sector;
    return SECTOR_SHORT[String(sector).trim().toLowerCase()] || sector;
  }

  /**
   * The watch-row sublabel REFERENCE shows: "NVIDIA · Technology". The sector
   * comes from the universe membership payload; a symbol the active universe
   * does not carry keeps the plain company name.
   *
   * Both halves are shortened to the reference's forms so the row stays two
   * lines: a legal name plus a full GICS sector wraps to three and makes the
   * whole panel taller than the reference's. The unabbreviated text is kept on
   * the element's title, so nothing is actually hidden.
   */
  function sublabelFor(symbol) {
    const name = shortName(nameFor(symbol));
    const sector = shortSector(s.memberSectors.get(symbol));
    return sector && name !== "—" ? name + " · " + sector : name;
  }

  /** The unabbreviated "name · sector", for the row tooltip. */
  function sublabelTitle(symbol) {
    const name = nameFor(symbol);
    const sector = s.memberSectors.get(symbol);
    return sector && name !== "—" ? name + " · " + sector : name;
  }

  /**
   * Fill in company names for watchlist symbols the universe does not carry.
   * `/api/research/resolve` is the vendor's symbol index (EDGAR fallback), so
   * this is a real lookup rather than a prettified ticker.
   * @param {string[]} symbols
   */
  async function fillNames(symbols) {
    const unknown = symbols.filter(function (symbol) {
      return !s.memberNames.has(symbol) && !s.nameLookups.has(symbol);
    });
    if (!unknown.length) return;
    let found = false;
    for (let i = 0; i < unknown.length; i += 1) {
      const symbol = unknown[i];
      s.nameLookups.add(symbol);
      try {
        const payload = await s.ctx.api.get("/api/research/resolve",
          { q: symbol, limit: 3 }, { signal: s.ctx.signal });
        if (!s) return;
        const results = (payload && payload.results) || [];
        const hit = results.filter(function (row) { return row.symbol === symbol; })[0];
        if (hit && hit.name) {
          s.memberNames.set(symbol, hit.name);
          found = true;
        }
      } catch (err) {
        if (cancelled(err)) return;
        console.warn("symbol lookup failed for " + symbol, err);
      }
    }
    if (found && s) renderWatchRows();
  }

  /** Paint one watch row from a quote payload. */
  function paintWatchRow(symbol, quote) {
    const node = s.watchNodes.get(symbol);
    if (!node) return;
    node.price.nodeValue = fmt.num(quote.price, 2) + " ";
    const pct = quote.change_pct;
    if (pct === null || pct === undefined) {
      node.change.textContent = "—";
      node.change.className = "";
      return;
    }
    node.change.textContent = fmt.pct(pct, { sign: true });
    node.change.className = pct >= 0 ? "green" : "red";
  }

  /** Click inside the watch rows: open a symbol, or remove it. */
  function onWatchClick(event) {
    const remove = event.target.closest("[data-remove]");
    if (remove) {
      event.stopPropagation();
      removeWatchSymbol(remove.dataset.remove);
      return;
    }
    const row = event.target.closest(".watch-row");
    if (row && row.dataset.symbol) selectSymbol(row.dataset.symbol);
  }

  /** Add the symbol in the add-form to the watchlist (creating one if needed). */
  async function addWatchSymbol() {
    const input = s.nodes.watchAddInput;
    const symbol = input.value.trim().toUpperCase();
    if (!symbol) return;
    const api = s.ctx.api;
    try {
      if (!s.watchlist) {
        s.watchlist = await api.watchlists.create(
          { name: "Primary research", symbols: [symbol], is_default: true },
          { signal: s.ctx.signal }
        );
      } else {
        s.watchlist = await api.post(
          "/api/watchlists/" + encodeURIComponent(s.watchlist.id) + "/symbols",
          { symbol: symbol }, { signal: s.ctx.signal }
        );
      }
    } catch (err) {
      if (cancelled(err)) return;
      s.ctx.toast.error("Could not add " + symbol + ": " + reason(err));
      return;
    }
    input.value = "";
    s.ctx.toast.ok(symbol + " added to " + s.watchlist.name);
    renderWatchRows();
    resubscribeTape();
  }

  /** Remove one symbol from the watchlist. */
  async function removeWatchSymbol(symbol) {
    if (!s.watchlist) return;
    try {
      s.watchlist = await s.ctx.api.del(
        "/api/watchlists/" + encodeURIComponent(s.watchlist.id)
          + "/symbols/" + encodeURIComponent(symbol),
        { signal: s.ctx.signal }
      );
    } catch (err) {
      if (cancelled(err)) return;
      s.ctx.toast.error("Could not remove " + symbol + ": " + reason(err));
      return;
    }
    s.ctx.toast(symbol + " removed from " + s.watchlist.name);
    renderWatchRows();
    resubscribeTape();
  }

  // ------------------------------------------------------------- live tape

  /** The symbol set the tape should carry: watchlist first, then the universe. */
  function tapeSymbols() {
    const out = [];
    const push = function (symbol) {
      const value = String(symbol || "").toUpperCase();
      if (value && out.indexOf(value) < 0) out.push(value);
    };
    if (s.watchlist) (s.watchlist.symbols || []).forEach(push);
    if (s.symbol) push(s.symbol);
    s.members.slice(0, TAPE_SYMBOLS).forEach(function (member) { push(member.symbol); });
    return out.slice(0, 30); // the server caps a tape cycle at 30 symbols
  }

  /** (Re)declare the tape subscription with the current symbol set. */
  function resubscribeTape() {
    if (!s) return;
    s.ctx.stream.subscribe("tape", { symbols: tapeSymbols() });
  }

  /** Handle one frame from the `tape` channel. */
  function onTape(payload, frame) {
    if (!s || !payload) return;
    const channel = (frame && frame.channel) || "";
    if (channel === "tape") {
      if (payload.event === "tape_error") {
        s.nodes.tapeLabel.title = "tape degraded: " + payload.detail;
      } else {
        s.nodes.tapeLabel.title = (payload.delayed === false ? "live feed" : "delayed feed")
          + " · " + (payload.feed || "") + " · " + (payload.symbols || []).length + " symbols";
      }
      return;
    }
    const symbol = channel.indexOf(".") > 0 ? channel.split(".")[1] : payload.symbol;
    if (!symbol) return;
    s.quotes.set(symbol, payload);
    paintTapeItem(symbol, payload);
    paintWatchRow(symbol, payload);
    if (symbol === s.symbol) applyLiveQuote(payload);
    renderBreadth();
  }

  /** Update the centre price (and the chart's last point) from a live quote. */
  function applyLiveQuote(quote) {
    const n = s.nodes;
    if (quote.price === null || quote.price === undefined) return;
    n.price.textContent = fmt.num(quote.price, 2);
    n.price.title = "live quote " + (quote.ts || "") + " · " + (quote.source || "");
    if (!s.chart || s.lastPointTime === null) return;
    const seconds = global.AlgoCharts.toSeconds(quote.ts);
    if (seconds === null) return;
    // Daily bars: pin the live point to the quote's own day (never earlier than
    // the newest bar) so successive quotes move one point instead of growing an
    // intraday tail on a one-year chart.
    const day = Math.max(Math.floor(seconds / 86400) * 86400, s.lastPointTime);
    s.chart.update({ time: day, value: quote.price });
  }

  // -------------------------------------------------------- centre symbol

  /** Load a symbol into the centre panel and the fundamentals panel. */
  function selectSymbol(symbol) {
    const next = String(symbol || "").trim().toUpperCase();
    if (!next) return;
    s.symbol = next;
    s.nodes.symbolInput.value = next;
    s.nodes.fundSymbol.textContent = next;
    if (s.watchNodes) {
      s.watchNodes.forEach(function (node, key) { node.row.classList.toggle("active", key === next); });
    }
    resubscribeTape();
    loadCompany(next);
    loadPrice(next);
  }

  /** Fetch the company payload and paint the price, name and fundamentals. */
  async function loadCompany(symbol) {
    const n = s.nodes;
    let payload;
    try {
      payload = await s.ctx.api.research.company(symbol, null, { signal: s.ctx.signal });
    } catch (err) {
      if (cancelled(err)) return;
      showChartNote(symbol + ": " + reason(err));
      n.price.textContent = "—";
      Object.keys(n.fund).forEach(function (key) { paintMetric(n.fund[key], null); });
      return;
    }
    if (!s || s.symbol !== symbol) return;
    hideChartNote();
    const quote = payload.quote || {};
    if (quote.price !== null && quote.price !== undefined) {
      n.price.textContent = fmt.num(quote.price, 2);
      n.price.title = (payload.profile && payload.profile.name ? payload.profile.name + " · " : "")
        + "quote " + (quote.ts || "") + " · " + (quote.source || "");
    }
    const metrics = payload.metrics || {};
    paintMetric(n.fund.market_cap, metrics.market_cap);
    paintMetric(n.fund.forward_pe, metrics.forward_pe);
    paintMetric(n.fund.revenue_yoy, metrics.revenue_yoy, { sign: true });
    paintMetric(n.fund.fcf_margin, metrics.fcf_margin, { sign: true });
    paintMetric(n.fund.return_on_invested_capital, metrics.return_on_invested_capital);
    paintMetric(n.fund.net_cash, metrics.net_cash);
    (payload.warnings || []).forEach(function (warning) {
      console.warn("research warning for " + symbol + ":", warning);
    });
  }

  /** Fetch a year of bars and draw the area chart plus the OHLC block. */
  async function loadPrice(symbol) {
    const n = s.nodes;
    let payload;
    try {
      payload = await s.ctx.api.research.price(symbol, "1y", { signal: s.ctx.signal });
    } catch (err) {
      if (cancelled(err)) return;
      showChartNote("price history for " + symbol + ": " + reason(err));
      return;
    }
    if (!s || s.symbol !== symbol) return;
    const points = payload.points || [];
    if (payload.error) showChartNote(String(payload.error));
    if (!points.length) {
      showChartNote("no price history for " + symbol);
      return;
    }
    const last = points[points.length - 1];
    n.ohlcOpen.textContent = fmt.num(last.open, 2);
    n.ohlcHigh.textContent = fmt.num(last.high, 2);
    n.ohlcLow.textContent = fmt.num(last.low, 2);
    n.ohlcVol.textContent = fmt.compact(last.volume, 1);
    n.ohlcVol.title = "session of " + fmt.etDate(last.time) + " · source " + (payload.source || "?");

    if (s.chart) s.chart.destroy();
    s.chart = global.AlgoCharts.priceChart(n.chartMount, { type: "area" });
    // REFERENCE draws its own grid behind the plot (.chart-grid), so the
    // library's grid stays off and the two do not overlay each other.
    s.chart.chart.applyOptions({
      grid: { vertLines: { visible: false }, horzLines: { visible: false } },
    });
    s.chart.setLine(points, "close").fit();
    s.lastPointTime = global.AlgoCharts.toSeconds(last.time);
  }

  function showChartNote(message) {
    const note = s.nodes.chartNote;
    note.style.display = "";
    note.firstChild.textContent = message;
  }

  function hideChartNote() {
    s.nodes.chartNote.style.display = "none";
  }

  // ------------------------------------------------------- market context

  /** Regime and the 200-day comparison, computed from real SPY closes. */
  async function loadMarketContext() {
    const n = s.nodes;
    let payload;
    try {
      payload = await s.ctx.api.research.price(FALLBACK_SYMBOL, "2y", { signal: s.ctx.signal });
    } catch (err) {
      if (cancelled(err)) return;
      n.ctxRegime.textContent = "—";
      n.ctxRegime.title = "SPY price history unavailable: " + reason(err);
      return;
    }
    if (!s) return;
    const closes = (payload.points || []).map(function (point) { return Number(point.close); })
      .filter(function (value) { return isFinite(value); });
    if (closes.length < REGIME_WINDOW) {
      n.ctxRegime.textContent = "—";
      n.ctxRegime.title = "only " + closes.length + " SPY closes available; "
        + REGIME_WINDOW + " are needed for the moving average";
      return;
    }
    const window = closes.slice(-REGIME_WINDOW);
    const average = window.reduce(function (a, b) { return a + b; }, 0) / window.length;
    const last = closes[closes.length - 1];
    const gap = last / average - 1;
    n.ctxRegime.textContent = gap >= 0 ? "RISK-ON" : "RISK-OFF";
    n.ctxRegime.className = gap >= 0 ? "green" : "red";
    n.ctxRegime.title = "SPY " + fmt.num(last, 2) + " vs its " + REGIME_WINDOW
      + "-day average " + fmt.num(average, 2) + " (source " + (payload.source || "?") + ")";
  }

  /** The VIX row: the index's own last close, from the price endpoint. */
  async function loadVix() {
    const n = s.nodes;
    let payload;
    try {
      payload = await s.ctx.api.research.price(VIX_SYMBOL, "1m", { signal: s.ctx.signal });
    } catch (err) {
      if (cancelled(err)) return;
      n.ctxVix.textContent = "—";
      n.ctxVix.title = VIX_SYMBOL + " unavailable: " + reason(err);
      return;
    }
    if (!s) return;
    const points = payload.points || [];
    const last = points.length ? Number(points[points.length - 1].close) : NaN;
    if (!isFinite(last)) {
      n.ctxVix.textContent = "—";
      n.ctxVix.title = "the vendor returned no " + VIX_SYMBOL + " closes";
      return;
    }
    n.ctxVix.textContent = fmt.num(last, 1);
    const previous = points.length > 1 ? Number(points[points.length - 2].close) : NaN;
    if (isFinite(previous)) {
      n.ctxVix.classList.remove("green", "red");
      n.ctxVix.classList.add(last <= previous ? "green" : "red");
    }
    n.ctxVix.title = "CBOE volatility index close " + fmt.num(last, 2)
      + (isFinite(previous) ? " · previous " + fmt.num(previous, 2) : "")
      + " · source " + (payload.source || "?")
      + (points[points.length - 1].time
        ? " · " + String(points[points.length - 1].time).slice(0, 10) : "");
  }

  /**
   * "S&P above 200D": the share of S&P 500 constituents closing above their OWN
   * 200-day average — index breadth, not the index against its own average.
   * Members without 200 stored sessions are excluded and named in the tooltip.
   */
  async function loadBreadth200() {
    const n = s.nodes;
    let payload;
    try {
      payload = await s.ctx.api.universes.breadth(BREADTH_UNIVERSE, { window: REGIME_WINDOW },
        { signal: s.ctx.signal });
    } catch (err) {
      if (cancelled(err)) return;
      n.ctxVs200.textContent = "—";
      n.ctxVs200.title = "constituent breadth unavailable: " + reason(err);
      return;
    }
    if (!s) return;
    const percent = payload.percent;
    if (percent === null || percent === undefined) {
      n.ctxVs200.textContent = "—";
      n.ctxVs200.title = payload.reason || "no breadth reading";
      return;
    }
    n.ctxVs200.textContent = fmt.pct(percent, { fraction: true, decimals: 0 });
    n.ctxVs200.className = percent >= 0.5 ? "green" : "red";
    n.ctxVs200.title = payload.above + " of " + payload.covered + " "
      + (payload.universe || BREADTH_UNIVERSE) + " constituents close above their own "
      + payload.window + "-day average"
      + (payload.requested > payload.covered
        ? " (" + (payload.requested - payload.covered) + " of " + payload.requested
          + " have fewer than " + payload.window + " stored sessions and are excluded)"
        : "")
      + (payload.as_of ? " · as of " + String(payload.as_of).slice(0, 10) : "");
  }

  /** Advance/decline across the quotes the live tape has delivered so far. */
  function renderBreadth() {
    const n = s.nodes;
    let advancing = 0;
    let declining = 0;
    s.quotes.forEach(function (quote) {
      const pct = quote.change_pct;
      if (pct === null || pct === undefined) return;
      if (pct > 0) advancing += 1;
      else if (pct < 0) declining += 1;
    });
    const counted = advancing + declining;
    if (!counted) {
      n.ctxBreadth.textContent = "—";
      n.ctxBreadth.title = "waiting for the first tape cycle";
      return;
    }
    if (declining === 0) {
      n.ctxBreadth.textContent = advancing + " / 0";
    } else {
      n.ctxBreadth.textContent = fmt.num(advancing / declining, 2);
    }
    n.ctxBreadth.className = advancing >= declining ? "green" : "red";
    n.ctxBreadth.title = advancing + " advancing, " + declining + " declining of "
      + counted + " quoted symbols on the live tape";
  }

  // ------------------------------------------------------------ intel tabs

  /** Load one intel tab the first time it is shown. */
  function loadIntel(key) {
    if (!s || s.intelLoaded[key]) return;
    s.intelLoaded[key] = true;
    if (key === "news") loadIntelNews();
    else if (key === "earnings") loadIntelEarnings();
    else if (key === "fred") loadIntelFred();
    else if (key === "trends") loadIntelTrends();
  }

  /** Market news, taken from the S&P 500 ETF's news feed. */
  async function loadIntelNews() {
    const panel = s.nodes.panelNews;
    let payload;
    try {
      payload = await s.ctx.api.research.news(FALLBACK_SYMBOL, { limit: 9 }, { signal: s.ctx.signal });
    } catch (err) {
      if (cancelled(err)) return;
      s.intelLoaded.news = false;
      ui.render(panel, el("div.list-row", el("span.red", { text: "Market news unavailable: " + reason(err) })));
      return;
    }
    if (!s) return;
    const items = payload.items || [];
    s.nodes.intelBadge.title = "news for " + FALLBACK_SYMBOL + " (S&P 500 ETF) via "
      + (payload.source || "?") + "; sentiment by " + (payload.scorer || "?");
    if (!items.length) {
      ui.render(panel, el("div.list-row", el("span", { text: "The vendor returned no market news." })));
      return;
    }
    const lead = items[0];
    const leadNode = el("article.news-lead",
      el("span.cyan", { style: "font-size:7px", text: "TOP MARKET STORY" }),
      el("h3", { text: lead.headline || "—" }),
      el("p", { text: lead.summary || "" }));
    if (lead.url) {
      leadNode.style.cursor = "pointer";
      leadNode.title = lead.url;
      leadNode.addEventListener("click", function () { global.open(lead.url, "_blank", "noopener"); });
    }
    const columns = [el("div"), el("div")];
    items.slice(1, 5).forEach(function (item, index) {
      columns[index < 2 ? 0 : 1].appendChild(newsRow(item));
    });
    ui.render(panel, el("div.intel-grid", leadNode, columns[0], columns[1]));
  }

  /** One `.list-row` for a news item, toned by its sentiment label. */
  function newsRow(item) {
    const tone = item.sentiment_label === "positive" ? "green"
      : (item.sentiment_label === "negative" ? "red" : "");
    const detail = [
      fmt.ago(item.published_at) + " ago",
      item.publisher || item.source || "",
      item.sentiment_label ? item.sentiment_label + " " + fmt.num(item.sentiment, 2) : "",
    ].filter(Boolean).join(" · ");
    const row = ui.listRow(item.headline || "—", detail, { detailTone: tone });
    if (item.url) {
      row.title = item.url;
      row.style.cursor = "pointer";
      row.addEventListener("click", function () { global.open(item.url, "_blank", "noopener"); });
    }
    return row;
  }

  /** Earnings calendar for the first few members of the active universe. */
  async function loadIntelEarnings() {
    const panel = s.nodes.panelEarnings;
    const symbols = s.members.slice(0, EARNINGS_SYMBOLS).map(function (m) { return m.symbol; });
    if (!symbols.length) {
      ui.render(panel, el("div.list-row", el("span", { text: "No universe members to look up." })));
      return;
    }
    ui.render(panel, el("div.list-row", el("span", {
      text: "Loading the next reporting dates for " + symbols.join(", ") + "…",
    })));

    const rows = [];
    await mapLimited(symbols, 2, async function (symbol) {
      try {
        const payload = await s.ctx.api.research.estimates(symbol, null, { signal: s.ctx.signal });
        const next = payload.next_earnings;
        if (next && next.date) {
          rows.push({
            symbol: symbol,
            date: next.date,
            days: next.days_away,
            eps: next.eps_estimate,
            revenue: revenueEstimate(payload),
          });
        }
      } catch (err) {
        if (!cancelled(err)) console.warn("earnings lookup failed for " + symbol, err);
      }
    });
    if (!s) return;
    rows.sort(function (a, b) { return String(a.date).localeCompare(String(b.date)); });

    ui.render(panel, ui.table({
      columns: [
        { label: "Date", render: function (row) { return fmt.etDate(row.date); } },
        { label: "Company", key: "symbol" },
        {
          label: "Timing",
          render: function (row) {
            const timing = sessionTiming(row.date);
            const node = el("span", { text: timing.text });
            node.title = timing.title;
            return node;
          },
        },
        { label: "Est. EPS", num: true, render: function (row) { return row.eps === null || row.eps === undefined ? "—" : fmt.usd(row.eps, { decimals: 2 }); } },
        { label: "Est. Revenue", num: true, render: function (row) { return row.revenue === null ? "—" : fmt.usd(row.revenue, { compact: true, decimals: 1 }); } },
      ],
      rows: rows,
      empty: "The vendor published no upcoming reporting dates for these symbols.",
    }));
  }

  /** The current-quarter consensus revenue from an estimates payload. */
  function revenueEstimate(payload) {
    const rows = payload.revenue_estimate || [];
    for (let i = 0; i < rows.length; i += 1) {
      if (rows[i] && rows[i].period === "0q" && isFinite(Number(rows[i].avg))) return Number(rows[i].avg);
    }
    return null;
  }

  /** BMO/AMC derived from the New York hour of the vendor's earnings timestamp. */
  function sessionTiming(value) {
    const date = fmt.toDate(value);
    if (!date) return { text: "—", title: "no timestamp" };
    const hour = Number(new Intl.DateTimeFormat("en-US", {
      timeZone: "America/New_York", hour12: false, hour: "2-digit",
    }).format(date));
    const clock = new Intl.DateTimeFormat("en-US", {
      timeZone: "America/New_York", hour12: false, hour: "2-digit", minute: "2-digit",
    }).format(date);
    if (hour >= 16) return { text: "AMC", title: "vendor timestamp " + clock + " ET (after the close)" };
    if (hour < 9) return { text: "BMO", title: "vendor timestamp " + clock + " ET (before the open)" };
    return { text: clock + " ET", title: "vendor timestamp during the session" };
  }

  /**
   * FRED macro levels from `/api/macro/series` — the Parquet store first, the
   * keyless FRED CSV endpoint when the stored copy is behind the publication
   * cadence. A series FRED will not serve renders "—" with its reason.
   */
  async function loadIntelFred() {
    const panel = s.nodes.panelFred;
    ui.render(panel, el("div.list-row", el("span", { text: "Loading FRED macro series…" })));
    let payload;
    try {
      payload = await s.ctx.api.macro.panel(null, { signal: s.ctx.signal });
    } catch (err) {
      if (cancelled(err)) return;
      ui.render(panel, el("div.list-row",
        el("span", { text: "FRED macro unavailable: " + reason(err) })));
      return;
    }
    if (!s) return;
    const series = payload.series || [];
    const grid = el("div.metric-grid");
    const notes = [];
    series.forEach(function (row) {
      const cell = metricCell(String(row.label || row.series_id).toUpperCase());
      if (row.value === null || row.value === undefined) {
        cell.value.textContent = "—";
        cell.value.title = row.reason || (row.series_id + ": no observations");
        notes.push(row.series_id + ": " + (row.reason || "no observations"));
      } else {
        const digits = Math.abs(Number(row.value)) >= 10 ? 1 : 2;
        cell.value.textContent = row.unit === "percent"
          ? fmt.num(row.value, digits) + "%"
          : fmt.num(row.value, digits);
        if (isFinite(Number(row.change))) {
          cell.value.classList.add(Number(row.change) >= 0 ? "green" : "red");
        }
        const bits = [row.series_id + " · " + (row.description || "")];
        if (row.as_of) bits.push("as of " + row.as_of);
        if (isFinite(Number(row.change)) && row.previous_as_of) {
          bits.push("previous " + fmt.num(row.previous, digits) + " (" + row.previous_as_of + ")");
        }
        if (isFinite(Number(row.change_1y))) {
          bits.push("year ago " + fmt.num(row.year_ago, digits));
        }
        bits.push("source " + (row.source || "fred") + " via " + (row.origin || "store"));
        cell.value.title = bits.join(" · ");
        if (row.stale && row.reason) notes.push(row.series_id + ": " + row.reason);
      }
      grid.appendChild(cell.cell);
    });
    const children = [grid];
    children.push(el("div.list-row", el("span", {
      text: payload.available + " of " + payload.total + " series carry a current level"
        + (notes.length ? " · " + notes.join(" · ") : "") + ".",
    })));
    ui.render(panel, children);
  }

  /** Google Trends interest for the first few universe members. */
  async function loadIntelTrends() {
    const panel = s.nodes.panelTrends;
    const symbols = s.members.slice(0, TRENDS_SYMBOLS).map(function (m) { return m.symbol; });
    if (!symbols.length) {
      ui.render(panel, el("div.list-row", el("span", { text: "No universe members to look up." })));
      return;
    }
    ui.render(panel, el("div.list-row", el("span", { text: "Loading Google Trends for " + symbols.join(", ") + "…" })));

    const grid = el("div.metric-grid");
    const notes = [];
    // Sequential: pytrends rate-limits hard, and a burst just yields 429s.
    for (let i = 0; i < symbols.length; i += 1) {
      const symbol = symbols[i];
      try {
        const payload = await s.ctx.api.research.altdata(symbol, null, { signal: s.ctx.signal });
        if (!s) return;
        const trends = (payload.signals || {}).google_trends || {};
        const cell = metricCell(String(trends.keyword || symbol).toUpperCase());
        if (trends.value === null || trends.value === undefined) {
          cell.value.textContent = "—";
          cell.value.title = trends.reason || "no Google Trends value";
        } else {
          cell.value.textContent = fmt.num(trends.value, 0);
          cell.value.title = (trends.unit || "") + " · " + (trends.geo || "") + " · " + (trends.timeframe || "");
        }
        grid.appendChild(cell.cell);
      } catch (err) {
        if (cancelled(err)) return;
        const cell = metricCell(symbol);
        cell.value.textContent = "—";
        cell.value.title = reason(err);
        grid.appendChild(cell.cell);
        notes.push(symbol + ": " + reason(err));
      }
    }
    if (!s) return;
    const children = [grid];
    if (notes.length) children.push(el("div.list-row", el("span.yellow", { text: notes.join(" · ") })));
    ui.render(panel, children);
  }

  // ---------------------------------------------------------- agent inbox

  /** The research inbox: real items when the agents backend serves them. */
  async function loadInbox() {
    const n = s.nodes;
    let payload;
    try {
      payload = await s.ctx.api.optional(s.ctx.api.agents.inbox(null, { signal: s.ctx.signal }));
    } catch (err) {
      if (cancelled(err)) return;
      n.inboxBadge.textContent = "error";
      n.inboxBadge.classList.add("red");
      ui.render(n.inboxBody, el("div.list-row", el("span.red", { text: reason(err) })));
      return;
    }
    if (!s) return;
    if (payload === null) {
      n.inboxBadge.textContent = "unavailable";
      n.inboxBadge.classList.add("yellow");
      n.inboxBadge.title = "GET /api/agents/inbox answered 404 in this build";
      ui.render(n.inboxBody, el("div.list-row",
        el("b", { text: "Agent inbox not served yet" }),
        el("span", { text: "GET /api/agents/inbox is not part of this build; nothing is shown rather than a placeholder queue." })));
      return;
    }
    const items = Array.isArray(payload) ? payload
      : (payload.items || payload.inbox || payload.results || []);
    n.inboxBadge.textContent = items.length + (items.length === 1 ? " item" : " items");
    n.inboxBadge.classList.toggle("green", items.length > 0);
    if (!items.length) {
      ui.render(n.inboxBody, el("div.list-row",
        el("b", { text: "Nothing needs a decision" }),
        el("span", { text: "The agent inbox is empty." })));
      return;
    }
    const body = el("div");
    items.slice(0, 6).forEach(function (item) {
      const urgency = String(item.urgency || item.severity || "").toLowerCase();
      const tone = urgency === "critical" ? "red" : (urgency === "high" ? "yellow" : "");
      const detail = [
        item.type || item.kind || "",
        item.symbol || "",
        item.source || item.run_id || "",
        item.age || (item.created_at ? fmt.ago(item.created_at) + " ago" : ""),
      ].filter(Boolean).join(" · ");
      const row = ui.listRow(item.title || item.summary || item.id || "—", detail, { leadTone: tone });
      if (item.reason) row.title = item.reason;
      // The backend hands each item the route and context the Agent Center
      // opens it with (approval / runs / findings), so the hand-off is its own.
      row.addEventListener("click", function () {
        s.ctx.router.go("agents", {
          sub: item.route || "inbox",
          params: item.context || {},
        });
      });
      row.style.cursor = "pointer";
      body.appendChild(row);
    });
    ui.render(n.inboxBody, body);
  }

  // ---------------------------------------------------------------- boot

  /** Load the active universe, then everything that depends on it. */
  async function loadUniverse() {
    let payload;
    try {
      payload = await s.ctx.api.universes.active({ signal: s.ctx.signal });
    } catch (err) {
      if (cancelled(err)) return;
      s.nodes.tapeLabel.textContent = "● UNIVERSE UNAVAILABLE";
      s.nodes.tapeLabel.title = reason(err);
      return;
    }
    if (!s) return;
    const scope = payload.scope || {};
    const universe = payload.universe || {};
    const symbols = payload.symbols || [];
    const scopeName = scope.name || universe.name || payload.active;
    s.members = symbols.map(function (symbol) { return { symbol: symbol, name: null }; });
    renderTape(scopeName);

    // Member names make the watch rows and tape tooltips real rather than blank.
    // Only a universe has a members endpoint: a watchlist is a bare symbol
    // list, and its names are filled in by the per-symbol resolve below.
    if (payload.active && scope.kind !== "watchlist") {
      try {
        const members = await s.ctx.api.universes.members(payload.active, null, { signal: s.ctx.signal });
        if (!s) return;
        const rows = (members && members.members) || [];
        if (rows.length) {
          s.members = rows.map(function (row) {
            return { symbol: row.symbol, name: row.name, sector: row.sector };
          });
          rows.forEach(function (row) {
            if (row.name) s.memberNames.set(row.symbol, row.name);
            if (row.sector) s.memberSectors.set(row.symbol, row.sector);
          });
          renderTape(scopeName);
          renderWatchRows();
        }
      } catch (err) {
        if (!cancelled(err)) console.warn("universe members failed", err);
      }
    }
    resubscribeTape();
    if (!s.symbol) selectSymbol(s.members.length ? s.members[0].symbol : FALLBACK_SYMBOL);
  }

  // -------------------------------------------------------------- module

  global.AlgoPages.register("overview", {
    /** @param {object} ctx router context (see ui/js/router.js) */
    mount: function (ctx) {
      s = {
        ctx: ctx,
        nodes: {},
        chart: null,
        lastPointTime: null,
        quotes: new Map(),
        members: [],
        memberNames: new Map(),
        memberSectors: new Map(),
        nameLookups: new Set(),
        tapeItems: new Map(),
        watchNodes: new Map(),
        watchlist: null,
        symbol: ctx.sub ? String(ctx.sub).toUpperCase() : null,
        filter: "",
        intelLoaded: {},
        offs: [],
      };
      build(ctx.root);
      if (s.symbol) s.nodes.symbolInput.value = s.symbol;

      // Live wiring: status (shell), tape (this page).
      s.offs.push(global.AlgoShell.onStatus(applyStatus));
      s.offs.push(ctx.stream.onState(function () {
        if (global.AlgoShell.status) applyStatus(global.AlgoShell.status);
      }));
      s.offs.push(ctx.stream.listen("tape", onTape, { symbols: [] }));
      s.offs.push(global.AlgoShell.onUniverseChange(function () {
        s.intelLoaded = {};
        loadUniverse();
      }));
      if (global.AlgoShell.status) applyStatus(global.AlgoShell.status);

      loadUniverse();
      loadWatchlist();
      loadMarketContext();
      loadBreadth200();
      loadVix();
      loadInbox();
      loadIntel("news");
      if (s.symbol) selectSymbol(s.symbol);
    },

    /** Same workspace, different `#overview/<SYMBOL>`. */
    route: function (ctx) {
      if (!s) return;
      s.ctx.sub = ctx.sub;
      if (ctx.sub) selectSymbol(String(ctx.sub).toUpperCase());
    },

    unmount: function (ctx) {
      if (!s) return;
      s.offs.forEach(function (off) {
        try {
          off();
        } catch (err) {
          console.warn("overview: teardown failed", err);
        }
      });
      if (s.chart) s.chart.destroy();
      ctx.stream.unsubscribe("tape");
      s = null;
    },
  });
})(window);
