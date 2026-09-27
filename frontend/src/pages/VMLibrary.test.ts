import { describe, it, expect } from 'vitest'
import { describeFailure, responseDetail } from './VMLibrary'

/** Axios recognises its own errors by the `isAxiosError` flag alone, so a literal stands in. */
const axiosError = (status: number, detail?: unknown) => ({
  isAxiosError: true,
  message: `Request failed with status code ${status}`,
  response: { status, data: detail === undefined ? {} : { detail } },
})

describe('describeFailure', () => {
  it('reads a 501 as the substrate', () => {
    const failure = describeFailure(
      axiosError(501, 'This install runs on the Kubernetes substrate.'),
      'The base images'
    )

    expect(failure).toEqual({
      kind: 'substrate',
      message: 'This install runs on the Kubernetes substrate.',
    })
  })

  it('does not read a 404 as the substrate', () => {
    // /images answers 404 for a base or golden image missing by id. Calling that the substrate
    // would replace the library with "No VM library on this install" on a host that has one.
    expect(describeFailure(axiosError(404, 'Base image not found'), 'The base images')).toEqual({
      kind: 'error',
      message: 'Base image not found',
    })
  })

  it('distinguishes a silent API from a server that answered', () => {
    expect(
      describeFailure(
        { isAxiosError: true, message: 'Network Error', response: undefined },
        'The snapshots'
      ).message
    ).toBe('The snapshots could not be read: the API did not answer.')
    expect(describeFailure(axiosError(500), 'The snapshots').message).toBe(
      'The snapshots could not be read (HTTP 500).'
    )
  })
})

describe('responseDetail', () => {
  it('never hands a 422 validation list to a toast', () => {
    expect(responseDetail(axiosError(422, [{ loc: ['body', 'name'], msg: 'field required' }]))).toBeNull()
    expect(responseDetail(axiosError(409, 'an image with that name exists'))).toBe(
      'an image with that name exists'
    )
  })
})
