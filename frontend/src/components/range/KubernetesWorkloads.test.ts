/// <reference types="vite/client" />
import { describe, it, expect } from 'vitest'
import {
  actionBlockedReason,
  actionFailureMessage,
  allowedActions,
  bootImageLabel,
  importLabel,
  isRouteMissing,
  isTransitional,
  osLabel,
  pollDelayMs,
  serverMessage,
  statusLabel,
  statusTitle,
  updatedAgo,
} from './KubernetesWorkloads'

/**
 * The panel's decisions, covered on their own.
 *
 * There is no DOM test environment in this project (vite.config.ts records why), so what is
 * tested here is everything the component decides before it renders anything: how often to read
 * the cluster, what a machine's state permits, and what a user is told when a call fails. The
 * component imports cleanly under the node environment because nothing at module scope touches
 * the document.
 */

describe('pollDelayMs', () => {
  it('reads often while a machine is still moving', () => {
    expect(pollDelayMs(['Running', 'Provisioning'])).toBe(5000)
    expect(pollDelayMs(['Starting'])).toBe(5000)
  })

  it('backs off once every machine has settled', () => {
    expect(pollDelayMs(['Running', 'Running'])).toBe(30000)
    expect(pollDelayMs(['Stopped'])).toBe(30000)
    // A wedged machine will not resolve on its own, so polling it fast buys nothing.
    expect(pollDelayMs(['ErrImagePull'])).toBe(30000)
  })

  it('treats an empty range as settled', () => {
    expect(pollDelayMs([])).toBe(30000)
  })

  it('backs further off after consecutive failures, but not past a minute', () => {
    expect(pollDelayMs(['Provisioning'], 1)).toBe(10000)
    expect(pollDelayMs(['Provisioning'], 3)).toBe(40000)
    expect(pollDelayMs(['Provisioning'], 10)).toBe(60000)
    expect(pollDelayMs(['Running'], 5)).toBe(60000)
  })
})

describe('isTransitional', () => {
  it('counts a machine the cluster has not described yet', () => {
    expect(isTransitional('Unknown')).toBe(true)
    expect(isTransitional('WaitingForVolumeBinding')).toBe(true)
  })

  it('does not count a settled or wedged machine', () => {
    expect(isTransitional('Running')).toBe(false)
    expect(isTransitional('absent')).toBe(false)
    expect(isTransitional('CrashLoopBackOff')).toBe(false)
  })
})

describe('allowedActions', () => {
  it('offers stop and restart for a running machine', () => {
    expect(allowedActions('Running')).toEqual(['stop', 'restart'])
  })

  it('offers start for a stopped machine', () => {
    expect(allowedActions('Stopped')).toEqual(['start'])
  })

  it('offers stop and restart for a wedged machine -- that is the case these controls exist for', () => {
    expect(allowedActions('CrashLoopBackOff')).toEqual(['stop', 'restart'])
    expect(allowedActions('ErrImagePull')).toEqual(['stop', 'restart'])
  })

  it('offers nothing mid-transition or before the machine exists', () => {
    expect(allowedActions('Starting')).toEqual([])
    expect(allowedActions('Provisioning')).toEqual([])
    expect(allowedActions('absent')).toEqual([])
  })

  it('lets the server override the inference', () => {
    expect(allowedActions('Running', ['start'])).toEqual(['start'])
    expect(allowedActions('Running', [])).toEqual([])
    expect(allowedActions('absent', ['start'])).toEqual(['start'])
  })

  it('ignores an action the panel cannot render', () => {
    expect(allowedActions('Running', ['pause', 'stop'])).toEqual(['stop'])
  })
})

describe('actionBlockedReason', () => {
  it('names the transition being waited on', () => {
    expect(actionBlockedReason('Provisioning', 'start')).toBe('This machine is still being created')
    expect(actionBlockedReason('Stopping', 'stop')).toBe('This machine is stopping')
  })

  it('explains a machine that was never created', () => {
    expect(actionBlockedReason('absent', 'start')).toContain('has not been created')
  })

  it('says why start is refused on a running machine', () => {
    expect(actionBlockedReason('Running', 'start')).toBe('This machine is already running')
  })

  it('uses the readable status, never the cluster enum, for the fallback', () => {
    expect(actionBlockedReason('Stopped', 'restart')).toBe('This machine is stopped')
  })
})

describe('statusLabel', () => {
  it('translates the states no operator should have to decode', () => {
    expect(statusLabel('absent')).toBe('Not created')
    expect(statusLabel('ErrImagePull')).toBe('Image unavailable')
    expect(statusLabel('ErrorUnschedulable')).toBe('No node has room')
  })

  it('leaves a plain state alone', () => {
    expect(statusLabel('Running')).toBe('Running')
    expect(statusLabel('Stopped')).toBe('Stopped')
  })
})

describe('statusTitle', () => {
  it('keeps the cluster word available for diagnosis', () => {
    expect(statusTitle('ErrImagePull')).toBe('Cluster status: ErrImagePull')
  })

  it('explains this API tokens rather than repeating them', () => {
    expect(statusTitle('absent')).not.toContain('absent')
    expect(statusTitle('Unknown')).not.toContain('Unknown')
  })
})

describe('osLabel', () => {
  it('capitalises the blueprint families', () => {
    expect(osLabel('linux', '22.04')).toBe('Linux 22.04')
    expect(osLabel('windows', '11')).toBe('Windows 11')
    expect(osLabel('macos', '14')).toBe('macOS 14')
  })

  it('renders nothing extra when a field is missing', () => {
    expect(osLabel('linux', '')).toBe('Linux')
    expect(osLabel('', '')).toBe('')
  })
})

describe('importLabel', () => {
  it('says nothing when there is no import, or it is done', () => {
    expect(importLabel(null)).toBeNull()
    expect(importLabel(undefined)).toBeNull()
    expect(importLabel({ phase: 'Succeeded' })).toBeNull()
  })

  it('names the phase in progress', () => {
    expect(importLabel({ phase: 'ImportInProgress' })).toBe('importing boot image')
    expect(importLabel({ phase: 'Failed' })).toBe('boot image import failed')
  })

  it('adds progress only when it is a number', () => {
    expect(importLabel({ phase: 'ImportInProgress', progress: '42.3%' })).toBe(
      'importing boot image · 42.3%'
    )
    // CDI reports "N/A" until it knows the size, which is not a figure to show anybody.
    expect(importLabel({ phase: 'ImportInProgress', progress: 'N/A' })).toBe('importing boot image')
  })

  it('still says something for a phase it has no wording for', () => {
    expect(importLabel({ phase: 'SomethingNew' })).toBe('preparing boot image')
  })
})

describe('bootImageLabel', () => {
  it('says nothing when the blueprint declares no boot image', () => {
    expect(bootImageLabel(null)).toBeNull()
    expect(bootImageLabel(undefined)).toBeNull()
    expect(bootImageLabel('  ')).toBeNull()
  })

  it('keeps the reference an operator would paste into a pull', () => {
    expect(bootImageLabel('quay.io/kubevirt/cirros-container-disk-demo@sha256:abc')).toBe(
      'quay.io/kubevirt/cirros-container-disk-demo@sha256:abc'
    )
  })

  it("drops CDI's transport scheme, which names a runtime this install does not have", () => {
    expect(bootImageLabel('docker://quay.io/kubevirt/cirros:1.0')).toBe(
      'quay.io/kubevirt/cirros:1.0'
    )
    expect(bootImageLabel('DOCKER://quay.io/kubevirt/cirros:1.0')).toBe(
      'quay.io/kubevirt/cirros:1.0'
    )
  })
})

describe('isRouteMissing', () => {
  it('recognises an install whose backend has no such endpoint', () => {
    expect(isRouteMissing(404, 'Not Found')).toBe(true)
    expect(isRouteMissing(404, null)).toBe(true)
    expect(isRouteMissing(405, 'Method Not Allowed')).toBe(true)
  })

  it('does not mistake a missing range for a missing endpoint', () => {
    expect(isRouteMissing(404, 'Range not found')).toBe(false)
    expect(isRouteMissing(404, 'Machine not found in this range')).toBe(false)
  })

  it('leaves every other refusal alone', () => {
    expect(isRouteMissing(403, 'You are not entitled to this range')).toBe(false)
    expect(isRouteMissing(500, null)).toBe(false)
    expect(isRouteMissing(409, 'A transition is already in flight')).toBe(false)
  })
})

describe('serverMessage', () => {
  it('uses the reason the server gave', () => {
    expect(serverMessage({ detail: 'This machine is already stopped' }, 'fallback')).toBe(
      'This machine is already stopped'
    )
  })

  it('keeps every part of a structured refusal', () => {
    const message = serverMessage(
      { detail: { message: 'Cannot restart', errors: ['no VMI'], hint: 'Start it first' } },
      'fallback'
    )
    expect(message).toContain('Cannot restart')
    expect(message).toContain('no VMI')
    expect(message).toContain('Start it first')
  })

  it('reads a validation refusal', () => {
    expect(
      serverMessage({ detail: [{ msg: 'value is not a valid uuid' }] }, 'fallback')
    ).toBe('value is not a valid uuid')
  })

  it('falls back rather than showing a body nobody wrote for a user', () => {
    expect(serverMessage(null, 'fallback')).toBe('fallback')
    expect(serverMessage('<html>502 Bad Gateway</html>', 'fallback')).toBe('fallback')
    expect(serverMessage({ detail: 'Not Found' }, 'fallback')).toBe('fallback')
    expect(serverMessage({ detail: '' }, 'fallback')).toBe('fallback')
    expect(serverMessage({ error: "KeyError('web')" }, 'fallback')).toBe('fallback')
    expect(serverMessage({ detail: [] }, 'fallback')).toBe('fallback')
  })
})

describe('actionFailureMessage', () => {
  it('repeats a refusal that names the fix', () => {
    // The 409 the power endpoint raises for a restart of a stopped machine.
    expect(
      actionFailureMessage(409, { detail: 'web is stopped; start it rather than restarting it' }, 'restart')
    ).toBe('web is stopped; start it rather than restarting it')
  })

  it('repeats an entitlement refusal', () => {
    expect(actionFailureMessage(403, { detail: 'You do not own this range' }, 'stop')).toBe(
      'You do not own this range'
    )
  })

  it('does not put the cluster client exception on the page', () => {
    // What the 502 handler interpolates: `kubernetes start of web failed: <exc>`.
    const message = actionFailureMessage(
      502,
      { detail: "kubernetes start of web failed: ApiException(status=403, reason='Forbidden')" },
      'start'
    )
    expect(message).not.toContain('ApiException')
    expect(message).toContain('could not be started')
  })
})

describe('updatedAgo', () => {
  it('does not count seconds nobody cares about', () => {
    expect(updatedAgo(0)).toBe('updated just now')
    expect(updatedAgo(9_000)).toBe('updated just now')
  })

  it('counts up in the largest useful unit', () => {
    expect(updatedAgo(12_000)).toBe('updated 12s ago')
    expect(updatedAgo(90_000)).toBe('updated 1m ago')
    expect(updatedAgo(3 * 3_600_000)).toBe('updated 3h ago')
  })

  it('never renders NaN at the user', () => {
    expect(updatedAgo(Number.NaN)).toBe('updated just now')
  })
})
