// frontend/src/stores/capabilitiesStore.ts
/**
 * What this install can do, asked once and answered by the backend.
 *
 * The product runs on one of two substrates and they do not offer the same things. Before this
 * store the frontend had no way to ask, so it drew the Docker product on a Kubernetes install and
 * the user met panels that could only fail. The backend owns the policy
 * (`/api/v1/system/capabilities`); this is the client for it.
 *
 * `features` is null until the answer arrives, and `can()` is false while it is. A gated surface
 * therefore appears a moment late rather than appearing wrongly and then vanishing -- on the
 * substrate that matters, wrongly means broken.
 */
import { useMemo } from 'react'
import { isAxiosError } from 'axios'
import { create } from 'zustand'
import { api } from '../services/api'

export interface Features {
  // Era A (Docker) only
  image_cache: boolean
  docker_registry: boolean
  image_library: boolean
  docker_status: boolean
  range_composition: boolean
  legacy_range_console: boolean
  // Era B (Kubernetes) only
  workloads: boolean
  range_apps: boolean
  // Both
  blueprints: boolean
  training_events: boolean
  content_library: boolean
  catalog: boolean
}

export type Feature = keyof Features

interface Capabilities {
  substrate: string
  substrate_label: string
  features: Features
}

interface CapabilitiesState {
  substrate: string | null
  substrateLabel: string | null
  features: Features | null
  isLoading: boolean
  /** Set when the answer could not be obtained at all, so a surface can say so. */
  unavailable: boolean
  fetchCapabilities: () => Promise<void>
}

/**
 * What an install that predates `/system/capabilities` can do.
 *
 * The endpoint shipped with the Kubernetes work, so a **404** from it is itself the answer:
 * this is a Docker install. Nothing else is. A 502 through the ingress, a timeout while the API
 * pod is still rolling after an update, or a 401 during token refresh at boot are all failures
 * to ask -- and answering them with the Docker feature set re-opens every Era A surface on a
 * Kubernetes install, which is the defect the whole store exists to close.
 */
const DOCKER_ONLY: Features = {
  image_cache: true,
  docker_registry: true,
  image_library: true,
  docker_status: true,
  range_composition: true,
  legacy_range_console: true,
  workloads: false,
  range_apps: false,
  blueprints: true,
  training_events: true,
  content_library: true,
  catalog: true,
}

const RETRIES = 3
const RETRY_DELAY_MS = 1500

const wait = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

export const useCapabilitiesStore = create<CapabilitiesState>((set) => ({
  substrate: null,
  substrateLabel: null,
  features: null,
  isLoading: false,
  unavailable: false,

  fetchCapabilities: async () => {
    set({ isLoading: true, unavailable: false })
    for (let attempt = 0; attempt < RETRIES; attempt++) {
      try {
        const response = await api.get<Capabilities>('/system/capabilities')
        set({
          substrate: response.data.substrate,
          substrateLabel: response.data.substrate_label,
          features: response.data.features,
          isLoading: false,
          unavailable: false,
        })
        return
      } catch (err: unknown) {
        if (isAxiosError(err) && err.response?.status === 404) {
          set({
            substrate: 'dind',
            substrateLabel: 'Docker',
            features: DOCKER_ONLY,
            isLoading: false,
            unavailable: false,
          })
          return
        }
        if (attempt < RETRIES - 1) await wait(RETRY_DELAY_MS * (attempt + 1))
      }
    }
    // Still no answer. `features` stays null, so every gated surface stays shut: a page that
    // cannot work is worse than a page that is missing, and this is recoverable -- the next
    // call to fetchCapabilities replaces it.
    set({ isLoading: false, unavailable: true })
  },
}))

/** True only once the backend has said so. */
export function useFeature(feature: Feature): boolean {
  return useCapabilitiesStore((s) => s.features?.[feature] ?? false)
}

/** The substrate this install runs on, or null until known.
 *
 * Three scalar selections and a memo, not one selector returning an object: a selector that
 * builds its result re-builds it on every snapshot, so the value is never reference-equal and
 * React is handed an uncached snapshot -- the shape it warns about, and one that re-renders every
 * consumer on any unrelated change to this store.
 */
export function useSubstrate(): { substrate: string | null; label: string | null; isKubernetes: boolean } {
  const substrate = useCapabilitiesStore((s) => s.substrate)
  const label = useCapabilitiesStore((s) => s.substrateLabel)
  return useMemo(
    () => ({ substrate, label, isKubernetes: substrate === 'kubernetes' }),
    [substrate, label]
  )
}
