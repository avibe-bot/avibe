import { useSyncExternalStore } from 'react';

import { canRunDesktopVaultPasskey, isDesktopShell } from './desktopShell';
import { openLinkInNewContext } from './pwaNavigation';

/**
 * Why this document hands protected-vault passkey steps to the user's browser, or null when it
 * runs them itself.
 *
 * - `desktop`: a desktop shell without the passkey capability. Its web view offers WebAuthn only
 *   to apps associated with the sandbox, and it sends every new window to the system browser, so
 *   the sandbox's top-level authorization window never reaches its opener.
 * - `fallback`: a shell with the capability, where a passkey step already failed in the app during
 *   this page's life (see {@link recordVaultPasskeyOutcome}).
 *
 * Either way setup, unlock, protected approvals, and reveals run in the same Workbench opened in
 * the browser. This decides what the page offers, never what is allowed: the sandbox enforces.
 */
export type VaultPasskeyHandoff = 'desktop' | 'fallback';

// In memory for the page's life, never stored. What makes a step fail in the app (the shell
// refusing the window, the system not offering this app the passkey, a passkey without PRF) does
// not change while the page is open, so another in-app try could only fail again. A reload or a
// relaunch tries the app again, by which time the system may have fetched the association (it does
// so after first launch) or a fixed shell may be running; a stored flag would keep the user in
// the browser after the cause is gone, with nothing in the app to undo it.
let failedInApp = false;
// A system refusal before any passkey sheet reaches the sandbox as the same `NotAllowedError` a
// user's cancel does, so it reports `passkey_cancelled`. One cancel is respected; a second in a row
// with no passkey step succeeding in between offers the browser rather than loop on the same prompt.
let cancelsInARow = 0;
const CANCELS_BEFORE_BROWSER = 2;
const listeners = new Set<() => void>();

export function vaultPasskeyHandoff(): VaultPasskeyHandoff | null {
  if (!isDesktopShell()) return null;
  if (!canRunDesktopVaultPasskey()) return 'desktop';
  return failedInApp ? 'fallback' : null;
}

/** Whether this document must hand protected-vault passkey steps to the user's browser. */
export function vaultPasskeyNeedsBrowser(): boolean {
  return vaultPasskeyHandoff() !== null;
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/**
 * {@link vaultPasskeyHandoff}, re-rendering when an in-app failure switches to the browser, so a
 * surface already on screen never offers a step that can only fail again.
 */
export function useVaultPasskeyHandoff(): VaultPasskeyHandoff | null {
  return useSyncExternalStore(subscribe, vaultPasskeyHandoff, () => null);
}

// The user ended the step: a passkey prompt or the sandbox window dismissed, or the attempt
// superseded. `passkey_cancelled` is counted separately (see `cancelsInARow`).
const CANCELLED = new Set([
  'authorization_cancelled', 'authorization_window_closed', 'setup_window_closed', 'vault_operation_aborted', 'sandbox_reset',
]);
// The same answer would come back in a browser: the request or its context is stale or invalid,
// this Avibe and the pinned sandbox disagree, or the vault itself is in a state the user must change.
const NOT_WHERE_IT_RUNS = new Set([
  'sandbox_integrity_failed', 'sandbox_build_mismatch', 'sandbox_capability_mismatch', 'build_hash_mismatch',
  'passkey_not_configured', 'passkey_multiple_not_supported', 'parent_value_seal_disabled',
  'policy_downgrade_unavailable', 'protected_hd_unavailable', 'sandbox_curve_unsupported',
  'sandbox_browser_required', 'sandbox_entry_unavailable', 'sandbox_browser_unavailable',
]);
const notWhereItRuns = (code: string): boolean =>
  NOT_WHERE_IT_RUNS.has(code) || code.startsWith('invalid_') || code.startsWith('context_')
  || code.startsWith('policy_context_') || code.endsWith('_invalid');

/**
 * The vault sandbox client reports how each passkey-bearing step ended (`null` for success). In a
 * shell that runs these steps itself, any failure other than the user cancelling switches this
 * page to the browser hand-off: the window could not be opened, the passkey was not offered, or
 * the ceremony errored. Elsewhere it does nothing.
 */
export function recordVaultPasskeyOutcome(error: unknown): void {
  if (!canRunDesktopVaultPasskey() || failedInApp) return;
  if (error == null) {
    cancelsInARow = 0;
    return;
  }
  const code = typeof error === 'object' && 'code' in error && typeof error.code === 'string' ? error.code : 'sandbox_error';
  if (code === 'passkey_cancelled') {
    cancelsInARow += 1;
    if (cancelsInARow < CANCELS_BEFORE_BROWSER) return;
  } else if (CANCELLED.has(code) || notWhereItRuns(code)) {
    return;
  }
  failedInApp = true;
  for (const listener of [...listeners]) listener();
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

// `request_id` is also the deep link Avibe sends to IM (`vault_request_url`).
const STEP_PARAMS = ['request_id', 'protection', 'add', 'name', 'edit', 'reveal', 'field'];

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
  openLinkInNewContext(url.toString());
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
