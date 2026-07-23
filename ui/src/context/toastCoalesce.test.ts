import { describe, expect, it } from 'vitest';

import { shouldCoalesceToast } from './toastCoalesce';

describe('shouldCoalesceToast (dedupe vs. per-instance action)', () => {
  const now = 1000;

  it('coalesces a plain duplicate still within its dedupe window', () => {
    expect(shouldCoalesceToast(false, { expiresAt: now + 100 }, now)).toBe(true);
  });

  it('never coalesces an actionable toast — its undo must target its own item', () => {
    expect(shouldCoalesceToast(true, { expiresAt: now + 100 }, now)).toBe(false);
  });

  it('does not coalesce a new (untracked) message', () => {
    expect(shouldCoalesceToast(false, undefined, now)).toBe(false);
  });

  it('does not coalesce once the dedupe window has expired', () => {
    expect(shouldCoalesceToast(false, { expiresAt: now - 1 }, now)).toBe(false);
  });
});
