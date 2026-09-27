/**
 * Every place the product names itself, in one module.
 *
 * The engine is one product and a distribution is another
 * ([ADR-0011](../../../docs/adr/0011-the-engine-and-the-distribution-are-separate-products.md)):
 * the engine ships the mechanism, a distribution ships the theme, the
 * capability package and the content. Before this module the product name was
 * 28 literals across ten files, so "what does this distribution call itself"
 * had no single answer and re-theming meant a find-and-replace over the UI.
 *
 * This is step 1 of `MIG-8` and is deliberately the smallest useful one: it is
 * the precondition for *either* way of shipping a distribution, whether that
 * turns out to be a per-client frontend build or the runtime branding endpoint
 * the ADR's 2026-09-01 amendment recommends. Nothing here decides that yet.
 *
 * `backend/tests/unit/test_branding_is_centralised.py` fails if a rendered
 * product name reappears anywhere else.
 *
 * NOT in scope, on purpose:
 *   - `frontend/src/lib/changelog.ts` — release notes are a historical record.
 *     "PROVING GROUND" in a note about what shipped in 0.42.0 is a statement
 *     about that release, not a label to re-theme.
 *   - Docker object names, the Python package, the database, `image_namespace`
 *     — engine internals, and step 3 of MIG-8, which is gated separately.
 */

export interface Branding {
  /** The product name, as rendered. Sidebar, login, page titles. */
  productName: string
  /** Appended to the browser tab on the shell's own pages. */
  tagline: string
}

/**
 * Defaults, used until the API answers and whenever it cannot.
 *
 * These are the ENGINE's. A distribution overrides them through
 * GET /api/v1/branding rather than by editing this file, which is what makes
 * standing one up a configuration change instead of a rebuild.
 */
export const BRANDING: Branding = {
  productName: 'CYROID',
  tagline: 'Cyber Range Orchestrator',
}

interface RemoteBranding {
  product_name?: string
  tagline?: string
  primary_palette?: Record<string, string>
}

/** "#2563eb" -> "37 99 235", the form Tailwind's <alpha-value> needs.
 *  Exported for the tests: it is the part with real edge cases. */
export function rgbTriplet(hex: string): string | null {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex.trim())
  if (!m) return null
  const n = parseInt(m[1], 16)
  return `${(n >> 16) & 255} ${(n >> 8) & 255} ${n & 255}`
}

/**
 * Apply what the API returned. Exported for the tests; call `loadBranding()`.
 *
 * Every field is optional and every bad value is ignored rather than thrown on:
 * a malformed colour in someone's configuration should cost them that colour,
 * not the whole application.
 */
export function applyBranding(remote: RemoteBranding): void {
  if (remote.product_name?.trim()) BRANDING.productName = remote.product_name.trim()
  if (remote.tagline?.trim()) BRANDING.tagline = remote.tagline.trim()

  // Guarded so this is callable without a DOM — the tests run in vitest's
  // node environment, and the name/tagline half is worth testing there.
  if (typeof document === 'undefined') return
  for (const [shade, hex] of Object.entries(remote.primary_palette ?? {})) {
    const triplet = rgbTriplet(hex)
    if (!triplet) continue
    document.documentElement.style.setProperty(`--color-primary-${shade}`, triplet)
  }
}

/**
 * Fetch the branding before the app renders.
 *
 * Called from main.tsx ahead of the first render so the name is right the first
 * time — the alternative is every screen flashing the engine's name and then
 * correcting itself, which looks like a bug on the sign-in page.
 *
 * Never rejects, and never blocks for long: a deployment whose API is down
 * should still render, with the defaults above.
 */
export async function loadBranding(timeoutMs = 2000): Promise<void> {
  try {
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), timeoutMs)
    const res = await fetch('/api/v1/branding', { signal: controller.signal })
    clearTimeout(timer)
    if (!res.ok) return
    applyBranding(await res.json())
  } catch {
    // Offline, timed out, or serving something that is not JSON. The defaults
    // are already correct for the engine.
  }
}

/** `Console: web-01 - PROVING GROUND`, and the plain product name with no page. */
export function pageTitle(page?: string): string {
  return page ? `${page} - ${BRANDING.productName}` : BRANDING.productName
}
