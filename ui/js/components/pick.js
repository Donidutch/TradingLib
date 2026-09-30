/* TradingLib — PICK mode (topbar: PICK · count · Export).
 *
 * Marks panels across the workspaces and exports the selection as JSON, so a
 * layout discussion can name panels unambiguously instead of describing them.
 *
 * Panel titles are not unique, so a mark is keyed by position within the active
 * workspace: "stock.p2" is the second .panel on Stock Research, "agents.runs.p3"
 * the third .panel of the Agent Center's runs sub-view. Marks survive reloads in
 * localStorage and are re-applied whenever the DOM changes, because pages
 * re-render their own contents.
 *
 * The dialog styles are injected here rather than living in ui/css/, which holds
 * the REFERENCE style blocks verbatim, one file per original block (SPEC D1a).
 */
(function (global) {
  "use strict";

  const STORE_KEY = "tradinglib-picks";

  const STYLE = `
    .pick-bar{display:flex;align-items:center;gap:8px}
    .pick-btn{appearance:none;background:transparent;border:1px solid var(--line2);
      border-radius:6px;color:var(--mid);font:inherit;padding:3px 8px;cursor:pointer}
    .pick-btn:hover{color:var(--txt);border-color:var(--dim)}
    .pick-btn.on{background:#101d33;border-color:var(--blu);color:var(--blu)}
    .pick-count{color:var(--grn)}

    body.pickmode .panel{outline:1px dashed rgba(126,166,255,.45);outline-offset:-1px;cursor:copy}
    body.pickmode .panel:hover{outline:2px solid var(--blu);outline-offset:-2px}
    body.pickmode #ac-root,body.pickmode #tl-root{user-select:none}
    .panel.picked{outline:2px solid var(--grn)!important;outline-offset:-2px!important;position:relative}
    .panel.picked::after{content:"✓ " attr(data-pick-n);position:absolute;top:4px;right:6px;
      background:var(--grn);color:#04150e;font:10px/1 ui-monospace,monospace;
      padding:3px 5px;border-radius:4px;z-index:9;pointer-events:none}

    #pickDialog{background:var(--panel);color:var(--txt);border:1px solid var(--line2);
      border-radius:12px;padding:0;max-width:760px;width:92vw}
    #pickDialog::backdrop{background:rgba(4,7,12,.72)}
    #pickDialog .hd{display:flex;justify-content:space-between;align-items:center;
      padding:12px 14px;border-bottom:1px solid var(--line2)}
    #pickDialog .bd{padding:14px}
    #pickDialog textarea{width:100%;height:44vh;background:var(--inset);color:var(--txt);
      border:1px solid var(--line2);border-radius:8px;padding:10px;
      font:11px/1.5 ui-monospace,monospace;resize:vertical}
    #pickDialog .ft{display:flex;gap:8px;justify-content:flex-end;padding:0 14px 14px}
  `;

  /** @type {Record<string, {page:string, sub:string, id:string, title:string}>} */
  let picks = {};
  let active = false;
  let applying = false;
  let countEl = null;
  let toggleBtn = null;
  let dialog = null;
  let pendingApply = 0;

  function load() {
    try {
      const raw = global.localStorage.getItem(STORE_KEY);
      picks = raw ? JSON.parse(raw) : {};
    } catch (err) {
      console.warn("pick mode: cannot read stored picks", err);
      picks = {};
    }
  }

  function persist() {
    try {
      global.localStorage.setItem(STORE_KEY, JSON.stringify(picks));
    } catch (err) {
      console.warn("pick mode: cannot persist picks", err);
    }
  }

  /** The workspace/sub-view a mark belongs to, read from the router. */
  function context() {
    const route = global.AlgoRouter && global.AlgoRouter.current;
    return {
      page: route ? route.key : "overview",
      sub: route && route.sub ? route.sub : "",
    };
  }

  /** The visible workspace container, in whichever tree is showing. */
  function activeSection() {
    const tlRoot = document.getElementById("tl-root");
    if (tlRoot && !tlRoot.hidden) return tlRoot.querySelector(".page.active");
    const acRoot = document.getElementById("ac-root");
    return acRoot ? acRoot.querySelector(".view.active") : null;
  }

  /** @returns {HTMLElement[]} */
  function panelsOnPage() {
    const section = activeSection();
    return section ? Array.prototype.slice.call(section.querySelectorAll(".panel")) : [];
  }

  function idFor(ctx, index) {
    return [ctx.page, ctx.sub, "p" + index].filter(Boolean).join(".");
  }

  /** A readable caption; panels without a title fall back to leading text. */
  function titleOf(panel) {
    const clean = function (s) { return (s || "").trim().replace(/\s+/g, " "); };
    const title = panel.querySelector(".ptitle, .panel-title, .v5-panel-title, h2, h3");
    if (title) return clean(title.textContent).slice(0, 60);
    return "(untitled) " + clean(panel.textContent).slice(0, 44);
  }

  /** Re-paint the marks for whatever is on screen now. */
  function apply() {
    if (applying) return;
    applying = true;
    const ctx = context();
    const ids = Object.keys(picks);
    document.querySelectorAll(".panel.picked").forEach(function (panel) {
      panel.classList.remove("picked");
      panel.removeAttribute("data-pick-n");
    });
    panelsOnPage().forEach(function (panel, index) {
      const id = idFor(ctx, index);
      if (!picks[id]) return;
      panel.classList.add("picked");
      panel.setAttribute("data-pick-n", String(ids.indexOf(id) + 1));
    });
    if (countEl) countEl.textContent = String(ids.length);
    applying = false;
  }

  function toggle(panel) {
    const ctx = context();
    const index = panelsOnPage().indexOf(panel);
    if (index < 0) return;
    const id = idFor(ctx, index);
    if (picks[id]) delete picks[id];
    else picks[id] = { page: ctx.page, sub: ctx.sub, id: id, title: titleOf(panel) };
    persist();
    apply();
  }

  const manifest = function () { return JSON.stringify(Object.values(picks), null, 2); };

  function buildDialog() {
    dialog = document.createElement("dialog");
    dialog.id = "pickDialog";
    dialog.innerHTML = ''
      + '<div class="hd"><strong>Selected panels</strong>'
      + '<button class="pick-btn" id="pickClose" type="button">Close</button></div>'
      + '<div class="bd"><textarea id="pickJson" readonly spellcheck="false"></textarea></div>'
      + '<div class="ft">'
      + '<button class="pick-btn" id="pickClear" type="button">Clear all</button>'
      + '<button class="pick-btn" id="pickCopy" type="button">Copy</button>'
      + '<button class="pick-btn" id="pickDownload" type="button">Download .json</button></div>';
    document.body.appendChild(dialog);

    dialog.querySelector("#pickClose").addEventListener("click", function () { dialog.close(); });
    dialog.querySelector("#pickCopy").addEventListener("click", function () {
      const area = dialog.querySelector("#pickJson");
      area.select();
      if (global.navigator.clipboard) {
        global.navigator.clipboard.writeText(area.value).then(function () {
          if (global.toast) global.toast.ok("Manifest copied");
        }).catch(function (err) {
          console.warn("clipboard write failed", err);
          if (global.toast) global.toast.error("Could not copy to the clipboard");
        });
      }
    });
    dialog.querySelector("#pickDownload").addEventListener("click", function () {
      const url = URL.createObjectURL(new Blob([manifest()], { type: "application/json" }));
      const link = document.createElement("a");
      link.href = url;
      link.download = "tradinglib-picks.json";
      link.click();
      URL.revokeObjectURL(url);
    });
    dialog.querySelector("#pickClear").addEventListener("click", function () {
      picks = {};
      persist();
      apply();
      dialog.querySelector("#pickJson").value = manifest();
    });
  }

  /** Install the topbar controls and the marking behaviour. Call once. */
  function init() {
    const host = document.querySelector(".topbar .top-right");
    if (!host) {
      console.error("pick mode: .topbar .top-right is missing");
      return;
    }
    load();

    const style = document.createElement("style");
    style.textContent = STYLE;
    document.head.appendChild(style);

    const bar = document.createElement("span");
    bar.className = "pick-bar";
    bar.innerHTML = ''
      + '<button class="pick-btn" id="pickToggle" type="button" aria-pressed="false">PICK</button>'
      + '<span class="pick-count" id="pickCount" title="panels selected">0</span>'
      + '<button class="pick-btn" id="pickExport" type="button">Export</button>';
    host.prepend(bar);

    countEl = bar.querySelector("#pickCount");
    toggleBtn = bar.querySelector("#pickToggle");

    toggleBtn.addEventListener("click", function () {
      active = !active;
      document.body.classList.toggle("pickmode", active);
      toggleBtn.classList.toggle("on", active);
      toggleBtn.setAttribute("aria-pressed", String(active));
      apply();
    });

    buildDialog();
    bar.querySelector("#pickExport").addEventListener("click", function () {
      dialog.querySelector("#pickJson").value = manifest();
      dialog.showModal();
    });

    // Capture phase: while picking, a click marks a panel instead of running
    // the page's own handler. The rail and topbar stay live so you can keep
    // navigating with pick mode on.
    document.addEventListener("click", function (event) {
      if (!active) return;
      if (event.target.closest("#rail, .topbar, #pickDialog, .palette")) return;
      const panel = event.target.closest(".panel");
      if (!panel) return;
      event.preventDefault();
      event.stopPropagation();
      toggle(panel);
    }, true);

    // Pages re-render their own contents, so marks are re-applied on any DOM
    // change rather than set once.
    const layout = document.querySelector(".layout");
    if (layout && typeof MutationObserver !== "undefined") {
      new MutationObserver(function () {
        if (applying) return;
        clearTimeout(pendingApply);
        pendingApply = setTimeout(apply, 60);
      }).observe(layout, {
        subtree: true, childList: true, attributes: true, attributeFilter: ["class", "hidden"],
      });
    }
    if (global.AlgoRouter && typeof global.AlgoRouter.onNavigate === "function") {
      global.AlgoRouter.onNavigate(function () { setTimeout(apply, 60); });
    }
    apply();
  }

  global.AlgoPick = {
    init: init,
    /** @returns {object[]} the current manifest entries */
    picks: function () { return Object.values(picks); },
    apply: apply,
  };
})(window);
