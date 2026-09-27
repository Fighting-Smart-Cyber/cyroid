import { describe, it, expect } from 'vitest'
import { resolveMachineTarget } from './machineTarget'

const workload = (name: string, status = 'Running', console_available = true) => ({
  name,
  status,
  console_available,
})

describe('resolveMachineTarget on Kubernetes', () => {
  const ctx = (...workloads: ReturnType<typeof workload>[]) => ({
    isKubernetes: true,
    workloads,
    vms: [],
  })

  it('points at the machine bar when the console is there to open', () => {
    expect(resolveMachineTarget('web-01', ctx(workload('web-01')))).toEqual({
      kind: 'notice',
      message: 'This step uses web-01. Choose it in the machine bar below.',
    })
  })

  // The bar's button is disabled until the console exists, so "choose it below" would send the
  // learner to a control they cannot press.
  it('says why a listed machine cannot be opened yet', () => {
    const target = resolveMachineTarget('web-01', ctx(workload('web-01', 'Provisioning', false)))
    expect(target.kind).toBe('notice')
    expect(target.kind === 'notice' && target.message).toContain('provisioning')
    expect(target.kind === 'notice' && target.message).not.toContain('machine bar')
  })

  it('names a machine the range does not have', () => {
    expect(resolveMachineTarget('kali', ctx(workload('web-01')))).toEqual({
      kind: 'notice',
      message: 'This step names kali, which is not a machine in this range.',
    })
  })

  // Mid-deploy the cluster lists nothing yet, and a machine that is late is not a machine that
  // is missing.
  it('does not call a machine missing when nothing has been listed yet', () => {
    expect(resolveMachineTarget('web-01', ctx())).toEqual({
      kind: 'notice',
      message: 'This step uses web-01. Your machines are still starting.',
    })
  })
})

describe('resolveMachineTarget on the Docker substrate', () => {
  const vms = [
    { id: 'vm-1', hostname: 'kali' },
    { id: 'vm-2', hostname: 'dc-01' },
  ]

  it('switches the console to the named machine', () => {
    expect(resolveMachineTarget('dc-01', { isKubernetes: false, workloads: [], vms })).toEqual({
      kind: 'select',
      vmId: 'vm-2',
    })
  })

  it('names a machine this range does not have', () => {
    expect(resolveMachineTarget('web-01', { isKubernetes: false, workloads: [], vms })).toEqual({
      kind: 'notice',
      message: 'This step names web-01, which is not a machine in this range.',
    })
  })

  // isKubernetes stays false while the cluster is being asked, and for good if that request
  // failed. With no rows in hand nothing has been checked, so blaming the guide would be a claim
  // the page cannot make.
  it('does not blame the guide when no machines are known at all', () => {
    expect(resolveMachineTarget('web-01', { isKubernetes: false, workloads: [], vms: [] })).toEqual(
      {
        kind: 'notice',
        message: 'This step uses web-01. Your machines are still starting.',
      }
    )
  })
})

// A guide authored in YAML, or parsed out of an MSEL, carries whatever spacing the author left.
it('matches a name the author left padded', () => {
  expect(
    resolveMachineTarget('  dc-01 ', {
      isKubernetes: false,
      workloads: [],
      vms: [{ id: 'vm-2', hostname: 'dc-01' }],
    })
  ).toEqual({ kind: 'select', vmId: 'vm-2' })
})
