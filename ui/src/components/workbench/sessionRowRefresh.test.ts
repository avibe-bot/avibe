import { describe, expect, it } from 'vitest';

import { createSessionRowRefreshGate } from './sessionRowRefresh';

describe('Session-row refresh ordering', () => {
  it('accepts only the newest overlapping read', async () => {
    const gate = createSessionRowRefreshGate();
    const firstRead = gate.begin();
    const secondRead = gate.begin();
    const firstReadIsCurrent = await firstRead;
    const secondReadIsCurrent = await secondRead;

    expect(firstReadIsCurrent()).toBe(false);
    expect(secondReadIsCurrent()).toBe(true);
  });

  it('rejects a read after a newer event or mutation is observed', async () => {
    const gate = createSessionRowRefreshGate();
    const readIsCurrent = await gate.begin();

    gate.invalidate();
    const retryIsCurrent = await gate.begin();

    expect(readIsCurrent()).toBe(false);
    expect(retryIsCurrent()).toBe(true);
  });

  it('holds reads until every overlapping mutation settles', async () => {
    const gate = createSessionRowRefreshGate();
    const finishFirst = gate.beginMutation();
    const finishSecond = gate.beginMutation();
    let readStarted = false;
    const read = gate.begin().then(() => {
      readStarted = true;
    });

    await Promise.resolve();
    expect(readStarted).toBe(false);
    finishFirst();
    await Promise.resolve();
    expect(readStarted).toBe(false);
    finishSecond();
    await read;
    expect(readStarted).toBe(true);
  });
});
