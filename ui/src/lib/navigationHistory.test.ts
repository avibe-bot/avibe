import { describe, expect, it } from 'vitest';

import { hasInAppBackEntry } from './navigationHistory';

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
});
