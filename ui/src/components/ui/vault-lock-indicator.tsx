import { Lock, Unlock } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { useVaultLock } from '@/lib/useProtectedVault';
import { cn } from '@/lib/utils';
import { Button } from './button';

/** mm:ss for a millisecond duration (rounded up so it never shows 0:00 while still ticking). */
function formatRemaining(ms: number): string {
  const total = Math.max(0, Math.ceil(ms / 1000));
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return `${minutes}:${String(seconds).padStart(2, '0')}`;
}

/**
 * Shown while the protected vault is unlocked: a pill with the time left before the plaintext
 * key auto-locks, plus a one-tap "Lock now". Renders nothing while the vault is locked.
 */
export function VaultLockIndicator({ className }: { className?: string }) {
  const { t } = useTranslation();
  const { unlocked, remainingMs, lockNow } = useVaultLock();
  if (!unlocked) return null;
  return (
    <div
      className={cn('flex items-center gap-2 rounded-full border border-mint/40 bg-mint-soft py-1 pl-3 pr-1', className)}
      title={t('vaults.lock.autoLockHint')}
    >
      <Unlock className="size-3.5 shrink-0 text-mint" />
      <span className="text-xs font-medium text-foreground">
        {t('vaults.lock.unlocked')} · <span className="tabular-nums">{formatRemaining(remainingMs)}</span>
      </span>
      <Button variant="ghost" size="sm" className="h-6 gap-1 rounded-full px-2 text-xs" onClick={lockNow}>
        <Lock className="size-3" />
        {t('vaults.lock.lockNow')}
      </Button>
    </div>
  );
}
