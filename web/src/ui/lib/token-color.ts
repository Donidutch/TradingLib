/**
 * Resolve a design-system color token to its current literal value (hex/rgb).
 * Use for canvas/SVG-attribute consumers (lightweight-charts, recharts) that
 * cannot read CSS `var()`. Re-read on theme change — e.g. key a useMemo on
 * `useTheme().theme` — so charts re-color when the theme flips.
 *
 *   tokenColor("up")            -> the value of --color-up
 *   tokenColor("--color-bg")    -> the value of --color-bg
 */
export function tokenColor(token: string): string {
  if (typeof document === "undefined") return "";
  const name = token.startsWith("--") ? token : `--color-${token}`;
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}
