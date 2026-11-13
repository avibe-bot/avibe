import { useState } from 'react';
import { Loader2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import type { AskTurnOffState } from '@/lib/useAskTurnOff';
import type { PolicyTurnOffFailure } from '@/lib/vaultSigningPolicy';
import { cn } from '@/lib/utils';
import { Button } from './button';

const FAILURE_KEYS = {
  popup_blocked: 'vaults.askOff.popupBlocked',
  setup_required: 'vaults.askOff.setupRequired',
  factor_unavailable: 'vaults.askOff.factorUnavailable',
  legacy_registration: 'vaults.askOff.legacyRegistration',
  expired: 'vaults.askOff.expired',
  clock_skew: 'vaults.askOff.clockSkew',
  changed: 'vaults.askOff.changed',
  failed: 'vaults.askOff.failed',
} as const satisfies Record<Exclude<PolicyTurnOffFailure, 'cancelled' | 'unavailable'>, string>;

/**
 * The line under an ask-before-signing switch while turning it off: waiting for the Passkey, why
 * it is still on, or (never set up) a way to set one up here. Cancelling says nothing.
 */
export function AskTurnOffNote({ state, onSetup, className }: { state: AskTurnOffState; onSetup?: () => void; className?: string }) {
  const { t } = useTranslation();
  if (state.phase === 'working') {
    return (
      <span role="status" className={cn('flex items-center gap-1.5 text-[11.5px] text-muted', className)}>
        <Loader2 className="size-3 animate-spin" />
        {t(state.settingUp ? 'vaults.askOff.settingUp' : 'vaults.askOff.waiting')}
      </span>
    );
  }
  if (state.phase !== 'ready') return null;
  if (state.setup === 'done' || state.setup === 'conflict') {
    return (
      <span role="status" className={cn('text-[11.5px] text-muted', className)}>
        {t(state.setup === 'done' ? 'vaults.askOff.setupDone' : 'vaults.askOff.setupConflict')}
      </span>
    );
  }
  if (!state.failure || state.failure === 'cancelled' || state.failure === 'unavailable') return null;
  if (state.failure === 'setup_required') {
    return (
      <span className={cn('flex flex-col items-start gap-1.5', className)}>
        <span role="note" className="text-[11.5px] text-muted">{t('vaults.askOff.setupRequired')}</span>
        {state.setup === 'failed' ? <span role="alert" className="text-[11.5px] text-destructive-ink">{t('vaults.askOff.setupFailed')}</span> : null}
        {onSetup ? <SetupWithAck onSetup={onSetup} /> : null}
      </span>
    );
  }
  // Red only when something actually went wrong; a timeout, a clock to fix, a change elsewhere or
  // a Passkey that can't do this yet are said in the neutral tone.
  const warn = state.failure === 'failed';
  return (
    <span role={warn ? 'alert' : 'note'} className={cn('text-[11.5px]', warn ? 'text-destructive-ink' : 'text-muted', className)}>
      {t(FAILURE_KEYS[state.failure])}
    </span>
  );
}

/**
 * The same no-recovery warning and acknowledgment as the Vault's own Passkey setup: this creates the
 * root every protected key will use, so it is not set up before the user has read that.
 */
function SetupWithAck({ onSetup }: { onSetup: () => void }) {
  const { t } = useTranslation();
  const [ack, setAck] = useState(false);
  return (
    <span className="flex flex-col items-start gap-1.5 rounded-lg border border-gold/40 bg-gold/10 p-2">
      <span className="text-[11.5px] leading-snug text-gold-ink">{t('vaults.protectedUnlock.passkeyUnrecoverableWarning')}</span>
      <label className="flex items-start gap-2 text-[11.5px] leading-snug text-muted-foreground">
        <input type="checkbox" checked={ack} onChange={(event) => setAck(event.target.checked)} className="mt-0.5 shrink-0" />
        <span>{t('vaults.protectedUnlock.passkeyAck')}</span>
      </label>
      <Button type="button" variant="secondary" size="xs" disabled={!ack} onClick={onSetup}>{t('vaults.askOff.setupAction')}</Button>
    </span>
  );
}
