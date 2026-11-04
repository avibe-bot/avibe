// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';

import { forgetBootSession, readBootSession, recordBootSession } from './bootSession';
import { OWNER_INSTANCE_CAPABILITIES } from './sessionInfo';

// Forgetting is what keeps a signed-out or revoked reader from being replayed on
// the next cold start, so it must hold even when storage refuses writes.

const reader = {
  remote: true as const,
  authenticated: true as const,
  email: 'alice@example.com',
  instance_kind: 'personal' as const,
  instance_role: 'owner' as const,
  capabilities: OWNER_INSTANCE_CAPABILITIES,
  authorization_state: 'current' as const,
};

afterEach(() => {
  vi.restoreAllMocks();
  window.localStorage.clear();
});

describe('forgetting the boot session', () => {
  it('deletes the record even when storage is too full to advance the generation', () => {
    recordBootSession(reader);
    expect(readBootSession()).not.toBeNull();
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('full', 'QuotaExceededError');
    });
    forgetBootSession();
    expect(readBootSession()).toBeNull();
  });

  it('ignores a record written after another tab forgot it, by the generation it carries', () => {
    recordBootSession(reader);
    const lateWrite = window.localStorage.getItem('avibe.boot-session.v1');
    expect(lateWrite).not.toBeNull();
    // Another tab forgets between this tab's generation check and its write...
    forgetBootSession();
    // ...and the write lands afterwards.
    window.localStorage.setItem('avibe.boot-session.v1', lateWrite!);
    expect(readBootSession()).toBeNull();
  });
});
