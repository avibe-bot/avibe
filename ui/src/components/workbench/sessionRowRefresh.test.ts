import { describe, expect, it } from 'vitest';

import { createSessionRowRefreshGate } from './sessionRowRefresh';

describe('Session-row refresh ordering', () => {
  it('accepts only the newest overlapping read', () => {
    const gate = createSessionRowRefreshGate();
    const firstReadIsCurrent = gate.begin();
    const secondReadIsCurrent = gate.begin();

    expect(firstReadIsCurrent()).toBe(false);
    expect(secondReadIsCurrent()).toBe(true);
  });

  it('rejects a read after a newer event or mutation is observed', () => {
    const gate = createSessionRowRefreshGate();
    const readIsCurrent = gate.begin();

    gate.invalidate();

    expect(readIsCurrent()).toBe(false);
  });
});
