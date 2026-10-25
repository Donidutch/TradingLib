"""Compare the vanilla console (``/``) with the React console (``/app``).

Each case loads one page of each console side by side, in frames of a single
headless Chromium on the same server, opens every tab in both, and compares
the figures on it (prices, percentages, multiples, counts, dates, and states
such as CURRENT or RISK-ON) as multisets. A figure on one side and not the
other is a difference; differences that are understood are listed in
``ALLOWED`` with the reason, everything else fails the run.

Usage (with the API running and ``web/`` built)::

    uv run python tools/compare_consoles.py                    # every case
    uv run python tools/compare_consoles.py stock --symbols AAPL,MSFT
    uv run python tools/compare_consoles.py --url http://127.0.0.1:8201

The script only reads pages. Chromium must be on PATH (``chromium``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any

import websockets

#: Seconds a page gets to load its data before it is read.
LOAD_SECONDS = 10
#: Seconds a tab gets after it is opened.
TAB_SECONDS = 6
#: Seconds the Overview gets: its tape needs one publisher cycle.
OVERVIEW_SECONDS = 20


@dataclass(frozen=True)
class Case:
    """One page in both consoles."""

    name: str
    old_url: str
    new_url: str
    #: Read once loaded, without opening tabs.
    old_root: str
    new_root: str
    #: Tab buttons and the visible panel, when the page has tabs.
    old_tabs: str | None = None
    old_panel: str | None = None
    new_tabs: str | None = None
    #: Controls whose labels are not figures (range switchers), hidden before reading.
    old_noise: tuple[str, ...] = ()
    new_noise: tuple[str, ...] = ()
    head: tuple[str, str] | None = None
    wait: int = LOAD_SECONDS


@dataclass(frozen=True)
class Allowed:
    """A known difference: which case and tab, which side, which figures, and why."""

    case: str
    tab: str
    side: str  # "old" or "new"
    figure: str  # a regular expression over one figure
    reason: str


ALLOWED: tuple[Allowed, ...] = (
    Allowed(
        "stock/*",
        "Financials",
        "old",
        r"\d{4}",
        "the old table keeps a year whose row is entirely null, because "
        "isFinite(Number(null)) is true in JavaScript; the port drops that year",
    ),
)


def lab_ids(url: str, limit: int = 3) -> dict[str, list[str]]:
    """The first few hypotheses and experiments on the server, for the lab cases."""
    out: dict[str, list[str]] = {"hypotheses": [], "experiments": []}
    for tab in out:
        try:
            payload = json.load(urllib.request.urlopen(f"{url}/api/lab/{tab}", timeout=30))
        except OSError:
            continue
        out[tab] = [row["id"] for row in payload.get(tab, [])[:limit]]
    return out


def cases(symbols: list[str], lab: dict[str, list[str]] | None = None) -> dict[str, list[Case]]:
    """The pages to compare, by group name."""
    overview = Case(
        name="overview",
        old_url="/#overview",
        new_url="/app/",
        old_root="#view-overview",
        new_root="main",
        wait=OVERVIEW_SECONDS,
    )
    stock = [
        Case(
            name=f"stock/{symbol}",
            old_url=f"/#stock/{symbol}",
            new_url=f"/app/stock/{symbol}",
            old_root="#view-stock",
            new_root="main",
            head=("#view-stock .stock-head", "main > div > div"),
            old_tabs="#view-stock .tabs .tab",
            old_panel="#view-stock .tab-panel.active",
            new_tabs="main [role=tab]",
            old_noise=("[data-range]",),
            new_noise=('[aria-label="Chart range"]',),
        )
        for symbol in symbols
    ]
    settings = Case(
        name="settings",
        old_url="/#settings",
        new_url="/app/settings",
        old_root="#view-settings",
        new_root="main",
    )
    docs = [
        Case(
            name=f"docs/{article}",
            old_url=f"/#docs/{article}",
            new_url=f"/app/docs/{article}",
            old_root="#view-docs",
            new_root="main",
        )
        for article in ("hypotheses", "backtests", "validation", "shortcuts", "autonomy")
    ]
    data = [
        Case(
            name=f"data/{tab}",
            old_url=f"/#data/{tab}",
            new_url=f"/app/data/{tab}",
            old_root="#view-data .tab-panel.active",
            new_root="main [role=tabpanel]",
            # Coverage and the screener's per-rule previews take a while on a cold cache.
            wait=30,
        )
        for tab in ("collections", "screener", "source-catalog", "ingestion", "quality")
    ]
    lab_cases = [
        Case(
            name=f"lab/{tab}" + (f"/{entity}" if entity else ""),
            old_url=f"/#tl/lab/{tab}" + (f"?id={entity}" if entity else ""),
            new_url=f"/app/lab/{tab}" + (f"?id={entity}" if entity else ""),
            old_root="#page-lab",
            new_root="main",
            wait=15,
        )
        for tab, ids in (lab or {"hypotheses": [], "experiments": []}).items()
        for entity in [None, *ids]
    ]
    return {"overview": [overview], "stock": stock, "settings": [settings], "docs": docs, "data": data, "lab": lab_cases}


#: Runs inside the harness page; compares one case and returns the result.
PAGE_SCRIPT = r"""
async (c) => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const TOKEN = /[-+]?\$?\d[\d,]*(?:\.\d+)?(?:[%×]|[TBMK]\b)?|\b(?:CURRENT|STALE|RISK-ON|RISK-OFF|DEGRADED|DELAYED|LIVE|IDLE|HEALTHY)\b/g;
  const frame = (src) => {
    const f = document.createElement("iframe");
    f.style.cssText = "width:1440px;height:960px";
    f.src = src;
    document.body.appendChild(f);
    return f;
  };
  // Hide controls whose labels are not figures, read, then restore.
  const read = (doc, selector, noise) => {
    const root = doc.querySelector(selector);
    if (!root) return null;
    const hidden = noise.flatMap((n) => [...root.querySelectorAll(n)]);
    hidden.forEach((el) => (el.style.display = "none"));
    const text = root.innerText;
    hidden.forEach((el) => (el.style.display = ""));
    return (text.toUpperCase().replace(/−/g, "-").match(TOKEN) || []).sort();
  };
  const o = frame(c.old_url), n = frame(c.new_url);
  await sleep(c.wait * 1000);
  const od = o.contentDocument, nd = n.contentDocument;
  const out = {};
  if (c.head) out.head = [read(od, c.head[0], []), read(nd, c.head[1], [])];
  if (!c.old_tabs) {
    out.page = [read(od, c.old_root, c.old_noise), read(nd, c.new_root, c.new_noise)];
  } else {
    for (const ot of od.querySelectorAll(c.old_tabs)) {
      const name = ot.textContent.trim();
      const nt = [...nd.querySelectorAll(c.new_tabs)].find((t) => t.textContent.trim() === name);
      if (!nt) { out[name] = [read(od, c.old_root, c.old_noise), null]; continue; }
      ot.click();
      nt.click();
      await sleep(c.tab_wait * 1000);
      out[name] = [
        read(od, c.old_panel, c.old_noise),
        read(nd, `[role=tabpanel][aria-label="${name}"]`, c.new_noise),
      ];
    }
  }
  o.remove();
  n.remove();
  return out;
}
"""


@dataclass
class Result:
    case: str
    area: str
    figures: int
    only_old: list[str] = field(default_factory=list)
    only_new: list[str] = field(default_factory=list)
    allowed: list[str] = field(default_factory=list)
    missing: str | None = None

    @property
    def ok(self) -> bool:
        return not self.missing and not self.only_old and not self.only_new


def compare(case: str, area: str, old: list[str] | None, new: list[str] | None) -> Result:
    """Multiset difference of two figure lists, with known differences set aside."""
    if old is None or new is None:
        side = "old" if old is None else "new"
        return Result(case, area, 0, missing=f"the {side} page has no {area!r}")
    rest = list(new)
    only_old: list[str] = []
    for figure in old:
        if figure in rest:
            rest.remove(figure)
        else:
            only_old.append(figure)
    result = Result(case, area, len(old), only_old=only_old, only_new=rest)
    for rule in ALLOWED:
        pattern = "^" + re.escape(rule.case).replace(r"\*", ".*") + "$"
        if not re.match(pattern, case) or rule.tab != area:
            continue
        side = result.only_old if rule.side == "old" else result.only_new
        kept = [f for f in side if not re.fullmatch(rule.figure, f)]
        if len(kept) < len(side):
            result.allowed.append(f"{rule.side}: {sorted(set(side) - set(kept))} — {rule.reason}")
            side[:] = kept
    return result


class Browser:
    """One headless Chromium driven over the DevTools protocol."""

    def __init__(self) -> None:
        exe = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")
        if not exe:
            sys.exit("compare_consoles: no chromium on PATH")
        self.profile = tempfile.TemporaryDirectory(prefix="compare-consoles-")
        self.proc = subprocess.Popen(
            [exe, "--headless=new", "--disable-gpu", "--no-sandbox", "--remote-debugging-port=9334",
             f"--user-data-dir={self.profile.name}", "--window-size=1440,960", "about:blank"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.ws: Any = None
        self.counter = 0

    async def __aenter__(self) -> Browser:
        for _ in range(100):
            try:
                targets = json.load(urllib.request.urlopen("http://127.0.0.1:9334/json"))
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("chromium did not open its debugging port")
        page = next(t for t in targets if t["type"] == "page")
        self.ws = await websockets.connect(page["webSocketDebuggerUrl"], max_size=50_000_000)
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.ws.close()
        self.proc.terminate()
        self.proc.wait(timeout=10)
        self.profile.cleanup()

    async def call(self, method: str, **params: Any) -> dict[str, Any]:
        self.counter += 1
        await self.ws.send(json.dumps({"id": self.counter, "method": method, "params": params}))
        while True:
            message = json.loads(await self.ws.recv())
            if message.get("id") == self.counter:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error']}")
                return message["result"]

    async def run_case(self, case: Case) -> dict[str, list[list[str] | None]]:
        argument = json.dumps({**case.__dict__, "tab_wait": TAB_SECONDS})
        result = await self.call(
            "Runtime.evaluate",
            expression=f"({PAGE_SCRIPT})({argument})",
            awaitPromise=True,
            returnByValue=True,
        )
        if "exceptionDetails" in result:
            raise RuntimeError(f"{case.name}: {result['exceptionDetails'].get('text')}")
        return result["result"]["value"]


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("groups", nargs="*", help="case groups to run (default: all)")
    parser.add_argument("--url", default="http://127.0.0.1:8200", help="server serving both consoles")
    parser.add_argument("--symbols", default="AAPL,MSFT,NVDA,SPY,GRAB", help="tickers for Stock Research")
    args = parser.parse_args(argv)

    groups = cases([s.strip().upper() for s in args.symbols.split(",") if s.strip()], lab_ids(args.url))
    chosen = args.groups or list(groups)
    unknown = [g for g in chosen if g not in groups]
    if unknown:
        parser.error(f"unknown group(s) {unknown}; choose from {list(groups)}")

    results: list[Result] = []
    async with Browser() as browser:
        await browser.call("Page.navigate", url=f"{args.url}/api/health")
        await asyncio.sleep(1)
        for group in chosen:
            for case in groups[group]:
                print(f"comparing {case.name} …", flush=True)
                for area, (old, new) in (await browser.run_case(case)).items():
                    results.append(compare(case.name, area, old, new))

    failed = [r for r in results if not r.ok]
    for r in results:
        mark = "ok  " if r.ok else "DIFF"
        print(f"{mark} {r.case:<14} {r.area:<22} {r.figures:>4} figures")
        for note in r.allowed:
            print(f"       allowed {note}")
        if r.missing:
            print(f"       {r.missing}")
        if r.only_old:
            print(f"       only in the old console: {r.only_old}")
        if r.only_new:
            print(f"       only in the React console: {r.only_new}")
    total = sum(r.figures for r in results)
    print(f"\n{len(results) - len(failed)} of {len(results)} areas match ({total} figures compared).")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
