import { describe, it, expect } from 'vitest'
import {
  apiErrorDetail,
  blueprintCapabilities,
  blueprintLegacyVms,
  blueprintNetworks,
  blueprintSchemaVersion,
  blueprintWorkloads,
  formatErrorDetail,
  isKubernetesBlueprint,
  machineNoun,
  pluralise,
  usesVisualEditor,
  validateBlueprintConfigJson,
} from './blueprints'

/**
 * The config these read is free-form JSON out of the database. A blueprint is hand-edited by
 * turns, so every reader has to survive a field being absent, null or the wrong type without
 * taking the page down with it — a thrown render is what the Kubernetes blueprint page used to
 * be, and it looked exactly like a hang.
 */

// The config `proving_ground.tools.seed_k8s_blueprint` writes, which is the only blueprint a
// fresh Kubernetes install has.
const V2 = {
  schemaVersion: 2,
  networks: [
    { name: 'dmz', subnet: '172.30.10.0/24', gateway: '172.30.10.1' },
    { name: 'internal', subnet: '172.30.20.0/24' },
  ],
  workloads: [
    {
      name: 'web',
      os: { family: 'linux', version: 'cirros-0.6' },
      cpus: 1,
      memoryMb: 256,
      bootImage: 'quay.io/kubevirt/cirros-container-disk-demo@sha256:ebdb8d',
      disks: [{ name: 'root', sizeGb: 1, boot: true }],
      interfaces: [
        { network: 'dmz', ip: '172.30.10.5', primary: true },
        { network: 'internal', ip: '172.30.20.5' },
      ],
    },
  ],
  capabilities: [
    {
      name: 'podinfo',
      version: '1.0.0',
      scope: 'shared',
      chart: { name: 'podinfo', version: '6.7.1', repository: 'https://example.invalid/charts' },
      hooks: { seed: {}, verify: {} },
    },
  ],
}

const V1 = {
  networks: [{ name: 'lan', subnet: '10.0.0.0/24', gateway: '10.0.0.1' }],
  vms: [{ hostname: 'kali', ip_address: '10.0.0.5' }],
}

describe('blueprintSchemaVersion', () => {
  it('reads the declared version', () => {
    expect(blueprintSchemaVersion(V2)).toBe(2)
    expect(isKubernetesBlueprint(V2)).toBe(true)
  })

  it('treats an unversioned config as v1 rather than guessing from its shape', () => {
    expect(blueprintSchemaVersion(V1)).toBe(1)
    expect(isKubernetesBlueprint(V1)).toBe(false)
  })

  it('falls back to v1 for anything that is not an object', () => {
    for (const junk of [null, undefined, 'v2', 7, [], { schemaVersion: '2' }]) {
      expect(blueprintSchemaVersion(junk)).toBe(1)
    }
  })
})

describe('blueprintNetworks', () => {
  it('reads both eras, gateway optional', () => {
    expect(blueprintNetworks(V2)).toEqual([
      { name: 'dmz', subnet: '172.30.10.0/24', gateway: '172.30.10.1' },
      { name: 'internal', subnet: '172.30.20.0/24', gateway: null },
    ])
    expect(blueprintNetworks(V1)).toHaveLength(1)
  })

  it('returns nothing rather than throwing when networks is missing or malformed', () => {
    expect(blueprintNetworks({})).toEqual([])
    expect(blueprintNetworks({ networks: 'dmz' })).toEqual([])
    expect(blueprintNetworks({ networks: [null, 3] })).toEqual([])
    expect(blueprintNetworks(undefined)).toEqual([])
  })
})

describe('blueprintWorkloads', () => {
  it('reads a v2 workload including its interfaces and disks', () => {
    const [web] = blueprintWorkloads(V2)
    expect(web.name).toBe('web')
    expect(web.osFamily).toBe('linux')
    expect(web.osVersion).toBe('cirros-0.6')
    expect(web.cpus).toBe(1)
    expect(web.memoryMb).toBe(256)
    expect(web.disks).toEqual([{ name: 'root', sizeGb: 1, boot: true }])
    expect(web.interfaces).toEqual([
      { network: 'dmz', ip: '172.30.10.5', primary: true },
      { network: 'internal', ip: '172.30.20.5', primary: false },
    ])
  })

  it('is empty for a v1 blueprint, which declares vms instead', () => {
    expect(blueprintWorkloads(V1)).toEqual([])
  })

  it('tolerates a workload missing os, cpus, disks and interfaces', () => {
    const [bare] = blueprintWorkloads({ schemaVersion: 2, workloads: [{ name: 'bare' }] })
    expect(bare).toEqual({
      name: 'bare',
      osFamily: null,
      osVersion: null,
      cpus: null,
      memoryMb: null,
      bootImage: null,
      interfaces: [],
      disks: [],
    })
  })
})

describe('blueprintLegacyVms', () => {
  it('reads a v1 vm', () => {
    expect(blueprintLegacyVms(V1)).toEqual([
      { hostname: 'kali', ipAddress: '10.0.0.5', image: null },
    ])
  })

  it('prefers the image tag, then the name, then the deprecated template', () => {
    expect(
      blueprintLegacyVms({ vms: [{ hostname: 'a', template_name: 'legacy' }] })[0].image
    ).toBe('legacy')
    expect(
      blueprintLegacyVms({
        vms: [{ hostname: 'a', base_image_tag: 'ubuntu:22.04', template_name: 'legacy' }],
      })[0].image
    ).toBe('ubuntu:22.04')
  })

  it('is empty for a v2 blueprint, which has no vms key at all', () => {
    expect(blueprintLegacyVms(V2)).toEqual([])
  })
})

describe('blueprintCapabilities', () => {
  it('reads name, version, scope and chart', () => {
    expect(blueprintCapabilities(V2)).toEqual([
      {
        name: 'podinfo',
        version: '1.0.0',
        scope: 'shared',
        chartName: 'podinfo',
        chartVersion: '6.7.1',
        repository: 'https://example.invalid/charts',
        hooks: ['seed', 'verify'],
      },
    ])
  })

  it('reports an undeclared scope as absent rather than assuming one', () => {
    const [capability] = blueprintCapabilities({
      capabilities: [{ name: 'thing', chart: { name: 'thing' } }],
    })
    expect(capability.scope).toBeNull()
  })

  it('is empty when none are declared', () => {
    expect(blueprintCapabilities(V1)).toEqual([])
  })
})

describe('usesVisualEditor', () => {
  it('opens an Era A blueprint in the visual editor, as it always did', () => {
    expect(usesVisualEditor(V1)).toBe(true)
    expect(usesVisualEditor({ networks: [], vms: [] })).toBe(true)
  })

  it('keeps a v2 blueprint out of it — it reads vms, which a v2 config has not got', () => {
    expect(usesVisualEditor(V2)).toBe(false)
  })

  // The visual editor subscripts config.networks and config.vms on its first render. Every one
  // of these reaches it as "not v2", and every one of them throws there rather than rendering.
  it('keeps out anything else it would throw on', () => {
    expect(usesVisualEditor({ schemaVersion: 3, networks: [], vms: [] })).toBe(false)
    expect(usesVisualEditor({ schemaVersion: '2', workloads: [] })).toBe(false)
    expect(usesVisualEditor({ networks: [] })).toBe(false)
    expect(usesVisualEditor({ networks: [], vms: {} })).toBe(false)
    expect(usesVisualEditor({})).toBe(false)
    expect(usesVisualEditor(null)).toBe(false)
  })
})

describe('machineNoun', () => {
  it('is singular for one', () => {
    // "1 machines" was on the ranges list and on every blueprint card.
    expect(machineNoun(2, false, 1)).toBe('machine')
    expect(machineNoun(1, false, 1)).toBe('VM')
    expect(machineNoun(2, false, 0)).toBe('machines')
    expect(machineNoun(2, false, 7)).toBe('machines')
  })

  it('is plural when no count is given, for a label with no number beside it', () => {
    expect(machineNoun(2, false)).toBe('machines')
    expect(machineNoun(1, false)).toBe('VMs')
  })

  it('follows the blueprint when its era is known', () => {
    expect(machineNoun(2, false)).toBe('machines')
    expect(machineNoun(1, true)).toBe('VMs')
  })

  it('follows the install when it is not', () => {
    expect(machineNoun(null, true)).toBe('machines')
    expect(machineNoun(undefined, false)).toBe('VMs')
  })
})

describe('validateBlueprintConfigJson', () => {
  it('accepts a v2 config, which declares no vms at all', () => {
    expect(validateBlueprintConfigJson(JSON.stringify(V2))).toBeNull()
  })

  it('accepts a v1 config', () => {
    expect(validateBlueprintConfigJson(JSON.stringify(V1))).toBeNull()
  })

  it('names the parse failure', () => {
    expect(validateBlueprintConfigJson('{ nope')).toMatch(/^Invalid JSON: /)
  })

  it('refuses anything that is not a JSON object', () => {
    expect(validateBlueprintConfigJson('[]')).toBe('Config must be a JSON object')
    expect(validateBlueprintConfigJson('"hello"')).toBe('Config must be a JSON object')
    expect(validateBlueprintConfigJson('null')).toBe('Config must be a JSON object')
  })

  it('refuses a schema version this build does not know', () => {
    expect(validateBlueprintConfigJson('{"schemaVersion": 3}')).toMatch(/schemaVersion must be/)
    expect(validateBlueprintConfigJson('{"schemaVersion": "2"}')).toMatch(/schemaVersion must be/)
  })

  it('still requires the v1 arrays on a v1 config', () => {
    expect(validateBlueprintConfigJson('{"networks": []}')).toBe('Config must have a "vms" array')
    expect(validateBlueprintConfigJson('{"vms": []}')).toBe('Config must have a "networks" array')
  })

  it('checks only that a v2 collection is a list, leaving the schema to the server', () => {
    expect(validateBlueprintConfigJson('{"schemaVersion": 2, "workloads": {}}')).toBe(
      '"workloads" must be an array'
    )
    expect(validateBlueprintConfigJson('{"schemaVersion": 2}')).toBeNull()
    // A workload missing every required field is the server's refusal to make, not ours.
    expect(validateBlueprintConfigJson('{"schemaVersion": 2, "workloads": [{}]}')).toBeNull()
  })
})

describe('apiErrorDetail', () => {
  const axiosError = (detail: unknown) => ({
    isAxiosError: true,
    response: { data: { detail } },
  })

  it('uses a string detail as it stands', () => {
    expect(apiErrorDetail(axiosError('Blueprint not found'), 'fallback')).toBe(
      'Blueprint not found'
    )
  })

  it('flattens the list of location and message that a 422 carries', () => {
    const detail = [
      { type: 'missing', loc: ['body', 'config', 'vms'], msg: 'Field required' },
      { type: 'missing', loc: ['body', 'config', 'networks', 1, 'gateway'], msg: 'Field required' },
    ]
    expect(apiErrorDetail(axiosError(detail), 'fallback')).toBe(
      'body.config.vms: Field required\nbody.config.networks.1.gateway: Field required'
    )
  })

  it('falls back rather than printing an object', () => {
    expect(apiErrorDetail(axiosError({ code: 9 }), 'Failed to save')).toBe('Failed to save')
    expect(apiErrorDetail(axiosError(''), 'Failed to save')).toBe('Failed to save')
    expect(apiErrorDetail(new Error('boom'), 'Failed to save')).toBe('Failed to save')
    expect(apiErrorDetail(undefined, 'Failed to save')).toBe('Failed to save')
  })

  it('reports nothing for a body with no usable detail', () => {
    expect(formatErrorDetail([])).toBeNull()
    expect(formatErrorDetail(undefined)).toBeNull()
  })
})

describe('pluralise', () => {
  it('counts the thing and names it once', () => {
    expect(pluralise(1, 'network')).toBe('1 network')
    expect(pluralise(2, 'network')).toBe('2 networks')
    expect(pluralise(0, 'instance')).toBe('0 instances')
  })

  it('treats a missing count as none rather than rendering undefined', () => {
    expect(pluralise(null, 'network')).toBe('0 networks')
    expect(pluralise(undefined, 'instance')).toBe('0 instances')
  })
})
