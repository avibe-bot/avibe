import { describe, expect, it } from 'vitest';

import { canUseAppsSurface } from './InstanceAuthorizationContext';

describe('canUseAppsSurface', () => {
  it.each([
    ['local', false, undefined, true],
    ['remote active Organization member', true, true, true],
    ['remote principal without Organization access', true, false, false],
    ['remote principal with an incomplete signal', true, undefined, false],
  ])('%s follows the temporary Apps policy', (_label, remote, temporaryAccess, expected) => {
    expect(canUseAppsSurface(remote, temporaryAccess)).toBe(expected);
  });
});
