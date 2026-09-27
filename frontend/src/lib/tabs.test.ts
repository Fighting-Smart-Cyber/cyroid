/// <reference types="vite/client" />
// frontend/src/lib/tabs.test.ts
import { describe, it, expect } from 'vitest'
import { nextTabIndex, tabId, tabPanelId } from './tabs'
// `?raw` rather than rendering the pages: there is no DOM test environment here, and importing
// either page pulls in the terminal emulator and the auth store. A textual guard is weaker than
// a rendered one, but it does fail when a tab is added without the pattern, which is the
// regression worth catching on a product that will be accredited.
import admin from '../pages/Admin.tsx?raw'
import rangeDetail from '../pages/RangeDetail.tsx?raw'

describe('tabId / tabPanelId', () => {
  it('gives a tab and its panel different ids', () => {
    expect(tabId('admin', 'users')).not.toBe(tabPanelId('admin', 'users'))
  })

  it('keeps two tab bars on one page apart', () => {
    expect(tabId('admin', 'users')).not.toBe(tabId('range', 'users'))
  })

  it('is stable, so aria-controls and aria-labelledby resolve to the same element', () => {
    expect(tabId('range', 'builder')).toBe(tabId('range', 'builder'))
    expect(tabPanelId('range', 'builder')).toBe(tabPanelId('range', 'builder'))
  })
})

describe('nextTabIndex', () => {
  it('moves right and left', () => {
    expect(nextTabIndex('ArrowRight', 0, 4)).toBe(1)
    expect(nextTabIndex('ArrowLeft', 2, 4)).toBe(1)
  })

  it('wraps at both ends rather than doing nothing', () => {
    expect(nextTabIndex('ArrowRight', 3, 4)).toBe(0)
    expect(nextTabIndex('ArrowLeft', 0, 4)).toBe(3)
  })

  it('jumps to the first and last tab', () => {
    expect(nextTabIndex('Home', 2, 4)).toBe(0)
    expect(nextTabIndex('End', 0, 4)).toBe(3)
  })

  // Tab has to keep leaving the tab bar, and a screen reader's own keys have to keep reaching
  // the page, so every other key is the caller's business and not the tab bar's.
  it('claims no other key', () => {
    for (const key of ['Tab', 'Enter', ' ', 'ArrowUp', 'ArrowDown', 'a', 'Escape']) {
      expect(nextTabIndex(key, 0, 4)).toBeNull()
    }
  })

  // The selected tab can be one an install does not offer -- capabilities arrive after the first
  // paint -- and an arrow key that returned null there would read as a dead tab bar.
  it('treats a selection outside the list as the first tab', () => {
    expect(nextTabIndex('ArrowRight', -1, 3)).toBe(1)
    expect(nextTabIndex('ArrowLeft', -1, 3)).toBe(2)
    expect(nextTabIndex('ArrowRight', 9, 3)).toBe(1)
  })

  it('has nowhere to go in an empty bar', () => {
    expect(nextTabIndex('ArrowRight', 0, 0)).toBeNull()
    expect(nextTabIndex('Home', 0, 0)).toBeNull()
  })

  it('stays put in a bar of one', () => {
    expect(nextTabIndex('ArrowRight', 0, 1)).toBe(0)
    expect(nextTabIndex('ArrowLeft', 0, 1)).toBe(0)
  })
})

describe.each([
  ['the Admin page', admin, 5],
  ['the range page', rangeDetail, 4],
])('%s has a tab bar and not a row of buttons', (_name, page, panelCount) => {
  it('declares a named tablist', () => {
    expect(page).toContain('role="tablist"')
    expect(page).toMatch(/role="tablist"[\s\S]{0,120}aria-label="/)
    // Both bars were a <nav> of bare buttons, which is what this is here to stop coming back.
    expect(page).not.toMatch(/<nav className="-mb-px/)
  })

  it('gives each tab its role, its state and the panel it controls', () => {
    expect(page).toContain('role="tab"')
    expect(page).toContain('aria-selected={selected}')
    expect(page).toContain('aria-controls={tabPanelId(')
  })

  it('makes the bar one Tab stop that the arrow keys move within', () => {
    expect(page).toContain('tabIndex={selected ? 0 : -1}')
    expect(page).toContain('onKeyDown={handleTabKeyDown}')
    expect(page).toContain('nextTabIndex(')
    // Focus has to follow the selection, or the roving tabindex leaves it on a tab that Tab can
    // no longer reach and the user is stranded outside the bar.
    expect(page).toMatch(/tabRefs\.current\[\w+\]\?\.focus\(\)/)
  })

  it('labels every panel by the tab that opens it', () => {
    const panels = page.match(/role="tabpanel"/g) ?? []
    const labels = page.match(/aria-labelledby=\{tabId\(/g) ?? []
    expect(panels).toHaveLength(panelCount)
    expect(labels).toHaveLength(panels.length)
  })
})
