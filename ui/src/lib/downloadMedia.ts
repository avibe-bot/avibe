import type { MouseEvent } from 'react';

import { apiFetch } from '@/lib/apiFetch';
import { isProxyMediaUrl } from '@/lib/mediaProxy';
import { isIosDevice, isStandalonePwa } from '@/lib/platform';

// Saving an agent-reply attachment (image / file) on an iOS Home-Screen PWA.
//
// The download buttons render a plain ``<a href="/api/media/...?download=1"
// download>``. That works everywhere with browser chrome — desktop, Android,
// in-browser iOS Safari — where the anchor downloads, or, if iOS previews
// instead, there's a back button. But in an INSTALLED iOS PWA there is no
// chrome and iOS ignores the ``download`` attribute, so the tap becomes a
// same-origin top-level navigation to the file preview with no way back: the
// app is trapped.
//
// In a standalone PWA the only save path that does NOT trap is the native share
// sheet (Web Share API level 2) — a dismissible overlay offering "Save to
// Files" / "Save Image" that returns to the app. A ``window.open`` / ``_blank``
// "fallback" is not safe: WebKit loads a same-origin in-scope URL in place in
// the standalone window, i.e. the very trap we're escaping. So when sharing
// can't run we do nothing (a rare no-op the user can just tap again — the
// refetch is cache-warm and instant) rather than navigate into the trap.

// Build the ``<a href>`` for a media download. Appends ``download=1`` with the
// right separator so it survives a URL that already carries a query (agent-reply
// images keep their ``?w=&h=`` pixel-dimension hints) instead of producing an
// invalid double ``?``.
export function mediaDownloadHref(url: string): string {
  return `${url}${url.includes('?') ? '&' : '?'}download=1`;
}

const MIME_EXT: Record<string, string> = {
  'image/png': 'png',
  'image/jpeg': 'jpg',
  'image/gif': 'gif',
  'image/webp': 'webp',
  'image/svg+xml': 'svg',
  'image/avif': 'avif',
  'image/heic': 'heic',
  'application/pdf': 'pdf',
};

// A name for the shared File. Prefer the caller's known filename; otherwise
// synthesize ``media.<ext>`` from the blob's MIME so the share sheet still has a
// sensible name + extension to save under.
function inferFilename(mime: string, fallback?: string): string {
  const base = (mime || '').split(';')[0].trim().toLowerCase();
  const ext = MIME_EXT[base] || base.split('/')[1] || '';
  const named = (fallback || '').trim();
  if (named) return /\.[a-z0-9]{1,8}$/i.test(named) || !ext ? named : `${named}.${ext}`;
  return `media.${ext || 'bin'}`;
}

function serverFilename(response: Response): string | undefined {
  // The media proxy emits an RFC 5987 UTF-8 filename for both preview and download.
  const encoded = response.headers.get('Content-Disposition')?.match(/(?:^|;)\s*filename\*=UTF-8''([^;]*)/i)?.[1];
  if (!encoded) return undefined;
  try {
    return decodeURIComponent(encoded);
  } catch {
    // An invalid header must not prevent saving with the caller's fallback name.
    return undefined;
  }
}

// Reload the current page so the server re-runs its auth gate and 302s to the
// cloud login (mirrors apiFetch's 401 recovery / RemoteLoginRedirect).
function recoverFromExpiredSession(): void {
  if (typeof window !== 'undefined') window.location.assign(window.location.href);
}

async function saveViaShareSheet(url: string, filename?: string): Promise<void> {
  if (typeof navigator === 'undefined' || typeof navigator.share !== 'function') return;
  try {
    // ``redirect: 'manual'`` so we can SEE an auth redirect instead of chasing
    // it: an expired remote-access session 302s GETs to the cloud login (only
    // non-GET gets the JSON 401 apiFetch handles), which would otherwise be a
    // cross-origin CORS throw swallowed below. Accept any type — binary media,
    // not JSON.
    const res = await apiFetch(url, {
      credentials: 'same-origin',
      headers: { Accept: '*/*' },
      redirect: 'manual',
    });
    // Session expired (302 → opaqueredirect, or a defensive 401): send the user
    // through the full login flow the old top-level navigation would have,
    // instead of a silent no-op.
    if (res.type === 'opaqueredirect' || res.status === 401) {
      recoverFromExpiredSession();
      return;
    }
    if (!res.ok) return;
    const blob = await res.blob();
    const file = new File([blob], serverFilename(res) || inferFilename(blob.type, filename), {
      type: blob.type || 'application/octet-stream',
    });
    // Probe the REAL file (type + size matter on iOS), not a dummy: canShare can
    // accept a tiny text file yet reject the actual media.
    if (typeof navigator.canShare === 'function' && !navigator.canShare({ files: [file] })) return;
    await navigator.share({ files: [file] });
  } catch {
    // Either the user dismissed the sheet (AbortError — a success) or the share
    // couldn't run: e.g. a large / slow fetch outlived the click's transient
    // activation, so share() rejected with NotAllowedError. Either way, never
    // navigate as a "fallback" — that re-traps the PWA. A second tap re-shares
    // from warm cache and succeeds.
  }
}

// Click handler for the media download anchors. Two gates before we intercept:
//   1. Only our same-origin ``/api/media`` proxy URLs — a non-proxy attachment
//      (ChatPage renders third-party hrefs as a click-through FileCard too) must
//      keep native navigation; we must not auto-fetch a remote host, and a
//      cross-origin fetch would just CORS-fail into a dead button anyway.
//   2. Only the installed iOS PWA traps on the native anchor download; every
//      other context falls through to the browser's own ``<a download>``.
// Propagation is the call site's concern (stopping a parent lightbox/zoom
// handler on every platform), so this only owns the iOS-specific
// ``preventDefault`` that stops the trapping navigation. ``url`` is the bare
// proxy URL — no ``?download=1`` needed, since we name the File ourselves.
export function handleMediaDownloadClick(e: MouseEvent, url: string, filename?: string): void {
  if (!isProxyMediaUrl(url)) return;
  if (!(isIosDevice() && isStandalonePwa())) return;
  e.preventDefault();
  void saveViaShareSheet(url, filename);
}
