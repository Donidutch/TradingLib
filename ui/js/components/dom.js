/* TradingLib — shared render helpers (window.AlgoUI).
 *
 * The SPA carries two class dialects, because the reference does:
 *
 *   "ac"  — the agent-center tree under #ac-root: .panel > .panel-title,
 *           .data-table, .badge, .list-row, .metric-grid > .metric, .stat-card
 *   "tl"  — the TL tree (#page-lab): .panel > .ptitle, .tbl, .chip, .kv
 *
 * Every builder takes a `tree` option ("ac" by default) and emits the matching
 * markup, so a page renders in whichever tree it lives in without inventing new
 * classes the stylesheets do not know.
 *
 * Everything here returns DOM nodes rather than HTML strings: text goes through
 * textContent, so no page can inject markup through a symbol or an API string.
 */
(function (global) {
  "use strict";

  /** Escape a value for the rare places that must build an HTML string. */
  function esc(value) {
    return String(value === undefined || value === null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  /**
   * Create an element.
   * @param {string} tag tag name, optionally with ".class.names"
   * @param {object|string|Node|Array|null} [attrs] attributes, or the children
   *   when it is a string/Node/Array. Special keys: `class`/`className`,
   *   `text`, `html` (trusted markup only), `style` (object or string),
   *   `dataset` (object), `on` ({event: handler}).
   * @param {...(string|Node|Array|null|undefined)} children
   * @returns {HTMLElement}
   */
  function el(tag, attrs) {
    let name = tag;
    let classes = [];
    const dot = tag.indexOf(".");
    if (dot > 0) {
      name = tag.slice(0, dot);
      classes = tag.slice(dot + 1).split(".").filter(Boolean);
    }
    const node = document.createElement(name);
    if (classes.length) node.classList.add.apply(node.classList, classes);

    let childStart = 2;
    const isChildish = attrs === null || attrs === undefined
      || typeof attrs === "string" || typeof attrs === "number"
      || attrs instanceof Node || Array.isArray(attrs);
    if (isChildish) {
      childStart = 1;
    } else {
      Object.keys(attrs).forEach(function (key) {
        const value = attrs[key];
        if (value === undefined || value === null || value === false) return;
        if (key === "class" || key === "className") {
          String(value).split(/\s+/).filter(Boolean).forEach(function (c) { node.classList.add(c); });
        } else if (key === "text") {
          node.textContent = String(value);
        } else if (key === "html") {
          node.innerHTML = String(value);
        } else if (key === "style") {
          if (typeof value === "string") node.setAttribute("style", value);
          else Object.keys(value).forEach(function (prop) { node.style[prop] = value[prop]; });
        } else if (key === "dataset") {
          Object.keys(value).forEach(function (prop) { node.dataset[prop] = String(value[prop]); });
        } else if (key === "on") {
          Object.keys(value).forEach(function (type) { node.addEventListener(type, value[type]); });
        } else if (value === true) {
          node.setAttribute(key, "");
        } else {
          node.setAttribute(key, String(value));
        }
      });
    }

    for (let i = childStart; i < arguments.length; i += 1) append(node, arguments[i]);
    return node;
  }

  /** Append a child of any accepted shape (string, Node, array, nullish). */
  function append(parent, child) {
    if (child === undefined || child === null || child === false) return;
    if (Array.isArray(child)) {
      child.forEach(function (item) { append(parent, item); });
      return;
    }
    if (child instanceof Node) {
      parent.appendChild(child);
      return;
    }
    parent.appendChild(document.createTextNode(String(child)));
  }

  /** Empty an element and return it. */
  function clear(node) {
    while (node && node.firstChild) node.removeChild(node.firstChild);
    return node;
  }

  /** Replace an element's children with `content`. */
  function render(node, content) {
    clear(node);
    append(node, content);
    return node;
  }

  /** @returns {DocumentFragment} */
  function frag(children) {
    const f = document.createDocumentFragment();
    append(f, children);
    return f;
  }

  // ------------------------------------------------------------------ badges

  /**
   * A small status chip. `tone` maps to the stylesheet's colour classes.
   * @param {string} text
   * @param {{tone?: "green"|"red"|"yellow"|"cyan"|"blue"|"", tree?: "ac"|"tl"}} [options]
   */
  function badge(text, options) {
    const opts = options || {};
    if (opts.tree === "tl") {
      const toneMap = { green: "grn", red: "red", yellow: "amb", blue: "blu", cyan: "blu" };
      const chip = el("span.chip", { text: text });
      if (opts.tone && toneMap[opts.tone]) chip.classList.add(toneMap[opts.tone]);
      if (opts.mini) chip.classList.add("mini");
      return chip;
    }
    const node = el("span.badge", { text: text });
    if (opts.tone) node.classList.add(opts.tone);
    return node;
  }

  // ------------------------------------------------------------------ panels

  /**
   * A titled panel.
   * @param {{title?: string|Node, badge?: string|Node, badgeTone?: string,
   *          body?: any, tree?: "ac"|"tl", className?: string,
   *          actions?: Node|Node[]}} options
   * @returns {HTMLElement} <section class="panel">
   */
  function panel(options) {
    const opts = options || {};
    const tree = opts.tree === "tl" ? "tl" : "ac";
    const section = el("section.panel");
    if (opts.className) {
      opts.className.split(/\s+/).filter(Boolean).forEach(function (c) { section.classList.add(c); });
    }
    if (opts.title !== undefined && opts.title !== null) {
      const head = el(tree === "tl" ? "div.ptitle" : "div.panel-title");
      append(head, opts.title);
      if (opts.badge !== undefined && opts.badge !== null) {
        append(head, typeof opts.badge === "string"
          ? badge(opts.badge, { tone: opts.badgeTone, tree: tree })
          : opts.badge);
      }
      if (opts.actions) append(head, opts.actions);
      section.appendChild(head);
    }
    if (opts.body !== undefined) append(section, opts.body);
    return section;
  }

  /**
   * A table.
   * @param {{columns: Array<string|{label: string, key?: string, num?: boolean,
   *            render?: (row:any, index:number) => any, className?: string}>,
   *          rows: any[], tree?: "ac"|"tl", empty?: string,
   *          rowAttrs?: (row:any, index:number) => object|null,
   *          className?: string}} options
   * @returns {HTMLElement} <table>
   */
  function table(options) {
    const opts = options || {};
    const tree = opts.tree === "tl" ? "tl" : "ac";
    const numClass = tree === "tl" ? "r" : "num";
    const columns = (opts.columns || []).map(function (column) {
      return typeof column === "string" ? { label: column } : column;
    });
    const rows = opts.rows || [];

    const node = el(tree === "tl" ? "table.tbl" : "table.data-table");
    if (opts.className) {
      opts.className.split(/\s+/).filter(Boolean).forEach(function (c) { node.classList.add(c); });
    }

    const headRow = el("tr");
    columns.forEach(function (column) {
      const th = el("th", { text: column.label });
      if (column.num) th.classList.add(numClass);
      if (column.className) th.classList.add(column.className);
      headRow.appendChild(th);
    });
    node.appendChild(el("thead", headRow));

    const body = el("tbody");
    if (!rows.length) {
      const td = el("td", { text: opts.empty || "No rows.", colspan: String(columns.length || 1) });
      td.classList.add(tree === "tl" ? "dim" : "muted");
      body.appendChild(el("tr", td));
    } else {
      rows.forEach(function (row, index) {
        const tr = el("tr");
        const attrs = opts.rowAttrs ? opts.rowAttrs(row, index) : null;
        if (attrs) {
          Object.keys(attrs).forEach(function (key) {
            if (key === "class") {
              String(attrs[key]).split(/\s+/).filter(Boolean).forEach(function (c) { tr.classList.add(c); });
            } else if (key === "dataset") {
              Object.keys(attrs[key]).forEach(function (p) { tr.dataset[p] = String(attrs[key][p]); });
            } else if (key === "on") {
              Object.keys(attrs[key]).forEach(function (t) { tr.addEventListener(t, attrs[key][t]); });
            } else {
              tr.setAttribute(key, String(attrs[key]));
            }
          });
        }
        columns.forEach(function (column) {
          const td = el("td");
          if (column.num) td.classList.add(numClass);
          if (column.className) td.classList.add(column.className);
          const value = column.render
            ? column.render(row, index)
            : (column.key ? row[column.key] : "");
          append(td, value === undefined || value === null ? "—" : value);
          tr.appendChild(td);
        });
        body.appendChild(tr);
      });
    }
    node.appendChild(body);
    return node;
  }

  /**
   * The `.metric-grid` block: a label above a value, repeated.
   * @param {Array<{label: string, value: any, tone?: string, id?: string}>} items
   */
  function metricGrid(items) {
    const grid = el("div.metric-grid");
    (items || []).forEach(function (item) {
      const value = el("b", { text: item.value === undefined || item.value === null ? "—" : String(item.value) });
      if (item.tone) value.classList.add(item.tone);
      if (item.id) value.id = item.id;
      grid.appendChild(el("div.metric", el("small", { text: item.label }), value));
    });
    return grid;
  }

  /** A `.stat-card` panel: one big number with a caption. */
  function statCard(label, value, options) {
    const opts = options || {};
    const b = el("b", { text: value === undefined || value === null ? "—" : String(value) });
    if (opts.tone) b.classList.add(opts.tone);
    const card = el("section.panel.stat-card", el("small", { text: label }), b);
    if (opts.extra) append(card, opts.extra);
    return card;
  }

  /** A `.list-row`: bold lead plus muted detail (ac tree). */
  function listRow(lead, detail, options) {
    const opts = options || {};
    const b = el("b");
    append(b, lead);
    if (opts.leadTone) b.classList.add(opts.leadTone);
    const span = el("span");
    append(span, detail);
    if (opts.detailTone) span.classList.add(opts.detailTone);
    const row = el("div.list-row", b, span);
    if (opts.onClick) {
      row.addEventListener("click", opts.onClick);
      row.style.cursor = "pointer";
    }
    return row;
  }

  /** A `.kv` row: label left, value right (tl tree). */
  function kv(label, value, options) {
    const opts = options || {};
    const right = el("span");
    append(right, value === undefined || value === null ? "—" : value);
    if (opts.tone) right.classList.add(opts.tone);
    const row = el("div.kv", el("span", { text: label }), right);
    if (opts.mini) row.classList.add("mini");
    // An optional tooltip: the number a row summarises is often worth stating
    // exactly (a sweep's cell count behind its batch count, say).
    if (opts.title) row.title = opts.title;
    return row;
  }

  /** A `.health-row`: label left, bold value right (ac tree). */
  function healthRow(label, value, options) {
    const opts = options || {};
    const right = el("b");
    append(right, value === undefined || value === null ? "—" : value);
    if (opts.tone) right.classList.add(opts.tone);
    return el("div.health-row", el("span", { text: label }), right);
  }

  /** A button in the local dialect. */
  function button(label, options) {
    const opts = options || {};
    const node = el("button.btn", { type: "button" });
    append(node, label);
    if (opts.primary) node.classList.add("primary");
    if (opts.small) node.classList.add("small");
    if (opts.className) {
      opts.className.split(/\s+/).filter(Boolean).forEach(function (c) { node.classList.add(c); });
    }
    if (opts.dataset) Object.keys(opts.dataset).forEach(function (k) { node.dataset[k] = String(opts.dataset[k]); });
    if (opts.onClick) node.addEventListener("click", opts.onClick);
    if (opts.disabled) node.disabled = true;
    return node;
  }

  /** The page header block used by the ac-tree workspaces. */
  function pageHead(kicker, title, right) {
    const left = el("div", el("small", { text: kicker }), el("h1", { text: title }));
    const head = el("div.page-head", left);
    if (right) append(head, right);
    return head;
  }

  /** A placeholder panel for "nothing here yet", stated plainly. */
  function emptyState(title, detail, options) {
    const opts = options || {};
    return panel({
      title: title,
      badge: opts.badge,
      badgeTone: opts.badgeTone,
      tree: opts.tree,
      body: el("div.list-row", el("span", { text: detail })),
    });
  }

  /**
   * An error panel that shows what actually failed. Never swallow the cause:
   * a page that cannot load its data says why.
   */
  function errorPanel(title, error, options) {
    const opts = options || {};
    const message = error && error.message ? error.message : String(error);
    return panel({
      title: title,
      badge: "error",
      badgeTone: "red",
      tree: opts.tree,
      body: el("div.list-row", el("span.red", { text: message })),
    });
  }

  // ---------------------------------------------------------------- formatting

  const NY_TIME = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York", hour12: false,
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
  const NY_DATE = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York", month: "short", day: "2-digit",
  });

  /** Current New York wall-clock time, "15:04:05". */
  function etClock(date) {
    return NY_TIME.format(date || new Date());
  }

  /**
   * "Aug 12" in New York.
   *
   * A date-only string ("2026-08-12") is a calendar date, not an instant:
   * parsing it as UTC midnight and then formatting in New York would show the
   * day before. Those are anchored at UTC noon so the calendar day survives.
   */
  function etDate(value) {
    if (typeof value === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value)) {
      const parsed = new Date(value + "T12:00:00Z");
      return isNaN(parsed.getTime()) ? "—" : NY_DATE.format(parsed);
    }
    const date = toDate(value);
    return date ? NY_DATE.format(date) : "—";
  }

  /** @param {any} value @returns {Date|null} */
  function toDate(value) {
    if (!value && value !== 0) return null;
    if (value instanceof Date) return isNaN(value.getTime()) ? null : value;
    if (typeof value === "number") return new Date(value < 1e12 ? value * 1000 : value);
    const date = new Date(String(value));
    return isNaN(date.getTime()) ? null : date;
  }

  /** Compact age: "12s", "4m", "3h", "2d", or "—" when unknown. */
  function ago(value, now) {
    const date = toDate(value);
    if (!date) return "—";
    const seconds = Math.max(0, Math.round(((now || Date.now()) - date.getTime()) / 1000));
    if (seconds < 60) return seconds + "s";
    if (seconds < 3600) return Math.round(seconds / 60) + "m";
    if (seconds < 86400) return Math.round(seconds / 3600) + "h";
    return Math.round(seconds / 86400) + "d";
  }

  /** Fixed-decimal number, or "—". */
  function num(value, decimals) {
    if (value === undefined || value === null || value === "" || isNaN(Number(value))) return "—";
    const digits = decimals === undefined ? 2 : decimals;
    return Number(value).toLocaleString("en-US", {
      minimumFractionDigits: digits, maximumFractionDigits: digits,
    });
  }

  /** Integer with thousands separators, or "—". */
  function int(value) {
    if (value === undefined || value === null || value === "" || isNaN(Number(value))) return "—";
    return Math.round(Number(value)).toLocaleString("en-US");
  }

  /** 1.23e9 → "1.23B". */
  function compact(value, decimals) {
    if (value === undefined || value === null || value === "" || isNaN(Number(value))) return "—";
    const n = Number(value);
    const abs = Math.abs(n);
    const digits = decimals === undefined ? 2 : decimals;
    const units = [[1e12, "T"], [1e9, "B"], [1e6, "M"], [1e3, "K"]];
    for (let i = 0; i < units.length; i += 1) {
      if (abs >= units[i][0]) return (n / units[i][0]).toFixed(digits) + units[i][1];
    }
    return n.toFixed(digits);
  }

  /** Money: 1234.5 → "$1.23K" (compact) or "$1,234.50". */
  function usd(value, options) {
    const opts = options || {};
    if (value === undefined || value === null || value === "" || isNaN(Number(value))) return "—";
    const n = Number(value);
    if (opts.compact) return (n < 0 ? "-$" : "$") + compact(Math.abs(n), opts.decimals);
    return (n < 0 ? "-$" : "$") + num(Math.abs(n), opts.decimals === undefined ? 2 : opts.decimals);
  }

  /**
   * Percentage. `fraction: true` means the input is 0.184 rather than 18.4.
   * `sign: true` prefixes "+" on positives.
   */
  function pct(value, options) {
    const opts = options || {};
    if (value === undefined || value === null || value === "" || isNaN(Number(value))) return "—";
    const n = Number(value) * (opts.fraction ? 100 : 1);
    const digits = opts.decimals === undefined ? 1 : opts.decimals;
    const body = Math.abs(n).toFixed(digits) + "%";
    if (n < 0) return "−" + body;
    return (opts.sign ? "+" : "") + body;
  }

  /** "green" for positive, "red" for negative, "" for zero/unknown. */
  function toneFor(value) {
    if (value === undefined || value === null || value === "" || isNaN(Number(value))) return "";
    const n = Number(value);
    if (n > 0) return "green";
    if (n < 0) return "red";
    return "";
  }

  /** Uppercase display of a machine status string, "—" when absent. */
  function statusText(value) {
    if (!value) return "—";
    return String(value).replace(/_/g, " ").toUpperCase();
  }

  global.AlgoUI = {
    esc: esc,
    el: el,
    append: append,
    clear: clear,
    render: render,
    frag: frag,
    panel: panel,
    table: table,
    badge: badge,
    metricGrid: metricGrid,
    statCard: statCard,
    listRow: listRow,
    kv: kv,
    healthRow: healthRow,
    button: button,
    pageHead: pageHead,
    emptyState: emptyState,
    errorPanel: errorPanel,
    fmt: {
      etClock: etClock,
      etDate: etDate,
      toDate: toDate,
      ago: ago,
      num: num,
      int: int,
      compact: compact,
      usd: usd,
      pct: pct,
      toneFor: toneFor,
      statusText: statusText,
    },
  };
})(window);
