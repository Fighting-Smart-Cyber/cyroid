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

export const BRANDING: Branding = {
  productName: 'PROVING GROUND',
  tagline: 'Cyber Range Orchestrator',
}

/** `Console: web-01 - PROVING GROUND`, and the plain product name with no page. */
export function pageTitle(page?: string): string {
  return page ? `${page} - ${BRANDING.productName}` : BRANDING.productName
}
