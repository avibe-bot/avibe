import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ArrowRight, Eye, EyeOff, KeyRound, Mail, RefreshCw } from 'lucide-react';
import { Button } from '../components/ui/button';
import { Input } from '../components/ui/input';
import { Label } from '../components/ui/label';
import mailMascot from '../assets/pet/vibey-message.png';

export interface AccountLoginViewProps {
  verify: boolean;
  email: string;
  value: string;
  busy: boolean;
  errorMessage: string | null;
  password?: { active: boolean; value: string; forgot: boolean; onChange: (value: string) => void; onToggle: () => void; onForgot: () => void };
  onValueChange: (value: string) => void;
  onSubmit: () => void;
  onChangeEmail: () => void;
  onResendCode: () => void;
  onContinueLocal: () => void;
}

/** ui/design: desktop/instances/email-*, code-*, code-error-*, password-*.
 * Credentials and native lifecycle belong to AccountApp; this view only renders the form. */
export function AccountLoginView({ verify, email, value, busy, errorMessage, password,
  onValueChange, onSubmit, onChangeEmail, onResendCode, onContinueLocal,
}: AccountLoginViewProps) {
  const { t } = useTranslation();
  const [reveal, setReveal] = useState(false);
  const title = t(verify ? 'desktopClient.checkInbox' : 'desktopClient.loginTitle');
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => { heading.current?.focus(); }, [title]);
  const passwordMode = !verify && password?.active;
  return <div data-account-login className="mx-auto my-auto w-full max-w-md shrink-0 py-4">
    <div className="mb-6 text-center">
      <img src={mailMascot} className="mx-auto mb-4 size-20 object-contain" alt="" />
      <h1 ref={heading} tabIndex={-1} className="text-3xl font-semibold tracking-tight outline-none">{title}</h1>
      <p className="mt-3 break-words text-sm leading-6 text-muted [overflow-wrap:anywhere]">{verify ? t('desktopClient.codeSent', { email }) : t('desktopClient.loginBody')}</p>
      {verify && <Button variant="link" size="sm" disabled={busy} onClick={onChangeEmail}>{t('desktopClient.changeEmail')}</Button>}
    </div>
    <div className="rounded-3xl border border-edge bg-surface p-6 shadow-elevation-1">
      <form onSubmit={(event) => { event.preventDefault(); onSubmit(); }}>
        <Label htmlFor="desktop-auth-field">{t(verify ? 'desktopClient.emailCode' : 'desktopClient.email')}</Label>
        <Input id="desktop-auth-field" className={verify ? 'mt-2 h-12 text-center text-2xl tracking-widest' : 'mt-2 h-12'}
          type={verify ? 'text' : 'email'} inputMode={verify ? 'numeric' : 'email'}
          autoComplete={verify ? 'one-time-code' : 'email'} value={value} disabled={busy}
          maxLength={verify ? 6 : undefined} pattern={verify ? '[0-9]{6}' : undefined}
          onChange={(event) => onValueChange(verify ? event.target.value.replace(/\D/g, '').slice(0, 6) : event.target.value)}
          required aria-invalid={!!errorMessage} aria-describedby={errorMessage ? 'desktop-login-error' : undefined} />
        {passwordMode ? <div className="mt-5">
          <div className="flex items-center justify-between gap-2"><Label htmlFor="desktop-password">{t('desktopClient.password')}</Label><Button type="button" variant="link" size="sm" disabled={busy} onClick={password.onForgot}>{t('desktopClient.forgotPassword')}</Button></div>
          <div className="relative mt-2"><Input id="desktop-password" type={reveal ? 'text' : 'password'} className="h-12 pr-12" autoComplete="current-password" value={password.value} onChange={(event) => password.onChange(event.target.value)} disabled={busy} required />
            <Button type="button" variant="ghost" size="icon" className="absolute right-1 top-1" onClick={() => setReveal(!reveal)} aria-label={t(reveal ? 'desktopClient.hidePassword' : 'desktopClient.showPassword')}>{reveal ? <EyeOff className="size-4" /> : <Eye className="size-4" />}</Button>
          </div>
        </div> : <p className="mt-3 text-xs leading-6 text-muted">{t(verify ? 'desktopClient.codeHelp' : 'desktopClient.newEmailHelp')}</p>}
        {errorMessage && <p id="desktop-login-error" role="alert" className="mt-4 text-sm leading-6 text-destructive-ink">{errorMessage}</p>}
        <Button variant="brand" size="lg" className="mt-6 w-full rounded-full" disabled={busy || !value.trim() || (verify && value.length !== 6) || (!!passwordMode && !password?.value)}>
          {busy && <RefreshCw className="size-4 animate-spin" />}
          {t(verify ? 'desktopClient.verifySignIn' : passwordMode ? 'desktopClient.passwordSignIn' : 'desktopClient.sendCode')}<ArrowRight className="size-4" />
        </Button>
      </form>
      {verify ? <div className="mt-4 text-center"><p className="text-xs text-muted">{t('desktopClient.noCode')}</p><Button variant="link" disabled={busy} onClick={onResendCode}>{t('desktopClient.resendCode')}</Button></div>
        : password && <Button variant="link" className="mt-3 w-full gap-2 text-foreground underline underline-offset-4" disabled={busy} onClick={() => { setReveal(false); password.onToggle(); }}>{passwordMode ? <Mail className="size-4" /> : <KeyRound className="size-4" />}{t(passwordMode ? 'desktopClient.useEmailCode' : 'desktopClient.usePassword')}</Button>}
    </div>
    {(passwordMode || password?.forgot) && <p role={password?.forgot ? 'status' : undefined} className="mt-4 text-center text-xs leading-6 text-muted">{t('desktopClient.passwordHelp')}</p>}
    {!verify && <p className="mt-4 text-center text-xs leading-5 text-muted">{t('desktopClient.registrationDisclosure')}</p>}
    <div className="mt-4 text-center"><Button variant="ghost" onClick={onContinueLocal}>{t('desktopClient.continueLocal')}</Button></div>
  </div>;
}
