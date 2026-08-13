import { describe, expect, it } from 'vitest';

import { InstanceAuthorizationContext } from './InstanceAuthorizationContext';

describe('InstanceAuthorizationContext', () => {
  it('defaults to a fail-closed capability projection', () => {
    expect(InstanceAuthorizationContext._currentValue?.capabilities.can_chat).toBe(false);
  });
});
