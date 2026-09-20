import { describe, expect, it } from 'vitest';

import { hasInAppBackEntry, inAppHistoryIndex } from './navigationHistory';

describe('browser navigation history', () => {
  it('allows back navigation only when React Router has an earlier in-app entry', () => {
    expect(hasInAppBackEntry({ idx: 1 })).toBe(true);
    expect(hasInAppBackEntry({ idx: 0 })).toBe(false);
    expect(hasInAppBackEntry({ idx: -1 })).toBe(false);
  });

  it('treats missing or malformed history state as a direct entry', () => {
    expect(hasInAppBackEntry(null)).toBe(false);
    expect(hasInAppBackEntry({})).toBe(false);
    expect(hasInAppBackEntry({ idx: '1' })).toBe(false);
  });

  it('reads non-negative integer router indexes for multi-entry returns', () => {
    expect(inAppHistoryIndex({ idx: 0 })).toBe(0);
    expect(inAppHistoryIndex({ idx: 4 })).toBe(4);
    expect(inAppHistoryIndex({ idx: -1 })).toBeNull();
    expect(inAppHistoryIndex({ idx: 1.5 })).toBeNull();
    expect(inAppHistoryIndex({ idx: Number.POSITIVE_INFINITY })).toBeNull();
    expect(inAppHistoryIndex({ idx: '4' })).toBeNull();
  });
});
