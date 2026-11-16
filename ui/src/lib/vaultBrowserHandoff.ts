import { isDesktopShell } from './desktopShell';
import { openLinkInNewContext } from './pwaNavigation';
import { noteVaultHandoff } from './vaultHandoffReturn';

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
  /** Replace fields of an existing protected secret. */
  | { kind: 'edit'; secretName: string }
  /** Show a protected secret's value. */
  | { kind: 'reveal'; secretName: string; field?: string };

// `request_id` is also the deep link Avibe sends to IM (`vault_request_url`). `from` and `session`
// mark a step the desktop app handed off, and the conversation it was handed off from.
const STEP_PARAMS = ['request_id', 'protection', 'add', 'name', 'edit', 'reveal', 'field', 'from', 'session'];

// What the desktop shell's `avibe://session/<id>` link accepts (`runtime-host/src/deep_link.rs`).
const DEEP_LINK_ID = /^[A-Za-z0-9._-]{1,128}$/;
const validSessionId = (id: string | null | undefined): id is string =>
  id != null && DEEP_LINK_ID.test(id) && id !== '.' && id !== '..';

/** The conversation the desktop app shows, which the browser tab can link back to. */
function currentChatSession(): string | null {
  const match = /^\/chat\/([^/]+)/.exec(window.location.pathname);
  const id = match?.[1];
  return validSessionId(id) ? id : null;
}

/** Opens Vaults in the user's browser on `step`. */
export function openVaultsInBrowser(step: VaultBrowserStep): void {
  const url = new URL('/vaults', window.location.origin);
  const params =
    step.kind === 'request'
      ? { request_id: step.requestId, ...(step.startProtected ? { protection: 'protected' } : {}) }
      : step.kind === 'add'
        ? { add: 'protected', ...(step.name ? { name: step.name } : {}) }
        : step.kind === 'edit'
          ? { edit: step.secretName }
          : { reveal: step.secretName, ...(step.field ? { field: step.field } : {}) };
  for (const [key, value] of Object.entries(params)) url.searchParams.set(key, value);
  url.searchParams.set('from', 'desktop');
  const session = currentChatSession();
  if (session) url.searchParams.set('session', session);
  noteVaultHandoff(step);
  openLinkInNewContext(url.toString());
}

/** A step the desktop app handed to this browser tab, and the conversation to link back to. */
export type DesktopVaultHandoff = { step: VaultBrowserStep; session: string | null };

/** The desktop hand-off a Vaults URL carries; an IM link or a plain visit carries none. */
export function readDesktopVaultHandoff(params: URLSearchParams): DesktopVaultHandoff | null {
  if (params.get('from') !== 'desktop') return null;
  const step = readVaultBrowserStep(params);
  if (!step) return null;
  const session = params.get('session');
  return { step, session: validSessionId(session) ? session : null };
}

/** The step a Vaults URL carries, whether the desktop app handed it off or IM linked to a request. */
export function readVaultBrowserStep(params: URLSearchParams): VaultBrowserStep | null {
  const requestId = params.get('request_id')?.trim();
  if (requestId) return { kind: 'request', requestId, startProtected: params.get('protection') === 'protected' };
  if (params.get('add') === 'protected') return { kind: 'add', name: params.get('name')?.trim() || undefined };
  const editName = params.get('edit')?.trim();
  if (editName) return { kind: 'edit', secretName: editName };
  const secretName = params.get('reveal')?.trim();
  const field = params.get('field')?.trim();
  return secretName ? { kind: 'reveal', secretName, ...(field ? { field } : {}) } : null;
}

/** `params` without a step, so a step resumed once doesn't resume again on reload. */
export function withoutVaultBrowserStep(params: URLSearchParams): URLSearchParams {
  const next = new URLSearchParams(params);
  for (const key of STEP_PARAMS) next.delete(key);
  return next;
}
