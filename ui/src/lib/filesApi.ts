// Client for the whole-machine File Browser backend (`/api/files/*`). Reuses the
// shared `apiFetch`, which attaches the CSRF header to mutating verbs and routes
// remote-auth-expiry redirects. Backend contract: `core/file_browser_service.py`.
import { apiFetch } from './apiFetch';

export type FsEntry = {
  name: string;
  kind: 'dir' | 'file' | 'symlink';
  size: number | null;
  mtime: number | null;
  ext: string;
};

export type FsListing = {
  ok: true;
  path: string;
  parent: string | null;
  entries: FsEntry[];
  truncated?: boolean;
  limit?: number;
};

// A recursive name-search hit (`/api/files/search_names`): an entry plus its absolute `path` and
// `rel` (path relative to the search root) so the UI can show where it lives and open/navigate to it.
export type NameHit = FsEntry & { path: string; rel: string };
export type NameSearchResponse = {
  ok: true;
  root: string;
  query: string;
  results: NameHit[];
  truncated: boolean;
  limit: number | null;
};

export type FsMeta = {
  ok: true;
  name: string;
  ext: string;
  kind: 'dir' | 'file' | 'symlink';
  size: number | null;
  mtime: number | null;
  mime: string | null;
  /** Content sniff: true when the file decodes as text, so an extensionless / unknown-type file
   *  still opens in the editor instead of downloading. Absent on older backends → treated as unknown. */
  text?: boolean;
};

export type Favorite = { key: string; path: string };

export class FilesApiError extends Error {
  code: string;
  constructor(code: string, message: string) {
    super(message);
    this.code = code;
    this.name = 'FilesApiError';
  }
}

export function fileBrowserErrorMessage(error: unknown, t: (key: string) => string, fallback: string): string {
  if (error instanceof FilesApiError) {
    // Every error code (backend not_found/permission_denied/... and the client-side
    // file_not_utf8) maps 1:1 to apps.fileBrowser.errors.<code>; fall back to the raw
    // message when no localized string exists.
    const key = `apps.fileBrowser.errors.${error.code}`;
    const translated = t(key);
    return translated === key ? error.message : translated;
  }
  return error instanceof Error ? error.message : fallback;
}

async function parse<T>(res: Response): Promise<T> {
  // Read the body as text first so a non-JSON response (an upstream HTML/error page, a truncated
  // body, an empty 2xx) is distinguishable from a real payload — rather than silently collapsing to
  // `{}` that callers would treat as valid data and then crash dereferencing (e.g. `data.results.map`).
  const raw = await res.text();
  let data: unknown;
  if (raw) {
    try {
      data = JSON.parse(raw);
    } catch {
      data = undefined;
    }
  }
  if (!res.ok || (data as { ok?: boolean } | undefined)?.ok === false) {
    const err = (data as { error?: { code?: string; message?: string } } | undefined)?.error || {};
    throw new FilesApiError(err.code || String(res.status), err.message || 'Request failed');
  }
  if (data === undefined) {
    // 2xx but the body was empty or not valid JSON (truncated / upstream error page). Surface it as
    // a failure so the caller shows an error state instead of feeding a malformed object to the UI.
    throw new FilesApiError('invalid_response', 'The server returned an unexpected response.');
  }
  return data as T;
}

function isWindowsPath(p: string): boolean {
  // Windows iff a drive root (C:\ or C:/) or a UNC path (\\server). A lone backslash is a
  // legal POSIX filename character (e.g. /tmp/a\b), so its mere presence must NOT flip us to
  // Windows mode — that would make joinPath build /tmp/a\b\child and break descendant access.
  return /^[A-Za-z]:[\\/]/.test(p) || /^\\\\/.test(p);
}

export function joinPath(base: string, name: string): string {
  const sep = isWindowsPath(base) ? '\\' : '/';
  return base.endsWith('/') || base.endsWith('\\') ? `${base}${name}` : `${base}${sep}${name}`;
}

// A user-entered entry name must be a single path component, so joinPath(base, name)
// can only ever address a child of `base`. Reject separators and '.'/'..'/empty —
// otherwise input like '../scratch' or 'sub/new' would mutate a sibling/nested folder.
// Mirrors the backend rename_path name validator.
export function isPlainEntryName(name: string): boolean {
  const trimmed = name.trim();
  return trimmed !== '' && trimmed !== '.' && trimmed !== '..' && !trimmed.includes('/') && !trimmed.includes('\\');
}

export function pathCrumbs(path: string): { label: string; path: string }[] {
  // Windows: split on either separator and keep the root intact.
  if (isWindowsPath(path)) {
    const normalized = path.replace(/\//g, '\\');
    if (/^\\\\/.test(normalized)) {
      // UNC: the root is the share (\\server\share) — you can't navigate above it, and the
      // leading \\ must be preserved or breadcrumb targets become invalid (server\, server\share).
      const parts = normalized.replace(/^\\+/, '').split('\\').filter(Boolean); // [server, share, dir, ...]
      const server = parts.shift() ?? '';
      const share = parts.shift();
      const root = share ? `\\\\${server}\\${share}` : `\\\\${server}`;
      const out: { label: string; path: string }[] = [{ label: root, path: root }];
      let cur = root;
      for (const part of parts) {
        cur = `${cur}\\${part}`;
        out.push({ label: part, path: cur });
      }
      return out;
    }
    const parts = normalized.split('\\').filter(Boolean);
    const drive = parts.shift() ?? '';
    const out: { label: string; path: string }[] = [{ label: `${drive}\\`, path: `${drive}\\` }];
    let cur = `${drive}\\`;
    for (const part of parts) {
      cur = cur.endsWith('\\') ? `${cur}${part}` : `${cur}\\${part}`;
      out.push({ label: part, path: cur });
    }
    return out;
  }
  const parts = path.split('/').filter(Boolean);
  const out: { label: string; path: string }[] = [{ label: '/', path: '/' }];
  let cur = '';
  for (const part of parts) {
    cur += `/${part}`;
    out.push({ label: part, path: cur });
  }
  return out;
}

// The parent directory of a path, Windows-aware (reuses pathCrumbs' separator handling) so the
// editor opens its explorer on the right root for POSIX, Windows drive, and UNC paths alike.
export function parentDir(path: string): string {
  const crumbs = pathCrumbs(path);
  return crumbs.length >= 2 ? crumbs[crumbs.length - 2].path : path;
}

export async function listDir(path: string, showHidden = false): Promise<FsListing> {
  const res = await apiFetch(
    `/api/files/list?path=${encodeURIComponent(path)}&show_hidden=${showHidden ? '1' : '0'}`,
  );
  return parse<FsListing>(res);
}

export async function fileMeta(path: string): Promise<FsMeta> {
  return parse<FsMeta>(await apiFetch(`/api/files/meta?path=${encodeURIComponent(path)}`));
}

export function contentUrl(path: string, download = false): string {
  return `/api/files/content?path=${encodeURIComponent(path)}${download ? '&download=1' : ''}`;
}

// Trigger a file download via a programmatic anchor click rather than window.open. An anchor
// download is not a popup, so — unlike window.open('_blank') — it isn't popup-blocked and survives
// an awaited metadata recheck without losing the click's user activation (Safari/iOS). The backend's
// Content-Disposition (download=1) names the file and forces the save in-place.
export function downloadFile(path: string): void {
  const a = document.createElement('a');
  a.href = contentUrl(path, true);
  a.rel = 'noopener';
  // target=_blank: if remote-auth expired, /content 302-redirects to the Cloud login — that
  // cross-origin redirect would otherwise replace the SPA in this tab. Sending it to a new context
  // keeps the app intact (a real attachment still downloads in-place, so no stray tab opens).
  a.target = '_blank';
  // download attr: keep this a download even if the server returns a JSON error (file removed /
  // permission) instead of an attachment, so the SPA is never navigated to the error body.
  a.download = '';
  a.style.display = 'none';
  document.body.appendChild(a);
  a.click();
  a.remove();
}

export async function readText(path: string): Promise<string> {
  const res = await apiFetch(contentUrl(path));
  if (!res.ok) {
    await parse(res); // throws a FilesApiError
  }
  const body = await res.arrayBuffer();
  try {
    return new TextDecoder('utf-8', { fatal: true }).decode(body);
  } catch (error) {
    if (error instanceof TypeError) {
      throw new FilesApiError('file_not_utf8', "This file isn't valid UTF-8 text.");
    }
    throw error;
  }
}

export async function writeFile(
  path: string,
  content: string,
  expectedMtime?: number | null,
  // create_only: backend refuses (errors.exists) if the path already exists, checked atomically
  // under its per-path write lock — used by "New File" so a name typo can never clobber a file.
  createOnly = false,
): Promise<{ ok: true; mtime: number }> {
  const res = await apiFetch('/api/files/write', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path, content, expected_mtime: expectedMtime ?? undefined, create_only: createOnly || undefined }),
  });
  return parse<{ ok: true; mtime: number }>(res);
}

export async function makeDir(path: string): Promise<{ ok: true }> {
  return parse(
    await apiFetch('/api/files/mkdir', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path }),
    }),
  );
}

// The backend's per-file upload cap (core/file_browser_service.py MAX_FILE_BYTES). Mirrored here so
// the UI can reject an oversized file before spending a request; the server enforces it regardless.
export const MAX_UPLOAD_BYTES = 25 * 1024 * 1024;

export type UploadResult = { name: string; path: string; size: number; mtime: number };

// Upload one file into `dir` (an absolute destination directory) as multipart/form-data. Rides the
// same apiFetch credentials + CSRF handling as the sibling mutations; we deliberately do NOT set
// Content-Type so the browser adds the multipart boundary itself. The binary part's filename is the
// target name unless `opts.name` overrides it. `overwrite` (default false) lets a caller replace an
// existing file after the backend reports errors.exists (409).
export async function uploadFile(
  dir: string,
  file: File,
  opts: { name?: string; overwrite?: boolean } = {},
): Promise<UploadResult> {
  const form = new FormData();
  form.append('dir', dir);
  form.append('file', file, opts.name || file.name);
  if (opts.overwrite) form.append('overwrite', 'true');
  return parse<UploadResult>(await apiFetch('/api/files/upload', { method: 'POST', body: form }));
}

export async function deletePath(path: string, recursive = false): Promise<{ ok: true }> {
  return parse(
    await apiFetch('/api/files/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path, recursive }),
    }),
  );
}

// Rename an entry in place (same parent). The backend validates the new name and refuses to
// clobber an existing destination (errors.exists). Returns the new absolute path.
export async function renamePath(path: string, newName: string): Promise<{ ok: true; path: string }> {
  return parse(
    await apiFetch('/api/files/rename', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path, new_name: newName }),
    }),
  );
}

// Move an entry from `src` to the absolute `dst` (used by drag-and-drop into a folder). The backend
// is symlink-safe, refuses to move a folder into itself, and — with overwrite=false (the default) —
// refuses to clobber an existing destination (errors.exists), which the UI surfaces to the user.
export async function movePath(src: string, dst: string, overwrite = false): Promise<{ ok: true }> {
  return parse(
    await apiFetch('/api/files/move', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ src, dst, overwrite: overwrite || undefined }),
    }),
  );
}

// Cross-file search + replace (backend: file_browser_service.search/replace/undo_replace).
// col/end are full-line UTF-16 offsets (the editor jump target); preview_col/preview_end index
// into `text` (the possibly windowed preview) for the row highlight.
export type SearchMatch = { line: number; col: number; end: number; preview_col: number; preview_end: number; text: string; line_truncated: boolean };
export type SearchFileResult = { path: string; rel: string; mtime: number | null; match_count: number; matches: SearchMatch[] };
export type SearchResponse = {
  root: string;
  query: string;
  results: SearchFileResult[];
  total_matches: number;
  total_files: number;
  truncated: boolean;
  truncated_reason: 'matches' | 'files' | null;
};
export type SearchOptions = { regex?: boolean; caseSensitive?: boolean; wholeWord?: boolean; include?: string; exclude?: string };
export type ReplaceResponse = {
  changed: { path: string; rel: string; replacements: number }[];
  skipped: { path: string; rel: string; reason: string }[];
  total_replacements: number;
  files_changed: number;
  truncated: boolean;
  undo_token: string | null;
};
export type UndoResponse = { restored: string[]; skipped: { path: string; reason: string }[] };

export async function searchFiles(root: string, query: string, opts: SearchOptions = {}, signal?: AbortSignal): Promise<SearchResponse> {
  const params = new URLSearchParams({ root, query });
  if (opts.regex) params.set('regex', '1');
  if (opts.caseSensitive) params.set('case', '1');
  if (opts.wholeWord) params.set('word', '1');
  if (opts.include) params.set('include', opts.include);
  if (opts.exclude) params.set('exclude', opts.exclude);
  return parse<SearchResponse>(await apiFetch(`/api/files/search?${params.toString()}`, { signal }));
}

// Recursive file/folder NAME search under `root` (backend: file_browser_service.search_names).
// Distinct from searchFiles, which greps file contents. `signal` lets the caller abort a stale
// in-flight search as the user keeps typing.
export async function searchNames(root: string, query: string, showHidden = false, signal?: AbortSignal): Promise<NameSearchResponse> {
  const params = new URLSearchParams({ root, query, show_hidden: showHidden ? '1' : '0' });
  return parse<NameSearchResponse>(await apiFetch(`/api/files/search_names?${params.toString()}`, { signal }));
}

export async function replaceInFiles(
  root: string,
  query: string,
  replacement: string,
  opts: SearchOptions & { paths?: string[]; expectedMtimes?: Record<string, number> } = {},
): Promise<ReplaceResponse> {
  return parse<ReplaceResponse>(
    await apiFetch('/api/files/search/replace', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        root,
        query,
        replacement,
        regex: opts.regex || undefined,
        case: opts.caseSensitive || undefined,
        word: opts.wholeWord || undefined,
        include: opts.include || undefined,
        exclude: opts.exclude || undefined,
        paths: opts.paths,
        expected_mtimes: opts.expectedMtimes,
      }),
    }),
  );
}

export async function undoReplace(token: string): Promise<UndoResponse> {
  return parse<UndoResponse>(
    await apiFetch('/api/files/search/undo', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token }),
    }),
  );
}

export async function systemFavorites(): Promise<Favorite[]> {
  const data = await parse<{ ok: true; favorites: Favorite[] }>(await apiFetch('/api/browse/favorites'));
  return data.favorites || [];
}
