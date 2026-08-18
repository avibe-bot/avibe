// Show Page link helpers shared by the admin Show Pages list and the in-chat
// share control. Avibe Cloud-qualified urls in the payload are null on a local
// install (no remote access configured), so fall back to the same-origin route
// the page is actually served at locally.

export type ShowPageLinkInfo = {
  session_id: string;
  visibility: string;
  active_url: string | null;
  share_id: string | null;
};

// Same-origin path a Show Page is served at locally. Limited and Public share
// one stable /p route; the server decides whether to start identity admission
// or serve the page anonymously.
export function localPath(page: ShowPageLinkInfo): string | null {
  if (page.visibility === 'private') return `/show/${encodeURIComponent(page.session_id)}/`;
  if ((page.visibility === 'limited' || page.visibility === 'public') && page.share_id) {
    return `/p/${encodeURIComponent(page.share_id)}/`;
  }
  return null;
}

// The Workbench always uses the authenticated author route. Shared /p links
// have separate admission rules and may intentionally reject non-public pages.
export function editorPath(page: ShowPageLinkInfo): string | null {
  if (page.visibility === 'private' || page.visibility === 'limited' || page.visibility === 'public') {
    return `/show/${encodeURIComponent(page.session_id)}/`;
  }
  return null;
}

export function liveHref(page: ShowPageLinkInfo): string | null {
  return page.active_url || localPath(page);
}

// Absolute, copyable/shareable href (origin-qualifies the same-origin fallback).
export function copyHref(page: ShowPageLinkInfo): string | null {
  const href = liveHref(page);
  if (!href) return null;
  return href.startsWith('http') ? href : window.location.origin + href;
}

// Protocol-stripped form for compact display.
export function displayLink(page: ShowPageLinkInfo): string | null {
  const href = liveHref(page);
  return href ? href.replace(/^https?:\/\//, '') : null;
}

// Custom share-link suffix (the /p/<share_id>/ segment). Mirrors the server
// rule in core/show_pages.validate_share_id so the field can give instant
// feedback before the request; the server stays the authority on uniqueness.
export const SHARE_ID_MIN_LENGTH = 3;
export const SHARE_ID_MAX_LENGTH = 64;
const SHARE_ID_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{1,62}[A-Za-z0-9]$/;

export function isValidShareId(value: string): boolean {
  return SHARE_ID_RE.test(value.trim());
}
