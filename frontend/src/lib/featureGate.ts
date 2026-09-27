// frontend/src/lib/featureGate.ts
/**
 * Whether a route may render on this install.
 *
 * Hiding a nav link does not make a route unreachable. A bookmark, a pasted link, the back button
 * and an in-app redirect all land on the route itself, and on the wrong substrate the page behind
 * it can only fail. The decision therefore belongs on the route, not on the link.
 *
 * The third answer is the one that is easy to get wrong. `features` is null until
 * /system/capabilities replies, and treating that as "off" redirects the user off a page they are
 * entitled to on every reload, before the answer has arrived. Unknown is not unavailable: the
 * route waits.
 */
import type { Feature, Features } from '../stores/capabilitiesStore'

/** `pending` means the backend has not answered yet -- render nothing, do not redirect. */
export type FeatureGate = 'pending' | 'allow' | 'deny'

export function featureGate(features: Features | null, feature: Feature): FeatureGate {
  if (!features) return 'pending'
  return features[feature] ? 'allow' : 'deny'
}
