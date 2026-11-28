import { useSyncExternalStore } from 'react';

// Lightweight in-memory overlay of Session titles learned from the live SSE
// stream (`session.activity` with `event: 'updated'` and `inbox.session.updated`).
//
// Initial titles arrive fresh from the server at read time inside each message's
// `references` (one indexed SQL query per page, zero per-chip requests). This
// store only patches chips that are already mounted while the page stays open
// (e.g. a newly spawned background Session auto-titles a few seconds after the
// parent Agent mentioned it, or the user renames a Session in the sidebar).
// Per-id subscriber sets keep each title update O(chips pointing at that id).

const titles = new Map<string, string | null>();
const listenersBySession = new Map<string, Set<() => void>>();

const MAX_ENTRIES = 2000;

function notify(sessionId: string): void {
  const listeners = listenersBySession.get(sessionId);
  if (!listeners) return;
  for (const listener of listeners) listener();
}

function normalizeTitle(title: string | null | undefined): string | null {
  if (typeof title !== 'string') return null;
  const trimmed = title.trim();
  return trimmed ? trimmed : null;
}

/** Record a live Session title observed over SSE or a local rename. */
export function setLiveSessionTitle(sessionId: string, title: string | null | undefined): void {
  const id = sessionId.trim();
  if (!id) return;
  const next = normalizeTitle(title);
  if (titles.has(id) && titles.get(id) === next) return;
  if (!titles.has(id) && titles.size >= MAX_ENTRIES) {
    const oldest = titles.keys().next().value;
    if (oldest !== undefined) titles.delete(oldest);
  }
  titles.set(id, next);
  notify(id);
}

/** Clear all cached live titles (e.g. on authorization/account reset or tests). */
export function clearLiveSessionTitles(): void {
  if (titles.size === 0) return;
  const affected = Array.from(titles.keys());
  titles.clear();
  for (const id of affected) notify(id);
}

/** Subscribe to live title updates for one Session id.
 *  Returns `undefined` when no live update has been observed yet. */
export function useLiveSessionTitle(sessionId: string): string | null | undefined {
  return useSyncExternalStore(
    (onStoreChange) => {
      let set = listenersBySession.get(sessionId);
      if (!set) {
        set = new Set();
        listenersBySession.set(sessionId, set);
      }
      set.add(onStoreChange);
      return () => {
        set?.delete(onStoreChange);
        if (set?.size === 0) listenersBySession.delete(sessionId);
      };
    },
    () => (titles.has(sessionId) ? titles.get(sessionId) : undefined),
    () => undefined,
  );
}
