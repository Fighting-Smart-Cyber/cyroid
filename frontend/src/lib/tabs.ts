// frontend/src/lib/tabs.ts
/**
 * What turns a row of buttons into a tab bar: the ids that tie a tab to the panel it controls,
 * and the arrow-key arithmetic that moves between them.
 *
 * Both tab bars in this product were plain `<button>` elements inside a `<div>`. To a screen
 * reader that is a row of buttons with no stated relationship to the region below it and no
 * count, so a user cannot tell that choosing one replaces that region, which one is chosen, or
 * how many choices there are. To a keyboard user every tab was its own Tab stop, so reaching the
 * content meant tabbing past all of them. The tab pattern answers both, and the parts of it that
 * are pure arithmetic live here: this project has no DOM test environment (vite.config.ts
 * records why), so anything worth pinning has to be reachable from a plain vitest run.
 */

/** The id of a tab's control, which its panel points at with `aria-labelledby`. */
export function tabId(group: string, tab: string): string {
  return `${group}-tab-${tab}`
}

/** The id of a tab's panel, which its tab points at with `aria-controls`. */
export function tabPanelId(group: string, tab: string): string {
  return `${group}-panel-${tab}`
}

/**
 * Where Left, Right, Home and End move within a tab bar, or null for a key the tab bar does not
 * own.
 *
 * Returning null rather than a number is what keeps the rest of the keyboard working: the caller
 * only calls `preventDefault` when it got an index, so Tab still leaves the bar and a screen
 * reader's own navigation keys still reach the page.
 *
 * Movement wraps, because the tab pattern says a tab bar is a ring -- pressing Left on the first
 * tab reaches the last one rather than doing nothing, which is otherwise indistinguishable from
 * a key the page ignored. A selection that is not in the list at all, which happens when the tab
 * a user chose is gated away by an install's capabilities, is treated as the first tab so that an
 * arrow key still moves.
 */
export function nextTabIndex(key: string, current: number, count: number): number | null {
  if (count <= 0) return null
  const from = current >= 0 && current < count ? current : 0
  switch (key) {
    case 'ArrowRight':
      return (from + 1) % count
    case 'ArrowLeft':
      return (from - 1 + count) % count
    case 'Home':
      return 0
    case 'End':
      return count - 1
    default:
      return null
  }
}
