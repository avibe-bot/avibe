// Versioned localStorage store for the Editor's "Recent" welcome lists: the folders a user has
// opened as an explorer root, and the files they've opened. Mirrors workbenchPersistence's shape —
// pure parse/serialize/mutate helpers (unit-testable, corruption-tolerant) plus thin storage
// wrappers — but is a DISTINCT concern from window persistence:
//
//   - workbenchPersistence restores the LAST session's open windows + tabs on a reload.
//   - this is a longer-lived, cross-session MRU history shown on the editor's welcome screen.
//
// Every editor window and the full-page /apps/editor route share this one store; there's no
// coordination, so writes are last-write-wins and the welcome screen reads fresh on mount.

// Bump the version suffix to invalidate an incompatible on-disk shape — a value under an older key
// is simply never read, so old/corrupt data is ignored silently (not migrated).
export const EDITOR_RECENTS_STORAGE_KEY = 'avibe.editor.recents.v1';

// Keep the welcome lists short (VS Code-style), most-recent-first. Also bounds the stored payload so
// one corrupt/oversized entry can't grow the store — or flood the welcome screen — without limit.
export const MAX_RECENT_FOLDERS = 8;
export const MAX_RECENT_FILES = 8;

export interface RecentFile {
  path: string;
  name: string;
}

export interface EditorRecents {
  // Absolute folder paths; the display label is derived from the last segment.
  folders: string[];
  files: RecentFile[];
}

interface PersistedRecents {
  version: 1;
  folders: string[];
  files: RecentFile[];
}

// A fresh empty value on every call — never a shared mutable object, so a caller holding it in
// React state can't be aliased by the next reader.
function empty(): EditorRecents {
  return { folders: [], files: [] };
}

function isNonEmptyString(v: unknown): v is string {
  return typeof v === 'string' && v.length > 0;
}

// The display label for a recent path: its last path segment (Windows- and POSIX-aware), falling
// back to the whole path for a root like "/". Shared by the welcome list (folder labels) and the
// file-name fallback below.
export function recentPathLabel(path: string): string {
  return path.split(/[\\/]/).filter(Boolean).pop() || path;
}

// Prepend `value` as the most-recent entry, drop any existing entry with the same key (so a re-open
// bumps it to the front rather than duplicating), and cap the list length.
function prepend<T>(list: T[], value: T, key: (item: T) => string, cap: number): T[] {
  const k = key(value);
  return [value, ...list.filter((item) => key(item) !== k)].slice(0, cap);
}

const folderKey = (p: string) => p;
const fileKey = (f: RecentFile) => f.path;

// Record a folder opened as the explorer root. Pure — returns a new EditorRecents.
export function addRecentFolder(recents: EditorRecents, path: string): EditorRecents {
  if (!isNonEmptyString(path)) return recents;
  return { ...recents, folders: prepend(recents.folders, path, folderKey, MAX_RECENT_FOLDERS) };
}

// Record an opened file. Pure — returns a new EditorRecents.
export function addRecentFile(recents: EditorRecents, file: RecentFile): EditorRecents {
  if (!isNonEmptyString(file.path)) return recents;
  const entry: RecentFile = { path: file.path, name: isNonEmptyString(file.name) ? file.name : recentPathLabel(file.path) };
  return { ...recents, files: prepend(recents.files, entry, fileKey, MAX_RECENT_FILES) };
}

// Drop a file whose path no longer resolves (opening it failed). Pure.
export function removeRecentFile(recents: EditorRecents, path: string): EditorRecents {
  return { ...recents, files: recents.files.filter((f) => f.path !== path) };
}

// De-dup a list by key, keeping the first occurrence (most-recent-first order) and capping length —
// so a hand-edited/corrupt blob with duplicate or oversized lists still parses to a sane shape.
function dedupeCap<T>(list: T[], key: (item: T) => string, cap: number): T[] {
  const seen = new Set<string>();
  const out: T[] = [];
  for (const item of list) {
    const k = key(item);
    if (seen.has(k)) continue;
    seen.add(k);
    out.push(item);
    if (out.length >= cap) break;
  }
  return out;
}

// Parse a raw stored payload into recents. Pure (no storage access) so it's unit-testable; any
// corruption — invalid JSON, wrong/absent version, non-array lists, malformed entries — yields the
// empty set or drops just the bad entries, and never throws.
export function parseEditorRecents(raw: string | null | undefined): EditorRecents {
  if (!raw) return empty();
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return empty();
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed) || (parsed as { version?: unknown }).version !== 1) {
    return empty();
  }
  const p = parsed as { folders?: unknown; files?: unknown };
  const folders = dedupeCap((Array.isArray(p.folders) ? p.folders : []).filter(isNonEmptyString), folderKey, MAX_RECENT_FOLDERS);
  const files = dedupeCap(
    (Array.isArray(p.files) ? p.files : []).flatMap((f): RecentFile[] => {
      if (!f || typeof f !== 'object') return [];
      const ff = f as { path?: unknown; name?: unknown };
      if (!isNonEmptyString(ff.path)) return [];
      // A missing/corrupt name falls back to the path's last segment, so a row always has a label.
      return [{ path: ff.path, name: isNonEmptyString(ff.name) ? ff.name : recentPathLabel(ff.path) }];
    }),
    fileKey,
    MAX_RECENT_FILES,
  );
  return { folders, files };
}

// Serialize recents to the versioned payload string. Pure.
export function serializeEditorRecents(recents: EditorRecents): string {
  const payload: PersistedRecents = { version: 1, folders: recents.folders, files: recents.files };
  return JSON.stringify(payload);
}

// Read + parse the persisted recents. Returns the empty set when storage is unavailable or empty.
export function loadEditorRecents(): EditorRecents {
  try {
    return parseEditorRecents(window.localStorage.getItem(EDITOR_RECENTS_STORAGE_KEY));
  } catch {
    return empty();
  }
}

// Serialize + write recents. Silently drops on any failure (storage disabled / quota exceeded) —
// recents is best-effort and must never interrupt the user.
function save(recents: EditorRecents): void {
  try {
    window.localStorage.setItem(EDITOR_RECENTS_STORAGE_KEY, serializeEditorRecents(recents));
  } catch {
    // ignore
  }
}

// Storage-backed recorders. Each reads fresh (last write wins across the windows sharing this store),
// applies the pure mutation, persists, and returns the updated recents for the caller to render.
export function rememberRecentFolder(path: string): EditorRecents {
  const next = addRecentFolder(loadEditorRecents(), path);
  save(next);
  return next;
}

export function rememberRecentFile(path: string, name: string): EditorRecents {
  const next = addRecentFile(loadEditorRecents(), { path, name });
  save(next);
  return next;
}

export function forgetRecentFile(path: string): EditorRecents {
  const next = removeRecentFile(loadEditorRecents(), path);
  save(next);
  return next;
}
