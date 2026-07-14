import { describe, expect, it } from 'vitest';

import { shouldGuardUnload } from './windowUnload';

describe('shouldGuardUnload', () => {
  it('is false when there are no windows', () => {
    expect(shouldGuardUnload([])).toBe(false);
  });

  it('is false when every window is minimized', () => {
    expect(shouldGuardUnload([{ minimized: true }, { minimized: true }])).toBe(false);
  });

  it('is true when at least one window is not minimized', () => {
    expect(shouldGuardUnload([{ minimized: false }])).toBe(true);
    expect(shouldGuardUnload([{ minimized: true }, { minimized: false }])).toBe(true);
  });
});
