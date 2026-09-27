import { describe, it, expect } from 'vitest';
import { AxiosError } from 'axios';
import { deployErrorDetail, deployFailureMessage } from './RangeWizardPage';

/**
 * The wizard used to end a failed deploy with "Failed to deploy range. Please check the console
 * for details." -- no reason, and an orphaned range left in the list on every attempt. These
 * cover the two halves of the message that replaced it: the server's own reason, and whether the
 * range the wizard created is still there.
 */

function serverRefusal(detail: unknown, status = 400): AxiosError {
  const error = new AxiosError(`Request failed with status code ${status}`);
  error.response = {
    data: { detail },
    status,
    statusText: '',
    headers: {},
    config: { headers: {} },
  } as unknown as AxiosError['response'];
  return error;
}

describe('deployErrorDetail', () => {
  it('returns the reason the server gave', () => {
    expect(
      deployErrorDetail(serverRefusal('Range has no blueprint to deploy'), 'unused')
    ).toBe('Range has no blueprint to deploy');
  });

  it('reads the object the image validator refuses with', () => {
    // The shape api/ranges.py raises on the Docker path, which is the only substrate that still
    // offers this page -- and the one failure a wizard user meets most.
    const message = deployErrorDetail(
      serverRefusal({
        message: 'Deployment validation failed. All images must be cached before deployment.',
        errors: ['VM web-01: image nginx:1.25 is not cached'],
        hint: 'Use the Image Cache page to pre-pull container images.',
      }),
      'unused'
    );
    expect(message).toContain('All images must be cached');
    expect(message).toContain('nginx:1.25');
    expect(message).toContain('Image Cache page');
  });

  it('keeps a long list of validation errors out of the toast', () => {
    const errors = Array.from({ length: 7 }, (_, i) => `VM host-${i}: image is not cached`);
    const message = deployErrorDetail(
      serverRefusal({ message: 'Deployment validation failed.', errors }),
      'unused'
    );
    expect(message).toContain('host-0');
    expect(message).toContain('And 4 more');
    expect(message).not.toContain('host-6');
  });

  it('reads the list FastAPI refuses a schema error with', () => {
    const listDetail = [{ loc: ['body', 'name'], msg: 'field required' }];
    expect(deployErrorDetail(serverRefusal(listDetail, 422), 'unused')).toContain(
      'field required'
    );
  });

  it('never passes off axios own message as the server reason', () => {
    // "Request failed with status code 400" is the status again and nothing more; saying so
    // plainly is the difference between a reason and an echo.
    const message = deployErrorDetail(serverRefusal(null, 403), 'fallback');
    expect(message).not.toContain('Request failed with status code');
    expect(message).toContain('403');
  });

  it('distinguishes a server that refused from an API that never answered', () => {
    expect(deployErrorDetail(new AxiosError('Network Error'), 'fallback')).toBe(
      'The API did not answer.'
    );
  });

  it('carries the message of a refusal raised here rather than by the server', () => {
    const local = new Error('None of the configured machines could be matched to a base image.');
    expect(deployErrorDetail(local, 'fallback')).toBe(local.message);
  });

  it('falls back when nothing threw anything legible', () => {
    expect(deployErrorDetail('some string', 'The server gave no reason.')).toBe(
      'The server gave no reason.'
    );
    expect(deployErrorDetail(undefined, 'The server gave no reason.')).toBe(
      'The server gave no reason.'
    );
  });
});

describe('deployFailureMessage', () => {
  it('names the range and the reason', () => {
    const message = deployFailureMessage('Recovery Lab', 'Range has no blueprint', false);
    expect(message).toContain('Recovery Lab');
    expect(message).toContain('Range has no blueprint');
  });

  it('says nothing about cleanup when the rollback worked', () => {
    const message = deployFailureMessage('Recovery Lab', 'Range has no blueprint', false);
    expect(message).not.toContain('Ranges page');
  });

  it('tells the user where the range is when the rollback did not', () => {
    const message = deployFailureMessage('Recovery Lab', 'Range has no blueprint', true);
    expect(message).toContain('Ranges page');
    expect(message).toContain('Recovery Lab');
  });

  it('reads as sentences whether or not the server punctuated its detail', () => {
    expect(deployFailureMessage('Lab', 'no blueprint', false)).toBe(
      'Could not deploy "Lab": no blueprint.'
    );
    expect(deployFailureMessage('Lab', 'no blueprint.', false)).toBe(
      'Could not deploy "Lab": no blueprint.'
    );
  });
});
