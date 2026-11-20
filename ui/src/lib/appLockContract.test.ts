import { describe, expect, it } from 'vitest';

import { APP_LOCK_FIXTURES, parseAppLockSetResult, parseAppLockStatus } from './appLockContract';

describe('App Lock wire contract', () => {
  it('decodes every fixture unchanged', () => {
    for (const [name, payload] of Object.entries(APP_LOCK_FIXTURES)) {
      const decoded = 'ok' in payload ? parseAppLockSetResult(payload) : parseAppLockStatus(payload);
      expect(decoded, name).toEqual(payload);
    }
  });

  it('rejects a status outside the contract', () => {
    const base = APP_LOCK_FIXTURES.iphoneOff;
    expect(parseAppLockStatus(null)).toBeNull();
    expect(parseAppLockStatus({ ...base, policy: 'thirtySeconds' })).toBeNull();
    expect(parseAppLockStatus({ ...base, available: 'yes' })).toBeNull();
    expect(parseAppLockStatus({ ...base, biometry: 'iris' })).toBeNull();
    expect(parseAppLockStatus({ ...base, biometryState: 'ok' })).toBeNull();
    expect(parseAppLockStatus({ ...base, bio_state: 'usable', biometryState: undefined })).toBeNull();
  });

  it('rejects a result without a status or with an unknown reason', () => {
    expect(parseAppLockSetResult({ ok: true })).toBeNull();
    expect(parseAppLockSetResult({ ...APP_LOCK_FIXTURES.setCancelled, reason: 'timeout' })).toBeNull();
    expect(parseAppLockSetResult({ ...APP_LOCK_FIXTURES.setCancelled, ok: 'false' })).toBeNull();
  });
});
