import { useEffect, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { ArrowRight, RefreshCw } from 'lucide-react';
import { Button } from '../components/ui/button';
import { Input } from '../components/ui/input';
import { Label } from '../components/ui/label';
import { cn } from '../lib/utils';
import logo from '../assets/logo.png';
import mailMascot from '../assets/pet/vibey-message.png';

export interface AccountLoginViewProps {
  verify: boolean;
  email: string;
  value: string;
  busy: boolean;
  errorMessage: string | null;
  onValueChange: (value: string) => void;
  onSubmit: () => void;
  onChangeEmail: () => void;
  onResendCode: () => void;
  onContinueLocal: () => void;
}

/** Shared by the bundled account container and ui/design: desktop/instances/email-*,
 * code-*, code-error-*. Native state and request ownership stay in AccountApp. */
export function AccountLoginView({
  verify, email, value, busy, errorMessage,
  onValueChange, onSubmit, onChangeEmail, onResendCode, onContinueLocal,
}: AccountLoginViewProps) {
  const { t } = useTranslation();
  const title = t(verify ? 'desktopClient.checkInbox' : 'desktopClient.loginTitle');
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => { heading.current?.focus(); }, [title]);
  return <div className="mx-auto my-auto grid w-full max-w-5xl shrink-0 grid-cols-2 overflow-hidden rounded-3xl border border-edge bg-surface">
    <div className="flex flex-col justify-center p-8" style={{ background: 'var(--gradient-wizard-platforms)' }}>
      <img src={logo} className="mb-6 size-8" alt="" />
      <img src={mailMascot} className="mb-6 size-28 object-contain" alt="" />
      <h1 className="whitespace-pre-line text-3xl font-semibold leading-tight tracking-tight">{t('desktopClient.loginHero')}</h1>
      <p className="mt-5 max-w-sm text-sm leading-7 text-muted">{t('desktopClient.loginHeroBody')}</p>
    </div>
    <div className="flex flex-col justify-center p-8">
      <div className="mb-7">
        <h1 ref={heading} tabIndex={-1} className="text-2xl font-semibold tracking-tight outline-none">{title}</h1>
        <p className="mt-3 max-w-xl text-sm leading-6 text-muted">{verify ? t('desktopClient.codeSent', { email }) : t('desktopClient.loginBody')}</p>
      </div>
      <form onSubmit={(event) => { event.preventDefault(); onSubmit(); }}>
        <Label htmlFor="desktop-auth-field">{t(verify ? 'desktopClient.emailCode' : 'desktopClient.email')}</Label>
        <Input id="desktop-auth-field" className={cn('mt-2 h-12', verify && 'text-xl tracking-widest')}
          type={verify ? 'text' : 'email'} inputMode={verify ? 'numeric' : 'email'}
          autoComplete={verify ? 'one-time-code' : 'email'} value={value}
          onChange={(event) => onValueChange(event.target.value)} required aria-invalid={!!errorMessage} />
        <p className="mt-3 text-xs leading-6 text-muted">{t(verify ? 'desktopClient.codeHelp' : 'desktopClient.registrationDisclosure')}</p>
        <Button variant="brand" size="lg" className="mt-6 w-full rounded-full" disabled={busy || !value.trim()}>
          {busy && <RefreshCw className="size-4 animate-spin" />}
          {t(verify ? 'desktopClient.verifySignIn' : 'desktopClient.sendCode')}<ArrowRight className="size-4" />
        </Button>
      </form>
      {verify && <div className="mt-4 flex flex-wrap justify-between gap-2">
        <Button variant="link" size="sm" disabled={busy} onClick={onChangeEmail}>{t('desktopClient.changeEmail')}</Button>
        <Button variant="link" size="sm" disabled={busy} onClick={onResendCode}>{t('desktopClient.resendCode')}</Button>
      </div>}
      <div className="mt-6">
        {errorMessage && <div role="alert" className="rounded-xl border border-gold/40 bg-gold/10 p-4 text-sm leading-6 text-foreground">{errorMessage}</div>}
        <Button variant="ghost" onClick={onContinueLocal}>{t('desktopClient.continueLocal')}</Button>
      </div>
    </div>
  </div>;
}
