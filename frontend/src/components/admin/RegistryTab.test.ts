import { describe, it, expect } from 'vitest'
import { describeFailure, pushableTag } from './RegistryTab'
import type { CachedImage } from '../../types'

/** Axios recognises its own errors by the `isAxiosError` flag alone, so a literal stands in. */
const axiosError = (status: number, detail?: unknown) => ({
  isAxiosError: true,
  message: `Request failed with status code ${status}`,
  response: { status, data: detail === undefined ? {} : { detail } },
})

const image = (tags: string[]): CachedImage => ({
  id: 'sha256:1f4e2a',
  tags,
  size_bytes: 1024,
  size_gb: 0,
  created: null,
})

describe('describeFailure', () => {
  it('reads a 501 as the substrate', () => {
    expect(
      describeFailure(axiosError(501, 'This install runs on the Kubernetes substrate.'), 'Registry status')
    ).toEqual({ kind: 'substrate', message: 'This install runs on the Kubernetes substrate.' })
  })

  it('does not read a 404 as the substrate', () => {
    // Any one substrate failure replaces the whole tab, so a 404 from a single route would claim
    // this host has no registry while the registry container is running next to the daemon.
    expect(describeFailure(axiosError(404, 'Push operation not found'), 'Registry status')).toEqual({
      kind: 'error',
      message: 'Push operation not found',
    })
  })

  it('keeps a 503 as a fault, which is what a stopped daemon is on a Docker host', () => {
    expect(describeFailure(axiosError(503, 'the call failed'), 'Registry status').kind).toBe('error')
  })
})

describe('pushableTag', () => {
  it('returns the first real tag', () => {
    expect(pushableTag(image(['nginx:1.27', 'nginx:latest']))).toBe('nginx:1.27')
  })

  it('skips a dangling tag, which the push would resolve to a repository named for a digest', () => {
    expect(pushableTag(image(['<none>:<none>', 'ubuntu:24.04']))).toBe('ubuntu:24.04')
    expect(pushableTag(image(['<none>:<none>']))).toBeNull()
  })

  it('treats an untagged image as unpushable rather than offering its id', () => {
    expect(pushableTag(image([]))).toBeNull()
  })
})
