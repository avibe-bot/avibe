import { describe, expect, it } from 'vitest';

import { deriveAppRows, filterShowPages, partitionByDock, type FilterablePage } from './appLibrary';

const BUILTINS = ['files', 'terminal', 'editor', 'library'];
const pin = (id: string) => ({ session_id: id });

describe('deriveAppRows (installed set)', () => {
  it('lists every built-in then every installed page, resolving kinds', () => {
    const rows = deriveAppRows(BUILTINS, [pin('s1'), pin('s2')], ['files', 'show:s1']);
    expect(rows.map((r) => r.dockId)).toEqual(['files', 'terminal', 'editor', 'library', 'show:s1', 'show:s2']);
    expect(rows.map((r) => r.kind)).toEqual(['builtin', 'builtin', 'builtin', 'builtin', 'showpage', 'showpage']);
    expect(rows[4]).toMatchObject({ kind: 'showpage', sessionId: 's1', removable: true });
    expect(rows[0]).toMatchObject({ kind: 'builtin', builtinId: 'files', removable: false });
  });

  it('includes the Library itself — it lists itself now (§7.1c)', () => {
    const rows = deriveAppRows(BUILTINS, [], []);
    expect(rows.map((r) => r.dockId)).toContain('library');
  });

  it('flags docked membership from `order`, independent of list membership', () => {
    // files + library + s1 are docked; terminal/editor + s2 are installed but undocked.
    const rows = deriveAppRows(BUILTINS, [pin('s1'), pin('s2')], ['files', 'library', 'show:s1']);
    const byId = Object.fromEntries(rows.map((r) => [r.dockId, r.docked]));
    expect(byId).toMatchObject({
      files: true,
      terminal: false,
      editor: false,
      library: true,
      'show:s1': true,
      'show:s2': false,
    });
  });

  it('keeps an undocked built-in in the list (empty order still lists every built-in)', () => {
    const rows = deriveAppRows(BUILTINS, [], []);
    expect(rows.map((r) => r.dockId)).toEqual(BUILTINS);
    expect(rows.every((r) => r.docked === false)).toBe(true);
  });

  it('de-duplicates a pin that repeats', () => {
    const rows = deriveAppRows(['files'], [pin('s1'), pin('s1')], []);
    expect(rows.map((r) => r.dockId)).toEqual(['files', 'show:s1']);
  });
});

describe('partitionByDock (docked / undocked split for the Apps view)', () => {
  it('orders docked rows by the Dock order and keeps undocked below in stable order', () => {
    const order = ['show:s1', 'files', 'library'];
    const rows = deriveAppRows(BUILTINS, [pin('s1'), pin('s2')], order);
    const { docked, undocked } = partitionByDock(rows, order);
    // Docked rows follow the Dock order exactly, regardless of derivation order.
    expect(docked.map((r) => r.dockId)).toEqual(['show:s1', 'files', 'library']);
    // Undocked rows keep derivation order: built-ins canonical, then pins.
    expect(undocked.map((r) => r.dockId)).toEqual(['terminal', 'editor', 'show:s2']);
  });

  it('produces a docked sequence equal to the Dock order (a reorder maps 1:1 onto it)', () => {
    const order = ['editor', 'files', 'show:s1'];
    const rows = deriveAppRows(BUILTINS, [pin('s1')], order);
    expect(partitionByDock(rows, order).docked.map((r) => r.dockId)).toEqual(order);
  });

  it('empty order → nothing docked, every row undocked in derivation order', () => {
    const rows = deriveAppRows(BUILTINS, [pin('s1')], []);
    const { docked, undocked } = partitionByDock(rows, []);
    expect(docked).toEqual([]);
    expect(undocked.map((r) => r.dockId)).toEqual([...BUILTINS, 'show:s1']);
  });

  it('does not mutate the input rows array', () => {
    const rows = deriveAppRows(BUILTINS, [], ['library', 'files']);
    const before = rows.map((r) => r.dockId);
    partitionByDock(rows, ['library', 'files']);
    expect(rows.map((r) => r.dockId)).toEqual(before);
  });
});

describe('filterShowPages', () => {
  const pages: FilterablePage[] = [
    { session_id: 'sess-a', visibility: 'public', title: 'Sales Dashboard' },
    { session_id: 'sess-b', visibility: 'private', title: '旅行计划' },
    { session_id: 'sess-c', visibility: 'offline', title: 'Weekly Report' },
    { session_id: 'sess-untitled', visibility: 'private', title: null },
  ];

  it('all keeps every visibility including offline', () => {
    expect(filterShowPages(pages, 'all', '').map((p) => p.session_id)).toEqual(['sess-a', 'sess-b', 'sess-c', 'sess-untitled']);
  });

  it('filters by a single visibility bucket', () => {
    expect(filterShowPages(pages, 'private', '').map((p) => p.session_id)).toEqual(['sess-b', 'sess-untitled']);
    expect(filterShowPages(pages, 'offline', '').map((p) => p.session_id)).toEqual(['sess-c']);
  });

  it('matches the query against title and session id', () => {
    expect(filterShowPages(pages, 'all', 'sales').map((p) => p.session_id)).toEqual(['sess-a']);
    expect(filterShowPages(pages, 'all', '旅行').map((p) => p.session_id)).toEqual(['sess-b']);
    // A page with no title still matches on its session id.
    expect(filterShowPages(pages, 'all', 'untitled').map((p) => p.session_id)).toEqual(['sess-untitled']);
  });

  it('combines visibility and query', () => {
    expect(filterShowPages(pages, 'private', 'zzz')).toEqual([]);
    expect(filterShowPages(pages, 'private', '旅行').map((p) => p.session_id)).toEqual(['sess-b']);
  });
});
