/// <reference types="vite/client" />
// frontend/src/lib/generatedPassword.test.ts
import { describe, it, expect, afterEach } from 'vitest'
import {
  generatePassword,
  NoSecureRandomSource,
  PASSWORD_ALPHABET,
  GENERATED_PASSWORD_LENGTH,
} from './generatedPassword'
// `?raw` rather than rendering the page: there is no DOM test environment here (vite.config.ts
// records why). A textual guard is weaker than a rendered one, but it is what fails if someone
// reintroduces Math.random into the credential path, which is the regression worth catching.
import admin from '../pages/Admin.tsx?raw'

const realCrypto = globalThis.crypto

afterEach(() => {
  Object.defineProperty(globalThis, 'crypto', {
    value: realCrypto,
    configurable: true,
    writable: true,
  })
})

describe('generatePassword', () => {
  it('draws from the CSPRNG, not from Math.random', () => {
    let calls = 0
    Object.defineProperty(globalThis, 'crypto', {
      value: {
        getRandomValues: (buffer: Uint8Array) => {
          calls += 1
          return realCrypto.getRandomValues(buffer)
        },
      },
      configurable: true,
      writable: true,
    })

    const password = generatePassword()
    expect(calls).toBeGreaterThan(0)
    expect(password).toHaveLength(GENERATED_PASSWORD_LENGTH)
  })

  it('refuses rather than falling back when the browser has no CSPRNG', () => {
    Object.defineProperty(globalThis, 'crypto', {
      value: undefined,
      configurable: true,
      writable: true,
    })

    expect(() => generatePassword()).toThrow(NoSecureRandomSource)
    // The refusal has to tell the operator what to do instead, because the button silently
    // producing nothing looks exactly like the button being broken.
    expect(() => generatePassword()).toThrow(/Type a password into the field instead/)
  })

  it('uses only the declared alphabet, at the declared length', () => {
    for (let i = 0; i < 200; i++) {
      const password = generatePassword()
      expect(password).toHaveLength(GENERATED_PASSWORD_LENGTH)
      for (const character of password) {
        expect(PASSWORD_ALPHABET).toContain(character)
      }
    }
  })

  it('rejects the byte values that would skew the alphabet', () => {
    // A two-character alphabet divides 256 exactly, so nothing is rejected; a three-character
    // one does not, and 255 is the value a modulo would fold back onto the first character.
    // Feeding it straight through proves the rejection happens rather than the bias.
    const scripted = [255, 255, 0, 1, 2]
    let next = 0
    Object.defineProperty(globalThis, 'crypto', {
      value: {
        getRandomValues: (buffer: Uint8Array) => {
          for (let i = 0; i < buffer.length; i++) {
            buffer[i] = scripted[next % scripted.length]
            next += 1
          }
          return buffer
        },
      },
      configurable: true,
      writable: true,
    })

    // 256 % 3 === 1, so 255 is the single rejected value and the first kept byte is 0 -> 'a'.
    expect(generatePassword(3, 'abc')).toBe('abc')
  })

  it('is not obviously constant', () => {
    const seen = new Set<string>()
    for (let i = 0; i < 50; i++) seen.add(generatePassword())
    expect(seen.size).toBe(50)
  })

  it('refuses a length or alphabet it cannot draw uniformly from', () => {
    expect(() => generatePassword(0)).toThrow(RangeError)
    expect(() => generatePassword(1.5)).toThrow(RangeError)
    expect(() => generatePassword(8, 'a')).toThrow(RangeError)
  })
})

describe('the admin console', () => {
  it('does not reach for Math.random', () => {
    // Blanket rather than scoped to the generator: the admin page is where accounts, tags and
    // roles are created, so there is nothing on it that should want a non-cryptographic random
    // number. Retry jitter elsewhere in the app is fine and is not covered by this.
    expect(admin).not.toMatch(/Math\.random/)
    expect(admin).toContain('generatePassword')
  })
})
