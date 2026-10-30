import { isApplicationRouteHref } from './applicationRoutes';

declare global {
  interface Window {
    __AVIBE_PWA_NAVIGATE_SAME_ORIGIN__?: (href: string) => boolean;
  }
}

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
 * Resolve a same-origin `_blank` navigation to the target the installed iOS
 * PWA should open in its current browsing context.
 *
 * Canonical AppShell routes stay on the SPA path, avoiding a reload and another
 * auth pass. Show Pages and every other same-origin document keep their literal
 * destination in the current context so WebKit never creates the secondary
 * context that iOS may incorrectly restore after process eviction. Callers
 * exclude download anchors before using this resolver.
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

    return {
      path: `${target.pathname}${target.search}${target.hash}`,
      navigation: isApplicationRouteHref(target.pathname) ? 'spa' : 'document',
    };
  } catch {
    return null;
  }
}

/**
 * Open a link that normally requests a new browsing context while honoring the
 * installed-PWA same-origin policy. The AppShell bridge returns true only when
 * it handled the destination in the current PWA context; desktop and external
 * destinations keep the browser's native `_blank` behavior.
 */
export function openLinkInNewContext(href: string, features?: string): Window | null {
  if (typeof window === 'undefined') return null;
  if (window.__AVIBE_PWA_NAVIGATE_SAME_ORIGIN__?.(href)) return null;
  return window.open(href, '_blank', features);
}
