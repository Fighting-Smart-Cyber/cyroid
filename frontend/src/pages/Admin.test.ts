import { describe, it, expect, vi } from 'vitest'

// Importing the page pulls in the store graph, which reads localStorage at module scope. These
// tests are of pure functions and never touch it, but vitest runs this suite in node, where
// there is none -- so it has to exist before the import below is hoisted past this point.
vi.hoisted(() => {
  const entries = new Map<string, string>()
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    writable: true,
    value: {
      getItem: (key: string) => entries.get(key) ?? null,
      setItem: (key: string, value: string) => void entries.set(key, value),
      removeItem: (key: string) => void entries.delete(key),
      clear: () => entries.clear(),
      key: () => null,
      length: 0,
    },
  })
})

import {
  visibleAdminTabs,
  selectedAdminTab,
  cleanupResidue,
  otherCleanupErrors,
  residueReason,
  cleanupOutcome,
  cleanupModeCopy,
  cleanupCounters,
  type SubstrateCleanupResult,
} from './Admin'

/**
 * The Admin page offers the Docker product's surfaces. On a Kubernetes install the endpoints
 * behind them have no daemon to talk to and answer 500 with no body, and the cleanup endpoint
 * can report ranges it failed to tear down. These are the decisions that keep either from
 * reaching the operator as a working-looking page.
 */

const KUBERNETES = { dockerRegistry: false }
const DOCKER = { dockerRegistry: true }

function result(overrides: Partial<SubstrateCleanupResult> = {}): SubstrateCleanupResult {
  return {
    ranges_cleaned: 0,
    dind_containers_removed: 0,
    containers_removed: 0,
    networks_removed: 0,
    database_records_updated: 0,
    database_records_deleted: 0,
    errors: [],
    orphaned_resources_cleaned: 0,
    ...overrides,
  }
}

describe('visibleAdminTabs', () => {
  it('drops the registry, which has nothing but a Docker registry behind it', () => {
    expect(visibleAdminTabs(KUBERNETES)).not.toContain('registry')
  })

  // The tab is named for Docker but holds the platform update, which has an in-cluster path, and
  // the resource metrics, which read the host on either substrate. Hiding it loses both.
  it('keeps Infrastructure on a Kubernetes install', () => {
    expect(visibleAdminTabs(KUBERNETES)).toEqual(['system', 'users', 'infrastructure', 'catalog'])
  })

  it('keeps every tab on a Docker install', () => {
    expect(visibleAdminTabs(DOCKER)).toEqual([
      'system',
      'users',
      'infrastructure',
      'catalog',
      'registry',
    ])
  })

  it('never returns an empty tab list', () => {
    expect(visibleAdminTabs(KUBERNETES).length).toBeGreaterThan(0)
  })
})

describe('selectedAdminTab', () => {
  it('keeps the selected tab while it exists', () => {
    expect(selectedAdminTab('registry', visibleAdminTabs(DOCKER))).toBe('registry')
  })

  // useFeature is false until the backend answers, so a tab can be gated away after a click.
  it('falls back to a tab that exists when the selected one is gated away', () => {
    expect(selectedAdminTab('registry', visibleAdminTabs(KUBERNETES))).toBe('system')
  })

  it('falls back to users rather than rendering nothing', () => {
    expect(selectedAdminTab('system', [])).toBe('users')
  })
})

describe('cleanupResidue', () => {
  it('reads nothing as nothing left behind', () => {
    expect(cleanupResidue(null)).toEqual([])
    expect(cleanupResidue(result())).toEqual([])
  })

  it('survives an API old enough to answer without the field', () => {
    expect(cleanupResidue(result({ residue: undefined }))).toEqual([])
    expect(cleanupResidue(result({ residue: null as unknown as undefined }))).toEqual([])
  })
})

describe('otherCleanupErrors', () => {
  // The endpoint reports a failed teardown as residue AND as an error.
  it('drops the errors the residue list already tells', () => {
    const left = otherCleanupErrors(
      result({
        errors: [
          "Range 'alpha' was not torn down: namespace still present",
          'Failed to commit database changes: deadlock',
        ],
        residue: [
          { range_id: 'a', range_name: 'alpha', reason: 'namespace still present' },
        ],
      })
    )
    expect(left).toEqual(['Failed to commit database changes: deadlock'])
  })

  it('keeps every error when nothing was left behind', () => {
    expect(otherCleanupErrors(result({ errors: ['boom'] }))).toEqual(['boom'])
  })

  // The reason is str(exception), so an exception raised with no message arrives as the empty
  // string, and every error in the list ends with it.
  it('does not let a reason with no text swallow the whole error list', () => {
    const left = otherCleanupErrors(
      result({
        errors: ['Failed to commit database changes: deadlock'],
        residue: [{ range_id: 'a', range_name: 'alpha', reason: '' }],
      })
    )
    expect(left).toEqual(['Failed to commit database changes: deadlock'])
  })
})

describe('residueReason', () => {
  it('reads back the reason the endpoint gave', () => {
    expect(residueReason({ range_id: 'a', range_name: 'alpha', reason: 'namespace still up' })).toBe(
      'namespace still up'
    )
  })

  it('says a reason is missing rather than trailing off after the range name', () => {
    expect(residueReason({ range_id: 'a', range_name: 'alpha', reason: '  ' })).toMatch(/reason/i)
  })
})

describe('cleanupOutcome', () => {
  it('reports a clean purge as a success', () => {
    const outcome = cleanupOutcome(result({ ranges_cleaned: 3 }), 'purge_ranges')
    expect(outcome.ok).toBe(true)
    expect(outcome.message).toBe('Purged 3 ranges.')
  })

  it('reports a clean reset as a success', () => {
    const outcome = cleanupOutcome(result({ ranges_cleaned: 1 }), 'reset_to_draft')
    expect(outcome.ok).toBe(true)
    expect(outcome.message).toBe('Reset 1 range to draft.')
  })

  // The defect this exists for: rows gone, namespaces running, and a green toast over it.
  it('refuses to call a cleanup that left ranges behind a success, and names them', () => {
    const outcome = cleanupOutcome(
      result({
        ranges_cleaned: 2,
        residue: [
          {
            range_id: 'a',
            range_name: 'alpha',
            reason: 'left residue in pg-range-alpha: namespace still present',
          },
          { range_id: 'b', range_name: 'beta', reason: 'volumes: disk-0' },
        ],
      }),
      'purge_ranges'
    )
    expect(outcome.ok).toBe(false)
    expect(outcome.message).toContain('alpha')
    expect(outcome.message).toContain('beta')
    expect(outcome.message).toContain('2 ranges')
  })

  it('does not report a success when the endpoint reported errors', () => {
    const outcome = cleanupOutcome(result({ errors: ['range alpha: 502'] }), 'reset_to_draft')
    expect(outcome.ok).toBe(false)
    expect(outcome.message).toContain('1 error')
  })
})

describe('cleanupModeCopy', () => {
  const dockerWords = /docker|dind|container/i

  it('states what a Kubernetes install actually destroys', () => {
    for (const mode of ['reset_to_draft', 'purge_ranges'] as const) {
      const copy = cleanupModeCopy(mode, true)
      const text = [copy.subtitle, copy.summary, ...copy.bullets].join(' ')
      expect(text).toMatch(/namespace/i)
      expect(text).toMatch(/virtual machines/i)
      expect(text).toMatch(/disks/i)
      expect(text).not.toMatch(dockerWords)
    }
  })

  it('leaves the Docker wording alone on a Docker install', () => {
    const copy = cleanupModeCopy('purge_ranges', false)
    expect(copy.summary).toMatch(/DinD containers/)
    expect(copy.bullets).toContain('Stop and remove all DinD range containers')
  })
})

describe('cleanupCounters', () => {
  it('does not report Docker counters on a Kubernetes install', () => {
    const labels = cleanupCounters(result(), true).map((c) => c.label)
    expect(labels).not.toContain('DinD containers')
    expect(labels).not.toContain('Legacy containers')
  })

  it('reports namespaces when the endpoint counted them', () => {
    const labels = cleanupCounters(result({ namespaces_removed: 4 }), true).map((c) => c.label)
    expect(labels).toContain('Namespaces removed')
  })

  it('keeps the Docker counters on a Docker install', () => {
    const labels = cleanupCounters(result(), false).map((c) => c.label)
    expect(labels).toContain('DinD containers')
    expect(labels).toContain('Orphaned')
  })
})
