import { describe, expect, it } from 'vitest';

import {
  acceptsProfileReportCompletion,
  profileReportLanguage,
  profileReportRequestKey,
} from './profileReportState';

describe('profile report request state', () => {
  it('normalizes the UI locale to the closed report-language allowlist', () => {
    expect(profileReportLanguage('zh-CN')).toBe('zh');
    expect(profileReportLanguage('ZH')).toBe('zh');
    expect(profileReportLanguage('en-US')).toBe('en');
  });

  it('discards a completion after either a profile refresh or language change', () => {
    const original = profileReportRequestKey(4, 'en');

    expect(acceptsProfileReportCompletion(original, profileReportRequestKey(5, 'en'))).toBe(false);
    expect(acceptsProfileReportCompletion(original, profileReportRequestKey(4, 'zh'))).toBe(false);
    expect(acceptsProfileReportCompletion(original, original)).toBe(true);
  });
});
