import { useEffect, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { Button } from '../components/ui/button';

export interface AccountSignOutViewProps {
  signingOut: boolean;
  errorMessage: string | null;
  onRetry: () => void;
  onBack: () => void;
}

/** ui/design: desktop/instances/sign-out-error-*. Native owns cleanup and this
 * page's admission; rendering it never authorizes a sign-out action. */
export function AccountSignOutView({ signingOut, errorMessage, onRetry, onBack }: AccountSignOutViewProps) {
  const { t } = useTranslation();
  const title = t(signingOut && !errorMessage ? 'desktopAccount.signingOut' : 'desktopAccount.signOut');
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => { heading.current?.focus(); }, [title]);
  return <div data-account-sign-out className="mx-auto max-w-xl pt-12">
    <h1 ref={heading} tabIndex={-1} className="text-2xl font-semibold tracking-tight outline-none">{title}</h1>
    <p className="mt-3 text-sm leading-6 text-muted">{t('desktopAccount.signOutReturn')}</p>
    {errorMessage && <p role="alert" className="mt-4 text-sm leading-6 text-destructive-ink">{errorMessage}</p>}
    <div className="mt-7 flex gap-3">{errorMessage && <Button variant="brand" onClick={onRetry}>{t('common.retry')}</Button>}<Button variant="outline" onClick={onBack}>{t('desktopAccount.backWorkbench')}</Button></div>
  </div>;
}
