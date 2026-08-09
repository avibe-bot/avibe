import { isApplicationRouteHref } from './applicationRoutes';

function normalizeHostname(hostname: string): string {
  return hostname.trim().toLowerCase().replace(/^\[|\]$/g, '').replace(/\.$/, '');
}

function isLoopbackHostname(hostname: string): boolean {
  const normalized = normalizeHostname(hostname);
  if (normalized === 'localhost' || normalized.endsWith('.localhost') || normalized === '::1') {
    return true;
  }

  const octets = normalized.split('.');
  return (
    octets.length === 4 &&
    octets[0] === '127' &&
    octets.every((octet) => /^\d{1,3}$/.test(octet) && Number(octet) <= 255)
  );
}

export function shouldBlockPwaLoopbackLink(href: string, currentHref: string): boolean {
  try {
    const current = new URL(currentHref);
    const target = new URL(href, current);
    if (target.protocol !== 'http:' && target.protocol !== 'https:') return false;

    // A loopback link is valid when the app itself is being used on loopback.
    // From a remote iPhone PWA it points at the phone, not the Avibe host.
    return !isLoopbackHostname(current.hostname) && isLoopbackHostname(target.hostname);
  } catch {
    return false;
  }
}

/**
 * Resolve a `_blank` link that is really an internal Avibe destination to the
 * target the installed iOS PWA should open in its current browsing context.
 *
 * A private Show Page root uses the in-shell app route so the user keeps Avibe
 * chrome and back navigation; deep private locations retain their complete URL
 * in a same-context document navigation. Canonical AppShell routes stay on the
 * SPA path, avoiding a reload and another auth pass. Public Show Pages load
 * their server-owned `/p/` document. Unrelated same-origin resources (downloads,
 * API endpoints, media) are not intercepted.
 */
export interface InternalPwaLinkTarget {
  path: string;
  navigation: 'spa' | 'document';
}

export function internalPwaLinkTarget(
  href: string,
  currentHref: string,
): InternalPwaLinkTarget | null {
  try {
    const current = new URL(currentHref);
    const target = new URL(href, current);
    if (target.origin !== current.origin || !['http:', 'https:'].includes(target.protocol)) return null;

    const privateShow = /^\/show\/([^/]+)(?:\/(.*))?$/.exec(target.pathname);
    if (privateShow) {
      const rootPath = privateShow[2] === undefined || privateShow[2] === '';
      if (rootPath && !target.search && !target.hash) {
        return { path: `/apps/show/${privateShow[1]}`, navigation: 'spa' };
      }
      return {
        path: `${target.pathname}${target.search}${target.hash}`,
        navigation: 'document',
      };
    }

    if (/^\/p\/[^/]+(?:\/.*)?$/.test(target.pathname)) {
      return {
        path: `${target.pathname}${target.search}${target.hash}`,
        navigation: 'document',
      };
    }

    if (!isApplicationRouteHref(target.pathname)) return null;
    return {
      path: `${target.pathname}${target.search}${target.hash}`,
      navigation: 'spa',
    };
  } catch {
    return null;
  }
}
