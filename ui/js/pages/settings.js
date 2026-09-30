/* TradingLib — Settings workspace, and the module that applies the settings.
 *
 * Two jobs live here:
 *
 * 1. The page: the REFERENCE's Settings view — "Appearance" (theme, density,
 *    timezone) and "Research defaults" (primary benchmark, default universe) as
 *    `.config-form > .panel.form-card > .form-row` — plus the API-keys card the
 *    SPEC requires. Every control writes to PUT /api/settings and every value
 *    is read back from GET /api/settings.
 *
 * 2. The applier, which runs once at load rather than on mount: theme, density
 *    and timezone have to be in force on whatever page the user lands on, not
 *    only after they visit Settings.
 *
 *    - theme/density set `data-theme`/`data-density` on <html> and are backed by
 *      the stylesheet injected below. The REFERENCE's theme toggle sets the
 *      attribute and nothing else renders differently; here the palette really
 *      changes, which is the dead-end SPEC section 1 asks to fix.
 *    - timezone takes over the topbar clock. app.js renders America/New_York
 *      into #clock every second; this module replaces that node with its own, so
 *      app.js keeps ticking a detached element and the clock on screen is the
 *      one the setting asks for. No file outside this module is touched.
 *
 * API keys are write-only end to end: the field posts a value, the response
 * carries only "configured / not configured" and where the value came from, and
 * nothing ever renders a key.
 */
(function (global) {
  "use strict";

  const THEME_STYLE_ID = "tradinglib-settings-theme";

  /** Light palette + density. Dark is the stylesheet's own default, so the only
   *  rules here are the overrides a non-default choice needs. */
  const THEME_CSS = [
    ':root[data-theme="light"]{',
    "  --bg:#f4f6fb; --panel:#ffffff; --panel2:#eef1f8; --inset:#e7ebf4;",
    "  --line:#d3dae8; --line2:#bfc9dc;",
    "  --txt:#26314a; --hi:#0d1424; --dim:#78849c; --mid:#4c5871;",
    "  --grn:#0f9d68; --red:#c8324a; --amb:#a5700a; --blu:#2f5fd0; --btn:#2456c8;",
    "}",
    ':root[data-theme="light"] .datastrip{background:#e9edf6}',
    ':root[data-theme="light"] .rail .navitem:hover{background:#e4e9f5}',
    ':root[data-theme="light"] .rail .navitem.active{background:#dde5f8}',
    ':root[data-theme="light"] .note{background:#eef2fb}',
    ':root[data-theme="light"] .rowitem.sel{background:#e6ecfa}',
    ':root[data-theme="light"] #ac-root .catalog-source[aria-pressed="true"]{background:#e4ecfd}',
    ':root[data-theme="light"] #ac-root .catalog-dataset[aria-pressed="true"]{background:#eef2fc}',
    ':root[data-theme="light"] #ac-root .protocol-pill.selected,',
    ':root[data-theme="light"] #ac-root .frequency-option[aria-pressed="true"]{',
    "  background:#dee7fb;color:#22437f}",
    ':root[data-theme="light"] #ac-root .segment-option.active{background:#dde5f8;color:#1b2b4d}',
    ':root[data-theme="light"] #collectionRows tr:hover{background:#eef2fb}',
    ':root[data-theme="light"] #collectionRows tr[aria-selected="true"]{background:#e2eafb}',
    ':root[data-theme="light"] ::selection{background:#c9d9ff;color:#0d1424}',

    /* density: comfortable opens the layout up; compact is the default face. */
    ':root[data-density="comfortable"] body{font-size:13px;line-height:1.65}',
    ':root[data-density="comfortable"] .main{padding:20px 22px 56px}',
    ':root[data-density="comfortable"] .panel{padding:16px}',
    ':root[data-density="comfortable"] #ac-root .list-row{padding:14px 15px}',
    ':root[data-density="comfortable"] #ac-root .data-table th,',
    ':root[data-density="comfortable"] #ac-root .data-table td{padding:10px 11px}',
    ':root[data-density="comfortable"] #ac-root .form-row{padding:12px 0}',
    ':root[data-density="comfortable"] .tbl td{padding:9px 10px}',
    ':root[data-density="comfortable"] .rail{width:196px}',
  ].join("\n");

  /** The applied settings, shared with any page that wants them. */
  const state = {
    /** @type {object|null} the last GET /api/settings payload */
    settings: null,
    /** @type {Array<Function>} */
    listeners: [],
  };

  function ensureThemeStyle() {
    if (document.getElementById(THEME_STYLE_ID)) return;
    const style = document.createElement("style");
    style.id = THEME_STYLE_ID;
    style.textContent = THEME_CSS;
    document.head.appendChild(style);
  }

  // ------------------------------------------------------------------- clock

  /** Owns #clock once a timezone is applied. See the file header. */
  const clock = {
    node: null,
    timer: 0,
    zone: "America/New_York",
    formatter: null,
    label: "ET",

    /** Short label for a zone: ET for New York, otherwise the city name. */
    labelFor: function (zone) {
      if (zone === "America/New_York") return "ET";
      if (zone === "UTC") return "UTC";
      const parts = String(zone).split("/");
      return (parts[parts.length - 1] || zone).replace(/_/g, " ");
    },

    apply: function (zone) {
      let formatter;
      try {
        formatter = new Intl.DateTimeFormat("en-GB", {
          timeZone: zone, hour12: false,
          hour: "2-digit", minute: "2-digit", second: "2-digit",
        });
      } catch (err) {
        console.warn("settings: unusable timezone " + zone, err);
        return;
      }
      this.zone = zone;
      this.formatter = formatter;
      this.label = this.labelFor(zone);

      if (!this.node) {
        const existing = document.getElementById("clock");
        if (!existing) return;
        // Take the element over: app.js keeps a reference to the old node and
        // goes on writing ET into it, harmlessly, off-screen.
        const replacement = existing.cloneNode(false);
        replacement.id = "clock";
        existing.replaceWith(replacement);
        this.node = replacement;
      }
      this.tick();
      if (!this.timer) {
        const self = this;
        this.timer = setInterval(function () { self.tick(); }, 1000);
      }
    },

    tick: function () {
      if (!this.node || !this.formatter) return;
      this.node.textContent = this.formatter.format(new Date()) + " " + this.label;
      this.node.title = this.zone;
    },
  };

  // ------------------------------------------------------------------- apply

  /**
   * Put a settings payload into effect and tell listeners.
   * @param {object} payload a GET/PUT /api/settings body
   */
  function applySettings(payload) {
    if (!payload) return;
    ensureThemeStyle();
    state.settings = payload;

    const root = document.documentElement;
    root.dataset.theme = payload.theme || "dark";
    root.dataset.density = payload.density || "compact";
    if (payload.timezone) clock.apply(payload.timezone);

    state.listeners.forEach(function (fn) {
      try {
        fn(payload);
      } catch (err) {
        console.error("settings listener failed", err);
      }
    });
    global.dispatchEvent(new CustomEvent("tradinglib:settings", { detail: payload }));
  }

  /** Load the settings once at boot so the whole shell is in the right skin. */
  function bootstrap() {
    if (!global.api) return;
    global.api.settings.get().then(applySettings).catch(function (err) {
      // A missing settings route is a real gap, not something to paper over.
      console.warn("settings could not be loaded: " + (err && err.message ? err.message : err));
    });
  }

  const AlgoSettings = {
    /** @returns {object|null} the last applied settings payload */
    get: function () { return state.settings; },
    /** @param {(settings: object) => void} fn @returns {() => void} unsubscribe */
    onChange: function (fn) {
      state.listeners.push(fn);
      return function () {
        const index = state.listeners.indexOf(fn);
        if (index >= 0) state.listeners.splice(index, 1);
      };
    },
    /** Re-read the settings from the API and apply them. */
    reload: function () {
      return global.api.settings.get().then(function (payload) {
        applySettings(payload);
        return payload;
      });
    },
    apply: applySettings,
  };

  // ======================================================================
  // the page
  // ======================================================================

  const page = {
    ctx: null,

    mount: function (ctx) {
      ensureThemeStyle();
      this.ctx = ctx;
      const ui = ctx.ui;
      ui.render(ctx.root, [
        ui.pageHead("SETTINGS", "Application preferences"),
        ui.panel({ title: "Settings", badge: "loading…", body: [] }),
      ]);
      this.load();
    },

    unmount: function () { this.ctx = null; },

    load: function () {
      const self = this;
      const ctx = this.ctx;
      ctx.api.settings.get({ signal: ctx.signal }).then(function (payload) {
        if (!self.ctx || ctx.signal.aborted) return;
        applySettings(payload);
        self.render(payload);
      }).catch(function (err) {
        if (err.cancelled || !self.ctx) return;
        ctx.ui.render(ctx.root, [
          ctx.ui.pageHead("SETTINGS", "Application preferences"),
          ctx.ui.errorPanel("Settings", err),
        ]);
      });
    },

    /**
     * Send a partial update and re-render from the server's answer, so the page
     * always shows the state the backend actually holds.
     */
    save: function (patch, description, control) {
      const self = this;
      const ctx = this.ctx;
      if (control) control.disabled = true;
      return ctx.api.settings.save(patch).then(function (payload) {
        applySettings(payload);
        self.render(payload);
        global.toast.ok(description);
        if (patch.default_universe) {
          // The default universe IS the active universe, so the topbar selector
          // has to follow it. Its value is set directly: dispatching a change
          // would make the shell POST /api/universes/active a second time.
          const shellSelect = document.getElementById("universeSelect");
          const active = payload.active_universe || patch.default_universe;
          if (shellSelect && shellSelect.value !== active) shellSelect.value = active;
          if (global.AlgoShell) global.AlgoShell.refreshStatus();
        }
        if (payload.restart_detail) global.toast.warn(payload.restart_detail, 6000);
        return payload;
      }).catch(function (err) {
        console.error("settings save failed", err);
        const detail = err && err.payload && err.payload.detail;
        global.toast.error(typeof detail === "string" ? detail
          : detail && detail.detail ? detail.detail
            : (err && err.message) || "could not save");
        self.load();
      }).then(function () {
        if (control) control.disabled = false;
      });
    },

    render: function (settings) {
      const self = this;
      const ctx = this.ctx;
      const ui = ctx.ui;
      const options = settings.options || {};

      // ---------------------------------------------------------- appearance
      const appearance = ui.panel({ title: "Appearance", className: "form-card" });

      const themeButton = ui.button(
        settings.theme === "light" ? "Switch to dark theme" : "Switch to light theme",
        {
          onClick: function (event) {
            const next = settings.theme === "light" ? "dark" : "light";
            self.save({ theme: next }, next === "light" ? "Light theme enabled"
              : "Dark theme enabled", event.currentTarget);
          },
        }
      );
      themeButton.id = "settingsTheme";
      appearance.appendChild(this.row("Theme", themeButton));

      appearance.appendChild(this.row("Density", this.select(
        (options.densities || ["compact", "comfortable"]).map(function (value) {
          return { value: value, label: value.charAt(0).toUpperCase() + value.slice(1) };
        }),
        settings.density,
        function (value, control) {
          self.save({ density: value }, "Density: " + value, control);
        }
      )));

      appearance.appendChild(this.row("Timezone", this.select(
        (options.timezones || []).map(function (zone) { return { value: zone, label: zone }; }),
        settings.timezone,
        function (value, control) {
          self.save({ timezone: value }, "Timezone: " + value, control);
        }
      )));
      appearance.appendChild(ui.el("div.list-row",
        ui.el("span.muted", {
          text: "The timezone drives the topbar clock and the timestamps in the workspaces."
            + " Market data itself is always stored in UTC.",
        })));

      // ---------------------------------------------------- research defaults
      const defaults = ui.panel({ title: "Research defaults", className: "form-card" });

      const benchmarks = (options.benchmarks || []).map(function (item) {
        return { value: item.symbol, label: item.label };
      });
      if (settings.benchmark && !benchmarks.filter(function (item) {
        return item.value === settings.benchmark;
      }).length) {
        benchmarks.unshift({ value: settings.benchmark, label: settings.benchmark });
      }
      defaults.appendChild(this.row("Primary benchmark", this.select(
        benchmarks, settings.benchmark,
        function (value, control) {
          self.save({ benchmark: value }, "Benchmark: " + value, control);
        }
      )));

      const universes = (options.universes || []).map(function (universe) {
        return { value: universe.id, label: universe.name };
      });
      defaults.appendChild(this.row("Default universe", this.select(
        universes.length ? universes : [{ value: settings.default_universe, label: settings.default_universe }],
        settings.default_universe,
        function (value, control) {
          self.save({ default_universe: value }, "Default universe: " + value, control);
        }
      )));
      defaults.appendChild(ui.el("div.list-row",
        ui.el("span.muted", {
          text: "The default universe is the active universe: it scopes the tape, the screener"
            + " and every agent run. The benchmark is the comparison series new backtests use.",
        })));

      // ------------------------------------------------------------ API keys
      const keys = ui.panel({
        title: "API keys",
        badge: (settings.keys || []).filter(function (key) { return key.configured; }).length
          + " / " + (settings.keys || []).length + " configured",
        className: "form-card",
      });
      (settings.keys || []).forEach(function (key) {
        keys.appendChild(self.keyRow(key));
      });
      keys.appendChild(ui.el("div.list-row",
        ui.el("span.muted", { text: settings.secret_storage || "" })));

      // ----------------------------------------------------------- providers
      const providers = ui.panel({ title: "Provider status", className: "form-card" });
      (settings.providers || []).forEach(function (provider) {
        const value = ui.el("b", {
          text: provider.configured ? "configured" : "not configured",
        });
        value.classList.add(provider.configured ? "green" : "yellow");
        const row = ui.el("div.form-row",
          ui.el("span", { text: provider.provider }), value);
        providers.appendChild(row);
        if (provider.note) {
          providers.appendChild(ui.el("div.list-row",
            ui.el("span.muted", { text: provider.note })));
        }
      });
      const budgets = settings.budgets || {};
      providers.appendChild(ui.el("div.list-row",
        ui.el("b", { text: "LLM budgets" }),
        ui.el("span", {
          text: "per run " + ctx.ui.fmt.usd(budgets.per_run_usd)
            + " · daily " + ctx.ui.fmt.usd(budgets.daily_usd)
            + " · monthly " + ctx.ui.fmt.usd(budgets.monthly_usd)
            + (settings.config_path ? " · from " + settings.config_path : ""),
        })));

      ui.render(ctx.root, [
        ui.pageHead("SETTINGS", "Application preferences"),
        ui.el("div.config-form", appearance, defaults),
        ui.el("div.config-form", keys, providers),
      ]);
    },

    /** One `.form-row`: label on the left, control on the right. */
    row: function (label, control) {
      return this.ctx.ui.el("div.form-row",
        this.ctx.ui.el("span", { text: label }), control);
    },

    /**
     * A `.select` bound to a change handler.
     * @param {Array<{value: string, label: string}>} options
     * @param {string} current
     * @param {(value: string, control: HTMLSelectElement) => void} onChange
     */
    select: function (options, current, onChange) {
      const select = this.ctx.ui.el("select.select");
      options.forEach(function (option) {
        const node = document.createElement("option");
        node.value = option.value;
        node.textContent = option.label;
        if (option.value === current) node.selected = true;
        select.appendChild(node);
      });
      select.addEventListener("change", function () { onChange(select.value, select); });
      return select;
    },

    /** One credential slot: a write-only field plus the real readback. */
    keyRow: function (key) {
      const self = this;
      const ui = this.ctx.ui;

      const input = ui.el("input.input", {
        type: "password",
        autocomplete: "off",
        placeholder: key.configured ? "configured — type to replace" : "not configured",
        "aria-label": key.label,
      });
      input.style.width = "100%";

      const save = ui.button("Save", {
        primary: true,
        onClick: function (event) {
          const value = input.value;
          if (!value) {
            global.toast.warn("Type a key first, or use Clear to remove the stored one");
            return;
          }
          const patch = { keys: {} };
          patch.keys[key.provider] = value;
          input.value = "";
          self.save(patch, key.label + " saved", event.currentTarget);
        },
      });
      const clear = ui.button("Clear", {
        disabled: !key.configured || key.source !== "stored",
        onClick: function (event) {
          const patch = { keys: {} };
          patch.keys[key.provider] = "";
          self.save(patch, key.label + " cleared", event.currentTarget);
        },
      });

      const status = ui.el("span", {
        text: key.configured
          ? "configured · " + (key.source || "unknown source")
            + (key.updated_at ? " · saved " + ui.fmt.etDate(key.updated_at) : "")
          : "not configured · reads " + key.env,
      });
      status.classList.add(key.configured ? "green" : "muted");

      const controls = ui.el("div", {
        style: "display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-top:6px",
      }, input, save, clear);
      input.style.flex = "1 1 180px";

      const row = ui.el("div.list-row",
        ui.el("b", { text: key.label }),
        status,
        controls);
      return row;
    },
  };

  global.AlgoSettings = AlgoSettings;
  global.AlgoPages.register("settings", {
    mount: function (ctx) { page.mount(ctx); },
    unmount: function (ctx) { page.unmount(ctx); },
  });

  // Apply the stored settings to the whole shell as soon as the API exists.
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bootstrap, { once: true });
  } else {
    bootstrap();
  }
})(window);
