import { useState } from 'react';
import { Fingerprint, KeyRound, Loader2, Lock, ShieldCheck } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { TFunction } from 'i18next';

import { cn } from '@/lib/utils';
import type { useProtectedVault } from '@/lib/useProtectedVault';
import { Button } from './button';
import { Input } from './input';

type Vault = ReturnType<typeof useProtectedVault>;

/** Map thrown codes / WebAuthn DOMExceptions to a friendly localized message. */
function friendlyError(t: TFunction, raw: string): string {
  if (raw.includes('passkey-prf-unavailable')) return t('vaults.protected.errors.prfUnavailable');
  if (raw.includes('passkey-cancelled') || raw.includes('NotAllowed') || raw.includes('AbortError')) {
    return t('vaults.protected.errors.cancelled');
  }
  if (raw.includes('passkey-not-configured')) return t('vaults.protected.errors.noPasskey');
  // unwrapVmk throws when no copy decrypts → wrong factor.
  if (raw.includes('decrypt') || raw.includes('No matching') || raw.includes('wrap')) {
    return t('vaults.protected.errors.wrongFactor');
  }
  return raw;
}

/**
 * Protected-tier setup / unlock panel: passkey-first (Touch ID / platform
 * authenticator via WebAuthn-PRF) with a "less secure" password fallback. Holds the
 * unlocked VMK in browser memory for the session so multiple protected secrets can be
 * created without re-prompting.
 */
export const VaultProtectedUnlock: React.FC<{ vault: Vault }> = ({ vault }) => {
  const { t } = useTranslation();
  const [password, setPassword] = useState('');
  const [usePassword, setUsePassword] = useState(false);
  const [busy, setBusy] = useState(false);

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    vault.setError(null);
    try {
      await fn();
      setPassword('');
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
        {t('vaults.protected.checking')}
      </div>
    );
  }

  if (vault.status === 'unlocked') {
    return (
      <div className="flex items-center gap-2 rounded-lg border border-mint/40 bg-mint-soft px-3 py-2 text-sm text-mint">
        <ShieldCheck className="size-4 shrink-0" />
        <span className="font-medium">{t('vaults.protected.unlocked')}</span>
        <Button type="button" variant="ghost" size="sm" className="ml-auto h-7 text-muted" onClick={vault.lock} disabled={busy}>
          <Lock className="size-3.5" />
          {t('vaults.protected.lock')}
        </Button>
      </div>
    );
  }

  const isSetup = vault.status === 'needs-setup';
  const showPasskey = isSetup || vault.hasPasskey();
  const showPasswordField = isSetup ? usePassword : true;

  return (
    <div className="flex flex-col gap-2.5 rounded-lg border border-border bg-surface-2 px-3 py-3">
      <div className="flex items-start gap-2">
        <Lock className="mt-0.5 size-4 shrink-0 text-muted" />
        <div className="flex flex-col gap-0.5">
          <span className="text-sm font-semibold">{isSetup ? t('vaults.protected.setupTitle') : t('vaults.protected.unlockTitle')}</span>
          <span className="text-xs text-muted-foreground">
            {isSetup ? t('vaults.protected.setupHelp') : t('vaults.protected.unlockHelp')}
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
          {isSetup ? t('vaults.protected.setupPasskey') : t('vaults.protected.unlockPasskey')}
        </Button>
      )}

      {isSetup && !usePassword && (
        <button
          type="button"
          className="self-start text-xs text-muted-foreground underline-offset-2 hover:underline"
          onClick={() => setUsePassword(true)}
        >
          {t('vaults.protected.usePasswordInstead')}
        </button>
      )}

      {showPasswordField && (
        <form
          className="flex flex-col gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (password) void run(isSetup ? () => vault.setupPassword(password) : () => vault.unlockPassword(password));
          }}
        >
          <label className="flex flex-col gap-1.5 text-xs font-medium text-muted-foreground">
            <span className="flex items-center gap-1.5">
              <KeyRound className="size-3.5" />
              {isSetup ? t('vaults.protected.setPasswordLabel') : t('vaults.protected.passwordLabel')}
            </span>
            <div className="flex items-center gap-2">
              <Input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder={t('vaults.protected.passwordPlaceholder')}
                autoComplete={isSetup ? 'new-password' : 'current-password'}
                className="min-w-0 flex-1"
              />
              <Button type="submit" disabled={busy || !password}>
                {busy && <Loader2 className="size-4 animate-spin" />}
                {isSetup ? t('vaults.protected.setupPasswordCta') : t('vaults.protected.unlockCta')}
              </Button>
            </div>
          </label>
          {isSetup && <span className={cn('text-xs text-warning')}>{t('vaults.protected.passwordLessSecure')}</span>}
        </form>
      )}

      {vault.error && <div className="text-xs text-destructive">{vault.error}</div>}
    </div>
  );
};
