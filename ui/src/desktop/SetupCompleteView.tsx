import { useEffect, useRef, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { ArrowRight, ShieldCheck } from 'lucide-react';

import { Button } from '../components/ui/button';

/** ui/design: desktop/bind/setup-complete-*. After Setup: start here, or sign in. Signing in binds
 *  nothing by itself; this computer is bound only from the Account page, by the person. */
export function SetupCompleteView({ onLocal, onSignIn, failure }: { onLocal: () => void; onSignIn: () => void; failure?: ReactNode }) {
  const { t } = useTranslation();
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => { heading.current?.focus(); }, []);
  return <div className="mx-auto max-w-xl pt-12">
    <ShieldCheck className="mb-6 size-10 text-mint-ink" />
    <div className="mb-7">
      <h1 ref={heading} tabIndex={-1} className="text-2xl font-semibold tracking-tight outline-none">{t('desktopClient.setupReady')}</h1>
      <p className="mt-3 max-w-xl text-sm leading-6 text-muted">{t('desktopClient.setupBody')}</p>
    </div>
    <div className="flex flex-wrap gap-3">
      <Button variant="brand" onClick={onLocal}>{t('desktopClient.startLocal')}</Button>
      <Button variant="outline" onClick={onSignIn}>{t('desktopClient.signInAndAdd')}<ArrowRight className="size-4" /></Button>
    </div>
    <p className="mt-5 text-sm text-muted">{t('desktopClient.signInLater')}</p>
    {failure}
  </div>;
}
