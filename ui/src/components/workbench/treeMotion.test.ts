import { describe, expect, it } from 'vitest';

import { treeMotion } from './treeMotion';

// The project tree as its motion follows it (docs/plans/web-ui-motion.md M7).

const rows = (...ids: string[]) => ids.map((id) => ({ id }));

describe('treeMotion', () => {
  it('lists each project, then the sessions shown under an open one, then its Load more', () => {
    const tree = treeMotion(rows('p1', 'p2', 'p3'), (id) => ({
      p1: { open: true, rows: rows('s1', 's2'), more: true },
      p2: { open: false, rows: null, more: false },
      p3: { open: true, rows: rows('s3'), more: false },
    })[id]!);
    expect(tree.ids).toEqual(['project:p1', 's1', 's2', 'more:p1', 'project:p2', 'project:p3', 's3']);
  });

  it('is another tree when a project opens, closes, moves, or its first page arrives', () => {
    const open = (sessions: string[] | null) => treeMotion(rows('p1', 'p2'), (id) => (
      id === 'p1' ? { open: true, rows: sessions === null ? null : rows(...sessions), more: false } : { open: false, rows: null, more: false }
    )).group;
    const closed = treeMotion(rows('p1', 'p2'), () => ({ open: false, rows: null, more: false })).group;
    const reordered = treeMotion(rows('p2', 'p1'), (id) => (id === 'p1' ? { open: true, rows: rows('s1'), more: false } : { open: false, rows: null, more: false })).group;
    expect(open(null)).not.toBe(open(['s1']));
    expect(closed).not.toBe(open(['s1']));
    expect(reordered).not.toBe(open(['s1']));
    // A session moving, arriving or leaving inside an open project is the same tree: it moves.
    expect(open(['s2', 's1'])).toBe(open(['s1']));
  });
});
