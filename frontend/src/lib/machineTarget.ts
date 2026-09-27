// frontend/src/lib/machineTarget.ts
/**
 * What a walkthrough step's "Open <machine>" button can actually do, on either substrate.
 *
 * The button used to do nothing at all in two different situations: a name matching no machine,
 * and every name on a Kubernetes range, where there are no VM rows to match against and the lab
 * panel owns which machine is on screen. A learner clicked and the lab sat there.
 *
 * It lives apart from the page so it can be tested: the lab imports the KubeVirt console, which
 * reaches for `window` the moment it is loaded, and there is no browser environment in the unit
 * suite.
 */
import type { Workload } from '../hooks/useRangeWorkloads'
import type { VM } from '../types'

export type MachineTarget = { kind: 'select'; vmId: string } | { kind: 'notice'; message: string }

export interface MachineTargetContext {
  /** True only once the backend has said this range is on Kubernetes. */
  isKubernetes: boolean
  workloads: Pick<Workload, 'name' | 'status' | 'console_available'>[]
  vms: Pick<VM, 'id' | 'hostname'>[]
}

/**
 * Decide what to do when a step asks for a machine by name.
 *
 * Every outcome either switches the console or says in words why it did not, and none of them
 * claims a machine is absent from a range whose machines this page has not managed to read.
 */
export function resolveMachineTarget(rawName: string, ctx: MachineTargetContext): MachineTarget {
  // A guide written in YAML, and a guide parsed out of an MSEL, arrive with whatever spacing the
  // author left around the name. No machine carries surrounding whitespace on either substrate,
  // so a stray space must not be the reason a step looks broken.
  const name = rawName.trim()
  const starting: MachineTarget = {
    kind: 'notice',
    message: `This step uses ${name}. Your machines are still starting.`,
  }
  const absent: MachineTarget = {
    kind: 'notice',
    message: `This step names ${name}, which is not a machine in this range.`,
  }

  if (ctx.isKubernetes) {
    const match = ctx.workloads.find(w => w.name === name)
    if (match?.console_available) {
      return {
        kind: 'notice',
        message: `This step uses ${name}. Choose it in the machine bar below.`,
      }
    }
    if (match) {
      // The bar lists it, but its button stays disabled until the console exists. Sending the
      // learner to a control they cannot press is the same dead end in a different place.
      return {
        kind: 'notice',
        message: `This step uses ${name}. It is ${match.status.toLowerCase()} — its console opens once it is running.`,
      }
    }
    // Mid-deploy the cluster has nothing to list yet, and calling the name wrong at that moment
    // would send the learner to their instructor over a machine that is simply late.
    return ctx.workloads.length ? absent : starting
  }

  const vm = ctx.vms.find(v => v.hostname === name)
  if (vm) return { kind: 'select', vmId: vm.id }

  // `isKubernetes` only turns true once the cluster has answered, and stays false for good if
  // that request failed -- so arriving here with no machines in hand means nothing was checked,
  // not that the guide is wrong.
  return ctx.vms.length ? absent : starting
}
