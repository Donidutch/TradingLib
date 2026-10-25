/** Leave the React app for a page that still lives in the vanilla console. */
export function openLegacy(href: string): void {
  window.location.assign(href);
}
