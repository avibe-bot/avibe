// Pure helpers for pinned Show Page apps: the letter avatar (icon-free tile) and
// the private surface path. Split out of ShowPageApp.tsx so the Dock and the
// registry can use them WITHOUT statically importing the (lazy-loaded) window
// component — keeping the app body code-split like every other app body.

// The brand accent set a pinned tile / window hashes into — the same CSS vars
// the built-in apps use (registry.tsx). A letter avatar on a hashed accent keeps
// multiple pinned pages distinguishable without an icon pipeline.
export const SHOW_PAGE_ACCENTS = ['--mint', '--cyan', '--violet', '--gold'] as const;

/** First grapheme of a label, grapheme-aware (keeps CJK, emoji, ZWJ sequences
 *  and combining marks intact). Empty string for blank input. Pure. */
export function firstGrapheme(text: string): string {
  const trimmed = (text ?? '').trim();
  if (!trimmed) return '';
  // Intl.Segmenter groups a full user-perceived character (e.g. 👩‍👧, é, 🇯🇵);
  // fall back to code-point iteration, which still never splits a surrogate pair.
  if (typeof Intl !== 'undefined' && typeof Intl.Segmenter === 'function') {
    const segmenter = new Intl.Segmenter(undefined, { granularity: 'grapheme' });
    const first = segmenter.segment(trimmed)[Symbol.iterator]().next();
    if (!first.done) return first.value.segment;
  }
  return Array.from(trimmed)[0] ?? '';
}

/** Deterministically map a session id to one of the brand accent vars. Pure. */
export function accentForSession(sessionId: string): string {
  let hash = 0;
  for (let i = 0; i < sessionId.length; i += 1) {
    // FNV-ish rolling hash kept in an unsigned 32-bit range so it is stable
    // across runs/devices (the accent must not flicker between reloads).
    hash = (hash * 31 + sessionId.charCodeAt(i)) >>> 0;
  }
  return SHOW_PAGE_ACCENTS[hash % SHOW_PAGE_ACCENTS.length];
}

/** The letter + accent var for a pinned Show Page's avatar tile. Pure. */
export function showPageAvatar(sessionId: string, title: string): { letter: string; accentVar: string } {
  const source = title.trim() || sessionId;
  return { letter: firstGrapheme(source).toUpperCase(), accentVar: accentForSession(sessionId) };
}

/** The private same-origin route a Show Page is always framed / opened at. Never
 *  the public /p/<share>/ link — the workbench app uses the authed surface. */
export function showPagePrivatePath(sessionId: string): string {
  return `/show/${encodeURIComponent(sessionId)}/`;
}

/** The URL for a page's own HTML icon, served by the dedicated
 *  `GET /api/show-pages/<sid>/icon` endpoint (§7.1f). ALL href resolution + policy
 *  lives server-side; the URL carries the session id in the PATH plus the
 *  server-issued opaque cache token as `?v=<token>`. The server NEVER derives
 *  resolution from the query — the token only VERSIONS the URL so it changes when
 *  the icon file changes, busting the cache with no client update-site enumeration
 *  (any payload refresh that changes the token changes the `src`, so the `<img>`
 *  refetches on its own). `iconVersion` doubles as the has-icon signal: null/blank →
 *  no icon → the caller falls back to the letter avatar (the endpoint also 404s on
 *  any rejection, and the tile's onError falls back). Pure. */
export function showPageIconUrl(sessionId: string, iconVersion: string | null | undefined): string | null {
  const token = (iconVersion ?? '').trim();
  if (!token) return null;
  return `/api/show-pages/${encodeURIComponent(sessionId)}/icon?v=${encodeURIComponent(token)}`;
}

/** The in-app route to the owning session's Chat page (`/chat/:sessionId`). The Show
 *  Page window's chat-bubble navigates here (SPA nav, not a new tab) and minimizes the
 *  window. Pass `{ showChat: true }` to append the `?view=chat` signal that tells
 *  ChatPage to leave Show Page mode — required when the target is the SAME session
 *  already open in Show Page mode (the `:sessionId` path alone wouldn't change, so the
 *  navigation would otherwise be a no-op). Reusable by any "show me the chat" entry
 *  point (inbox, deep links). Pure. */
export function sessionChatPath(sessionId: string, opts?: { showChat?: boolean }): string {
  const path = `/chat/${encodeURIComponent(sessionId)}`;
  return opts?.showChat ? `${path}?view=chat` : path;
}
