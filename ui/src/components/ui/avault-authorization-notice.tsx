import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { CheckCircle2, KeyRound, Loader2 } from 'lucide-react';

import { useInstanceAuthorization } from '@/context/InstanceAuthorizationContext';
import { useAvaultAuthorization } from '@/lib/useAvaultAuthorization';
import { cn } from '@/lib/utils';
import { Button } from './button';

/**
 * Explains avault's one-time macOS Keychain authorization and offers the action. Renders
 * when the backend has recorded that avault needs it, or when `forceVisible` because a
 * Vault call just failed with that code.
 */
export function AvaultAuthorizationNotice({ forceVisible = false, className }: { forceVisible?: boolean; className?: string }) {
  const { t } = useTranslation();
  const { capabilities } = useInstanceAuthorization();
  const { status, authorize } = useAvaultAuthorization();
  // Set once this notice asks; only then does it keep showing that attempt's outcome.
  const [requested, setRequested] = useState(false);
  const attempt = status?.attempt ?? null;
  const running = attempt?.state === 'running';
  const outcome = requested && !running ? attempt : null;
  if (!forceVisible && !status?.required && !running && !outcome) return null;

  const onAuthorize = () => {
    setRequested(true);
    void authorize();
  };

  if (outcome?.state === 'succeeded') {
    return (
      <div
        role="status"
        className={cn('flex items-start gap-2.5 rounded-xl border border-mint/30 bg-mint/[0.08] px-4 py-3 text-sm', className)}
      >
        <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-mint-ink" />
        <span className="text-foreground">{t('vaults.avaultAuthorization.succeeded')}</span>
      </div>
    );
  }

  const failureReason = outcome?.state === 'failed' ? outcome.reason || 'failed' : null;
  return (
    <div
      role="alert"
      className={cn('flex flex-col gap-2.5 rounded-xl border border-gold/40 bg-gold/10 px-4 py-3 text-sm', className)}
    >
      <div className="flex items-start gap-2.5">
        <KeyRound className="mt-0.5 size-4 shrink-0 text-gold-ink" />
        <div className="flex min-w-0 flex-col gap-1">
          <span className="font-semibold text-foreground">{t('vaults.avaultAuthorization.title')}</span>
          <span className="leading-snug text-foreground">{t('vaults.avaultAuthorization.body')}</span>
          <span className="leading-snug text-muted">{t('vaults.avaultAuthorization.steps')}</span>
        </div>
      </div>
      {failureReason ? (
        <div className="flex flex-col gap-1 rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2 text-[12.5px] leading-snug text-destructive-ink">
          <span>
            {t(`vaults.avaultAuthorization.failed.${failureReason}`, {
              defaultValue: t('vaults.avaultAuthorization.failed.failed'),
            })}
          </span>
          {outcome?.detail ? <span className="break-words font-mono text-[11px] text-muted">{outcome.detail}</span> : null}
        </div>
      ) : null}
      <div className="flex flex-wrap items-center gap-2.5 pl-6">
        {capabilities.can_manage_instance ? (
          <Button size="sm" variant="brand" disabled={running} onClick={onAuthorize}>
            {running ? <Loader2 className="size-3.5 animate-spin" /> : <KeyRound className="size-3.5" />}
            {t('vaults.avaultAuthorization.action')}
          </Button>
        ) : (
          <span className="text-xs text-muted">{t('vaults.avaultAuthorization.askAdmin')}</span>
        )}
        {running ? <span className="text-xs text-muted">{t('vaults.avaultAuthorization.waiting')}</span> : null}
      </div>
    </div>
  );
}
