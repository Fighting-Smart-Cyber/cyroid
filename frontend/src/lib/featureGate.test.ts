import { describe, it, expect } from 'vitest'
import { featureGate } from './featureGate'
import type { Feature, Features } from '../stores/capabilitiesStore'

/**
 * These mirror what capabilities_for() returns for each substrate. They are written out rather
 * than derived so a flag that changes sides in the backend shows up here as a failing test.
 */
const DOCKER: Features = {
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

const KUBERNETES: Features = {
  image_cache: false,
  docker_registry: false,
  image_library: false,
  docker_status: false,
  range_composition: false,
  legacy_range_console: false,
  workloads: true,
  range_apps: true,
  blueprints: true,
  training_events: true,
  content_library: true,
  catalog: true,
}

/** The routes App.tsx gates, with the flag each one is gated on. */
const GATED_ROUTES: Array<[string, Feature]> = [
  ['/cache', 'image_cache'],
  ['/vm-library', 'image_library'],
  ['/ranges/new', 'range_composition'],
]

describe('featureGate', () => {
  it('waits while the backend has not answered', () => {
    // A redirect fired here throws the user off a page they are entitled to, on every reload.
    for (const [route, feature] of GATED_ROUTES) {
      expect(featureGate(null, feature), route).toBe('pending')
    }
  })

  it('denies every gated route on Kubernetes', () => {
    for (const [route, feature] of GATED_ROUTES) {
      expect(featureGate(KUBERNETES, feature), route).toBe('deny')
    }
  })

  it('allows every gated route on Docker', () => {
    for (const [route, feature] of GATED_ROUTES) {
      expect(featureGate(DOCKER, feature), route).toBe('allow')
    }
  })

  it('never denies a flag for want of an answer', () => {
    for (const feature of Object.keys(DOCKER) as Feature[]) {
      expect(featureGate(null, feature), feature).not.toBe('deny')
    }
  })

  it('treats an Era B flag the same way in reverse', () => {
    // The gate is not "is this Docker" -- it reads the flag, so it works for both eras.
    expect(featureGate(DOCKER, 'workloads')).toBe('deny')
    expect(featureGate(KUBERNETES, 'workloads')).toBe('allow')
  })
})
