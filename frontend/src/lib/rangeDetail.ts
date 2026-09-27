// frontend/src/lib/rangeDetail.ts
/**
 * The decisions the range page makes that are worth testing on their own.
 *
 * They live here rather than beside the JSX because importing pages/RangeDetail.tsx pulls in the
 * terminal emulator, the auth store and the rest of the browser-only graph, and this project has
 * no DOM test environment -- vite.config.ts records why. A helper in this directory is reachable
 * from a plain vitest run.
 */
import type { Network, VM } from '../types'

/**
 * Turn a FastAPI `detail` into one line of toast text.
 *
 * Deploy and sync answer failures with either a plain string or a structured
 * `{message, errors, hint}`, and both have to survive. These were native browser dialogs, which
 * block the page, cannot be styled as a failure and look nothing like the rest of the product's
 * reporting -- the toast store is what everything else on the range page reports with.
 */
export function detailToMessage(detail: unknown, fallback: string): string {
  if (typeof detail === 'string' && detail) return detail
  if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
    const structured = detail as { message?: string; errors?: string[]; hint?: string }
    const parts = [structured.message || fallback]
    if (structured.errors?.length) parts.push(structured.errors.join('; '))
    if (structured.hint) parts.push(structured.hint)
    // The toast paragraph collapses newlines, so the parts are joined as one readable line.
    return parts.join(' · ')
  }
  return fallback
}

/**
 * Whether the range has rows that exist in the database but not yet on the host.
 *
 * Sync pushes Network and VM rows into a range's already-deployed DinD container, so it means
 * nothing where a range is not composed from those rows: adding a network on the Kubernetes
 * substrate left an orphan row, which turned this true, which offered a Sync whose only possible
 * answer was that the range is missing DinD configuration and should be deployed -- about a range
 * that was running.
 */
export function needsResourceSync(
  composesFromRows: boolean,
  networks: Pick<Network, 'docker_network_id'>[],
  vms: Pick<VM, 'container_id'>[]
): boolean {
  if (!composesFromRows) return false
  return networks.some((n) => !n.docker_network_id) || vms.some((v) => !v.container_id)
}
