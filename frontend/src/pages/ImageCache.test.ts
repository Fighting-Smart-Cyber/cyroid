import { describe, it, expect } from 'vitest'

/**
 * `authStore` reads `localStorage` while its module body runs, and vitest's environment here is
 * `node` -- there is no jsdom dependency and adding one is out of scope. Importing the page for
 * its pure helpers therefore has to put a store in place first, which is why this is a top-level
 * await rather than a plain import: the stub must exist before the module graph is evaluated.
 */
const store = new Map<string, string>()
;(globalThis as { localStorage?: unknown }).localStorage = {
  getItem: (key: string) => store.get(key) ?? null,
  setItem: (key: string, value: string) => void store.set(key, value),
  removeItem: (key: string) => void store.delete(key),
  clear: () => store.clear(),
  key: () => null,
  length: 0,
}

const { describeFailure, noDirectDownload, responseDetail, structuredMessage } = await import(
  './ImageCache'
)

/**
 * Axios recognises its own errors by the `isAxiosError` flag alone, so a literal is a faithful
 * stand-in for one -- no network, no adapter.
 */
const axiosError = (status: number, detail?: unknown) => ({
  isAxiosError: true,
  message: `Request failed with status code ${status}`,
  response: { status, data: detail === undefined ? {} : { detail } },
})

const noAnswer = { isAxiosError: true, message: 'Network Error', response: undefined }

describe('describeFailure', () => {
  it("reads a 501 as the substrate and keeps the server's own sentence", () => {
    const failure = describeFailure(
      axiosError(
        501,
        'This install runs on the Kubernetes substrate. GET /api/v1/cache/stats belongs to the Docker substrate and is not available here.'
      ),
      'The image cache'
    )

    expect(failure.kind).toBe('substrate')
    expect(failure.message).toContain('Kubernetes substrate')
  })

  it('falls back to a plain sentence when a 501 carries no detail', () => {
    expect(describeFailure(axiosError(501), 'The image cache')).toEqual({
      kind: 'substrate',
      message: 'The image cache is not available on this install.',
    })
  })

  it('does not read a 404 as the substrate', () => {
    // A Docker host answers 404 for a resource missing by id -- a pull status, a custom ISO.
    // Classifying one as `substrate` renders "No image cache on this install" and tells the
    // operator a cluster pulls range images, on a host where neither is true.
    const failure = describeFailure(axiosError(404, 'Custom ISO not found'), 'The image cache')

    expect(failure.kind).toBe('error')
    expect(failure.message).toBe('Custom ISO not found')
  })

  it('reads a 503 as a fault and keeps the daemon message the handler attached', () => {
    const failure = describeFailure(
      axiosError(503, 'GET /api/v1/cache/stats needs the Docker daemon and the call failed'),
      'The image cache'
    )

    expect(failure.kind).toBe('error')
    expect(failure.message).toContain('needs the Docker daemon')
  })

  it('says the API did not answer rather than quoting axios', () => {
    expect(describeFailure(noAnswer, 'The image cache')).toEqual({
      kind: 'error',
      message: 'The image cache could not be read: the API did not answer.',
    })
  })

  it('names the status when the body carries nothing readable', () => {
    expect(describeFailure(axiosError(500), 'The image cache').message).toBe(
      'The image cache could not be read (HTTP 500).'
    )
  })

  it('does not put a 422 validation list in front of the user', () => {
    const validation = axiosError(422, [{ loc: ['body', 'image'], msg: 'field required' }])

    expect(describeFailure(validation, 'The image cache')).toEqual({
      kind: 'error',
      message: 'The image cache could not be read (HTTP 422).',
    })
  })

  it('handles something that is not an axios error at all', () => {
    expect(describeFailure(new TypeError('boom'), 'The image cache')).toEqual({
      kind: 'error',
      message: 'The image cache could not be read.',
    })
  })
})

describe('responseDetail', () => {
  it('accepts only a non-empty string', () => {
    expect(responseDetail(axiosError(500, 'no space left'))).toBe('no space left')
    expect(responseDetail(axiosError(500, '   '))).toBeNull()
    expect(responseDetail(axiosError(500, { status: 'no_direct_download' }))).toBeNull()
    expect(responseDetail(axiosError(422, [{ msg: 'field required' }]))).toBeNull()
  })
})

describe('noDirectDownload', () => {
  it("narrows the ISO routes' structured refusal field by field", () => {
    expect(
      noDirectDownload(
        axiosError(400, {
          status: 'no_direct_download',
          message: 'that publisher does not serve this ISO directly',
          download_page: 'https://example.invalid/iso',
        })
      )
    ).toEqual({
      message: 'that publisher does not serve this ISO directly',
      downloadPage: 'https://example.invalid/iso',
    })
  })

  it('drops fields that are not strings rather than rendering them', () => {
    expect(
      noDirectDownload(axiosError(400, { status: 'no_direct_download', message: { code: 7 } }))
    ).toEqual({ message: undefined, downloadPage: undefined })
  })

  it('ignores a detail of any other shape', () => {
    expect(noDirectDownload(axiosError(400, { status: 'something_else' }))).toBeNull()
    expect(noDirectDownload(axiosError(400, 'plain string'))).toBeNull()
    expect(noDirectDownload(axiosError(400))).toBeNull()
  })
})

describe('structuredMessage', () => {
  it('finds a readable sentence inside a structured detail', () => {
    expect(structuredMessage(axiosError(400, { message: 'that version is withdrawn' }))).toBe(
      'that version is withdrawn'
    )
    expect(structuredMessage(axiosError(400, { detail: 'nested' }))).toBe('nested')
  })

  it('refuses a blank one, so the caller falls through to its own words', () => {
    expect(structuredMessage(axiosError(400, { message: '  ' }))).toBeNull()
    expect(structuredMessage(axiosError(400, 'already a string'))).toBeNull()
  })
})
