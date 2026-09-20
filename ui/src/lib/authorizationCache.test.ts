import { describe, expect, it } from 'vitest';

import { isAuthorizationSensitiveReadPath } from './authorizationCache';

describe('isAuthorizationSensitiveReadPath', () => {
  it.each([
    '/api/agents',
    '/api/skills?scope=global',
    '/api/vault/secrets',
    '/api/show-pages',
  ])('invalidates the %s resource cache after authorization changes', (path) => {
    expect(isAuthorizationSensitiveReadPath(path)).toBe(true);
  });

  it('does not flush unrelated stable reads', () => {
    expect(isAuthorizationSensitiveReadPath('/api/version')).toBe(false);
  });
});
