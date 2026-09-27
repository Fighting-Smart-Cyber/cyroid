import { describe, expect, it } from 'vitest'
import { catalogItemSupport, unsupportedListingNotice } from './catalogSupport'

const kubernetes = { substrate: 'kubernetes', isKubernetes: true, hasImageLibrary: false }
const docker = { substrate: 'dind', isKubernetes: false, hasImageLibrary: true }
const unknown = { substrate: null, isKubernetes: false, hasImageLibrary: false }

describe('catalogItemSupport', () => {
  it('gives no verdict until the substrate is known', () => {
    expect(catalogItemSupport({ type: 'blueprint' }, unknown)).toBeNull()
  })

  it('refuses a blueprint that reports no schema version on Kubernetes', () => {
    const support = catalogItemSupport({ type: 'blueprint' }, kubernetes)
    expect(support?.supported).toBe(false)
    expect(support?.reason).toContain('Era A blueprint')
    // The absence is explained rather than presented as a declaration the item made.
    expect(support?.reason).toContain('reports no schema version')
  })

  it('refuses a blueprint that declares v1 on Kubernetes, without the absence caveat', () => {
    const support = catalogItemSupport({ type: 'blueprint', schema_version: 1 }, kubernetes)
    expect(support?.supported).toBe(false)
    expect(support?.reason).toContain('Era A blueprint')
    expect(support?.reason).not.toContain('reports no schema version')
  })

  it('accepts a v2 blueprint on Kubernetes', () => {
    expect(catalogItemSupport({ type: 'blueprint', schema_version: 2 }, kubernetes)).toEqual({
      supported: true,
      reason: '',
      badge: '',
    })
  })

  it('refuses a v2 blueprint on Docker, and leaves Era A alone there', () => {
    expect(catalogItemSupport({ type: 'blueprint', schema_version: 2 }, docker)?.supported).toBe(
      false
    )
    expect(catalogItemSupport({ type: 'blueprint' }, docker)?.supported).toBe(true)
    expect(catalogItemSupport({ type: 'blueprint', schema_version: 1 }, docker)?.supported).toBe(
      true
    )
  })

  it('refuses a schema version neither era uses', () => {
    const support = catalogItemSupport({ type: 'blueprint', schema_version: 7 }, kubernetes)
    expect(support?.supported).toBe(false)
    expect(support?.reason).toContain('schema version 7')
  })

  it('ignores a schema version that is not a number', () => {
    for (const junk of [null, '2', {}, []]) {
      const support = catalogItemSupport({ type: 'blueprint', schema_version: junk }, kubernetes)
      expect(support?.reason).toContain('Era A blueprint')
    }
  })

  it('refuses image items only where there is no image library', () => {
    for (const type of ['image', 'base_image']) {
      expect(catalogItemSupport({ type }, kubernetes)?.supported).toBe(false)
      expect(catalogItemSupport({ type }, kubernetes)?.reason).toContain('image library')
      expect(catalogItemSupport({ type }, docker)?.supported).toBe(true)
    }
  })

  it('leaves era-neutral item types alone on both substrates', () => {
    for (const type of ['content', 'scenario']) {
      expect(catalogItemSupport({ type }, kubernetes)?.supported).toBe(true)
      expect(catalogItemSupport({ type }, docker)?.supported).toBe(true)
    }
  })

  it('gives every refusal a badge and every acceptance none', () => {
    expect(catalogItemSupport({ type: 'blueprint' }, kubernetes)?.badge).toBe('Not supported here')
    expect(catalogItemSupport({ type: 'blueprint' }, docker)?.badge).toBe('')
  })
})

describe('unsupportedListingNotice', () => {
  it('counts in the singular and names the substrate the items were written for', () => {
    expect(unsupportedListingNotice(1, true)).toContain('1 item in this catalog')
    expect(unsupportedListingNotice(1, true)).toContain('for the Docker substrate')
    expect(unsupportedListingNotice(4, false)).toContain('4 items in this catalog')
    expect(unsupportedListingNotice(4, false)).toContain('for the Kubernetes substrate')
  })
})
