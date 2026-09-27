// frontend/src/lib/catalogSupport.ts
/**
 * Whether a catalog item can run on this install, answered before anyone installs it.
 *
 * The catalog is a storefront over a source that may have been written for either substrate, and
 * the install itself runs on one of them. An Era A blueprint describes VMs and networks that the
 * Kubernetes substrate refuses at deploy; a Docker image item is built into an image library a
 * Kubernetes install does not have. Both install with green ticks and then fail at the one moment
 * the user cares about. This says so first, in a sentence, beside the item.
 *
 * The backend resolver answers the same question for an install plan
 * (`catalog/dependencies.py`), and these sentences are deliberately its sentences: a refusal a
 * user reads twice should not be worded two ways. The browser cannot call the resolver, which
 * plans one blueprint at a time and only for blueprints, so the rule is restated here over what
 * the item listing carries.
 */
import { SCHEMA_VERSION_K8S, SCHEMA_VERSION_LEGACY } from './blueprints'

/** Only the fields the verdict reads, so a catalog summary and a catalog detail both fit. */
export interface CatalogItemLike {
  type: string
  /**
   * Reported by the backend when it knows; absence is not nothing. A blueprint that declares no
   * `schemaVersion` is Era A by definition, which is also what an index that never mentions one
   * describes today.
   */
  schema_version?: unknown
}

/** What this install is, as the pages already know it from `useSubstrate` / `useFeature`. */
export interface InstallTarget {
  /** null until /system/capabilities has answered. No verdict is given before then. */
  substrate: string | null
  isKubernetes: boolean
  /** Whether this install has an image library to build or import a Docker image into. */
  hasImageLibrary: boolean
}

export interface CatalogSupport {
  supported: boolean
  /** One sentence, shown to the user verbatim. Empty when supported. */
  reason: string
  /** Short enough to sit beside the item's type. Empty when supported. */
  badge: string
}

const SUPPORTED: CatalogSupport = { supported: true, reason: '', badge: '' }

const BADGE = 'Not supported here'

const ERA_A_ON_KUBERNETES =
  'This is an Era A blueprint: it describes VMs and networks for the Docker substrate. ' +
  'This install deploys a range as a Kubernetes namespace of machines and capabilities, so the ' +
  'blueprint would install and then be refused at deploy.'

const NO_SCHEMA_REPORTED =
  ' The catalog reports no schema version for this item, and a blueprint without one is Era A ' +
  'by definition.'

const ERA_B_ON_DOCKER =
  'This is a Kubernetes blueprint: it describes machines and capability charts. This install ' +
  'runs on Docker, which deploys neither, so the blueprint would install and then be refused at ' +
  'deploy.'

const NO_IMAGE_LIBRARY =
  'Installing this builds or imports a Docker image into the image library, which this install ' +
  'does not have. A machine here boots the digest-pinned image its blueprint names.'

function unsupported(reason: string): CatalogSupport {
  return { supported: false, reason, badge: BADGE }
}

/** The version the item reports, or null when it reports none. */
export function reportedSchemaVersion(item: CatalogItemLike): number | null {
  return typeof item.schema_version === 'number' ? item.schema_version : null
}

/**
 * Whether this install can run `item`, or null while the substrate is still unknown.
 *
 * Null rather than a guess: capabilities are fetched at startup and a card that claimed "not
 * supported" for one frame and then took it back would be worse than a card that waits.
 */
export function catalogItemSupport(
  item: CatalogItemLike,
  target: InstallTarget
): CatalogSupport | null {
  if (target.substrate === null) return null

  if (item.type === 'image' || item.type === 'base_image') {
    return target.hasImageLibrary ? SUPPORTED : unsupported(NO_IMAGE_LIBRARY)
  }

  // Scenarios and content are era-neutral: a scenario is a YAML document and content is a
  // walkthrough, and neither names a substrate.
  if (item.type !== 'blueprint') return SUPPORTED

  const declared = reportedSchemaVersion(item)
  if (declared !== null && declared !== SCHEMA_VERSION_LEGACY && declared !== SCHEMA_VERSION_K8S) {
    return unsupported(
      `This blueprint declares schema version ${declared}, which this install does not ` +
        'understand. A newer blueprint than the engine is a refusal rather than a best effort.'
    )
  }

  if (target.isKubernetes) {
    if (declared === SCHEMA_VERSION_K8S) return SUPPORTED
    return unsupported(declared === null ? ERA_A_ON_KUBERNETES + NO_SCHEMA_REPORTED : ERA_A_ON_KUBERNETES)
  }

  return declared === SCHEMA_VERSION_K8S ? unsupported(ERA_B_ON_DOCKER) : SUPPORTED
}

/** The one-line banner for a listing that contains items this install cannot run. */
export function unsupportedListingNotice(count: number, isKubernetes: boolean): string {
  const items = `${count} item${count === 1 ? '' : 's'}`
  const written = isKubernetes ? 'for the Docker substrate' : 'for the Kubernetes substrate'
  return `${items} in this catalog were written ${written}. They are marked below: they can still be browsed, but they cannot be deployed on this install.`
}
