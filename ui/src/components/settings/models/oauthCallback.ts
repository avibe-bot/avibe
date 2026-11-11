// What a callback sign-in link says about where its grant comes back.

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
    const host = target.hostname.replace(/^\[|\]$/g, '');
    return {
      address: `${target.origin}${target.pathname}`,
      loopback: host === 'localhost' || host === '127.0.0.1' || host === '::1',
    };
  } catch {
    return null;
  }
}
