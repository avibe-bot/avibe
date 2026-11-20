import {
  AlertTriangle,
  ExternalLink,
  Eye,
  EyeOff,
  KeyRound,
  Mail,
  RotateCw,
  ShieldCheck,
} from 'lucide-react';
import {
  type FormEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from 'react';
import { useTranslation } from 'react-i18next';

import mailMascot from '@/assets/pet/vibey-message.png';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import type { TranslationKey } from '@/i18n/types';
import { cn } from '@/lib/utils';

import { alternative, PageMark, Primary, raised, SignInFrame } from './chrome';
import { useSignInPlatform } from './chromeContext';
import type { SecondFactorStep, SignInErrorKey } from './errors';
import { SignInDivider, SignInProviderRow } from './SignInProviders';
import type { SignInMethods, SignInProvider, SignInStep, WeakPasswordReason } from './types';

/** What the running operation is, as the steps show it. */
export type SignInBusy = string | null;
type SecondFactorSession = Extract<SignInStep, { status: 'secondFactor' }>;

/**
 * The first step's ways in: email is the one primary action; Google, GitHub and Apple follow
 * below avibe.bot's 「或」 as one row of marks (ui/design: mobile-app/welcome/providers-row), then
 * passkey sign-in as a quiet link. A sign-in that failed says so above them.
 */
export function SignInStart({
  locked,
  errorKey = null,
  methods,
  onContinue,
  onProvider,
  onPasskey,
  waiting = null,
}: {
  locked: boolean;
  errorKey?: SignInErrorKey | null;
  methods: SignInMethods;
  onContinue: () => void;
  onProvider?: (provider: SignInProvider) => void;
  onPasskey?: () => void;
  /** A provider sign-in waiting in the default browser, which this page can end. */
  waiting?: { onCancel: () => void } | null;
}) {
  const { t } = useTranslation();
  return (
    <>
      {waiting ? (
        <div role="status" className="flex flex-col items-center gap-2 text-center text-sm leading-6 text-muted">
          <p>{t('signIn.provider.continueInBrowser')}</p>
          <Button type="button" variant="outline" size="sm" className="rounded-full" onClick={waiting.onCancel}>
            {t('common.cancel')}
          </Button>
        </div>
      ) : null}
      {errorKey ? (
        <p role="alert" className="text-center text-xs leading-6 text-destructive-ink">
          {t(errorKey as TranslationKey)}
        </p>
      ) : null}
      <Primary disabled={locked} onClick={onContinue}>
        {t('mobileApp.welcome.continueWithEmail')}
      </Primary>
      {onProvider && methods.providers.length > 0 ? (
        <>
          <SignInDivider />
          <SignInProviderRow providers={methods.providers} disabled={locked} onProvider={onProvider} />
        </>
      ) : null}
      {onPasskey && methods.passkey ? (
        <Button
          type="button"
          variant="link"
          size="compact"
          disabled={locked}
          className={cn(alternative, 'gap-2 self-center')}
          onClick={onPasskey}
        >
          <KeyRound className="size-4" />
          {t('signIn.passkey.use')}
        </Button>
      ) : null}
    </>
  );
}

export type EmailMode = 'code' | 'password';

// ui/design: mobile-app/welcome, mobile-app/password
/**
 * The email step. Code mode sends a verification code; password mode turns the
 * same form into email + password at once (the email is the saved login's
 * username). The password lives only in this step's field state: switching back
 * to code mode clears it, and leaving the step unmounts it.
 */
export function EmailEntry({
  mode,
  email,
  busy,
  locked,
  cooldown,
  errorKey,
  retryIn,
  onBack,
  onEmailChange,
  onSubmit,
  onMode,
  onSignIn,
  onPasswordEdit,
  onForgot,
  onUseCode,
}: {
  mode: EmailMode;
  email: string;
  busy: SignInBusy;
  locked: boolean;
  cooldown: number;
  errorKey: SignInErrorKey | null;
  retryIn: number;
  onBack: () => void;
  onEmailChange: (value: string) => void;
  onSubmit: () => void;
  onMode: (mode: EmailMode) => void;
  onSignIn: (password: string) => void;
  onPasswordEdit: () => void;
  onForgot: () => void;
  onUseCode: () => void;
}) {
  const { t } = useTranslation();
  const passwordMode = mode === 'password';
  const emailField = useRef<HTMLInputElement>(null);
  const passwordField = useRef<HTMLInputElement>(null);
  const [password, setPassword] = useState('');
  const [shown, setShown] = useState(false);
  const { codeActionRegion: actionRegion, revealCodeActions: revealActions } =
    useCodeActionReveal<HTMLFormElement>('mobile-login-password');
  const emailInvalid = errorKey === 'mobileApp.email.invalid';
  // In password mode the email slot shows only the email's own error.
  const emailError = passwordMode ? (emailInvalid ? errorKey : null) : errorKey;
  const passwordError = passwordMode && !emailInvalid ? errorKey : null;
  const wrong = errorKey === 'mobileApp.password.invalid';
  // The email code is the fix for these, so a way to send one appears; it stays
  // while its request runs.
  const codeFixes = wrong || errorKey === 'mobileApp.password.unconfirmed' || busy === 'send';
  const signingIn = busy === 'signIn';

  // Entering password mode focuses the field the person needs next.
  useEffect(() => {
    if (mode !== 'password') return;
    const target = emailField.current?.value.trim() ? passwordField.current : emailField.current;
    target?.focus();
  }, [mode]);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (passwordMode) onSignIn(password);
    else onSubmit();
  };
  const switchMode = (next: EmailMode) => {
    if (locked) return;
    // Code mode keeps no password: it is cleared on the way out.
    setPassword('');
    setShown(false);
    onMode(next);
  };

  return (
    <SignInFrame back={{ disabled: locked, onBack: passwordMode ? () => switchMode('code') : onBack }}>
      <div className="flex flex-col items-center pt-8 pb-6">
        <img src={mailMascot} alt="" className="mb-5 size-28 object-contain" />
        <h1 className="text-center text-3xl font-semibold leading-tight">
          {t('mobileApp.email.title')}
        </h1>
        <p className="mt-3 text-center text-sm leading-6 text-muted">
          {t('mobileApp.email.subtitle')}
        </p>
      </div>
      {/* The page checks the email itself and shows its own copy, not WebKit's bubble. */}
      <form
        ref={actionRegion}
        noValidate
        onSubmit={submit}
        className="scroll-mb-4 rounded-3xl border border-edge bg-surface/70 p-6 shadow-elevation-0"
      >
        <div className="flex flex-col gap-6">
          <div className="flex flex-col gap-3">
            <Label htmlFor="mobile-login-email">{t('mobileApp.email.label')}</Label>
            <Input
              ref={emailField}
              id="mobile-login-email"
              name={passwordMode ? 'username' : 'email'}
              type="email"
              inputMode="email"
              autoComplete={passwordMode ? 'username' : 'email'}
              autoCapitalize="none"
              spellCheck={false}
              enterKeyHint={passwordMode ? 'next' : undefined}
              value={email}
              placeholder={t('mobileApp.email.placeholder')}
              disabled={locked}
              aria-invalid={Boolean(emailError) || undefined}
              aria-describedby={passwordMode
                ? 'mobile-login-email-feedback'
                : 'mobile-login-email-hint mobile-login-email-feedback'}
              className={cn(
                'h-14 rounded-2xl px-4 text-base',
                passwordMode && emailInvalid && 'border-destructive',
              )}
              onChange={(event) => onEmailChange(event.target.value)}
              onKeyDown={(event) => {
                // Return moves on to the password instead of submitting.
                if (!passwordMode || event.key !== 'Enter' || event.nativeEvent.isComposing) return;
                event.preventDefault();
                passwordField.current?.focus();
              }}
            />
            {passwordMode ? null : (
              <p id="mobile-login-email-hint" className="text-xs leading-6 text-muted">
                {t('mobileApp.email.hint')}
              </p>
            )}
            {/* Password mode reserves no space for the email's error until it has one. */}
            <div
              id="mobile-login-email-feedback"
              className={cn(passwordMode ? !emailError && 'hidden' : 'min-h-6')}
            >
              {emailError ? (
                <p role="alert" className="text-xs leading-6 text-destructive-ink">
                  {t(emailError as TranslationKey)}
                </p>
              ) : null}
            </div>
          </div>
          {passwordMode ? (
            <div className="flex flex-col gap-3">
              <div className="flex items-center justify-between gap-3">
                <Label htmlFor="mobile-login-password">{t('mobileApp.password.label')}</Label>
                <Button
                  type="button"
                  variant="link"
                  size="compact"
                  disabled={locked}
                  className="h-auto px-0 text-sm text-muted underline"
                  onClick={onForgot}
                >
                  {t('mobileApp.password.forgot')}
                </Button>
              </div>
              <div className="relative">
                <Input
                  ref={passwordField}
                  id="mobile-login-password"
                  name="password"
                  type={shown ? 'text' : 'password'}
                  autoComplete="current-password"
                  autoCapitalize="none"
                  autoCorrect="off"
                  spellCheck={false}
                  enterKeyHint="go"
                  value={password}
                  disabled={locked}
                  aria-invalid={wrong || undefined}
                  aria-describedby="mobile-login-password-feedback"
                  className={cn(
                    'h-14 rounded-2xl pr-14 pl-4 text-base',
                    wrong && 'border-destructive',
                  )}
                  onFocus={revealActions}
                  onChange={(event) => {
                    setPassword(event.target.value);
                    onPasswordEdit();
                  }}
                />
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  disabled={locked}
                  aria-pressed={shown}
                  aria-label={t('mobileApp.password.show')}
                  className="absolute top-1/2 right-1.5 size-11 -translate-y-1/2 rounded-xl text-muted"
                  onClick={() => setShown((value) => !value)}
                >
                  {shown ? <EyeOff className="size-5" /> : <Eye className="size-5" />}
                </Button>
              </div>
              <div id="mobile-login-password-feedback" className="min-h-6">
                {passwordError ? (
                  <p role="alert" className="text-xs leading-6 text-destructive-ink">
                    {t(passwordError as TranslationKey)}
                  </p>
                ) : null}
              </div>
            </div>
          ) : null}
          <div className="flex flex-col gap-3">
            {passwordMode ? (
              <div aria-busy={signingIn}>
                <Primary type="submit" disabled={locked || password.length === 0 || retryIn > 0}>
                  {signingIn
                    ? t('mobileApp.password.signingIn')
                    : retryIn > 0
                      ? t('mobileApp.password.retryIn', { count: retryIn })
                      : t('mobileApp.password.signIn')}
                </Primary>
              </div>
            ) : (
              <div aria-busy={busy === 'send'}>
                <Primary type="submit" disabled={locked || cooldown > 0}>
                  {busy === 'send'
                    ? t('mobileApp.email.sending')
                    : cooldown > 0
                      ? t('mobileApp.email.sendIn', { count: cooldown })
                      : t('mobileApp.email.send')}
                </Primary>
              </div>
            )}
            <Button
              type="button"
              variant="link"
              size="compact"
              disabled={locked}
              className={cn(alternative, 'gap-2 self-center')}
              onClick={() => switchMode(passwordMode ? 'code' : 'password')}
            >
              {passwordMode ? <Mail className="size-4" /> : <KeyRound className="size-4" />}
              {t(passwordMode ? 'mobileApp.password.codeEntry' : 'mobileApp.password.entry')}
            </Button>
          </div>
        </div>
      </form>
      {passwordMode && codeFixes ? (
        <div className="mt-6" aria-busy={busy === 'send'}>
          <Button
            type="button"
            variant="outline"
            size="lg"
            disabled={locked || cooldown > 0}
            className="w-full gap-2 whitespace-normal rounded-full"
            onClick={onUseCode}
          >
            <Mail className="size-4 shrink-0" />
            {busy === 'send' ? t('mobileApp.email.sending') : t('mobileApp.password.useCode')}
          </Button>
        </div>
      ) : null}
      <p className="mt-auto pt-8 pb-4 text-center text-xs leading-6 text-muted">
        {t(passwordMode ? 'mobileApp.password.footer' : 'mobileApp.email.footer')}
      </p>
    </SignInFrame>
  );
}

export function OtpCells({
  code,
  label,
  invalid,
  busy,
  onChange,
  onFocus,
}: {
  code: string;
  label: string;
  invalid: boolean;
  busy: boolean;
  onChange: (value: string) => void;
  onFocus: () => void;
}) {
  const input = useRef<HTMLInputElement>(null);
  return (
    <div className="relative">
      <Label htmlFor="mobile-login-code" className="sr-only">
        {label}
      </Label>
      <Input
        ref={input}
        id="mobile-login-code"
        className="absolute inset-0 z-10 h-full w-full cursor-text opacity-0 disabled:opacity-0"
        type="text"
        inputMode="numeric"
        autoComplete="one-time-code"
        pattern="[0-9]{6}"
        maxLength={6}
        value={code}
        disabled={busy}
        aria-invalid={invalid || undefined}
        aria-describedby="mobile-login-code-hint mobile-login-code-feedback"
        onFocus={onFocus}
        onChange={(event) => onChange(event.target.value.replace(/\D/g, '').slice(0, 6))}
      />
      <div aria-hidden="true" className="grid grid-cols-6 gap-2">
        {Array.from({ length: 6 }, (_, index) => (
          <span
            key={index}
            className={cn(
              'grid aspect-square min-w-0 place-items-center rounded-2xl border bg-surface text-2xl font-medium tabular-nums shadow-elevation-0 transition-colors duration-(--motion-duration-fast)',
              invalid ? 'border-destructive text-destructive-ink' : 'border-edge',
              !invalid && !busy && index === code.length && 'border-mint ring-2 ring-mint/30',
            )}
          >
            {code[index] || (!busy && index === code.length ? (
              <span className="h-6 w-px bg-mint-ink" />
            ) : null)}
          </span>
        ))}
      </div>
    </div>
  );
}

/** Keeps a field's actions above the software keyboard while the field has focus. */
function useCodeActionReveal<Region extends HTMLElement>(fieldId = 'mobile-login-code') {
  const codeActionRegion = useRef<Region>(null);
  const revealCodeActions = useCallback(() => {
    window.requestAnimationFrame(() => {
      const region = codeActionRegion.current;
      if (typeof region?.scrollIntoView === 'function') {
        region.scrollIntoView({ block: 'end' });
      }
    });
  }, []);

  useEffect(() => {
    const revealWhenFocused = () => {
      if (document.activeElement?.id === fieldId) revealCodeActions();
    };
    window.addEventListener('resize', revealWhenFocused);
    window.visualViewport?.addEventListener('resize', revealWhenFocused);
    return () => {
      window.removeEventListener('resize', revealWhenFocused);
      window.visualViewport?.removeEventListener('resize', revealWhenFocused);
    };
  }, [fieldId, revealCodeActions]);

  return { codeActionRegion, revealCodeActions };
}

export function CodeEntry({
  email,
  code,
  busy,
  locked,
  errorKey,
  cooldown,
  providerCooldownActive,
  resent,
  onBack,
  onCodeChange,
  onVerify,
  onResend,
}: {
  email: string;
  code: string;
  busy: SignInBusy;
  locked: boolean;
  errorKey: SignInErrorKey | null;
  cooldown: number;
  providerCooldownActive: boolean;
  resent: boolean;
  onBack: () => void;
  onCodeChange: (value: string) => void;
  onVerify: () => void;
  onResend: () => void;
}) {
  const { t } = useTranslation();
  const { codeActionRegion, revealCodeActions } = useCodeActionReveal<HTMLDivElement>();

  return (
    <SignInFrame back={{ disabled: locked, onBack: onBack }}>
      <div className="flex flex-col items-center pt-8 pb-6">
        <img src={mailMascot} alt="" className="mb-5 size-28 object-contain" />
        <h1 className="text-center text-3xl font-semibold leading-tight">
          {t('mobileApp.code.title')}
        </h1>
      </div>
      <div className="pb-7 text-center">
        <p className="text-sm leading-7 text-muted">{t('mobileApp.code.sentTo')}</p>
        <p className="break-all text-sm font-medium leading-6">{email}</p>
        <Button
          type="button"
          variant="link"
          size="compact"
          disabled={locked}
          className="mt-1 px-0 text-muted underline"
          onClick={onBack}
        >
          {t('mobileApp.code.changeEmail')}
        </Button>
      </div>
      <div ref={codeActionRegion} data-mobile-code-actions className="scroll-mb-4">
        <OtpCells
          code={code}
          label={t('mobileApp.code.label')}
          invalid={errorKey === 'mobileApp.code.invalid'}
          busy={locked}
          onChange={onCodeChange}
          onFocus={revealCodeActions}
        />
        <p id="mobile-login-code-hint" className="mt-5 text-center text-sm leading-6 text-muted">
          {t('mobileApp.code.hint')}
        </p>
        <div
          id="mobile-login-code-feedback"
          className="mt-3 min-h-6 text-center text-xs leading-6"
        >
          {errorKey ? (
            <p role="alert" className="text-destructive-ink">
              {t(errorKey as TranslationKey)}
            </p>
          ) : resent ? (
            <p role="status" className="text-mint-ink">{t('mobileApp.code.resent')}</p>
          ) : null}
        </div>
        <div className="mt-5" aria-busy={busy === 'verify'}>
          <Primary
            disabled={locked || providerCooldownActive || code.length !== 6}
            onClick={onVerify}
          >
            {busy === 'verify'
              ? t('mobileApp.code.verifying')
              : providerCooldownActive
                ? t('mobileApp.email.sendIn', { count: cooldown })
                : t('mobileApp.code.verify')}
          </Primary>
        </div>
      </div>
      <div className="mt-6 text-center text-sm leading-7">
        <p className="text-muted">{t('mobileApp.code.resendPrompt')}</p>
        <Button
          type="button"
          variant="link"
          size="compact"
          disabled={locked || cooldown > 0}
          className={alternative}
          onClick={onResend}
        >
          {busy === 'resend'
            ? t('mobileApp.code.resending')
            : cooldown > 0
              ? t('mobileApp.code.resendIn', { count: cooldown })
              : t('mobileApp.code.resend')}
        </Button>
      </div>
      <p className="mt-auto pt-7 pb-3 text-center text-xs leading-6 text-muted">
        {t('mobileApp.code.footer')}
      </p>
    </SignInFrame>
  );
}

// ui/design: mobile-app/two-factor
export function SecondFactor({
  session,
  step,
  code,
  busy,
  locked,
  errorKey,
  retryMinutes,
  onStep,
  onCodeChange,
  onVerify,
  onPasskey,
  onOpenSecurity,
  onSetupDone,
  onDifferentAccount,
}: {
  session: SecondFactorSession;
  step: SecondFactorStep;
  code: string;
  busy: SignInBusy;
  locked: boolean;
  errorKey: SignInErrorKey | null;
  retryMinutes: number;
  onStep: (step: SecondFactorStep) => void;
  onCodeChange: (value: string) => void;
  onVerify: () => void;
  onPasskey: () => void;
  onOpenSecurity: () => void;
  onSetupDone: () => void;
  onDifferentAccount: () => void;
}) {
  const { t } = useTranslation();
  const desktop = useSignInPlatform() === 'desktop';
  const { codeActionRegion, revealCodeActions } = useCodeActionReveal<HTMLFormElement>();
  const hasPasskey = session.methods.includes('passkey');
  const hasTotp = session.methods.includes('totp');
  const setup = step === 'setup';
  const usePasskeyInstead = { step: 'passkey', label: t('mobileApp.twoFactor.usePasskeyInstead') } as const;
  const useTotp = { step: 'totp', label: t('mobileApp.twoFactor.useTotp') } as const;
  const useRecovery = { step: 'recovery', label: t('mobileApp.twoFactor.useRecovery') } as const;
  const alternatives = step === 'passkey'
    ? [...(hasTotp ? [useTotp] : []), useRecovery]
    : step === 'totp'
      ? [...(hasPasskey ? [usePasskeyInstead] : []), useRecovery]
      : step === 'recovery'
        ? hasPasskey ? [usePasskeyInstead] : hasTotp ? [useTotp] : []
        : [];
  const notice = errorKey === 'mobileApp.twoFactor.rateLimited'
    ? t('mobileApp.twoFactor.rateLimited', { count: retryMinutes })
    : errorKey
      ? t(errorKey as TranslationKey)
      : null;
  const feedback = (
    <div id="mobile-login-code-feedback" className="mt-3 min-h-6 text-center text-xs leading-6">
      {notice ? <p role="alert" className="text-destructive-ink">{notice}</p> : null}
    </div>
  );
  const verify = (ready: boolean) => (
    <div className="mt-5" aria-busy={busy === 'verify'}>
      <Primary type="submit" disabled={locked || !ready}>
        {busy === 'verify' ? t('mobileApp.twoFactor.verifying') : t('mobileApp.twoFactor.verify')}
      </Primary>
    </div>
  );
  const submit = (event: FormEvent) => {
    event.preventDefault();
    onVerify();
  };

  return (
    <SignInFrame>
      <div className="flex flex-col items-center pt-8 pb-6">
        <PageMark>
          <ShieldCheck className="size-7 text-mint-ink" />
        </PageMark>
        <h1 className="text-center text-3xl font-semibold leading-tight tracking-tight">
          {t(setup ? 'mobileApp.twoFactor.setupTitle' : 'mobileApp.twoFactor.title')}
        </h1>
      </div>
      <div className="pb-7 text-center">
        <p className="text-sm leading-7 text-muted">
          {setup
            ? t('mobileApp.twoFactor.requiredFor')
            : t(session.email ? 'mobileApp.twoFactor.onFor' : 'mobileApp.twoFactor.onThisAccount')}
        </p>
        {session.email ? (
          <p className="break-all text-sm font-medium leading-6">{session.email}</p>
        ) : null}
      </div>
      {step === 'passkey' ? (
        <>
          <div aria-busy={busy === 'passkey'}>
            <Primary disabled={locked} onClick={onPasskey}>
              <KeyRound className="size-4" />
              {busy === 'passkey'
                ? t('mobileApp.twoFactor.waitingForPasskey')
                : t('mobileApp.twoFactor.usePasskey')}
            </Primary>
          </div>
          <p id="mobile-login-code-hint" className="mt-5 text-center text-sm leading-6 text-muted">
            {t(desktop ? 'signIn.desktop.passkeyHint' : 'mobileApp.twoFactor.passkeyHint')}
          </p>
          {feedback}
        </>
      ) : null}
      {step === 'totp' ? (
        <form
          ref={codeActionRegion}
          data-mobile-code-actions
          className="scroll-mb-4"
          onSubmit={submit}
        >
          <OtpCells
            code={code}
            label={t('mobileApp.twoFactor.totpLabel')}
            invalid={errorKey === 'mobileApp.twoFactor.codeInvalid'}
            busy={locked}
            onChange={onCodeChange}
            onFocus={revealCodeActions}
          />
          <p id="mobile-login-code-hint" className="mt-5 text-center text-sm leading-6 text-muted">
            {t('mobileApp.twoFactor.totpHint')}
          </p>
          {feedback}
          {verify(code.length === 6)}
        </form>
      ) : null}
      {step === 'recovery' ? (
        <form onSubmit={submit}>
          <div className={cn(raised, 'flex flex-col gap-3')}>
            <Label htmlFor="mobile-recovery-code">{t('mobileApp.twoFactor.recoveryLabel')}</Label>
            <Input
              id="mobile-recovery-code"
              type="text"
              autoComplete="one-time-code"
              autoCapitalize="none"
              spellCheck={false}
              value={code}
              placeholder={t('mobileApp.twoFactor.recoveryPlaceholder')}
              disabled={locked}
              aria-invalid={errorKey === 'mobileApp.twoFactor.recoveryInvalid' || undefined}
              aria-describedby="mobile-login-code-hint mobile-login-code-feedback"
              className="h-14 rounded-2xl px-4 text-base tracking-wide"
              onChange={(event) => onCodeChange(event.target.value)}
            />
            <p id="mobile-login-code-hint" className="text-xs leading-6 text-muted">
              {t('mobileApp.twoFactor.recoveryHint')}
            </p>
          </div>
          {feedback}
          {verify(code.trim().length > 0)}
        </form>
      ) : null}
      {setup ? (
        <>
          <div className={cn(raised, 'text-sm leading-7 text-muted')}>
            {t('mobileApp.twoFactor.setupBody')}
          </div>
          <div className="mt-5 flex flex-col gap-3">
            <Button
              type="button"
              variant="brand"
              size="lg"
              disabled={locked}
              className="w-full gap-2 rounded-full"
              onClick={onOpenSecurity}
            >
              <ExternalLink className="size-4" />
              {t('mobileApp.twoFactor.openSecurity')}
            </Button>
            <Button
              type="button"
              variant="outline"
              size="lg"
              disabled={locked}
              className="w-full gap-2 whitespace-normal rounded-full"
              onClick={onSetupDone}
            >
              <RotateCw className={cn('size-4 shrink-0', busy === 'refresh' && 'animate-spin')} />
              {t('mobileApp.twoFactor.setupDone')}
            </Button>
          </div>
          {notice ? feedback : null}
        </>
      ) : null}
      {alternatives.length > 0 ? (
        <div className="mt-6 flex flex-col items-center gap-1">
          {alternatives.map((option) => (
            <Button
              key={option.step}
              type="button"
              variant="link"
              size="compact"
              disabled={locked}
              className={alternative}
              onClick={() => onStep(option.step)}
            >
              {option.label}
            </Button>
          ))}
        </div>
      ) : null}
      <div className="mt-auto pt-7 pb-3 text-center">
        <Button
          type="button"
          variant="link"
          size="compact"
          disabled={locked}
          className="h-auto px-0 text-xs text-muted underline"
          onClick={onDifferentAccount}
        >
          {t('mobileApp.twoFactor.differentAccount')}
        </Button>
      </div>
    </SignInFrame>
  );
}

// ui/design: mobile-app/password/forgot
/**
 * Forgot password. Email sign-in always works, so the code comes first; a new
 * password is set on avibe.bot's Account security page, opened in Safari.
 */
export function ForgotPassword({
  email,
  busy,
  locked,
  cooldown,
  errorKey,
  onBack,
  onSendCode,
  onOpenSecurity,
}: {
  email: string;
  busy: boolean;
  locked: boolean;
  cooldown: number;
  errorKey: SignInErrorKey | null;
  onBack: () => void;
  onSendCode: () => void;
  onOpenSecurity: () => void;
}) {
  const { t } = useTranslation();
  const desktop = useSignInPlatform() === 'desktop';
  return (
    <SignInFrame back={{ disabled: locked, onBack: onBack }}>
      <div className="flex flex-col items-center pt-8 pb-6">
        <PageMark>
          <KeyRound className="size-7 text-mint-ink" />
        </PageMark>
        <h1 className="text-center text-3xl font-semibold leading-tight tracking-tight">
          {t('mobileApp.password.forgotTitle')}
        </h1>
        <p className="mt-3 text-center text-sm leading-6 text-muted">
          {t('mobileApp.password.forgotSubtitle')}
        </p>
      </div>
      <div className="pb-7 text-center">
        <p className="text-sm leading-7 text-muted">{t('mobileApp.password.account')}</p>
        <p className="break-all text-sm font-medium leading-6">{email}</p>
      </div>
      <section className={cn(raised, 'flex flex-col gap-4')}>
        <div>
          <h2 className="text-base font-semibold">{t('mobileApp.password.signInNowTitle')}</h2>
          <p className="mt-2 text-sm leading-6 text-muted">{t('mobileApp.password.signInNowBody')}</p>
        </div>
        <div aria-busy={busy}>
          <Primary disabled={locked || cooldown > 0} onClick={onSendCode}>
            {busy
              ? t('mobileApp.email.sending')
              : cooldown > 0
                ? t('mobileApp.email.sendIn', { count: cooldown })
                : t('mobileApp.email.send')}
          </Primary>
        </div>
        {errorKey ? (
          <p role="alert" className="text-xs leading-6 text-destructive-ink">
            {t(errorKey as TranslationKey)}
          </p>
        ) : null}
      </section>
      <section className={cn(raised, 'mt-4 flex flex-col gap-4')}>
        <div>
          <h2 className="text-base font-semibold">{t('mobileApp.password.newPasswordTitle')}</h2>
          <p className="mt-2 text-sm leading-6 text-muted">{t(desktop ? 'signIn.desktop.newPasswordBody' : 'mobileApp.password.newPasswordBody')}</p>
        </div>
        <Button
          type="button"
          variant="outline"
          size="lg"
          disabled={locked}
          className="w-full gap-2 whitespace-normal rounded-full"
          onClick={onOpenSecurity}
        >
          <ExternalLink className="size-4 shrink-0" />
          {t(desktop ? 'signIn.desktop.openInBrowser' : 'mobileApp.password.openInSafari')}
        </Button>
      </section>
    </SignInFrame>
  );
}

// ui/design: mobile-app/password/weak
/**
 * Signed in, but avibe.bot reported the password as weak. Shown once, after any
 * second factor and before the instances; Continue never blocks.
 */
export function WeakPasswordNotice({
  email,
  reasons,
  locked,
  onContinue,
  onOpenSecurity,
}: {
  email: string;
  reasons: WeakPasswordReason[];
  locked: boolean;
  onContinue: () => void;
  onOpenSecurity: () => void;
}) {
  const { t } = useTranslation();
  const desktop = useSignInPlatform() === 'desktop';
  return (
    <SignInFrame>
      <div className="flex flex-1 flex-col items-center justify-center py-10 text-center">
        <PageMark warn>
          <AlertTriangle className="size-7 text-gold-ink" />
        </PageMark>
        <h1 className="text-3xl font-semibold leading-tight tracking-tight">
          {t('mobileApp.password.weakTitle')}
        </h1>
        <p className="mt-4 text-sm leading-7 text-muted">{t('mobileApp.password.signedInAs')}</p>
        <p className="break-all text-sm font-medium leading-6">{email}</p>
        <div className={cn(raised, 'mt-7 w-full text-left text-sm leading-7 text-muted')}>
          {t(reasons.includes('pwned') ? 'mobileApp.password.weakPwned' : 'mobileApp.password.weakRules')}
        </div>
        <div className="mt-8 flex w-full flex-col gap-3">
          <Primary disabled={locked} onClick={onContinue}>
            {t('common.continue')}
          </Primary>
          <Button
            type="button"
            variant="outline"
            size="lg"
            disabled={locked}
            className="w-full gap-2 whitespace-normal rounded-full"
            onClick={onOpenSecurity}
          >
            <ExternalLink className="size-4 shrink-0" />
            {t(desktop ? 'signIn.desktop.changeInBrowser' : 'mobileApp.password.changeInSafari')}
          </Button>
        </div>
      </div>
    </SignInFrame>
  );
}

// ui/design: sign-in/passkey (proposed)
/**
 * Passkey sign-in with no email first, in avibe.bot's words for its own passkey card. Native
 * runs the system prompt; this step only starts it and says how it ended. It is not the passkey
 * second factor, which an email or password sign-in reaches on its own step.
 */
export function PasskeySignIn({
  busy,
  locked,
  errorKey,
  onPasskey,
  onBack,
}: {
  busy: SignInBusy;
  locked: boolean;
  errorKey: SignInErrorKey | null;
  onPasskey: () => void;
  onBack: () => void;
}) {
  const { t } = useTranslation();
  const waiting = busy === 'primaryPasskey';
  return (
    <SignInFrame back={{ disabled: locked, onBack }}>
      <div className="flex flex-col items-center pt-8 pb-6">
        <PageMark>
          <KeyRound className="size-7 text-mint-ink" />
        </PageMark>
        <h1 className="text-center text-3xl font-semibold leading-tight tracking-tight">
          {t('signIn.passkey.title')}
        </h1>
        <p className="mt-3 max-w-sm text-center text-sm leading-6 text-muted">
          {t('signIn.passkey.lead')}
        </p>
      </div>
      <div aria-busy={waiting}>
        <Primary disabled={locked} onClick={onPasskey}>
          <KeyRound className="size-4" />
          {waiting
            ? t('signIn.passkey.waiting')
            : errorKey
              ? t('signIn.passkey.retry')
              : t('signIn.passkey.use')}
        </Primary>
      </div>
      <div className="mt-5 min-h-12 text-center text-sm leading-6">
        {waiting ? (
          <p className="text-muted">{t('signIn.passkey.waitingHint')}</p>
        ) : errorKey ? (
          <p role="alert" className="text-destructive-ink">{t(errorKey as TranslationKey)}</p>
        ) : null}
      </div>
    </SignInFrame>
  );
}
