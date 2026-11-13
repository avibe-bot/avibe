import { useTranslation } from 'react-i18next';

import { cn } from '@/lib/utils';

// Its own module so the Chat approval card can show it without pulling in the wallet dialog.
/** `#1`: an account's position, the number an Agent passes as --account. */
export function AccountIndex({ index, className }: { index: number; className?: string }) {
  const { t } = useTranslation();
  return (
    <span className={cn('inline-flex h-5 min-w-7 shrink-0 items-center justify-center rounded-md bg-violet-soft px-1.5 font-mono text-[11.5px] font-semibold text-violet-ink', className)}>
      {t('vaults.hd.accountIndex', { account: index })}
    </span>
  );
}
