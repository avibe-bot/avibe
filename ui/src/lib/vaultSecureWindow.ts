import type { TFunction } from 'i18next';

import { isVaultPasskeyFallback } from './vaultBrowserHandoff';
import { VaultSandboxError } from './vaultSandboxClient';

/** What a failure of the secure window that takes a Protected value means for the person (nothing
 *  was saved in any of them), or null for any other error. A failure that moved the desktop app to
 *  the browser hand-off says nothing here: the panel's own browser note speaks (empty string). */
export function secureWindowFailure(t: TFunction, err: unknown): string | null {
  if (isVaultPasskeyFallback(err)) return '';
  const code = err instanceof VaultSandboxError ? err.code : (err as { code?: unknown } | null)?.code;
  switch (code) {
    case 'authorization_cancelled': return t('vaults.dialog.window.cancelled');
    case 'authorization_window_closed': return t('vaults.dialog.window.closed');
    // The page stopping its wait (the window's answer was lost) is a timeout too: no record came back.
    case 'authorization_window_timeout':
    case 'sandbox_request_timeout': return t('vaults.dialog.window.timeout');
    case 'authorization_popup_blocked': return t('vaults.dialog.window.blocked');
    case 'vault_operation_aborted':
    case 'sandbox_reset': return t('vaults.dialog.window.reset');
    case 'passkey_cancelled': return t('vaults.protectedUnlock.errors.cancelled');
    case 'passkey_prf_unavailable': return t('vaults.protectedUnlock.errors.prfUnavailableUnlock');
    default: return null;
  }
}
