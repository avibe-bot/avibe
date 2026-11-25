import { ChevronDown, LogOut, UserRound } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';

/** Proposal: with this computer's binding in Instances, the Account page keeps only who is signed
 *  in and signing out; both fit a small menu on the Account row at the foot of Instances. */
export function DesktopAccountMenu({ email }: { email: string }) {
  const { t } = useTranslation();
  return <section className="mx-auto mt-6 w-full max-w-2xl" data-account-menu-proposal>
    <h2 className="mb-2 text-sm font-medium text-muted">{t('desktopAccount.accountTitle')}</h2>
    <div className="relative">
      <Button variant="ghost" aria-expanded className="h-auto w-full justify-start gap-3 rounded-xl border border-edge bg-surface p-4 [--press-scale:1]">
        <UserRound className="size-5 shrink-0 text-muted" /><span className="min-w-0 flex-1 truncate text-left">{email}</span><ChevronDown className="size-4 shrink-0 text-muted" />
      </Button>
      <div role="menu" className="absolute right-0 top-full z-10 mt-2 w-64 rounded-xl border border-edge bg-popover p-1 text-popover-foreground shadow-elevation-2">
        <p className="px-3 py-2 text-xs text-muted">{t('desktopAccount.currentAccount')}</p>
        <p className="truncate px-3 pb-2 text-sm font-medium">{email}</p>
        <div className="border-t border-edge pt-1">
          <Button role="menuitem" variant="ghost" className="w-full justify-start gap-2 text-sm"><LogOut className="size-4" />{t('desktopClient.signOut')}</Button>
        </div>
      </div>
    </div>
  </section>;
}
