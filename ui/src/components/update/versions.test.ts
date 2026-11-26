import { describe, expect, it } from 'vitest';
import { compareVersions, keepCheckedOffer } from './versions';
import type { UpdateSnapshot } from './types';

describe('compareVersions', () => {
  it('orders the PEP 440 forms a Runtime reports', () => {
    const ordered = ['3.2.8.dev1', '3.2.8a1', '3.2.8b2', '3.2.8rc1', '3.2.8rc10', '3.2.8', '3.2.8.post1', '3.2.9'];
    for (let index = 1; index < ordered.length; index += 1) {
      expect(compareVersions(ordered[index - 1], ordered[index])).toBeLessThan(0);
    }
    expect(compareVersions('3.2', '3.2.0')).toBe(0);
    expect(compareVersions('3.2.8-rc.1', '3.2.8rc1')).toBe(0);
  });
});

describe('keepCheckedOffer', () => {
  const read = (patch: Partial<UpdateSnapshot>): UpdateSnapshot => ({
    current: '3.2.7', latest: '3.2.7', has_update: false, error: null, prereleases: true, ...patch,
  });
  const checked = read({ latest: '3.2.8rc1', has_update: true });

  it.each([
    ['an unprompted read without an offer', read({}), '3.2.8rc1'],
    ['an older announced offer', read({ latest: '3.2.7.post1', has_update: true }), '3.2.8rc1'],
    ['a newer announced offer', read({ latest: '3.2.9', has_update: true }), '3.2.9'],
    ['another running version', read({ current: '3.2.8rc1', latest: '3.2.8rc1' }), '3.2.8rc1'],
    ['another channel', read({ prereleases: false }), '3.2.7'],
  ])('over %s offers %s', (_case, unprompted, latest) => {
    expect(keepCheckedOffer(unprompted, checked).latest).toBe(latest);
  });

  it('keeps nothing once the running version changes', () => {
    expect(keepCheckedOffer(read({ current: '3.2.8rc1', latest: '3.2.8rc1' }), checked).has_update).toBe(false);
  });
});
