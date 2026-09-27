// frontend/src/lib/generatedPassword.ts
/**
 * The password an administrator generates for a new account.
 *
 * SEC-096: this was `Math.random()`. V8 seeds one xorshift128+ state per context and hands out
 * its stream; an attacker who observes a few outputs of that stream can solve for the state and
 * then produce every value it will ever yield, forwards and backwards. The generated string is
 * the initial credential of a real account on a platform that will be accredited, so it has to
 * come from the platform's CSPRNG, not from the one that exists to shuffle animations.
 *
 * The second, quieter defect is bias. `byte % alphabet.length` maps 256 byte values onto 70
 * characters, so the first 46 characters of the alphabet come up on four byte values each and
 * the remaining 24 on three -- a third more likely, for no reason the reader of the code would
 * notice. That narrows the search an attacker has to do. Rejecting the bytes above the largest
 * whole multiple of the alphabet size removes it entirely, at the cost of redrawing about 18%
 * of the time.
 *
 * This lives in lib/ rather than in the page because there is no DOM test environment in this
 * repository (vite.config.ts records why), so a pure function is the only form of this that can
 * be tested at all.
 */

/** Mixed case, digits and punctuation -- 70 characters, unchanged from the original generator. */
export const PASSWORD_ALPHABET =
  'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789!@#$%^&*'

/** 16 characters over this alphabet is a little over 98 bits, which is well past any offline attack. */
export const GENERATED_PASSWORD_LENGTH = 16

/**
 * Thrown rather than falling back to a weaker source. A generator that quietly degrades to
 * `Math.random()` when the Web Crypto API is missing is the bug this file exists to remove, so
 * the failure is loud and the caller tells the operator to type a password instead.
 */
export class NoSecureRandomSource extends Error {
  constructor() {
    super(
      'This browser did not provide crypto.getRandomValues, so no password can be generated ' +
        'safely. Type a password into the field instead, or open the console over HTTPS.'
    )
    this.name = 'NoSecureRandomSource'
  }
}

/**
 * The CSPRNG, or a refusal. `crypto.getRandomValues` is available in insecure contexts as well
 * as secure ones -- only `crypto.subtle` requires HTTPS -- so serving this console over plain
 * HTTP on a lab network does not take the generator away.
 */
function fillRandomBytes(buffer: Uint8Array): void {
  const source = globalThis.crypto
  if (!source || typeof source.getRandomValues !== 'function') {
    throw new NoSecureRandomSource()
  }
  source.getRandomValues(buffer)
}

/**
 * A password of `length` characters drawn uniformly from `alphabet`.
 *
 * Throws {@link NoSecureRandomSource} if the browser has no CSPRNG, and never returns a value
 * derived from anything else.
 */
export function generatePassword(
  length: number = GENERATED_PASSWORD_LENGTH,
  alphabet: string = PASSWORD_ALPHABET
): string {
  if (!Number.isInteger(length) || length < 1) {
    throw new RangeError('generatePassword: length must be a positive whole number')
  }
  if (alphabet.length < 2 || alphabet.length > 256) {
    throw new RangeError('generatePassword: alphabet must hold between 2 and 256 characters')
  }

  // The largest multiple of the alphabet size that fits in a byte. Bytes at or above it would
  // map onto the first few characters a second time, which is where the bias comes from, so
  // they are thrown away and redrawn.
  const unbiasedCeiling = 256 - (256 % alphabet.length)

  const out: string[] = []
  const draw = new Uint8Array(length)
  // Bounded rather than `while (true)`: each pass keeps ~82% of its bytes, so the chance of
  // needing more than a few passes is negligible, and a bound means a pathological RNG cannot
  // hang the tab.
  for (let pass = 0; pass < 64 && out.length < length; pass++) {
    fillRandomBytes(draw)
    for (let i = 0; i < draw.length && out.length < length; i++) {
      const byte = draw[i]
      if (byte >= unbiasedCeiling) continue
      out.push(alphabet[byte % alphabet.length])
    }
  }

  if (out.length < length) {
    throw new NoSecureRandomSource()
  }
  return out.join('')
}
