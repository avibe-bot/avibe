// Show Page icon bytes held for the life of this document, keyed by their
// content-versioned URL and handed to <img> as object URLs.
//
// Remote Workbench requests receive icons as `private, no-store` so the browser
// HTTP cache never outlives the server's per-request ACL check. Once an
// unmounted <img> is garbage-collected, its next mount downloads the icon
// again. The hover Dock unmounts whenever it hides, so every reveal waited one
// tunnel round trip per pinned icon before anything appeared.
//
// Reuse is safe here because the server enforces `?v=` against the bytes it
// serves: one URL always names the same content. Lookups only come from URLs an
// authorized inventory is rendering right now, and nothing outlives the page.
// The budget bounds memory when a library holds many large icons; the least
// recently used entries go first.
const BUDGET_BYTES = 16 * 1024 * 1024;

type Entry = { src: string; bytes: number };

// Map iteration order is insertion order, so the first entry is the least
// recently used one.
const entries = new Map<string, Entry>();
const pending = new Map<string, Promise<string>>();
let totalBytes = 0;

/** The object URL already held for an icon URL, or null. Pure: safe in render. */
export function peekShowPageIcon(url: string): string | null {
  return entries.get(url)?.src ?? null;
}

/** Resolve an icon URL to an object URL, downloading it at most once at a time.
 *  Rejects when the download fails so the caller can retry or fall back. */
export function loadShowPageIcon(url: string): Promise<string> {
  const hit = entries.get(url);
  if (hit) {
    entries.delete(url);
    entries.set(url, hit);
    return Promise.resolve(hit.src);
  }
  let request = pending.get(url);
  if (!request) {
    request = download(url).finally(() => pending.delete(url));
    pending.set(url, request);
  }
  return request;
}

/** Drop a held icon whose bytes the browser could not decode. */
export function forgetShowPageIcon(url: string): void {
  const entry = entries.get(url);
  if (!entry) return;
  entries.delete(url);
  totalBytes -= entry.bytes;
  URL.revokeObjectURL(entry.src);
}

async function download(url: string): Promise<string> {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`Show Page icon request failed with ${response.status}`);
  const blob = await response.blob();
  const entry = { src: URL.createObjectURL(blob), bytes: blob.size };
  entries.set(url, entry);
  totalBytes += entry.bytes;
  for (const [key, old] of entries) {
    if (totalBytes <= BUDGET_BYTES || key === url) break;
    entries.delete(key);
    totalBytes -= old.bytes;
    URL.revokeObjectURL(old.src);
  }
  return entry.src;
}
