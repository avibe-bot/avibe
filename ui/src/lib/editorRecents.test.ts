import { describe, expect, it } from 'vitest';

import {
  MAX_RECENT_FILES,
  MAX_RECENT_FOLDERS,
  addRecentFile,
  addRecentFolder,
  parseEditorRecents,
  recentPathLabel,
  removeRecentFile,
  serializeEditorRecents,
  type EditorRecents,
} from './editorRecents';

const empty: EditorRecents = { folders: [], files: [] };

describe('editor recents — round trip', () => {
  it('serializes and parses back to the same folders + files', () => {
    const recents: EditorRecents = {
      folders: ['/src', '/tmp/work'],
      files: [{ path: '/src/a.ts', name: 'a.ts' }],
    };
    expect(parseEditorRecents(serializeEditorRecents(recents))).toEqual(recents);
  });
});

describe('editor recents — add folder (most-recent-first, dedup, cap)', () => {
  it('prepends the newest folder', () => {
    const r = addRecentFolder(addRecentFolder(empty, '/a'), '/b');
    expect(r.folders).toEqual(['/b', '/a']);
  });

  it('re-opening a folder bumps it to the front without duplicating', () => {
    const r = addRecentFolder(addRecentFolder(addRecentFolder(empty, '/a'), '/b'), '/a');
    expect(r.folders).toEqual(['/a', '/b']);
  });

  it('caps folders at MAX_RECENT_FOLDERS, dropping the oldest', () => {
    let r = empty;
    for (let i = 0; i < MAX_RECENT_FOLDERS + 5; i += 1) r = addRecentFolder(r, `/f${i}`);
    expect(r.folders).toHaveLength(MAX_RECENT_FOLDERS);
    expect(r.folders[0]).toBe(`/f${MAX_RECENT_FOLDERS + 4}`); // newest first
    expect(r.folders).not.toContain('/f0'); // oldest evicted
  });

  it('ignores an empty folder path', () => {
    expect(addRecentFolder(empty, '').folders).toEqual([]);
  });
});

describe('editor recents — add file (most-recent-first, dedup, cap)', () => {
  it('prepends the newest file and dedups by path (name refreshes)', () => {
    const r = addRecentFile(addRecentFile(empty, { path: '/a.ts', name: 'a.ts' }), { path: '/a.ts', name: 'renamed.ts' });
    expect(r.files).toEqual([{ path: '/a.ts', name: 'renamed.ts' }]);
  });

  it('caps files at MAX_RECENT_FILES', () => {
    let r = empty;
    for (let i = 0; i < MAX_RECENT_FILES + 5; i += 1) r = addRecentFile(r, { path: `/f${i}.ts`, name: `f${i}.ts` });
    expect(r.files).toHaveLength(MAX_RECENT_FILES);
    expect(r.files[0].path).toBe(`/f${MAX_RECENT_FILES + 4}.ts`);
  });

  it('falls back to the last path segment when a name is missing', () => {
    const r = addRecentFile(empty, { path: '/x/y/z.ts', name: '' });
    expect(r.files[0]).toEqual({ path: '/x/y/z.ts', name: 'z.ts' });
  });

  it('ignores a file with an empty path', () => {
    expect(addRecentFile(empty, { path: '', name: 'x' }).files).toEqual([]);
  });
});

describe('editor recents — remove file', () => {
  it('drops only the matching path, leaving folders + other files intact', () => {
    const recents: EditorRecents = {
      folders: ['/src'],
      files: [{ path: '/a.ts', name: 'a.ts' }, { path: '/b.ts', name: 'b.ts' }],
    };
    const r = removeRecentFile(recents, '/a.ts');
    expect(r.files).toEqual([{ path: '/b.ts', name: 'b.ts' }]);
    expect(r.folders).toEqual(['/src']);
  });
});

describe('editor recents — corrupt / old data is ignored', () => {
  it('returns empty for invalid JSON', () => {
    expect(parseEditorRecents('{not json')).toEqual(empty);
  });

  it('returns empty for null / undefined / empty input', () => {
    expect(parseEditorRecents(null)).toEqual(empty);
    expect(parseEditorRecents(undefined)).toEqual(empty);
    expect(parseEditorRecents('')).toEqual(empty);
  });

  it('returns empty for a mismatched schema version', () => {
    expect(parseEditorRecents(JSON.stringify({ version: 2, folders: ['/a'], files: [] }))).toEqual(empty);
  });

  it('returns empty for a non-object / array payload', () => {
    expect(parseEditorRecents(JSON.stringify(['/a']))).toEqual(empty);
    expect(parseEditorRecents(JSON.stringify(42))).toEqual(empty);
  });

  it('coerces non-array lists to empty rather than throwing', () => {
    expect(parseEditorRecents(JSON.stringify({ version: 1, folders: 'nope', files: {} }))).toEqual(empty);
  });

  it('drops non-string folders and malformed file entries but keeps valid siblings', () => {
    const raw = JSON.stringify({
      version: 1,
      folders: ['/good', 42, null, '/good2'],
      files: [
        { path: '/a.ts', name: 'a.ts' },
        { name: 'no-path.ts' },
        { path: 123 },
        'bogus',
        { path: '/b.ts', name: 'b.ts' },
      ],
    });
    expect(parseEditorRecents(raw)).toEqual({
      folders: ['/good', '/good2'],
      files: [{ path: '/a.ts', name: 'a.ts' }, { path: '/b.ts', name: 'b.ts' }],
    });
  });

  it('de-dups and caps a corrupt oversized payload on read', () => {
    const raw = JSON.stringify({
      version: 1,
      folders: [...Array.from({ length: MAX_RECENT_FOLDERS + 10 }, (_, i) => `/f${i}`), '/f0'],
      files: Array.from({ length: MAX_RECENT_FILES + 10 }, () => ({ path: '/dup.ts', name: 'dup.ts' })),
    });
    const r = parseEditorRecents(raw);
    expect(r.folders).toHaveLength(MAX_RECENT_FOLDERS);
    expect(r.files).toEqual([{ path: '/dup.ts', name: 'dup.ts' }]); // all dups collapse to one
  });
});

describe('recentPathLabel', () => {
  it('returns the last segment for POSIX and Windows paths', () => {
    expect(recentPathLabel('/a/b/c.ts')).toBe('c.ts');
    expect(recentPathLabel('C:\\work\\proj')).toBe('proj');
    expect(recentPathLabel('/a/b/')).toBe('b');
  });

  it('falls back to the whole path for a root', () => {
    expect(recentPathLabel('/')).toBe('/');
  });
});
