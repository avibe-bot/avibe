import { describe, expect, it } from 'vitest';

import { chooseListMotion, countChanges, diffOrder, planListMotion, type Box } from './listMotion';

// The choreography of a list change (docs/plans/web-ui-motion.md M7, §5, §6),
// without layout: rows are 100 px tall, stacked from the top of the screen.

const view = { top: 0, bottom: 800 };
const stacked = (ids: string[], height = 100): Map<string, Box> =>
  new Map(ids.map((id, index) => [id, { top: index * height, left: 0, width: 360, height }]));
const sorted = (set: Set<string>) => [...set].sort();

describe('diffOrder', () => {
  it('names the conversation a reply took to the top as the one that moved, not the rows it passed', () => {
    const diff = diffOrder(['a', 'b', 'c', 'd'], ['c', 'a', 'b', 'd']);
    expect(sorted(diff.moved)).toEqual(['c']);
    expect(diff.inserted.size + diff.removed.size).toBe(0);
  });

  it('tells new rows and removed rows apart from moved ones', () => {
    const diff = diffOrder(['a', 'b', 'c'], ['n', 'a', 'c']);
    expect(sorted(diff.inserted)).toEqual(['n']);
    expect(sorted(diff.removed)).toEqual(['b']);
    expect(diff.moved.size).toBe(0);
  });

  it('sees no change in the same order', () => {
    const diff = diffOrder(['a', 'b'], ['a', 'b']);
    expect(diff.inserted.size + diff.removed.size + diff.moved.size).toBe(0);
  });
});

describe('chooseListMotion', () => {
  it('moves for an ordinary change', () => {
    expect(chooseListMotion({ reduced: false, changes: 1, recentChanges: 0 })).toBe('move');
  });

  it('fades under reduced motion', () => {
    expect(chooseListMotion({ reduced: true, changes: 1, recentChanges: 0 })).toBe('fade');
  });

  it('fades the fourth change within a second (a burst)', () => {
    expect(chooseListMotion({ reduced: false, changes: 1, recentChanges: 2 })).toBe('move');
    expect(chooseListMotion({ reduced: false, changes: 1, recentChanges: 3 })).toBe('fade');
  });

  it('cross-fades a change of more than five rows', () => {
    expect(chooseListMotion({ reduced: false, changes: 5, recentChanges: 0 })).toBe('move');
    expect(chooseListMotion({ reduced: false, changes: 6, recentChanges: 0 })).toBe('crossfade');
  });

  it('counts inserted, removed and moved rows', () => {
    expect(countChanges(['a', 'b', 'c'], ['c', 'a', 'n'])).toBe(3);
  });
});

describe('planListMotion', () => {
  it('moves a card to the top: it leaves its slot, the cards above glide down, it comes in at the top', () => {
    const prevIds = ['a', 'b', 'c', 'd'];
    const nextIds = ['c', 'a', 'b', 'd'];
    const plan = planListMotion({ prevIds, nextIds, before: stacked(prevIds), after: stacked(nextIds), viewport: view, mode: 'move' });
    expect(plan.leaves).toEqual([{ id: 'c', box: { top: 200, left: 0, width: 360, height: 100 }, kind: 'moved' }]);
    expect(plan.glides).toEqual([{ id: 'a', dx: 0, dy: -100 }, { id: 'b', dx: 0, dy: -100 }]);
    expect(plan.enters).toEqual([{ id: 'c' }]);
  });

  it('leaves a removed card where it was and glides the cards below it up', () => {
    const prevIds = ['a', 'b', 'c'];
    const nextIds = ['a', 'c'];
    const plan = planListMotion({ prevIds, nextIds, before: stacked(prevIds), after: stacked(nextIds), viewport: view, mode: 'move' });
    expect(plan.leaves.map(({ id, kind }) => ({ id, kind }))).toEqual([{ id: 'b', kind: 'removed' }]);
    expect(plan.glides).toEqual([{ id: 'c', dx: 0, dy: 100 }]);
    expect(plan.enters).toEqual([]);
  });

  it('glides rows a taller card above pushed down, with no change of order', () => {
    const ids = ['a', 'b'];
    const after = new Map(stacked(ids));
    after.set('b', { top: 120, left: 0, width: 360, height: 100 });
    const plan = planListMotion({ prevIds: ids, nextIds: ids, before: stacked(ids), after, viewport: view, mode: 'move' });
    expect(plan.glides).toEqual([{ id: 'b', dx: 0, dy: -20 }]);
  });

  it('in the fade forms, nothing glides: what changed fades in, and a row that left fades only where nothing took its place', () => {
    const prevIds = ['a', 'b', 'c', 'd'];
    const nextIds = ['c', 'a', 'b', 'd'];
    for (const mode of ['fade', 'crossfade'] as const) {
      const plan = planListMotion({ prevIds, nextIds, before: stacked(prevIds), after: stacked(nextIds), viewport: view, mode });
      expect(plan.glides).toEqual([]);
      // b now sits where c was: no copy of c fades over it.
      expect(plan.leaves).toEqual([]);
      expect(plan.enters).toEqual([{ id: 'c' }]);
    }
    // The last row removed leaves its place empty: it fades out there.
    const plan = planListMotion({ prevIds, nextIds: ['a', 'b', 'c'], before: stacked(prevIds), after: stacked(['a', 'b', 'c']), viewport: view, mode: 'fade' });
    expect(plan.leaves.map(({ id }) => id)).toEqual(['d']);
  });

  it('leaves rows more than a screen off the visible band alone', () => {
    const prevIds = Array.from({ length: 30 }, (_, index) => `r${index}`);
    const nextIds = ['r25', ...prevIds.filter((id) => id !== 'r25')];
    const plan = planListMotion({ prevIds, nextIds, before: stacked(prevIds), after: stacked(nextIds), viewport: view, mode: 'move', limit: 100 });
    // r25 left from 2500 px, past the band (800 px and one screen more): no copy of it.
    expect(plan.leaves).toEqual([]);
    expect(plan.enters).toEqual([{ id: 'r25' }]);
    // Rows glide while they were or will be within a screen of the visible band (to 1600 px), and no further.
    expect(plan.glides.map(({ id }) => id)).toEqual(Array.from({ length: 17 }, (_, index) => `r${index}`));
  });

  it('animates at most eight elements, the ones on screen first', () => {
    const prevIds = Array.from({ length: 20 }, (_, index) => `r${index}`);
    const nextIds = ['n', ...prevIds];
    const plan = planListMotion({ prevIds, nextIds, before: stacked(prevIds), after: stacked(nextIds), viewport: view, mode: 'move' });
    const animated = plan.leaves.length + plan.glides.length + plan.enters.length;
    expect(animated).toBe(8);
    expect(plan.enters).toEqual([{ id: 'n' }]);
    expect(plan.glides.map(({ id }) => id)).toEqual(['r0', 'r1', 'r2', 'r3', 'r4', 'r5', 'r6']);
  });
});
