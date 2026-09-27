import { describe, expect, it } from 'vitest'
import type { Workload } from '../../hooks/useRangeWorkloads'
import { hasUnreportedInterfaces, phaseTone, rangeInterfaces, runningCount } from './namespaceView'

/**
 * `addresses` is keyed the way the cluster keys it: `default` for the pod network and `netN` for
 * the range attachments, numbered per machine in blueprint order. Naming the keys after the
 * range's own networks -- which the answer does not carry -- is what made a cross-machine
 * grouping of those keys look correct when it was not.
 */
function workload(name: string, status: string, addresses: Record<string, string>): Workload {
  return {
    name,
    os_family: 'linux',
    os_version: '24.04',
    cpus: 2,
    memory_mb: 4096,
    status,
    addresses,
    console_available: status === 'Running',
  }
}

describe('runningCount', () => {
  it('counts only the machines the cluster reports as Running', () => {
    const workloads = [
      workload('web', 'Running', {}),
      workload('db', 'Stopped', {}),
      workload('dc', 'Provisioning', {}),
      workload('gone', 'absent', {}),
    ]
    expect(runningCount(workloads)).toBe(1)
  })

  it('is zero for a range with no machines', () => {
    expect(runningCount([])).toBe(0)
  })
})

describe('rangeInterfaces', () => {
  it("lists one machine's attachments, sorted, without the pod network", () => {
    const w = workload('web', 'Running', {
      net2: '10.20.0.5',
      default: '10.42.0.8',
      net1: '172.30.10.5',
    })
    expect(rangeInterfaces(w)).toEqual([
      { iface: 'net1', address: '172.30.10.5' },
      { iface: 'net2', address: '10.20.0.5' },
    ])
  })

  it('is empty when the machine reports only the pod network', () => {
    expect(rangeInterfaces(workload('web', 'Running', { default: '10.42.0.8' }))).toEqual([])
  })

  it('is empty when the cluster reports no addresses at all', () => {
    expect(rangeInterfaces(workload('web', 'Provisioning', {}))).toEqual([])
  })

  it('ignores an interface the cluster reports without an address', () => {
    expect(rangeInterfaces(workload('web', 'Running', { net1: '' }))).toEqual([])
  })
})

describe('phaseTone', () => {
  it('reads a wedged machine as failed rather than as another stopped one', () => {
    for (const status of [
      'CrashLoopBackOff',
      'DataVolumeError',
      'ErrImagePull',
      'ErrorDataVolumeNotFound',
      'ErrorPvcNotFound',
      'ErrorUnschedulable',
      'ImagePullBackOff',
    ]) {
      expect(phaseTone(status)).toBe('failed')
    }
  })

  it('separates running from the states that are going somewhere on their own', () => {
    expect(phaseTone('Running')).toBe('running')
    for (const status of ['Provisioning', 'Starting', 'Stopping', 'Terminating', 'Migrating']) {
      expect(phaseTone(status)).toBe('transitional')
    }
  })

  it('reads deliberately-down states as idle', () => {
    for (const status of ['Stopped', 'Paused', 'absent']) {
      expect(phaseTone(status)).toBe('idle')
    }
  })

  it('treats a status this build has never seen as transitional, not as harmless', () => {
    expect(phaseTone('Unknown')).toBe('transitional')
    expect(phaseTone('SomeFutureKubeVirtState')).toBe('transitional')
  })
})

describe('hasUnreportedInterfaces', () => {
  it('is true when any machine reports none, which a stopped machine never does', () => {
    expect(
      hasUnreportedInterfaces([
        workload('web', 'Running', { default: '10.42.0.8', net1: '172.30.10.5' }),
        workload('db', 'Stopped', {}),
      ])
    ).toBe(true)
  })

  it('is false once every machine is reporting one', () => {
    expect(
      hasUnreportedInterfaces([
        workload('web', 'Running', { default: '10.42.0.8', net1: '172.30.10.5' }),
        workload('db', 'Running', { default: '10.42.0.9', net1: '172.30.10.6' }),
      ])
    ).toBe(false)
  })

  it('is false for a range with no machines, which has nothing to leave unreported', () => {
    expect(hasUnreportedInterfaces([])).toBe(false)
  })
})
