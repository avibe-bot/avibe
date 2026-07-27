import { describe, expect, it } from 'vitest';

import { movedOrder } from './reorder';

const IDS = ['a', 'b', 'c', 'd'];

describe('movedOrder', () => {
  it('swaps with the previous entry when moving up', () => {
    expect(movedOrder(IDS, 2, -1)).toEqual(['a', 'c', 'b', 'd']);
  });

  it('swaps with the next entry when moving down', () => {
    expect(movedOrder(IDS, 0, 1)).toEqual(['b', 'a', 'c', 'd']);
  });

  it('is a no-op at either end rather than dropping or duplicating an id', () => {
    expect(movedOrder(IDS, 0, -1)).toEqual(IDS);
    expect(movedOrder(IDS, 3, 1)).toEqual(IDS);
  });

  it('is a no-op for an out-of-range index', () => {
    expect(movedOrder(IDS, -1, 1)).toEqual(IDS);
    expect(movedOrder(IDS, 9, -1)).toEqual(IDS);
  });

  it('never mutates the input and always preserves the id multiset', () => {
    const input = [...IDS];
    for (let i = 0; i < input.length; i++) {
      for (const d of [-1, 1]) {
        const out = movedOrder(input, i, d);
        expect(input).toEqual(IDS);
        expect([...out].sort()).toEqual([...IDS].sort());
        expect(out.length).toBe(IDS.length);
      }
    }
  });

  it('round-trips: moving down then back up restores the original order', () => {
    const down = movedOrder(IDS, 1, 1);
    expect(movedOrder(down, 2, -1)).toEqual(IDS);
  });
});
