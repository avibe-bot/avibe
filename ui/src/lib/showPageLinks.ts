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

// Same-origin path a Show Page is served at locally. Null when the page is
// offline (or public without a share id), where there is no live route.
export function localPath(page: ShowPageLinkInfo): string | null {
  if (page.visibility === 'private') return `/show/${encodeURIComponent(page.session_id)}/`;
  if (page.visibility === 'public' && page.share_id) return `/p/${encodeURIComponent(page.share_id)}/`;
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
