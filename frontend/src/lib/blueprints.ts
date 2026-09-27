// frontend/src/lib/blueprints.ts
/**
 * Reading a blueprint config that may describe either era, and saying what a refusal said.
 *
 * `RangeBlueprint.config` is free-form JSON that the backend versions explicitly: no
 * `schemaVersion` means v1 — networks and VMs, Era A — and 2 means workloads and capabilities,
 * Era B. The blueprint UI read the v1 fields unconditionally, so on a Kubernetes install the one
 * blueprint present showed an empty VMs table beside the machine it declares, and threw outright
 * once `vms` was absent rather than empty. Nothing here trusts a field to be there: the config is
 * whatever is in the database, which no TypeScript type can promise.
 */

export const SCHEMA_VERSION_LEGACY = 1
export const SCHEMA_VERSION_K8S = 2

const SUPPORTED_SCHEMA_VERSIONS = [SCHEMA_VERSION_LEGACY, SCHEMA_VERSION_K8S]

export interface BlueprintNetwork {
  name: string
  subnet: string | null
  gateway: string | null
}

export interface BlueprintInterface {
  network: string
  ip: string | null
  primary: boolean
}

export interface BlueprintDisk {
  name: string
  sizeGb: number | null
  boot: boolean
}

export interface BlueprintWorkload {
  name: string
  osFamily: string | null
  osVersion: string | null
  cpus: number | null
  memoryMb: number | null
  bootImage: string | null
  interfaces: BlueprintInterface[]
  disks: BlueprintDisk[]
}

export interface BlueprintLegacyVm {
  hostname: string
  ipAddress: string | null
  image: string | null
}

export interface BlueprintCapability {
  name: string
  version: string | null
  /** Required by the contract with no default, so an absent scope is reported, never assumed. */
  scope: string | null
  chartName: string | null
  chartVersion: string | null
  repository: string | null
  hooks: string[]
}

function asRecord(value: unknown): Record<string, unknown> | null {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return null
  return value as Record<string, unknown>
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : []
}

function text(value: unknown): string | null {
  if (typeof value === 'string' && value.trim() !== '') return value
  if (typeof value === 'number') return String(value)
  return null
}

function count(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

/** v1 by definition when unversioned: that is what every blueprint written before Era B is. */
export function blueprintSchemaVersion(config: unknown): number {
  const declared = asRecord(config)?.schemaVersion
  return typeof declared === 'number' ? declared : SCHEMA_VERSION_LEGACY
}

export function isKubernetesBlueprint(config: unknown): boolean {
  return blueprintSchemaVersion(config) === SCHEMA_VERSION_K8S
}

/**
 * Whether the visual editor can open this config.
 *
 * It reads `config.networks` and `config.vms` on its first render without checking either, so
 * anything else it is handed throws before the modal paints and, with no error boundary, takes
 * the application down with it. "Not v2" is not the question: a config from a schema newer than
 * this build knows, and a v1 config that lost one of its arrays, both reach it the same way. The
 * JSON editor reads nothing it has not checked, so it is the answer for all of them.
 */
export function usesVisualEditor(config: unknown): boolean {
  if (blueprintSchemaVersion(config) !== SCHEMA_VERSION_LEGACY) return false
  const record = asRecord(config)
  return Array.isArray(record?.networks) && Array.isArray(record?.vms)
}

/** Both eras declare networks, and both spell them the same way. */
export function blueprintNetworks(config: unknown): BlueprintNetwork[] {
  return asArray(asRecord(config)?.networks).flatMap((entry) => {
    const net = asRecord(entry)
    if (!net) return []
    return [
      {
        name: text(net.name) ?? 'unnamed',
        subnet: text(net.subnet),
        gateway: text(net.gateway),
      },
    ]
  })
}

export function blueprintWorkloads(config: unknown): BlueprintWorkload[] {
  return asArray(asRecord(config)?.workloads).flatMap((entry) => {
    const workload = asRecord(entry)
    if (!workload) return []
    const os = asRecord(workload.os)
    return [
      {
        name: text(workload.name) ?? 'unnamed',
        osFamily: text(os?.family),
        osVersion: text(os?.version),
        cpus: count(workload.cpus),
        memoryMb: count(workload.memoryMb),
        bootImage: text(workload.bootImage),
        interfaces: asArray(workload.interfaces).flatMap((raw) => {
          const iface = asRecord(raw)
          if (!iface) return []
          return [
            {
              network: text(iface.network) ?? 'unnamed',
              ip: text(iface.ip),
              primary: iface.primary === true,
            },
          ]
        }),
        disks: asArray(workload.disks).flatMap((raw) => {
          const disk = asRecord(raw)
          if (!disk) return []
          return [
            {
              name: text(disk.name) ?? 'unnamed',
              sizeGb: count(disk.sizeGb),
              boot: disk.boot === true,
            },
          ]
        }),
      },
    ]
  })
}

/** The Era A machine list. Read as defensively as the rest: `vms` is required of a v1 config by
 *  the backend's own schema, and a config that lost it still has to render. */
export function blueprintLegacyVms(config: unknown): BlueprintLegacyVm[] {
  return asArray(asRecord(config)?.vms).flatMap((entry) => {
    const vm = asRecord(entry)
    if (!vm) return []
    return [
      {
        hostname: text(vm.hostname) ?? 'unnamed',
        ipAddress: text(vm.ip_address),
        image: text(vm.base_image_tag) ?? text(vm.base_image_name) ?? text(vm.template_name),
      },
    ]
  })
}

/** Capabilities are era-neutral — a v1 blueprint may declare them too. */
export function blueprintCapabilities(config: unknown): BlueprintCapability[] {
  return asArray(asRecord(config)?.capabilities).flatMap((entry) => {
    const capability = asRecord(entry)
    if (!capability) return []
    const chart = asRecord(capability.chart)
    const hooks = asRecord(capability.hooks)
    return [
      {
        name: text(capability.name) ?? 'unnamed',
        version: text(capability.version),
        scope: text(capability.scope),
        chartName: text(chart?.name),
        chartVersion: text(chart?.version),
        repository: text(chart?.repository),
        hooks: hooks ? Object.keys(hooks) : [],
      },
    ]
  })
}

/**
 * What to call a blueprint's machines. "machines" on Kubernetes, "VMs" on Docker, matching the
 * dashboard and the workload grid. The list endpoint answers counts rather than config, so a
 * caller with no schema version to hand falls back to the substrate the install runs on.
 *
 * `count` makes it singular for one. Leaving it out is the plural, which is right for a label
 * with no number beside it -- but a card reading "1 machines" is the kind of thing a reader
 * notices before they notice anything else on the page.
 */
export function machineNoun(
  schemaVersion: number | null | undefined,
  isKubernetes: boolean,
  count?: number | null
): string {
  const kubernetes =
    schemaVersion === SCHEMA_VERSION_K8S
      ? true
      : schemaVersion === SCHEMA_VERSION_LEGACY
        ? false
        : isKubernetes
  const word = kubernetes ? 'machine' : 'VM'
  return count === 1 ? word : `${word}s`
}

/** "2 networks", "1 network" -- the same rule for the other count on those cards. */
export function pluralise(count: number | null | undefined, word: string): string {
  return `${count ?? 0} ${count === 1 ? word : word + 's'}`
}

/**
 * Enough checking to name what is wrong before a round trip; the server stays the authority on
 * the schema. Anything stricter here would refuse a config the backend would have accepted, which
 * is worse than a 422 the user can read.
 */
export function validateBlueprintConfigJson(source: string): string | null {
  let parsed: unknown
  try {
    parsed = JSON.parse(source)
  } catch (err) {
    return `Invalid JSON: ${err instanceof Error ? err.message : String(err)}`
  }

  const config = asRecord(parsed)
  if (!config) return 'Config must be a JSON object'

  const declared = config.schemaVersion
  if (
    declared !== undefined &&
    (typeof declared !== 'number' ||
      !Number.isInteger(declared) ||
      !SUPPORTED_SCHEMA_VERSIONS.includes(declared))
  ) {
    return `schemaVersion must be ${SUPPORTED_SCHEMA_VERSIONS.join(' or ')} — omit it for an Era A blueprint`
  }

  if (blueprintSchemaVersion(config) === SCHEMA_VERSION_K8S) {
    for (const key of ['networks', 'workloads', 'capabilities'] as const) {
      if (config[key] !== undefined && !Array.isArray(config[key])) {
        return `"${key}" must be an array`
      }
    }
    return null
  }

  if (!Array.isArray(config.networks)) return 'Config must have a "networks" array'
  if (!Array.isArray(config.vms)) return 'Config must have a "vms" array'
  return null
}

// Re-exported so the callers that already import them from here keep working; the
// implementation lives in ./apiError, which is where a general helper belongs.
export { apiErrorDetail, formatErrorDetail } from './apiError'
