import { vaultPasskeyNeedsBrowser } from './vaultBrowserHandoff';

export type VaultEntryIssue = 'address' | 'browser' | 'desktop';

/**
 * Parent WebCrypto precondition for sandbox integrity checks, not an origin
 * allowlist or a full WebAuthn/PRF capability probe. The HTTPS sandbox owns the
 * credential RP, so neither the parent hostname nor its WebAuthn API gates this.
 */
function webauthnAvailable(): boolean {
  return typeof window !== 'undefined' && typeof crypto !== 'undefined' && Boolean(crypto.subtle);
}

/** Parent-entry preconditions, NOT a promise of WebAuthn/PRF support.
 * Keep the intersection of the pinned sandbox's RPC allowlist and frame-ancestors
 * policy (vault-sandbox 52b690a): loopback HTTP(S) on any port, Avibe HTTPS on 443.
 * CSP's http host sources also match https upgrades; Avibe sources have no :*.
 * The sandbox still owns its RP, integrity verification and credential checks.
 */
export function vaultEntryIssue(): VaultEntryIssue | null {
  if (vaultPasskeyNeedsBrowser()) return 'desktop';
  if (typeof window === 'undefined') return 'browser';
  const { protocol, hostname, port } = window.location;
  const loopback = ['localhost', '127.0.0.1', '[::1]'].includes(hostname)
    && (protocol === 'http:' || protocol === 'https:');
  const avibe = protocol === 'https:' && (hostname === 'avibe.bot' || hostname.endsWith('.avibe.bot'))
    && (port === '' || port === '443');
  if (!loopback && !avibe) return 'address';
  // Secure-context state includes ancestors; a trustworthy host under an HTTP
  // ancestor is not enough. Parent WebAuthn is deliberately not required: the
  // top-level HTTPS sandbox, not this parent, runs the passkey ceremony.
  if (!window.isSecureContext || !webauthnAvailable()) return 'browser';
  return null;
}

/** Shared fail-fast defense for programmatic callers, before preparation or UI. */
export function assertVaultEntryAvailable(): void {
  const issue = vaultEntryIssue();
  if (issue) throw Object.assign(new Error('Protected Vault is unavailable in this entry context'), {
    code: issue === 'desktop' ? 'sandbox_browser_required' : issue === 'browser' ? 'sandbox_browser_unavailable' : 'sandbox_entry_unavailable',
  });
}
