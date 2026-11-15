// The Add key dialog's parts for a protected seed-phrase wallet (ui/design: vault/mnemonic/protected/*):
// the tier line that replaces the ask switch, the launch button, what the page says while and after
// the secure window works, and the done screen. Words and passphrase never appear on this page.
import { AlertTriangle, ExternalLink, Loader2, Lock, RefreshCw, ShieldCheck, TriangleAlert } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import type { ProtectedHdPhase } from '@/lib/useProtectedHdCreate';
import type { ProtectedHdAccount0 } from '@/lib/vaultProtectedHd';

import { Button } from './button';
import { HdDoneStep, HdNotice } from './vault-hd-wallet-steps';

/** Instead of "Ask before account #0 signs": on this tier every signature is approved. */
export function ProtectedHdTierNote() {
  const { t } = useTranslation();
  return (
    <div className="flex items-center gap-2 rounded-[10px] border border-edge bg-surface-2 px-3 py-2.5 text-[12.5px] text-muted shadow-elevation-0">
      <Lock className="size-3.5 shrink-0" />
      {t('vaults.hd.protected.tierNote')}
    </div>
  );
}

const KNOWN_REFUSALS = {
  secret_exists: 'vaults.hd.protected.refusals.secret_exists',
  secret_name_case_conflict: 'vaults.hd.protected.refusals.secret_name_case_conflict',
  vault_already_initialized: 'vaults.hd.protected.refusals.vault_already_initialized',
} as const;
const knownRefusal = (code: string | null) =>
  code && Object.hasOwn(KNOWN_REFUSALS, code) ? KNOWN_REFUSALS[code as keyof typeof KNOWN_REFUSALS] : null;

export function ProtectedHdLaunchNotice({ phase, name }: { phase: ProtectedHdPhase; name: string }) {
  const { t } = useTranslation();
  switch (phase.kind) {
    case 'waiting':
      return <HdNotice tone="neutral" icon={ShieldCheck}>{t('vaults.hd.protected.notice.waiting')}</HdNotice>;
    case 'failed':
      return (
        <HdNotice tone={phase.reason === 'blocked' ? 'warning' : 'neutral'} icon={phase.reason === 'blocked' ? TriangleAlert : AlertTriangle}>
          {t(`vaults.hd.protected.notice.${phase.reason}`)}
        </HdNotice>
      );
    case 'saving':
      return <HdNotice tone="neutral" icon={ShieldCheck}>{t('vaults.hd.protected.notice.saving')}</HdNotice>;
    case 'checking':
    case 'unconfirmed':
      return (
        <HdNotice tone={phase.kind === 'checking' ? 'neutral' : 'warning'} icon={phase.kind === 'checking' ? ShieldCheck : TriangleAlert}>
          <span>{t(phase.kind === 'checking' ? 'vaults.hd.protected.notice.checking' : 'vaults.hd.protected.notice.unconfirmed', { name })}</span>
          {phase.refusal ? (
            <span className="text-muted">
              {t('vaults.hd.protected.notice.refusal', {
                // A known refusal reads in the UI's language; anything else is Avibe's own words.
                message: knownRefusal(phase.refusal.code) ? t(knownRefusal(phase.refusal.code)!) : phase.refusal.text,
              })}
            </span>
          ) : null}
        </HdNotice>
      );
    case 'mismatch':
      return <HdNotice tone="error" icon={TriangleAlert}>{t('vaults.hd.protected.notice.mismatch', { name })}</HdNotice>;
    default:
      return null;
  }
}

/** The dialog's primary button for this tier. Submitting the form launches the window. */
export function ProtectedHdLaunchButton({ phase, mode, disabled, onCheckAgain }: {
  phase: ProtectedHdPhase;
  mode: 'create' | 'import';
  disabled: boolean;
  onCheckAgain: () => void;
}) {
  const { t } = useTranslation();
  const spinner = <Loader2 className="size-4 animate-spin" />;
  if (phase.kind === 'waiting') return <Button type="button" disabled>{spinner}{t('vaults.hd.protected.waitingCta')}</Button>;
  if (phase.kind === 'saving') return <Button type="button" disabled>{spinner}{t('vaults.hd.protected.savingCta')}</Button>;
  if (phase.kind === 'checking') return <Button type="button" disabled>{spinner}{t('vaults.hd.protected.checkingCta')}</Button>;
  if (phase.kind === 'unconfirmed') {
    return <Button type="button" onClick={onCheckAgain}><RefreshCw className="size-4" />{t('vaults.hd.protected.checkAgain')}</Button>;
  }
  if (phase.kind === 'mismatch') return null;
  return (
    <Button type="submit" disabled={disabled}>
      <ExternalLink className="size-4" />
      {t(mode === 'create' ? 'vaults.hd.protected.launchCreate' : 'vaults.hd.protected.launchImport')}
    </Button>
  );
}

/** Back on the page once the saved wallet matched: the standard done screen, protected variant. */
export function ProtectedHdDone({ name, account0, onFinish }: { name: string; account0: ProtectedHdAccount0; onFinish: () => void }) {
  const { t } = useTranslation();
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <HdDoneStep name={name} account={account0} protectedTier />
      <div className="mt-1 flex justify-end">
        <Button type="button" onClick={onFinish}>{t('vaults.hd.done.finish')}</Button>
      </div>
    </div>
  );
}
