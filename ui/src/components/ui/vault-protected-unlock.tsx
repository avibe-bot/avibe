import { useState } from 'react';
import { Fingerprint, KeyRound, Loader2, Lock, RefreshCw, ShieldCheck } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { TFunction } from 'i18next';

import type { useProtectedVault } from '@/lib/useProtectedVault';
import { Button } from './button';
import { Input } from './input';

type Vault = ReturnType<typeof useProtectedVault>;

/** Map thrown codes / WebAuthn DOMExceptions to a friendly localized message. */
function friendlyError(t: TFunction, raw: string): string {
  if (raw.includes('passkey-prf-unavailable')) return t('vaults.protectedUnlock.errors.prfUnavailable');
  if (raw.includes('passkey-cancelled') || raw.includes('NotAllowed') || raw.includes('AbortError')) {
    return t('vaults.protectedUnlock.errors.cancelled');
  }
  if (raw.includes('passkey-not-configured')) return t('vaults.protectedUnlock.errors.noPasskey');
  if (raw.includes('vmk-discovery-failed')) return t('vaults.protectedUnlock.errors.discoveryFailed');
  // unwrapVmk throws when no copy decrypts → wrong factor.
  if (raw.includes('decrypt') || raw.includes('No matching') || raw.includes('wrap')) {
    return t('vaults.protectedUnlock.errors.wrongFactor');
  }
  return raw;
}

/**
 * Protected-tier setup / unlock panel: passkey-first (Touch ID / platform
 * authenticator via WebAuthn-PRF) with a "less secure" password fallback. The unlocked
 * VMK is cached for the session by {@link useProtectedVault}, so multiple protected
 * secrets can be created without re-prompting.
 */
export const VaultProtectedUnlock: React.FC<{ vault: Vault }> = ({ vault }) => {
  const { t } = useTranslation();
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [usePassword, setUsePassword] = useState(false);
  const [busy, setBusy] = useState(false);

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    vault.setError(null);
    try {
      await fn();
      setPassword('');
      setConfirm('');
    } catch (err) {
      vault.setError(friendlyError(t, err instanceof Error ? err.message : String(err)));
    } finally {
      setBusy(false);
    }
  };

  if (vault.status === 'checking') {
    return (
      <div className="flex items-center gap-2 rounded-lg border border-border bg-surface-2 px-3 py-2 text-sm text-muted">
        <Loader2 className="size-4 animate-spin" />
        {t('vaults.protectedUnlock.checking')}
      </div>
    );
  }

  if (vault.status === 'error') {
    return (
      <div className="flex flex-col gap-2 rounded-lg border border-destructive/40 bg-destructive/10 px-3 py-3 text-sm">
        <span className="font-medium text-destructive">{t('vaults.protectedUnlock.errorTitle')}</span>
        {vault.error && <span className="text-xs text-destructive">{friendlyError(t, vault.error)}</span>}
        <Button type="button" variant="secondary" size="sm" className="self-start" onClick={() => run(vault.refresh)} disabled={busy}>
          {busy ? <Loader2 className="size-3.5 animate-spin" /> : <RefreshCw className="size-3.5" />}
          {t('vaults.protectedUnlock.retry')}
        </Button>
      </div>
    );
  }

  if (vault.status === 'unlocked') {
    return (
      <div className="flex items-center gap-2 rounded-lg border border-mint/40 bg-mint-soft px-3 py-2 text-sm text-mint">
        <ShieldCheck className="size-4 shrink-0" />
        <span className="font-medium">{t('vaults.protectedUnlock.unlocked')}</span>
        <Button type="button" variant="ghost" size="sm" className="ml-auto h-7 text-muted" onClick={vault.lock} disabled={busy}>
          <Lock className="size-3.5" />
          {t('vaults.protectedUnlock.lock')}
        </Button>
      </div>
    );
  }

  const isSetup = vault.status === 'needs-setup';
  const showPasskey = isSetup || vault.hasPasskey();
  const showPasswordField = isSetup ? usePassword : true;

  const submitPassword = () => {
    if (!password) return;
    if (isSetup) {
      if (password !== confirm) {
        vault.setError(t('vaults.protectedUnlock.errors.mismatch'));
        return;
      }
      void run(() => vault.setupPassword(password));
    } else {
      void run(() => vault.unlockPassword(password));
    }
  };

  return (
    <div className="flex flex-col gap-2.5 rounded-lg border border-border bg-surface-2 px-3 py-3">
      <div className="flex items-start gap-2">
        <Lock className="mt-0.5 size-4 shrink-0 text-muted" />
        <div className="flex flex-col gap-0.5">
          <span className="text-sm font-semibold">
            {isSetup ? t('vaults.protectedUnlock.setupTitle') : t('vaults.protectedUnlock.unlockTitle')}
          </span>
          <span className="text-xs text-muted-foreground">
            {isSetup ? t('vaults.protectedUnlock.setupHelp') : t('vaults.protectedUnlock.unlockHelp')}
          </span>
        </div>
      </div>

      {showPasskey && (
        <Button
          type="button"
          variant="secondary"
          onClick={() => run(isSetup ? vault.setupPasskey : vault.unlockPasskey)}
          disabled={busy}
        >
          {busy ? <Loader2 className="size-4 animate-spin" /> : <Fingerprint className="size-4" />}
          {isSetup ? t('vaults.protectedUnlock.setupPasskey') : t('vaults.protectedUnlock.unlockPasskey')}
        </Button>
      )}

      {isSetup && !usePassword && (
        <button
          type="button"
          className="self-start text-xs text-muted-foreground underline-offset-2 hover:underline"
          onClick={() => setUsePassword(true)}
        >
          {t('vaults.protectedUnlock.usePasswordInstead')}
        </button>
      )}

      {showPasswordField && (
        <form
          className="flex flex-col gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            submitPassword();
          }}
        >
          <label className="flex flex-col gap-1.5 text-xs font-medium text-muted-foreground">
            <span className="flex items-center gap-1.5">
              <KeyRound className="size-3.5" />
              {isSetup ? t('vaults.protectedUnlock.setPasswordLabel') : t('vaults.protectedUnlock.passwordLabel')}
            </span>
            <Input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder={t('vaults.protectedUnlock.passwordPlaceholder')}
              autoComplete={isSetup ? 'new-password' : 'current-password'}
            />
          </label>
          {isSetup && (
            <label className="flex flex-col gap-1.5 text-xs font-medium text-muted-foreground">
              {t('vaults.protectedUnlock.confirmLabel')}
              <Input
                type="password"
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
                placeholder={t('vaults.protectedUnlock.confirmPlaceholder')}
                autoComplete="new-password"
              />
            </label>
          )}
          <div className="flex items-center justify-between gap-2">
            {isSetup ? (
              <span className="text-xs text-warning">{t('vaults.protectedUnlock.passwordLessSecure')}</span>
            ) : (
              <span />
            )}
            <Button type="submit" disabled={busy || !password || (isSetup && !confirm)}>
              {busy && <Loader2 className="size-4 animate-spin" />}
              {isSetup ? t('vaults.protectedUnlock.setupPasswordCta') : t('vaults.protectedUnlock.unlockCta')}
            </Button>
          </div>
        </form>
      )}

      {vault.error && <div className="text-xs text-destructive">{friendlyError(t, vault.error)}</div>}
    </div>
  );
};
