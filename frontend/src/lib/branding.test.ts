import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { BRANDING, applyBranding, rgbTriplet } from './branding'

/**
 * A distribution re-themes the engine through GET /api/v1/branding rather than
 * by rebuilding (ADR-0011, MIG-8). What that endpoint returns is someone's
 * configuration, so this has to survive whatever is in it.
 */

const DEFAULTS = { ...BRANDING }

// Braces matter: an arrow returning the object hands vitest a value it treats
// as a cleanup callback, which tsc rejects.
beforeEach(() => {
  Object.assign(BRANDING, DEFAULTS)
})
afterEach(() => {
  Object.assign(BRANDING, DEFAULTS)
})

describe('rgbTriplet', () => {
  it('converts hex to the triplet Tailwind needs', () => {
    expect(rgbTriplet('#2563eb')).toBe('37 99 235')
  })

  it('accepts a missing hash, surrounding space and either case', () => {
    expect(rgbTriplet('2563EB')).toBe('37 99 235')
    expect(rgbTriplet('  #2563eb  ')).toBe('37 99 235')
  })

  it('handles the ends of the range', () => {
    expect(rgbTriplet('#000000')).toBe('0 0 0')
    expect(rgbTriplet('#ffffff')).toBe('255 255 255')
  })

  it('rejects anything that is not a six-digit hex', () => {
    // Rejected rather than coerced: a wrong colour silently applied is harder
    // to notice than one that did not change.
    for (const bad of ['', '#fff', 'red', '#12345', '#1234567', 'rgb(1,2,3)', '#zzzzzz']) {
      expect(rgbTriplet(bad), `${bad} should be rejected`).toBeNull()
    }
  })
})

describe('applyBranding', () => {
  it('overrides the product name and tagline', () => {
    // A neutral name on purpose: test_branding_is_centralised.py forbids a
    // distribution's name anywhere outside the branding module, and a test is
    // no exception. It caught this exact line.
    applyBranding({ product_name: 'ACME RANGE', tagline: 'Training ranges' })
    expect(BRANDING.productName).toBe('ACME RANGE')
    expect(BRANDING.tagline).toBe('Training ranges')
  })

  it('leaves the defaults alone when the response omits fields', () => {
    applyBranding({})
    expect(BRANDING.productName).toBe(DEFAULTS.productName)
    expect(BRANDING.tagline).toBe(DEFAULTS.tagline)
  })

  it('ignores empty and whitespace-only values', () => {
    // An unset configuration key arriving as "" must not blank the UI.
    applyBranding({ product_name: '', tagline: '   ' })
    expect(BRANDING.productName).toBe(DEFAULTS.productName)
    expect(BRANDING.tagline).toBe(DEFAULTS.tagline)
  })

  it('trims what it does accept', () => {
    applyBranding({ product_name: '  ACME RANGE  ' })
    expect(BRANDING.productName).toBe('ACME RANGE')
  })

  it('does not throw on a malformed palette', () => {
    // A bad colour should cost that colour, not the application.
    expect(() =>
      applyBranding({ primary_palette: { '600': 'not-a-colour', '700': '#1d4ed8' } })
    ).not.toThrow()
  })

  it('defaults to the engine, not to a distribution', () => {
    // Shipping the engine named after one distribution is the inversion MIG-8
    // exists to undo.
    expect(DEFAULTS.productName).toBe('CYROID')
  })
})
