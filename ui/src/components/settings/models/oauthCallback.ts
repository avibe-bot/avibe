// What a callback sign-in link says about where its grant comes back.

import { isDesktopShell } from '@/lib/desktopShell';

const isLoopback = (hostname: string): boolean => {
  const host = hostname.replace(/^\[|\]$/g, '');
  return host === 'localhost' || host === '127.0.0.1' || host === '::1';
};

/**
 * Where a callback sign-in sends the browser, read from the sign-in link's own
 * `redirect_uri`, and whether that is this machine's loopback, where the gateway's
 * listener can take the callback without a paste. `null` when the link names none.
 */
export function callbackRedirect(authUrl: string | null | undefined): { address: string; loopback: boolean } | null {
  if (!authUrl) return null;
  try {
    const redirect = new URL(authUrl).searchParams.get('redirect_uri');
    if (!redirect) return null;
    const target = new URL(redirect);
    return { address: `${target.origin}${target.pathname}`, loopback: isLoopback(target.hostname) };
  } catch {
    return null;
  }
}

/**
 * Whether this page runs on the machine Avibe runs on: opened at a loopback address,
 * or in the desktop app. Only then is the browser that signs in likely to be the one
 * whose loopback the gateway listens on. A page reached through a remote address — a
 * phone on the tunnel — signs in somewhere else, and its callback has to be pasted.
 */
export const pageOnAvibeMachine = (hostname: string = window.location.hostname): boolean =>
  isLoopback(hostname) || isDesktopShell();
