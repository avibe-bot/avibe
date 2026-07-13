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
