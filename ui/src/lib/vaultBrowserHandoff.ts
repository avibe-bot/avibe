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

/**
 * A Vaults step the desktop app hands to the browser, where the Vaults page resumes it at the point
 * the user left and stops at the first click the step needs (a passkey prompt or the sandbox window
 * only starts from one). What identifies the step travels, and so does a choice of the Protected
 * tier; the draft stays behind, and above all a secret value never appears in a URL.
 */
export type VaultBrowserStep =
  /** Review or answer a pending request; `startProtected` reopens a provision answer on Protected. */
  | { kind: 'request'; requestId: string; startProtected?: boolean }
  /** Add a new protected secret, fixed to `name` when an agent asked for it by name. */
  | { kind: 'add'; name?: string }
  /** Show a protected secret's value. */
  | { kind: 'reveal'; secretName: string };

// `request_id` is also the deep link Avibe sends to IM (`vault_request_url`).
const STEP_PARAMS = ['request_id', 'protection', 'add', 'name', 'reveal'];

/** Opens Vaults in the user's browser on `step`. */
export function openVaultsInBrowser(step: VaultBrowserStep): void {
  const url = new URL('/vaults', window.location.origin);
  const params =
    step.kind === 'request'
      ? { request_id: step.requestId, ...(step.startProtected ? { protection: 'protected' } : {}) }
      : step.kind === 'add'
        ? { add: 'protected', ...(step.name ? { name: step.name } : {}) }
        : { reveal: step.secretName };
  for (const [key, value] of Object.entries(params)) url.searchParams.set(key, value);
  openLinkInNewContext(url.toString());
}

/** The step a Vaults URL carries, whether the desktop app handed it off or IM linked to a request. */
export function readVaultBrowserStep(params: URLSearchParams): VaultBrowserStep | null {
  const requestId = params.get('request_id')?.trim();
  if (requestId) return { kind: 'request', requestId, startProtected: params.get('protection') === 'protected' };
  if (params.get('add') === 'protected') return { kind: 'add', name: params.get('name')?.trim() || undefined };
  const secretName = params.get('reveal')?.trim();
  return secretName ? { kind: 'reveal', secretName } : null;
}

/** `params` without a step, so a step resumed once doesn't resume again on reload. */
export function withoutVaultBrowserStep(params: URLSearchParams): URLSearchParams {
  const next = new URLSearchParams(params);
  for (const key of STEP_PARAMS) next.delete(key);
  return next;
}
