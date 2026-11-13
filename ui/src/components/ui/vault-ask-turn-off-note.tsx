import { Loader2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import type { AskTurnOffState } from '@/lib/useAskTurnOff';
import type { PolicyTurnOffFailure } from '@/lib/vaultSigningPolicy';
import { cn } from '@/lib/utils';

const FAILURE_KEYS = {
  popup_blocked: 'vaults.askOff.popupBlocked',
  setup_required: 'vaults.askOff.setupRequired',
  factor_unavailable: 'vaults.askOff.factorUnavailable',
  expired: 'vaults.askOff.expired',
  clock_skew: 'vaults.askOff.clockSkew',
  changed: 'vaults.askOff.changed',
  failed: 'vaults.askOff.failed',
} as const satisfies Record<Exclude<PolicyTurnOffFailure, 'cancelled' | 'unavailable'>, string>;

/**
 * The one line under an ask-before-signing switch while turning it off: waiting for the Passkey,
 * or why it is still on. Cancelling says nothing; the switch simply stays on.
 */
export function AskTurnOffNote({ state, className }: { state: AskTurnOffState; className?: string }) {
  const { t } = useTranslation();
  if (state.phase === 'working') {
    return (
      <span role="status" className={cn('flex items-center gap-1.5 text-[11.5px] text-muted', className)}>
        <Loader2 className="size-3 animate-spin" />
        {t('vaults.askOff.waiting')}
      </span>
    );
  }
  if (state.phase !== 'ready' || !state.failure || state.failure === 'cancelled' || state.failure === 'unavailable') return null;
  const warn = state.failure !== 'setup_required';
  return (
    <span role={warn ? 'alert' : 'note'} className={cn('text-[11.5px]', warn ? 'text-destructive-ink' : 'text-muted', className)}>
      {t(FAILURE_KEYS[state.failure])}
    </span>
  );
}
