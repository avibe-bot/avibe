import { useState } from 'react';
import { Fingerprint, KeyRound, Loader2, Lock, RefreshCw, ShieldCheck } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { TFunction } from 'i18next';

import { webauthnAvailable } from '@/lib/useProtectedVault';
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
  if (raw.includes('vault-already-initialized')) return t('vaults.protectedUnlock.errors.alreadyInitialized');
  if (raw.includes('vmk-discovery-failed')) return t('vaults.protectedUnlock.errors.discoveryFailed');
  // unwrapVmk throws when no copy decrypts → wrong factor.
  if (raw.includes('decrypt') || raw.includes('No matching') || raw.includes('wrap')) {
    return t('vaults.protectedUnlock.errors.wrongFactor');
  }
  return raw;
}

const PANEL = 'flex flex-col gap-2.5 rounded-lg border border-border bg-surface-2 px-3 py-3';

/**
 * Protected-tier setup / unlock panel. Setup always captures a password (the recovery
 * root) and offers a passkey on top (Touch ID / Windows Hello, when the origin allows
 * WebAuthn); unlock accepts either factor. The unlocked VMK is cached for the session
 * by {@link useProtectedVault}, so multiple protected secrets can be created without
 * re-prompting.
 */
export const VaultProtectedUnlock: React.FC<{ vault: Vault }> = ({ vault }) => {
  const { t } = useTranslation();
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [busy, setBusy] = useState(false);
  const [ackLoss, setAckLoss] = useState(false);

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

  const canUsePasskey = webauthnAvailable();

  if (vault.status === 'needs-setup') {
    const passwordValid = password.trim().length > 0 && password === confirm;
    const submitPassword = () => {
      if (password.trim().length === 0) return;
      if (password !== confirm) {
        vault.setError(t('vaults.protectedUnlock.errors.mismatch'));
        return;
      }
      void run(() => vault.setupPassword(password));
    };
    return (
      <div className={PANEL}>
        <div className="flex items-start gap-2">
          <Lock className="mt-0.5 size-4 shrink-0 text-muted" />
          <div className="flex flex-col gap-0.5">
            <span className="text-sm font-semibold">{t('vaults.protectedUnlock.setupTitle')}</span>
            <span className="text-xs text-muted-foreground">{t('vaults.protectedUnlock.setupHelp')}</span>
          </div>
        </div>

        {/* Passkey — most secure (no phishable password), but unrecoverable: explicit ack required. */}
        {canUsePasskey && (
          <div className="flex flex-col gap-2 rounded-lg border border-border bg-surface p-2.5">
            <span className="flex items-center gap-1.5 text-xs font-semibold">
              <Fingerprint className="size-3.5 text-mint" />
              {t('vaults.protectedUnlock.passkeyOptionTitle')}
            </span>
            <span className="text-xs text-muted-foreground">{t('vaults.protectedUnlock.passkeyOptionHelp')}</span>
            <div className="rounded-md border border-warning/40 bg-warning/10 px-2.5 py-1.5 text-xs text-warning">
              {t('vaults.protectedUnlock.passkeyUnrecoverableWarning')}
            </div>
            <label className="flex items-start gap-2 text-xs text-muted-foreground">
              <input type="checkbox" checked={ackLoss} onChange={(e) => setAckLoss(e.target.checked)} className="mt-0.5" />
              <span>{t('vaults.protectedUnlock.passkeyAck')}</span>
            </label>
            <Button type="button" onClick={() => run(vault.setupPasskey)} disabled={busy || !ackLoss}>
              {busy ? <Loader2 className="size-4 animate-spin" /> : <Fingerprint className="size-4" />}
              {t('vaults.protectedUnlock.setupWithPasskey')}
            </Button>
          </div>
        )}

        {/* Password — recoverable alternative (weaker; can be phished/leaked). */}
        <form
          className="flex flex-col gap-2 rounded-lg border border-border bg-surface p-2.5"
          onSubmit={(e) => {
            e.preventDefault();
            submitPassword();
          }}
        >
          <span className="flex items-center gap-1.5 text-xs font-semibold">
            <KeyRound className="size-3.5" />
            {t('vaults.protectedUnlock.passwordOptionTitle')}
          </span>
          <span className="text-xs text-muted-foreground">{t('vaults.protectedUnlock.passwordOptionHelp')}</span>
          <label className="flex flex-col gap-1.5 text-xs text-muted-foreground">
            {t('vaults.protectedUnlock.setPasswordLabel')}
            <Input type="password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder={t('vaults.protectedUnlock.passwordPlaceholder')} autoComplete="new-password" />
          </label>
          <label className="flex flex-col gap-1.5 text-xs text-muted-foreground">
            {t('vaults.protectedUnlock.confirmLabel')}
            <Input type="password" value={confirm} onChange={(e) => setConfirm(e.target.value)} placeholder={t('vaults.protectedUnlock.confirmPlaceholder')} autoComplete="new-password" />
          </label>
          <Button type="submit" variant={canUsePasskey ? 'ghost' : 'secondary'} disabled={busy || !passwordValid}>
            {busy && <Loader2 className="size-4 animate-spin" />}
            {t('vaults.protectedUnlock.setupWithPassword')}
          </Button>
        </form>

        {vault.error && <div className="text-xs text-destructive">{friendlyError(t, vault.error)}</div>}
      </div>
    );
  }

  // status === 'locked'
  const showUnlockPasskey = vault.hasPasskey() && canUsePasskey;
  const showUnlockPassword = vault.hasPassword();
  return (
    <div className={PANEL}>
      <div className="flex items-start gap-2">
        <Lock className="mt-0.5 size-4 shrink-0 text-muted" />
        <div className="flex flex-col gap-0.5">
          <span className="text-sm font-semibold">{t('vaults.protectedUnlock.unlockTitle')}</span>
          <span className="text-xs text-muted-foreground">{t('vaults.protectedUnlock.unlockHelp')}</span>
        </div>
      </div>
      {showUnlockPasskey && (
        <Button type="button" variant="secondary" onClick={() => run(vault.unlockPasskey)} disabled={busy}>
          {busy ? <Loader2 className="size-4 animate-spin" /> : <Fingerprint className="size-4" />}
          {t('vaults.protectedUnlock.unlockPasskey')}
        </Button>
      )}
      {showUnlockPassword && (
        <form
          className="flex items-end gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (password.trim()) void run(() => vault.unlockPassword(password));
          }}
        >
          <label className="flex flex-1 flex-col gap-1.5 text-xs font-medium text-muted-foreground">
            <span className="flex items-center gap-1.5">
              <KeyRound className="size-3.5" />
              {t('vaults.protectedUnlock.passwordLabel')}
            </span>
            <Input type="password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder={t('vaults.protectedUnlock.passwordPlaceholder')} autoComplete="current-password" />
          </label>
          <Button type="submit" disabled={busy || !password.trim()}>
            {busy && <Loader2 className="size-4 animate-spin" />}
            {t('vaults.protectedUnlock.unlockCta')}
          </Button>
        </form>
      )}
      {!showUnlockPasskey && !showUnlockPassword && (
        <div className="rounded-md border border-warning/40 bg-warning/10 px-2.5 py-1.5 text-xs text-warning">
          {t('vaults.protectedUnlock.unlockUnavailableHere')}
        </div>
      )}
      {vault.error && <div className="text-xs text-destructive">{friendlyError(t, vault.error)}</div>}
    </div>
  );
};
