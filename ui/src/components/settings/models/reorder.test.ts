import { describe, expect, it } from 'vitest';

import { movedOrder, sameIds } from './reorder';

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

describe('sameIds', () => {
  it('compares contents, not identity — the whole reason it exists', () => {
    expect(sameIds(IDS, [...IDS])).toBe(true);
    expect(sameIds(IDS, ['a', 'c', 'b', 'd'])).toBe(false);
    expect(sameIds(IDS, ['a', 'b', 'c'])).toBe(false);
    // Same set, different order IS a different order: this list is the spend order.
    expect(sameIds(['a', 'b'], ['b', 'a'])).toBe(false);
    expect(sameIds([], [])).toBe(true);
  });

  // The composition that decides whether a `follow` backend gets forked to
  // `custom`. `movedOrder` hands back a FRESH array at either boundary, so the
  // commit path has to compare contents; on `!==` alone every ArrowUp on row 1
  // would silently cost the user their recommended order.
  it('sees a boundary move as no change at all', () => {
    const up = movedOrder(IDS, 0, -1);
    expect(up).not.toBe(IDS);
    expect(sameIds(IDS, up)).toBe(true);
    expect(sameIds(IDS, movedOrder(IDS, IDS.length - 1, 1))).toBe(true);
  });

  it('sees a drag that lands back where it started as no change', () => {
    expect(sameIds(IDS, movedOrder(movedOrder(IDS, 1, 1), 2, -1))).toBe(true);
  });

  it('still sees a real move as a change', () => {
    expect(sameIds(IDS, movedOrder(IDS, 1, 1))).toBe(false);
  });
});
