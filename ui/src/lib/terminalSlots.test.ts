import { afterEach, describe, expect, it } from 'vitest';

import { acquireTerminalSlot, releaseTerminalSlot, _resetTerminalSlots } from './terminalSlots';

afterEach(() => _resetTerminalSlots());

describe('terminal session slots', () => {
  it('hands out distinct slots so concurrent terminals never collide', () => {
    expect(acquireTerminalSlot()).toBe(0);
    expect(acquireTerminalSlot()).toBe(1);
    expect(acquireTerminalSlot()).toBe(2);
  });

  it('reuses a released slot — the pool stays bounded by concurrency, not churn', () => {
    const a = acquireTerminalSlot(); // 0
    const b = acquireTerminalSlot(); // 1
    expect([a, b]).toEqual([0, 1]);
    releaseTerminalSlot(a);
    // The next terminal must reuse slot 0, not grow to 2 — otherwise opening and
    // closing terminals would leak session ids until the backend cap is exhausted.
    expect(acquireTerminalSlot()).toBe(0);
    expect(acquireTerminalSlot()).toBe(2); // 1 still held, 0 just retaken
  });

  it('fills the lowest free slot after releases in any order', () => {
    [0, 1, 2, 3].forEach(() => acquireTerminalSlot());
    releaseTerminalSlot(2);
    releaseTerminalSlot(0);
    expect(acquireTerminalSlot()).toBe(0);
    expect(acquireTerminalSlot()).toBe(2);
    expect(acquireTerminalSlot()).toBe(4);
  });
});
