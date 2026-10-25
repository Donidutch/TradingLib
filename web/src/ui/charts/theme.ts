/* Chart colours come from the design tokens at draw time; canvas libraries
 * cannot read var(). */
export function tokenValue(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

export function chartColours() {
  return {
    text: tokenValue("--ds-text-muted"),
    grid: tokenValue("--ds-border-subtle"),
    border: tokenValue("--ds-border"),
    crosshair: tokenValue("--ds-border-strong"),
    up: tokenValue("--ds-up"),
    down: tokenValue("--ds-down"),
    accent: tokenValue("--ds-accent"),
  };
}

/** ISO time or date → the UTC seconds lightweight-charts expects. */
export function toSeconds(value: string): number | null {
  const ms = new Date(value).getTime();
  return Number.isNaN(ms) ? null : Math.floor(ms / 1000);
}
