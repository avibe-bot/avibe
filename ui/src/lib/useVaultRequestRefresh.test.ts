import { describe, expect, it } from 'vitest';

import { shouldPollVaultRequests } from './useVaultRequestRefresh';

describe('Vault request refresh mode', () => {
  it('polls only while the controller event bridge is unavailable', () => {
    expect(shouldPollVaultRequests(false)).toBe(true);
    expect(shouldPollVaultRequests(true)).toBe(false);
  });
});
