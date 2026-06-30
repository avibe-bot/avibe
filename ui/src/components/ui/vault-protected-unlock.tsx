import { useState } from 'react';
import { KeyRound, Loader2, Lock, RefreshCw, ScanFace, ShieldCheck, Sparkles } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { TFunction } from 'i18next';

import { webauthnAvailable } from '@/lib/useProtectedVault';
import type { useProtectedVault } from '@/lib/useProtectedVault';
import { Badge } from './badge';
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

const PANEL = 'flex flex-col gap-4 rounded-2xl border border-border bg-surface px-6 pb-5 pt-6';

/**
 * Protected-tier setup / unlock panel — design.pen frames `kAmWj` (setup) and `g5Q7F`
 * (unlock). Setup leads with a single recommended passkey path and progressively reveals
 * the recoverable password fields behind a "Use a password instead" link; unlock leads
 * with passkey and reveals password the same way. Both are form-free: the panel often
 * lives inside the create dialog's own `<form>`, so a nested `<form>` here would be invalid
 * HTML and (in practice) trigger a full-page reload. Every action is a button `onClick`.
 *
 * Password is always the recovery root in the crypto layer; a passkey is the quick primary
 * unlock added on top. The unlocked VMK is cached for the session by {@link useProtectedVault}.
 *
 * `secretName` is shown in the unlock subtitle ("<NAME> is protected …"); it is optional
 * because the create-dialog gating step has no single secret name yet.
 */
export const VaultProtectedUnlock: React.FC<{ vault: Vault; secretName?: string; onDismiss?: () => void }> = ({
  vault,
  secretName,
  onDismiss,
}) => {
  const { t } = useTranslation();
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [busy, setBusy] = useState(false);
  // Progressive disclosure: password fields stay hidden until the user opts out of the
  // recommended passkey path. Resets whenever the panel re-mounts for a new state.
  const [showPassword, setShowPassword] = useState(false);
  // Passkey-only setup has no password/second-passkey/recovery fallback yet, so a lost
  // passkey is unrecoverable — gate the recommended action behind an explicit ack.
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
      <div className="flex items-center gap-2 rounded-2xl border border-border bg-surface px-4 py-3 text-sm text-muted">
        <Loader2 className="size-4 animate-spin" />
        {t('vaults.protectedUnlock.checking')}
      </div>
    );
  }

  if (vault.status === 'error') {
    return (
      <div className="flex flex-col gap-2 rounded-2xl border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm">
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
      <div className="flex items-center gap-2 rounded-2xl border border-mint/40 bg-mint-soft px-4 py-3 text-sm text-mint">
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
  const passwordValid = password.trim().length > 0 && password === confirm;

  // ---- Setup (needs-setup): design.pen `kAmWj` ---------------------------------------
  if (vault.status === 'needs-setup') {
    const submitPassword = () => {
      if (password.trim().length === 0) return;
      if (password !== confirm) {
        vault.setError(t('vaults.protectedUnlock.errors.mismatch'));
        return;
      }
      void run(() => vault.setupPassword(password));
    };
    // Password is the only factor when WebAuthn isn't available here — surface the fields
    // immediately rather than hiding them behind a passkey card that can't be used.
    const passwordRevealed = showPassword || !canUsePasskey;
    return (
      <div className={PANEL}>
        <div className="flex flex-col items-center gap-4">
          <span className="flex size-13 items-center justify-center rounded-2xl bg-mint-soft">
            <ShieldCheck className="size-7 text-mint" />
          </span>
          <div className="flex flex-col items-center gap-1.5">
            <span className="text-center text-[17px] font-bold text-foreground">{t('vaults.protectedUnlock.setupTitle')}</span>
            <span className="max-w-sm text-center text-[13px] leading-snug text-muted-foreground">
              {t('vaults.protectedUnlock.setupSubtitle')}
            </span>
          </div>
        </div>

        {/* Recommended: passkey — the single primary path (design leads with this). */}
        {canUsePasskey && (
          <div className="flex flex-col items-center gap-2.5 rounded-xl border-[1.5px] border-mint bg-mint-soft p-4">
            <Badge variant="success" className="border-transparent bg-mint uppercase tracking-wide text-background">
              <Sparkles className="size-3" />
              {t('vaults.protectedUnlock.recommended')}
            </Badge>
            <Button type="button" variant="brand" className="w-full" onClick={() => run(vault.setupPasskey)} disabled={busy || !ackLoss}>
              {busy ? <Loader2 className="size-5 animate-spin" /> : <ScanFace className="size-5" />}
              {t('vaults.protectedUnlock.addPasskey')}
            </Button>
            <span className="text-center text-[11.5px] text-muted-foreground">{t('vaults.protectedUnlock.passkeyCaption')}</span>
          </div>
        )}

        {/* Unrecoverable acknowledgement — a passkey-only vault has no password/second-passkey/
            recovery fallback yet, so a lost passkey means the protected secrets are gone. Gate
            the recommended action behind an explicit ack until a recovery flow exists. */}
        {canUsePasskey && (
          <div className="flex flex-col gap-2 rounded-xl border border-warning/40 bg-warning/10 p-3">
            <span className="text-[11.5px] leading-snug text-warning">{t('vaults.protectedUnlock.passkeyUnrecoverableWarning')}</span>
            <label className="flex items-start gap-2 text-[11.5px] leading-snug text-muted-foreground">
              <input type="checkbox" checked={ackLoss} onChange={(e) => setAckLoss(e.target.checked)} className="mt-0.5 shrink-0" />
              <span>{t('vaults.protectedUnlock.passkeyAck')}</span>
            </label>
          </div>
        )}

        {/* Progressive disclosure — password fields stay hidden until requested. */}
        {!passwordRevealed ? (
          <button
            type="button"
            onClick={() => setShowPassword(true)}
            className="flex items-center justify-center gap-2 text-[13px] font-medium text-muted-foreground transition-colors hover:text-foreground"
          >
            {t('vaults.protectedUnlock.usePasswordInstead')}
            <Badge variant="destructive">{t('vaults.protectedUnlock.lessSecure')}</Badge>
          </button>
        ) : (
          <div className="flex flex-col gap-2.5 rounded-xl border border-border bg-surface-2 p-3">
            <span className="flex items-center gap-1.5 text-xs font-semibold">
              <KeyRound className="size-3.5" />
              {t('vaults.protectedUnlock.passwordOptionTitle')}
            </span>
            <label className="flex flex-col gap-1.5 text-xs text-muted-foreground">
              {t('vaults.protectedUnlock.setPasswordLabel')}
              <Input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder={t('vaults.protectedUnlock.passwordPlaceholder')}
                autoComplete="new-password"
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    e.preventDefault();
                    submitPassword();
                  }
                }}
              />
            </label>
            <label className="flex flex-col gap-1.5 text-xs text-muted-foreground">
              {t('vaults.protectedUnlock.confirmLabel')}
              <Input
                type="password"
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
                placeholder={t('vaults.protectedUnlock.confirmPlaceholder')}
                autoComplete="new-password"
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    e.preventDefault();
                    submitPassword();
                  }
                }}
              />
            </label>
            <Button type="button" variant="secondary" onClick={submitPassword} disabled={busy || !passwordValid}>
              {busy && <Loader2 className="size-4 animate-spin" />}
              {t('vaults.protectedUnlock.createPassword')}
            </Button>
          </div>
        )}

        {onDismiss && (
          <button
            type="button"
            onClick={onDismiss}
            className="text-center text-[12.5px] font-medium text-muted-foreground transition-colors hover:text-foreground"
          >
            {t('vaults.protectedUnlock.maybeLater')}
          </button>
        )}

        {vault.error && <div className="text-center text-xs text-destructive">{friendlyError(t, vault.error)}</div>}
      </div>
    );
  }

  // ---- Unlock (locked): design.pen `g5Q7F` -------------------------------------------
  const showUnlockPasskey = vault.hasPasskey() && canUsePasskey && vault.passkeyUsableHere();
  const showUnlockPassword = vault.hasPassword();
  // If the passkey can't be used here, the password is the only way in — reveal it up front.
  const passwordRevealed = showPassword || !showUnlockPasskey;
  const submitUnlock = () => {
    if (password.trim()) void run(() => vault.unlockPassword(password));
  };
  return (
    <div className={PANEL}>
      <div className="flex flex-col items-center gap-4">
        <span className="flex size-13 items-center justify-center rounded-2xl bg-gold/15">
          <ScanFace className="size-7 text-gold" />
        </span>
        <div className="flex flex-col items-center gap-1.5">
          <span className="text-center text-[17px] font-bold text-foreground">{t('vaults.protectedUnlock.unlockTitle')}</span>
          {secretName && (
            <span className="max-w-sm text-center text-[13px] leading-snug text-muted-foreground">
              {t('vaults.protectedUnlock.unlockSubtitle', { name: secretName })}
            </span>
          )}
        </div>
      </div>

      {showUnlockPasskey && (
        <div className="flex flex-col items-center gap-1.5">
          <Button type="button" variant="brand" className="w-full" onClick={() => run(vault.unlockPasskey)} disabled={busy}>
            {busy ? <Loader2 className="size-5 animate-spin" /> : <ScanFace className="size-5" />}
            {t('vaults.protectedUnlock.unlockWithPasskey')}
          </Button>
          <span className="text-center text-[11px] text-muted-foreground">{t('vaults.protectedUnlock.unlockPasskeyCaption')}</span>
        </div>
      )}

      {showUnlockPassword &&
        (!passwordRevealed ? (
          <button
            type="button"
            onClick={() => setShowPassword(true)}
            className="flex items-center justify-center gap-2 text-[13px] font-medium text-muted-foreground transition-colors hover:text-foreground"
          >
            {t('vaults.protectedUnlock.usePasswordInline')}
            <Badge variant="destructive">{t('vaults.protectedUnlock.lessSecure')}</Badge>
          </button>
        ) : (
          <div className="flex items-end gap-2">
            <label className="flex flex-1 flex-col gap-1.5 text-xs font-medium text-muted-foreground">
              <span className="flex items-center gap-1.5">
                <KeyRound className="size-3.5" />
                {t('vaults.protectedUnlock.passwordLabel')}
              </span>
              <Input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder={t('vaults.protectedUnlock.passwordPlaceholder')}
                autoComplete="current-password"
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    e.preventDefault();
                    submitUnlock();
                  }
                }}
              />
            </label>
            <Button type="button" onClick={submitUnlock} disabled={busy || !password.trim()}>
              {busy && <Loader2 className="size-4 animate-spin" />}
              {t('vaults.protectedUnlock.unlockCta')}
            </Button>
          </div>
        ))}

      {!showUnlockPasskey && !showUnlockPassword && (
        <div className="rounded-md border border-warning/40 bg-warning/10 px-2.5 py-1.5 text-xs text-warning">
          {t('vaults.protectedUnlock.unlockUnavailableHere')}
        </div>
      )}

      {/* Mint factor-safety note. */}
      <div className="flex items-start gap-2 rounded-lg bg-mint-soft px-3 py-2.5">
        <ShieldCheck className="mt-0.5 size-[15px] shrink-0 text-mint" />
        <span className="text-[11px] leading-snug text-foreground">{t('vaults.protectedUnlock.factorNote')}</span>
      </div>

      {onDismiss && (
        <button
          type="button"
          onClick={onDismiss}
          className="text-center text-[12.5px] font-medium text-muted-foreground transition-colors hover:text-foreground"
        >
          {t('vaults.protectedUnlock.cancel')}
        </button>
      )}

      {vault.error && <div className="text-center text-xs text-destructive">{friendlyError(t, vault.error)}</div>}
    </div>
  );
};
