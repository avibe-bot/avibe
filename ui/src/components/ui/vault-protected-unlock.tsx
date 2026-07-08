import { useState } from 'react';
import { Loader2, Lock, RefreshCw, ScanFace, ShieldCheck, Sparkles } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { TFunction } from 'i18next';

import { webauthnAvailable } from '@/lib/useProtectedVault';
import type { useProtectedVault } from '@/lib/useProtectedVault';
import { Badge } from './badge';
import { Button } from './button';

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
 * (unlock). The panel is form-free: it often lives inside the create dialog's own `<form>`,
 * so a nested `<form>` here would be invalid HTML and (in practice) trigger a full-page reload.
 * Every action is a button `onClick`. The unlocked VMK is cached for the session by
 * {@link useProtectedVault}.
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
  const [busy, setBusy] = useState(false);
  // Passkey-only setup has no recovery fallback yet, so a lost passkey is
  // unrecoverable — gate the recommended action behind an explicit ack.
  const [ackLoss, setAckLoss] = useState(false);

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    vault.setError(null);
    try {
      await fn();
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

  // ---- Setup (needs-setup): design.pen `kAmWj` ---------------------------------------
  if (vault.status === 'needs-setup') {
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

        {/* Ack sits ABOVE the button: the natural order is read-the-risk → check → the
            (now-enabled) Add-Passkey button right below it. The button gates on `ackLoss`. */}
        {canUsePasskey && (
          <div className="flex flex-col gap-2 rounded-xl border border-warning/40 bg-warning/10 p-3">
            <span className="text-[11.5px] leading-snug text-warning">{t('vaults.protectedUnlock.passkeyUnrecoverableWarning')}</span>
            <label className="flex items-start gap-2 text-[11.5px] leading-snug text-muted-foreground">
              <input type="checkbox" checked={ackLoss} onChange={(e) => setAckLoss(e.target.checked)} className="mt-0.5 shrink-0" />
              <span>{t('vaults.protectedUnlock.passkeyAck')}</span>
            </label>
          </div>
        )}

        {canUsePasskey ? (
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
        ) : (
          <div className="rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-xs leading-snug text-warning">
            {t('vaults.protectedUnlock.unlockUnavailableHere')}
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

      {!showUnlockPasskey && (
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
