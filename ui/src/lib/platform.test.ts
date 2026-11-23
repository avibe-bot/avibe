import { describe, expect, it, vi } from 'vitest';

import { isRealSafari } from './platform';

describe('isRealSafari', () => {
  it('accepts desktop Safari and rejects Chromium Safari-compatible user agents', () => {
    vi.stubGlobal('navigator', {
      platform: 'MacIntel',
      userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/27.0 Safari/605.1.15',
      maxTouchPoints: 0,
    });
    expect(isRealSafari()).toBe(true);

    vi.stubGlobal('navigator', {
      platform: 'MacIntel',
      userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36',
      maxTouchPoints: 0,
    });
    expect(isRealSafari()).toBe(false);
  });
});
