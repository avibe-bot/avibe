import { describe, expect, it } from 'vitest';

import {
  acceptsProfilePageCompletion,
  profilePageFreshness,
  profileReportLanguage,
} from './profileReportState';

describe('profile page request state', () => {
  it('normalizes the UI locale to the closed page-language allowlist', () => {
    expect(profileReportLanguage('zh-CN')).toBe('zh');
    expect(profileReportLanguage('ZH')).toBe('zh');
    expect(profileReportLanguage('en-US')).toBe('en');
  });

  it('discards a completion after the language changes or a newer operation starts', () => {
    expect(acceptsProfilePageCompletion('en', 'zh', 2, 2)).toBe(false);
    expect(acceptsProfilePageCompletion('en', 'en', 1, 2)).toBe(false);
    expect(acceptsProfilePageCompletion('en', 'en', 2, 2)).toBe(true);
  });

  it('uses opaque source snapshots for current, stale, and unknown states', () => {
    expect(profilePageFreshness('sha256:a', 'sha256:a')).toBe('current');
    expect(profilePageFreshness('sha256:a', 'sha256:b')).toBe('stale');
    expect(profilePageFreshness(undefined, 'sha256:b')).toBe('unknown');
    expect(profilePageFreshness('sha256:a', null)).toBe('unknown');
  });
});
