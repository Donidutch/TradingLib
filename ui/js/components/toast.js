/* TradingLib — toast notifications (window.toast).
 *
 * One `#toast` element in the shell, driven here. The stylesheet does the
 * animation: `.toast` is transparent and `.toast.show` is visible, so showing a
 * toast is a class toggle plus a timer.
 *
 * Rapid calls queue rather than overwrite: an action's confirmation is not lost
 * because the next one arrived 200ms later.
 */
(function (global) {
  "use strict";

  const DEFAULT_MS = 2600;
  /** @type {Array<{message: string, tone: string, ms: number}>} */
  const queue = [];
  let showing = false;
  let hideTimer = 0;
  let clearTimer = 0;

  /** @returns {HTMLElement|null} */
  function element() {
    return document.getElementById("toast");
  }

  function next() {
    const node = element();
    if (!node) return;
    if (showing || queue.length === 0) return;
    const item = queue.shift();
    showing = true;

    node.textContent = item.message;
    node.classList.remove("green", "red", "yellow", "cyan");
    if (item.tone) node.classList.add(item.tone);
    // Force a reflow so a toast that follows another still animates in.
    void node.offsetWidth;
    node.classList.add("show");

    hideTimer = setTimeout(function () {
      node.classList.remove("show");
      clearTimer = setTimeout(function () {
        node.textContent = "";
        showing = false;
        next();
      }, 220);
    }, item.ms);
  }

  /**
   * Show a toast.
   * @param {string} message text (never markup)
   * @param {"green"|"red"|"yellow"|"cyan"|string} [tone] colour class
   * @param {number} [ms] visible duration
   */
  function toast(message, tone, ms) {
    if (!message) return;
    queue.push({
      message: String(message),
      tone: tone || "",
      ms: typeof ms === "number" ? ms : DEFAULT_MS,
    });
    // Cap the backlog: a burst of events must not queue a minute of toasts.
    while (queue.length > 4) queue.shift();
    next();
  }

  /** Convenience wrappers. */
  toast.ok = function (message, ms) { toast(message, "green", ms); };
  toast.warn = function (message, ms) { toast(message, "yellow", ms); };
  toast.error = function (message, ms) { toast(message, "red", ms || 4200); };

  /** Drop anything pending and hide the current toast immediately. */
  toast.clear = function () {
    queue.length = 0;
    clearTimeout(hideTimer);
    clearTimeout(clearTimer);
    const node = element();
    if (node) {
      node.classList.remove("show");
      node.textContent = "";
    }
    showing = false;
  };

  global.toast = toast;
})(window);
