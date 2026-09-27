// frontend/src/components/diagnostics/namespaceView.ts
/**
 * What the Diagnostics tab can say about a Kubernetes range, derived from one answer.
 *
 * /ranges/{id}/workloads returns the namespace and, per machine, a phase and an address per
 * interface. How much of the range is up, and which attachments a machine is reporting, are
 * derivations rather than fields, and they live here rather than in the component so they can be
 * tested without a DOM.
 *
 * **The interface names in that answer are ordinals, not the range's network names.** KubeVirt
 * reports `status.interfaces[].name`, and the VM spec sets that to `default` for the pod network
 * and `net1`, `net2`, ... for the range attachments, numbered in the order each workload declares
 * them. `net1` on two machines is the first attachment of each, which need not be the same range
 * network -- so attachments are only ever grouped per machine here. Keying a cross-machine
 * grouping on that name asserts a shared network the answer does not establish, and the name the
 * blueprint gave the network is not in the answer at all.
 */
import type { Workload } from '../../hooks/useRangeWorkloads'

/**
 * KubeVirt attaches every machine to the pod network under this interface name. It is the
 * cluster's, not one of the range's own networks, so it is not an attachment the operator
 * declared and showing it as one invents a network nobody asked for.
 */
export const POD_NETWORK = 'default'

export interface InterfaceAddress {
  iface: string
  address: string
}

/** How many of the range's machines the cluster currently reports as Running. */
export function runningCount(workloads: Workload[]): number {
  return workloads.filter((w) => w.status === 'Running').length
}

/**
 * The attachments one machine is reporting, beyond the pod network, ready to render.
 *
 * KubeVirt fills in interfaces only while a machine is running, so an empty list means the
 * cluster is reporting no addresses for that machine -- which for anything but a Running machine
 * is expected rather than a fault. Falling back to the blueprint's networks would claim an
 * attachment exists when nothing is attached.
 */
export function rangeInterfaces(workload: Workload): InterfaceAddress[] {
  return Object.entries(workload.addresses ?? {})
    .filter(([iface, address]) => iface !== POD_NETWORK && Boolean(address))
    .map(([iface, address]) => ({ iface, address }))
    .sort((a, b) => a.iface.localeCompare(b.iface))
}

/**
 * KubeVirt statuses that will not resolve on their own -- the same set the deploy path refuses
 * on (`capability/lifecycle.py` `_VM_TERMINAL`). A machine in one of these is wedged, and a
 * diagnostics panel that paints it the same neutral grey as Stopped is the reason an operator
 * scrolls past it.
 */
const WEDGED = new Set([
  'CrashLoopBackOff',
  'DataVolumeError',
  'ErrImagePull',
  'ErrorDataVolumeNotFound',
  'ErrorPvcNotFound',
  'ErrorUnschedulable',
  'ImagePullBackOff',
])

/** How a machine's phase should read at a glance: settled, working on it, or stuck. */
export type PhaseTone = 'running' | 'transitional' | 'failed' | 'idle'

/**
 * The tone for one KubeVirt `printableStatus`.
 *
 * Unknown statuses read as transitional rather than idle: KubeVirt gains states faster than this
 * panel does, and a state this build has never heard of is one nobody has decided is harmless.
 */
export function phaseTone(status: string): PhaseTone {
  if (status === 'Running') return 'running'
  if (WEDGED.has(status)) return 'failed'
  if (status === 'Stopped' || status === 'Paused' || status === 'absent') return 'idle'
  return 'transitional'
}

/**
 * True when at least one of the range's machines is reporting no attachment.
 *
 * The panel says why rather than leaving a blank where an address belongs: a stopped machine
 * with no address is the cluster's truth, and an operator reading it as "this machine has no
 * network" would be reading it wrong.
 */
export function hasUnreportedInterfaces(workloads: Workload[]): boolean {
  return workloads.some((w) => rangeInterfaces(w).length === 0)
}
