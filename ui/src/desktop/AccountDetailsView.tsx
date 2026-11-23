import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { ArrowLeft, LogOut, UserRound } from 'lucide-react';
import { Button } from '../components/ui/button';

export interface AccountDetailsViewProps {
  email: string;
  localRemoteUrl: string | null;
  busy: boolean;
  errorMessage?: string | null;
  onBack: () => void;
  onSignOut: () => void;
  /** This computer's binding (ComputerBindingCard), below the account. */
  children?: ReactNode;
}

/** ui/design: desktop/instances/profile-*, profile-disabled-*.
 * Pure presentation; the account container owns the explicit cleanup action. */
export function AccountDetailsView({ email, localRemoteUrl, busy, errorMessage, onBack, onSignOut, children }: AccountDetailsViewProps) {
  const { t } = useTranslation();
  return <div data-account-details className="mx-auto w-full max-w-2xl pt-6">
    <Button variant="ghost" size="sm" className="mb-5 -ml-3" onClick={onBack}><ArrowLeft className="size-4" />{t('desktopClient.instancesTitle')}</Button>
    <h1 className="mb-6 text-2xl font-semibold tracking-tight">{t('desktopClient.accountTitle')}</h1>
    <div className="rounded-xl border border-edge bg-surface p-5">
      <p className="mb-3 text-xs font-medium text-muted">{t('desktopClient.currentAccount')}</p>
      <div className="flex items-start gap-3"><UserRound className="mt-0.5 size-5 shrink-0 text-muted" /><p className="min-w-0 break-words text-sm font-semibold [overflow-wrap:anywhere]">{email}</p></div>
      {localRemoteUrl && <div className="mt-5 border-t border-edge pt-4"><p className="mb-2 text-xs text-muted">{t('desktopClient.localRemoteAccess')}</p><a className="break-words text-sm underline underline-offset-4 [overflow-wrap:anywhere]" href={localRemoteUrl} target="_blank" rel="noreferrer">{localRemoteUrl}</a></div>}
    </div>
    {children}
    {errorMessage && <p role="alert" className="mt-4 text-sm leading-6 text-destructive-ink">{errorMessage}</p>}
    <Button variant="outline" className="mt-6 gap-3 [--press-scale:1]" disabled={busy} onClick={onSignOut}><LogOut className="size-4" />{t('desktopClient.signOut')}</Button>
  </div>;
}
