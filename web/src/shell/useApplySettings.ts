import { useEffect } from "react";
import { useSettings } from "../api/hooks";

/**
 * Put the stored theme and density into effect on <html>, on every page and
 * from the first load, not only after a visit to Settings.
 */
export function useApplySettings(): void {
  const settings = useSettings();
  const theme = settings.data?.theme;
  const density = settings.data?.density;
  useEffect(() => {
    const root = document.documentElement;
    root.dataset.theme = theme ?? "dark";
    root.dataset.density = density ?? "compact";
  }, [theme, density]);
  useEffect(() => {
    if (settings.isError) console.warn("settings could not be loaded:", settings.error.message);
  }, [settings.isError, settings.error]);
}
