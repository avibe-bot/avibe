import { describe, expect, it } from 'vitest';

import { deriveAppRows, filterShowPages, type FilterablePage } from './appLibrary';

const BUILTINS = new Set(['files', 'terminal', 'editor', 'library']);

describe('deriveAppRows', () => {
  it('keeps the docked set in order, resolving kinds', () => {
    const rows = deriveAppRows(['files', 'terminal', 'editor', 'show:s1', 'show:s2'], BUILTINS, 'library');
    expect(rows.map((r) => r.dockId)).toEqual(['files', 'terminal', 'editor', 'show:s1', 'show:s2']);
    expect(rows.map((r) => r.kind)).toEqual(['builtin', 'builtin', 'builtin', 'showpage', 'showpage']);
    expect(rows[3]).toMatchObject({ kind: 'showpage', sessionId: 's1', removable: true });
    expect(rows[0]).toMatchObject({ kind: 'builtin', builtinId: 'files', removable: false });
  });

  it('excludes the Library app itself from the list', () => {
    const rows = deriveAppRows(['files', 'library', 'show:s1'], BUILTINS, 'library');
    expect(rows.map((r) => r.dockId)).toEqual(['files', 'show:s1']);
  });

  it('drops unknown ids and de-duplicates', () => {
    const rows = deriveAppRows(['files', 'files', 'app:remote1', 'show:s1'], BUILTINS, 'library');
    expect(rows.map((r) => r.dockId)).toEqual(['files', 'show:s1']);
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
