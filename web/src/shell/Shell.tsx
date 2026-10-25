/* The console chrome around every page: topbar, datastrip, rail, palette and
 * the live wiring. Owns nothing inside a page. */
import { Outlet } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { useApi } from "../api/context";
import { Datastrip } from "./Datastrip";
import { dataIndicator, engineIndicator } from "./health";
import { Palette } from "./Palette";
import { Rail } from "./Rail";
import { Topbar } from "./Topbar";
import { useShellLive } from "./useShellLive";
import "./pick.css";

export function Shell() {
  const { client } = useApi();
  const { status, stream, apiOnline } = useShellLive();
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [main, setMain] = useState<HTMLElement | null>(null);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key.toLowerCase() === "k" && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        setPaletteOpen((open) => !open);
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  const health = { status, stream, apiOnline, apiError: client.lastError?.message ?? null };

  return (
    <div className="flex min-h-full flex-col">
      <Topbar engine={engineIndicator(health)} onSearch={() => setPaletteOpen(true)} main={main} />
      <Datastrip status={status} data={dataIndicator(health)} />
      <div className="flex flex-1">
        <Rail />
        <main ref={setMain} tabIndex={-1} className="min-w-0 flex-1 px-4 pb-10 pt-3.5">
          <Outlet />
        </main>
      </div>
      <Palette open={paletteOpen} onClose={() => setPaletteOpen(false)} />
    </div>
  );
}
