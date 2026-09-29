import { isDesktopShell } from './desktopShell';
import { openLinkInNewContext } from './pwaNavigation';

/**
 * Whether this document must hand protected-vault passkey steps to the user's
 * browser. The desktop shell's WKWebView offers WebAuthn only to apps holding a
 * matching associated-domains entitlement, and the shell sends every new window
 * to the system browser, so the sandbox's top-level authorization window never
 * reaches its opener. Setup, unlock, protected approvals, and reveals therefore
 * run in the same local Workbench opened in the browser.
 */
export function vaultPasskeyNeedsBrowser(): boolean {
  return isDesktopShell();
}

/** Opens Vaults in the user's browser, focused on `requestId` when given. */
export function openVaultsInBrowser(requestId?: string | null): void {
  const url = new URL('/vaults', window.location.origin);
  // The same deep link Avibe sends to IM (`vault_request_url`): Vaults opens the request itself.
  if (requestId) url.searchParams.set('request_id', requestId);
  openLinkInNewContext(url.toString());
}
