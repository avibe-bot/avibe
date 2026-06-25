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
};

export type FsMeta = {
  ok: true;
  name: string;
  ext: string;
  kind: 'dir' | 'file' | 'symlink';
  size: number | null;
  mtime: number | null;
  mime: string | null;
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

async function parse<T>(res: Response): Promise<T> {
  const data = await res.json().catch(() => ({}) as Record<string, unknown>);
  if (!res.ok || (data as { ok?: boolean }).ok === false) {
    const err = (data as { error?: { code?: string; message?: string } }).error || {};
    throw new FilesApiError(err.code || String(res.status), err.message || 'Request failed');
  }
  return data as T;
}

export function joinPath(base: string, name: string): string {
  return base.endsWith('/') ? `${base}${name}` : `${base}/${name}`;
}

export function pathCrumbs(path: string): { label: string; path: string }[] {
  const parts = path.split('/').filter(Boolean);
  const out: { label: string; path: string }[] = [{ label: '/', path: '/' }];
  let cur = '';
  for (const part of parts) {
    cur += `/${part}`;
    out.push({ label: part, path: cur });
  }
  return out;
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

export async function readText(path: string): Promise<string> {
  const res = await apiFetch(contentUrl(path));
  if (!res.ok) {
    await parse(res); // throws a FilesApiError
  }
  return res.text();
}

export async function writeFile(
  path: string,
  content: string,
  expectedMtime?: number | null,
): Promise<{ ok: true; mtime: number }> {
  const res = await apiFetch('/api/files/write', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path, content, expected_mtime: expectedMtime ?? undefined }),
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

export async function deletePath(path: string, recursive = false): Promise<{ ok: true }> {
  return parse(
    await apiFetch('/api/files/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path, recursive }),
    }),
  );
}

export async function systemFavorites(): Promise<Favorite[]> {
  const data = await parse<{ ok: true; favorites: Favorite[] }>(await apiFetch('/api/browse/favorites'));
  return data.favorites || [];
}
