import {
  AlertTriangle,
  ArrowLeft,
  ArrowRight,
  Check,
  ChevronRight,
  ExternalLink,
  Eye,
  EyeOff,
  KeyRound,
  Laptop,
  Mail,
  MonitorDown,
  Plus,
  RotateCw,
  ShieldCheck,
  Sparkles,
  UserRound,
} from 'lucide-react';
import {
  type FormEvent,
  type ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { useTranslation } from 'react-i18next';

import heroMascot from '../../../assets/mascot/cloud-tuanzi-v2.png';
import logo from '@/assets/logo.png';
import mailMascot from '@/assets/pet/vibey-message.png';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Skeleton, SkeletonShimmer } from '@/components/ui/skeleton';
import type { TranslationKey } from '@/i18n/types';
import { cn } from '@/lib/utils';

import {
  ACCOUNT_SECURITY_URL,
  bootstrapMobileSession,
  logoutMobileSession,
  normalizeMobileBridgeError,
  openMobileExternal,
  refreshMobileSession,
  requestMobileOtp,
  returnToMobileInstance,
  selectMobileInstance,
  signInMobileWithPassword,
  type MobileBridgeError,
  type MobileInstance,
  type MobileSessionState,
  type MobileWeakPasswordReason,
  verifyMobileOtp,
  verifyMobilePasskey,
  verifyMobileSecondFactor,
} from './bridge';

type Screen =
  | 'boot'
  | 'bootError'
  | 'welcome'
  | 'email'
  | 'code'
  | 'password'
  | 'forgot'
  | 'secondFactor'
  | 'weakPassword'
  | 'instances'
  | 'empty'
  | 'cli'
  | 'account';

type BusyAction =
  | 'boot'
  | 'send'
  | 'verify'
  | 'resend'
  | 'signIn'
  | 'passkey'
  | 'refresh'
  | 'select'
  | 'logout'
  | 'return';
type CooldownSource = 'client' | 'provider' | null;
type CooldownScope = 'send' | 'all' | null;
type MobileOperation =
  | 'bootstrap'
  | 'request'
  | 'verify'
  | 'password'
  | 'totp'
  | 'recovery'
  | 'passkey'
  | 'refresh'
  | 'select'
  | 'logout';
type MobileErrorKey =
  | 'mobileApp.email.invalid'
  | 'mobileApp.code.invalid'
  | 'mobileApp.code.rateLimited'
  | 'mobileApp.password.invalid'
  | 'mobileApp.password.unconfirmed'
  | 'mobileApp.password.rateLimited'
  | 'mobileApp.twoFactor.passkeyFailed'
  | 'mobileApp.twoFactor.codeInvalid'
  | 'mobileApp.twoFactor.recoveryInvalid'
  | 'mobileApp.twoFactor.rateLimited'
  | 'mobileApp.errors.network';
type SecondFactorSession = Extract<MobileSessionState, { status: 'twoFactorRequired' }>;
type SecondFactorStep = 'passkey' | 'totp' | 'recovery' | 'setup';

const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const CODE_PATTERN = /^\d{6}$/;
// Native opens the account page with this fragment over a live instance, and
// then answers `app.returnToInstance`; `#account` has no instance behind it.
const RUNTIME_ACCOUNT_HASH = '#runtime-account';
const DEFAULT_COOLDOWN_SECONDS = 60;
const FAILED_COOLDOWN_SECONDS = 15;
// Sign in waits this long after a password rate limit that names no Retry-After.
const PASSWORD_RETRY_SECONDS = 60;
// avibe.bot counts second-factor attempts over this window.
const SECOND_FACTOR_RETRY_MINUTES = 15;
const PASSKEY_FAILURES = new Set(['passkey_cancelled', 'passkey_unavailable', 'invalid']);

const providerCooldownSeconds = (error: MobileBridgeError): number => {
  if (typeof error.retry_after_seconds === 'number' && error.retry_after_seconds > 0) {
    return Math.max(1, Math.ceil(error.retry_after_seconds));
  }
  return error.code === 'rate_limited' ? FAILED_COOLDOWN_SECONDS : 0;
};

type OperationToken = number;

function useOperationOwner() {
  const nextToken = useRef(0);
  const currentToken = useRef<OperationToken | null>(null);
  const [operation, setOperation] = useState<{
    token: OperationToken;
    action: BusyAction;
  } | null>(null);

  const beginOperation = useCallback((action: BusyAction): OperationToken | null => {
    if (currentToken.current !== null) return null;
    const token = nextToken.current + 1;
    nextToken.current = token;
    currentToken.current = token;
    setOperation({ token, action });
    return token;
  }, []);

  const ownsOperation = useCallback(
    (token: OperationToken): boolean => currentToken.current === token,
    [],
  );

  const finishOperation = useCallback((token: OperationToken): boolean => {
    if (currentToken.current !== token) return false;
    currentToken.current = null;
    setOperation((active) => active?.token === token ? null : active);
    return true;
  }, []);

  const isOperationLocked = useCallback(
    (): boolean => currentToken.current !== null,
    [],
  );

  return {
    beginOperation,
    busy: operation?.action ?? null,
    finishOperation,
    isOperationLocked,
    locked: operation !== null,
    ownsOperation,
  };
}

const installGuideUrl = (language: string): string =>
  language.toLowerCase().startsWith('zh')
    ? 'https://github.com/avibe-bot/avibe/blob/master/docs/INSTALL_FOR_AI_ZH.md'
    : 'https://github.com/avibe-bot/avibe/blob/master/docs/INSTALL_FOR_AI.md';

const screenForSession = (session: MobileSessionState): Screen => {
  if (session.status === 'signedOut') return 'welcome';
  if (session.status === 'twoFactorRequired') return 'secondFactor';
  return session.instances.length === 0 ? 'empty' : 'instances';
};

/** Passkey first when the account has one; an account that must turn it on sets it up. */
const firstSecondFactorStep = (session: SecondFactorSession): SecondFactorStep => {
  if (session.gate === 'enroll') return 'setup';
  if (session.methods.includes('passkey')) return 'passkey';
  return session.methods.includes('totp') ? 'totp' : 'recovery';
};

const secondFactorRetrySeconds = (error: MobileBridgeError): number =>
  typeof error.retry_after_seconds === 'number' && error.retry_after_seconds > 0
    ? error.retry_after_seconds
    : SECOND_FACTOR_RETRY_MINUTES * 60;

const secondFactorRetryMinutes = (error: MobileBridgeError): number =>
  Math.ceil(secondFactorRetrySeconds(error) / 60);

/** True when an edit, not a re-render, makes a six-digit code complete. */
const completesCode = (next: string, previous: string): boolean =>
  CODE_PATTERN.test(next) && next !== previous;

const pageErrorKey = (
  error: MobileBridgeError,
  operation: MobileOperation,
): MobileErrorKey => {
  if (operation === 'password') {
    if (error.code === 'invalid_credentials') return 'mobileApp.password.invalid';
    if (error.code === 'email_not_confirmed') return 'mobileApp.password.unconfirmed';
    if (error.code === 'rate_limited') return 'mobileApp.password.rateLimited';
    return 'mobileApp.errors.network';
  }
  if (
    (error.code === 'invalid_email' || error.code === 'email_address_invalid')
    && operation === 'request'
  ) {
    return 'mobileApp.email.invalid';
  }
  if (error.code === 'invalid_or_expired' && operation === 'verify') {
    return 'mobileApp.code.invalid';
  }
  if (operation === 'totp' || operation === 'recovery' || operation === 'passkey') {
    if (error.code === 'rate_limited') return 'mobileApp.twoFactor.rateLimited';
    if (operation === 'passkey' && PASSKEY_FAILURES.has(error.code)) {
      return 'mobileApp.twoFactor.passkeyFailed';
    }
    if (error.code === 'invalid') {
      return operation === 'totp'
        ? 'mobileApp.twoFactor.codeInvalid'
        : 'mobileApp.twoFactor.recoveryInvalid';
    }
  }
  if (error.code === 'rate_limited') {
    return 'mobileApp.code.rateLimited';
  }
  return 'mobileApp.errors.network';
};

function CloudScreen({
  children,
}: {
  children: ReactNode;
}) {
  return (
    <main
      className="h-dvh w-full overflow-y-auto bg-bg text-foreground"
      style={{ background: 'var(--gradient-wizard-platforms)' }}
    >
      <div className="mx-auto flex min-h-full w-full max-w-xl shrink-0 flex-col px-6 pt-[max(1.5rem,env(safe-area-inset-top))] pb-[max(1.5rem,env(safe-area-inset-bottom))]">
        {children}
      </div>
    </main>
  );
}

function Brand({
  back,
  account,
  disabled,
  onBack,
  onAccount,
}: {
  back?: boolean;
  account?: boolean;
  disabled?: boolean;
  onBack?: () => void;
  onAccount?: () => void;
}) {
  const { t } = useTranslation();
  return (
    <header className="flex h-11 shrink-0 items-center justify-between">
      <div className="flex min-w-0 items-center gap-2.5">
        {back ? (
          <Button
            type="button"
            variant="ghost"
            size="icon"
            disabled={disabled}
            aria-label={t('common.back')}
            onClick={onBack}
          >
            <ArrowLeft className="size-5" />
          </Button>
        ) : (
          <img src={logo} alt="" className="size-8" />
        )}
        <span className="text-lg font-semibold">{t('mobileApp.brand.name')}</span>
      </div>
      {account ? (
        <Button
          type="button"
          variant="ghost"
          size="icon"
          disabled={disabled}
          className="rounded-full border border-border bg-surface/70"
          aria-label={t('mobileApp.account.title')}
          onClick={onAccount}
        >
          <UserRound className="size-4" />
        </Button>
      ) : (
        <span className="text-xs text-muted">{t('mobileApp.brand.agentOs')}</span>
      )}
    </header>
  );
}

function Primary({
  children,
  disabled,
  onClick,
  type = 'button',
}: {
  children: ReactNode;
  disabled?: boolean;
  onClick?: () => void;
  type?: 'button' | 'submit';
}) {
  return (
    <Button
      type={type}
      variant="brand"
      size="lg"
      disabled={disabled}
      onClick={onClick}
      className="w-full gap-3 rounded-full shadow-mint-card-sm"
    >
      {children}
      <ArrowRight className="size-4" />
    </Button>
  );
}

function PageTitle({
  title,
  subtitle,
}: {
  title: string;
  subtitle?: string;
}) {
  return (
    <div className="pt-9 pb-7">
      <h1 className="text-3xl font-semibold leading-tight">{title}</h1>
      {subtitle ? <p className="mt-4 text-sm leading-6 text-muted">{subtitle}</p> : null}
    </div>
  );
}

const raised = 'rounded-3xl border border-border bg-surface/80 p-5 shadow-mint-card-sm';
/** A secondary way through a step, below its primary action. */
const alternative = 'h-auto max-w-full whitespace-normal px-0 text-sm text-foreground underline';

/** The raised tile that heads a sign-in step. */
function PageMark({ children, warn = false }: { children: ReactNode; warn?: boolean }) {
  return (
    <span
      className={cn(
        'mb-5 grid size-16 place-items-center rounded-3xl border bg-surface/80 shadow-mint-card-sm',
        warn ? 'border-gold/40' : 'border-border',
      )}
    >
      {children}
    </span>
  );
}

function RecoveryCard({
  errorKey,
  locked,
  onRetry,
}: {
  errorKey: MobileErrorKey;
  locked: boolean;
  onRetry: () => void;
}) {
  const { t } = useTranslation();
  return (
    <div className={cn(raised, 'py-8 text-center')}>
      <AlertTriangle className="mx-auto mb-4 size-7 text-gold-ink" />
      <p className="text-base font-semibold">{t('mobileApp.instances.unavailable')}</p>
      <p role="alert" className="mt-3 text-sm leading-6 text-muted">
        {t(errorKey as TranslationKey)}
      </p>
      <Button
        type="button"
        variant="outline"
        disabled={locked}
        className="mt-5 rounded-full"
        onClick={onRetry}
      >
        <RotateCw className="size-4" />
        {t('common.retry')}
      </Button>
    </div>
  );
}

function Welcome({
  locked,
  onContinue,
}: {
  locked: boolean;
  onContinue: () => void;
}) {
  const { t } = useTranslation();
  return (
    <CloudScreen>
      <Brand />
      <div className="relative flex flex-1 flex-col items-center justify-center py-8">
        <div className="relative mb-8 grid size-64 place-items-center">
          <div className="absolute inset-0 rounded-full border border-mint/20 bg-surface/30 shadow-mint-card-lg" />
          <div className="absolute inset-6 rounded-full border border-surface/70 bg-surface/30" />
          <img
            src={heroMascot}
            alt={t('mobileApp.welcome.mascotAlt')}
            className="relative size-52 object-contain motion-safe:animate-[mobile-mascot-arrive_var(--motion-spring-smooth-duration)_var(--motion-spring-smooth)_both]"
          />
          <span className="absolute -right-1 top-9 grid size-12 rotate-12 place-items-center rounded-2xl border border-border bg-surface shadow-mint-card-sm">
            <Sparkles className="size-5 text-mint-ink" />
          </span>
          <span className="absolute -left-1 bottom-5 rounded-full border border-border bg-surface px-4 py-2 text-sm shadow-mint-card-sm">
            {t('mobileApp.welcome.greeting')}
          </span>
        </div>
        <h1 className="text-center text-4xl font-semibold leading-tight">
          {t('mobileApp.welcome.titleLine1')}
          <br />
          {t('mobileApp.welcome.titleLine2')}
        </h1>
        <p className="mt-5 max-w-xs text-center text-sm leading-7 text-muted">
          {t('mobileApp.welcome.bodyLine1')}
          <br />
          {t('mobileApp.welcome.bodyLine2')}
        </p>
      </div>
      <div className="flex flex-col gap-4 pb-3">
        <Primary disabled={locked} onClick={onContinue}>
          {t('mobileApp.welcome.action')}
        </Primary>
        <p className="text-center text-xs leading-relaxed text-muted">
          {t('mobileApp.welcome.footerLine1')}
          <br />
          {t('mobileApp.welcome.footerLine2')}
        </p>
      </div>
    </CloudScreen>
  );
}

function EmailEntry({
  email,
  busy,
  locked,
  cooldown,
  errorKey,
  onBack,
  onEmailChange,
  onSubmit,
  onPassword,
}: {
  email: string;
  busy: boolean;
  locked: boolean;
  cooldown: number;
  errorKey: MobileErrorKey | null;
  onBack: () => void;
  onEmailChange: (value: string) => void;
  onSubmit: () => void;
  onPassword: () => void;
}) {
  const { t } = useTranslation();
  const submit = (event: FormEvent) => {
    event.preventDefault();
    onSubmit();
  };
  return (
    <CloudScreen>
      <Brand back disabled={locked} onBack={onBack} />
      <div className="flex flex-col items-center pt-8 pb-6">
        <img src={mailMascot} alt="" className="mb-5 size-28 object-contain" />
        <h1 className="text-center text-3xl font-semibold leading-tight">
          {t('mobileApp.email.title')}
        </h1>
        <p className="mt-3 text-center text-sm leading-6 text-muted">
          {t('mobileApp.email.subtitle')}
        </p>
      </div>
      <form
        onSubmit={submit}
        className="rounded-3xl border border-border bg-surface/70 p-6 shadow-mint-card"
      >
        <div className="flex flex-col gap-6">
          <div className="flex flex-col gap-3">
            <Label htmlFor="mobile-login-email">{t('mobileApp.email.label')}</Label>
            <Input
              id="mobile-login-email"
              type="email"
              inputMode="email"
              autoComplete="email"
              autoCapitalize="none"
              spellCheck={false}
              value={email}
              placeholder={t('mobileApp.email.placeholder')}
              disabled={locked}
              aria-invalid={Boolean(errorKey) || undefined}
              aria-describedby="mobile-login-email-hint mobile-login-email-feedback"
              className="h-14 rounded-2xl bg-surface px-4 text-base shadow-sm"
              onChange={(event) => onEmailChange(event.target.value)}
            />
            <p id="mobile-login-email-hint" className="text-xs leading-6 text-muted">
              {t('mobileApp.email.hint')}
            </p>
            <div id="mobile-login-email-feedback" className="min-h-6">
              {errorKey ? (
                <p role="alert" className="text-xs leading-6 text-destructive-ink">
                  {t(errorKey as TranslationKey)}
                </p>
              ) : null}
            </div>
          </div>
          <div className="flex flex-col gap-3">
            <div aria-busy={busy}>
              <Primary type="submit" disabled={locked || cooldown > 0}>
                {busy
                  ? t('mobileApp.email.sending')
                  : cooldown > 0
                    ? t('mobileApp.email.sendIn', { count: cooldown })
                    : t('mobileApp.email.send')}
              </Primary>
            </div>
            <Button
              type="button"
              variant="link"
              size="compact"
              disabled={locked}
              className={cn(alternative, 'gap-2 self-center')}
              onClick={onPassword}
            >
              <KeyRound className="size-4" />
              {t('mobileApp.password.entry')}
            </Button>
          </div>
        </div>
      </form>
      <p className="mt-auto pt-8 pb-4 text-center text-xs leading-6 text-muted">
        {t('mobileApp.email.footer')}
      </p>
    </CloudScreen>
  );
}

function OtpCells({
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
        className="absolute inset-0 z-10 h-full w-full cursor-text opacity-0"
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
              'grid aspect-square min-w-0 place-items-center rounded-2xl border bg-surface text-2xl font-medium tabular-nums shadow-mint-card-sm transition-colors duration-(--motion-duration-fast)',
              invalid ? 'border-destructive text-destructive-ink' : 'border-border',
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

function CodeEntry({
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
  busy: BusyAction | null;
  locked: boolean;
  errorKey: MobileErrorKey | null;
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
    <CloudScreen>
      <Brand back disabled={locked} onBack={onBack} />
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
    </CloudScreen>
  );
}

// ui/design: mobile-app/two-factor
function SecondFactor({
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
  busy: BusyAction | null;
  locked: boolean;
  errorKey: MobileErrorKey | null;
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
    <CloudScreen>
      <Brand />
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
            {t('mobileApp.twoFactor.passkeyHint')}
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
              className="h-14 rounded-2xl bg-surface px-4 text-base tracking-wide shadow-sm"
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
              className="w-full gap-2 rounded-full shadow-mint-card-sm"
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
    </CloudScreen>
  );
}

// ui/design: mobile-app/password
/**
 * Sign in with the avibe.bot password for the email from the email step. The
 * password lives only in this step's field state: leaving the step unmounts
 * it, and a hidden username field lets the system match a saved password.
 */
function PasswordEntry({
  email,
  busy,
  locked,
  errorKey,
  retryIn,
  codeCooldown,
  onBack,
  onEdit,
  onSubmit,
  onForgot,
  onUseCode,
}: {
  email: string;
  busy: BusyAction | null;
  locked: boolean;
  errorKey: MobileErrorKey | null;
  retryIn: number;
  codeCooldown: boolean;
  onBack: () => void;
  onEdit: () => void;
  onSubmit: (password: string) => void;
  onForgot: () => void;
  onUseCode: () => void;
}) {
  const { t } = useTranslation();
  const [password, setPassword] = useState('');
  const [shown, setShown] = useState(false);
  const { codeActionRegion: actionRegion, revealCodeActions: revealActions } =
    useCodeActionReveal<HTMLFormElement>('mobile-login-password');
  const signingIn = busy === 'signIn';
  const wrong = errorKey === 'mobileApp.password.invalid';
  // The email code is the fix for these, so its way in becomes a button.
  const codeFixes = wrong || errorKey === 'mobileApp.password.unconfirmed';
  const useCodeLabel = busy === 'send' ? t('mobileApp.email.sending') : t('mobileApp.password.useCode');
  const submit = (event: FormEvent) => {
    event.preventDefault();
    onSubmit(password);
  };

  return (
    <CloudScreen>
      <Brand back disabled={locked} onBack={onBack} />
      <div className="flex flex-col items-center pt-8 pb-6">
        <PageMark>
          <KeyRound className="size-7 text-mint-ink" />
        </PageMark>
        <h1 className="text-center text-3xl font-semibold leading-tight tracking-tight">
          {t('mobileApp.password.title')}
        </h1>
      </div>
      <div className="pb-7 text-center">
        <p className="text-sm leading-7 text-muted">{t('mobileApp.password.signingInAs')}</p>
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
      <form
        ref={actionRegion}
        className="scroll-mb-4 rounded-3xl border border-border bg-surface/70 p-6 shadow-mint-card"
        onSubmit={submit}
      >
        <Input
          type="email"
          name="username"
          autoComplete="username"
          value={email}
          readOnly
          tabIndex={-1}
          aria-hidden="true"
          className="sr-only"
        />
        <div className="flex flex-col gap-6">
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
                  'h-14 rounded-2xl bg-surface pr-14 pl-4 text-base shadow-sm',
                  wrong && 'border-destructive',
                )}
                onFocus={revealActions}
                onChange={(event) => {
                  setPassword(event.target.value);
                  onEdit();
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
              {errorKey ? (
                <p role="alert" className="text-xs leading-6 text-destructive-ink">
                  {t(errorKey as TranslationKey)}
                </p>
              ) : null}
            </div>
          </div>
          <div aria-busy={signingIn}>
            <Primary type="submit" disabled={locked || password.length === 0 || retryIn > 0}>
              {signingIn
                ? t('mobileApp.password.signingIn')
                : retryIn > 0
                  ? t('mobileApp.password.retryIn', { count: retryIn })
                  : t('mobileApp.password.signIn')}
            </Primary>
          </div>
        </div>
      </form>
      <div className="mt-6 flex flex-col items-center" aria-busy={busy === 'send'}>
        {codeFixes ? (
          <Button
            type="button"
            variant="outline"
            size="lg"
            disabled={locked || codeCooldown}
            className="w-full gap-2 whitespace-normal rounded-full"
            onClick={onUseCode}
          >
            <Mail className="size-4 shrink-0" />
            {useCodeLabel}
          </Button>
        ) : (
          <Button
            type="button"
            variant="link"
            size="compact"
            disabled={locked || codeCooldown}
            className={cn(alternative, 'gap-2')}
            onClick={onUseCode}
          >
            <Mail className="size-4" />
            {useCodeLabel}
          </Button>
        )}
      </div>
      <p className="mt-auto pt-7 pb-3 text-center text-xs leading-6 text-muted">
        {t('mobileApp.password.footer')}
      </p>
    </CloudScreen>
  );
}

// ui/design: mobile-app/password/forgot
/**
 * Forgot password. Email sign-in always works, so the code comes first; a new
 * password is set on avibe.bot's Account security page, opened in Safari.
 */
function ForgotPassword({
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
  errorKey: MobileErrorKey | null;
  onBack: () => void;
  onSendCode: () => void;
  onOpenSecurity: () => void;
}) {
  const { t } = useTranslation();
  return (
    <CloudScreen>
      <Brand back disabled={locked} onBack={onBack} />
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
          <p className="mt-2 text-sm leading-6 text-muted">{t('mobileApp.password.newPasswordBody')}</p>
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
          {t('mobileApp.password.openInSafari')}
        </Button>
      </section>
    </CloudScreen>
  );
}

// ui/design: mobile-app/password/weak
/**
 * Signed in, but avibe.bot reported the password as weak. Shown once, after any
 * second factor and before the instances; Continue never blocks.
 */
function WeakPasswordNotice({
  email,
  reasons,
  locked,
  onContinue,
  onOpenSecurity,
}: {
  email: string;
  reasons: MobileWeakPasswordReason[];
  locked: boolean;
  onContinue: () => void;
  onOpenSecurity: () => void;
}) {
  const { t } = useTranslation();
  return (
    <CloudScreen>
      <Brand />
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
            {t('mobileApp.password.changeInSafari')}
          </Button>
        </div>
      </div>
    </CloudScreen>
  );
}

function InstanceRow({
  instance,
  current,
  disabled,
  onClick,
}: {
  instance: MobileInstance;
  current?: boolean;
  disabled?: boolean;
  onClick: () => void;
}) {
  const { t } = useTranslation();
  return (
    <button
      type="button"
      disabled={disabled}
      className={cn(
        raised,
        'w-full text-left transition-transform duration-(--motion-duration-instant) active:scale-(--motion-scale-press) disabled:opacity-60',
        current && 'border-mint/50',
      )}
      onClick={onClick}
    >
      <span className="flex items-start gap-3">
        <span className="grid size-11 shrink-0 place-items-center rounded-2xl bg-mint-soft">
          <Laptop className={cn('size-5', instance.available ? 'text-mint-ink' : 'text-muted')} />
        </span>
        <span className="min-w-0 flex-1">
          <span className="block break-words text-base font-semibold">{instance.name}</span>
          <span className="mt-1 block break-all text-xs leading-5 text-muted">{instance.host}</span>
          <span className="mt-3 flex flex-wrap items-center gap-2">
            <Badge variant={instance.available ? 'success' : 'secondary'}>
              {t(instance.available ? 'mobileApp.instances.online' : 'mobileApp.instances.offline')}
            </Badge>
            {instance.relationship === 'shared' ? (
              <Badge variant="info">{t('mobileApp.instances.shared')}</Badge>
            ) : null}
            {current ? (
              <Badge variant="outline">
                <Check className="size-3" />
                {t('mobileApp.instances.current')}
              </Badge>
            ) : null}
          </span>
          <span className="mt-2 block break-words text-xs leading-5 text-muted">
            {instance.relationship === 'shared'
              ? t('mobileApp.instances.sharedBy', {
                  owner: instance.owner_email ?? t('mobileApp.instances.unknownOwner'),
                })
              : t('mobileApp.instances.yours')}
          </span>
        </span>
        <ChevronRight className="mt-3 size-4 shrink-0 text-muted" />
      </span>
    </button>
  );
}

function InstanceSkeleton() {
  const { t } = useTranslation();
  return (
    <SkeletonShimmer
      label={t('mobileApp.instances.loading')}
      className={cn(raised, 'flex gap-3')}
    >
      <Skeleton className="size-11 rounded-2xl" />
      <div className="min-w-0 flex-1">
        <Skeleton className="h-6 w-3/4" />
        <Skeleton className="mt-1 h-5 w-full" />
        <Skeleton className="mt-3 h-6 w-1/3 rounded-full" />
        <Skeleton className="mt-2 h-5 w-1/2" />
      </div>
    </SkeletonShimmer>
  );
}

function Instances({
  session,
  busy,
  locked,
  errorKey,
  offlineSelection,
  onRefresh,
  onSelect,
  onAccount,
  onAdd,
}: {
  session: Extract<MobileSessionState, { status: 'signedIn' }>;
  busy: BusyAction | null;
  locked: boolean;
  errorKey: MobileErrorKey | null;
  offlineSelection: MobileInstance | null;
  onRefresh: () => void;
  onSelect: (instance: MobileInstance) => void;
  onAccount: () => void;
  onAdd: () => void;
}) {
  const { t } = useTranslation();
  const refreshing = busy === 'refresh';
  return (
    <CloudScreen>
      <Brand account disabled={locked} onAccount={onAccount} />
      <div className="flex items-start justify-between gap-3">
        <PageTitle
          title={session.selected_instance_id
            ? t('mobileApp.instances.switchTitle')
            : t('mobileApp.instances.title')}
          subtitle={t('mobileApp.instances.subtitle')}
        />
        <Button
          type="button"
          variant="ghost"
          size="icon"
          className="mt-9 shrink-0 rounded-full"
          disabled={locked}
          aria-label={t('mobileApp.instances.refresh')}
          onClick={onRefresh}
        >
          <RotateCw className={cn('size-4', refreshing && 'animate-spin')} />
        </Button>
      </div>
      {refreshing ? (
        <p role="status" className="mb-4 text-center text-sm text-muted">
          {t('mobileApp.instances.refreshing')}
        </p>
      ) : null}
      {errorKey ? (
        <RecoveryCard errorKey={errorKey} locked={locked} onRetry={onRefresh} />
      ) : (
        <div className="flex flex-col gap-4" aria-busy={refreshing || busy === 'select'}>
          {session.instances.map((instance) => (
            <InstanceRow
              key={instance.id}
              instance={instance}
              current={instance.id === session.selected_instance_id}
              disabled={locked}
              onClick={() => onSelect(instance)}
            />
          ))}
        </div>
      )}
      {offlineSelection ? (
        <div role="status" className={cn(raised, 'mt-4')}>
          <p className="break-words text-sm font-semibold">
            {t('mobileApp.instances.offlineTitle', { name: offlineSelection.name })}
          </p>
          <p className="mt-2 text-xs leading-6 text-muted">
            {t('mobileApp.instances.offlineBody')}
          </p>
        </div>
      ) : null}
      <div className="mt-auto pt-8 pb-3">
        <Button
          type="button"
          variant="outline"
          size="lg"
          disabled={locked}
          className="w-full gap-2 rounded-full"
          onClick={onAdd}
        >
          <Plus className="size-4" />
          {t('mobileApp.instances.add')}
        </Button>
      </div>
    </CloudScreen>
  );
}

function EmptyInstances({
  cli,
  busy,
  locked,
  errorKey,
  onBack,
  onAccount,
  onRefresh,
  onShowCli,
  onOpenGuide,
}: {
  cli: boolean;
  busy: boolean;
  locked: boolean;
  errorKey: MobileErrorKey | null;
  onBack: () => void;
  onAccount: () => void;
  onRefresh: () => void;
  onShowCli: () => void;
  onOpenGuide: () => void;
}) {
  const { t } = useTranslation();
  return (
    <CloudScreen>
      <Brand
        back={cli}
        account={!cli}
        disabled={locked}
        onBack={onBack}
        onAccount={onAccount}
      />
      <div className="pt-7">
        <span className="grid size-16 place-items-center rounded-3xl border border-border bg-surface/80 shadow-mint-card-sm">
          <Laptop className="size-7 text-mint-ink" />
        </span>
      </div>
      <PageTitle
        title={t(cli ? 'mobileApp.empty.cliTitle' : 'mobileApp.empty.title')}
        subtitle={t(cli ? 'mobileApp.empty.cliSubtitle' : 'mobileApp.empty.subtitle')}
      />
      {errorKey ? (
        <div className="mb-6">
          <RecoveryCard errorKey={errorKey} locked={locked} onRetry={onRefresh} />
        </div>
      ) : null}
      {cli ? (
        <div className={cn(raised, 'flex flex-col gap-4')}>
          <p className="text-sm leading-6 text-muted">{t('mobileApp.empty.cliBody')}</p>
          <h2 className="text-sm font-semibold">{t('mobileApp.empty.stepInstall')}</h2>
          <code className="break-all rounded-xl bg-surface-2 p-3 font-mono text-xs leading-6">
            {t('mobileApp.empty.installCommand')}
          </code>
          <h2 className="text-sm font-semibold">{t('mobileApp.empty.stepPair')}</h2>
          <code className="rounded-xl bg-surface-2 p-3 font-mono text-xs">
            {t('mobileApp.empty.pairCommand')}
          </code>
          <p className="text-xs leading-6 text-muted">{t('mobileApp.empty.pairBody')}</p>
          <Button
            type="button"
            variant="link"
            disabled={locked}
            className="self-start px-0"
            onClick={onOpenGuide}
          >
            {t('mobileApp.empty.guide')}
            <ExternalLink className="size-4" />
          </Button>
        </div>
      ) : (
        <>
          <p className="mb-6 text-sm leading-7 text-muted">{t('mobileApp.empty.body')}</p>
          <div className="flex flex-col gap-4">
            <button
              type="button"
              disabled={locked}
              className={cn(
                raised,
                'flex items-start gap-3 text-left disabled:opacity-60',
              )}
              onClick={onOpenGuide}
            >
              <span className="grid size-11 shrink-0 place-items-center rounded-2xl bg-mint-soft">
                <MonitorDown className="size-5 text-mint-ink" />
              </span>
              <span className="min-w-0 flex-1">
                <span className="block text-sm font-semibold">{t('mobileApp.empty.desktopTitle')}</span>
                <span className="mt-2 block text-xs leading-6 text-muted">{t('mobileApp.empty.desktopBody')}</span>
              </span>
              <ExternalLink className="mt-2 size-4 shrink-0 text-muted" />
            </button>
            <button
              type="button"
              disabled={locked}
              className={cn(
                raised,
                'flex items-start gap-3 text-left disabled:opacity-60',
              )}
              onClick={onShowCli}
            >
              <span className="grid size-11 shrink-0 place-items-center rounded-2xl bg-mint-soft">
                <Laptop className="size-5 text-mint-ink" />
              </span>
              <span className="min-w-0 flex-1">
                <span className="block text-sm font-semibold">{t('mobileApp.empty.commandTitle')}</span>
                <span className="mt-2 block text-xs leading-6 text-muted">{t('mobileApp.empty.commandBody')}</span>
              </span>
              <ChevronRight className="mt-2 size-4 shrink-0 text-muted" />
            </button>
          </div>
        </>
      )}
      <div className="mt-auto pt-7 pb-3">
        <Button
          type="button"
          variant="outline"
          size="lg"
          disabled={locked}
          className="w-full whitespace-normal rounded-full"
          onClick={onRefresh}
        >
          <RotateCw className={cn('size-4 shrink-0', busy && 'animate-spin')} />
          {t('mobileApp.empty.refresh')}
        </Button>
      </div>
    </CloudScreen>
  );
}

function Account({
  session,
  busy,
  locked,
  errorKey,
  onBack,
  onSwitch,
  onLogout,
}: {
  session: Extract<MobileSessionState, { status: 'signedIn' }>;
  busy: boolean;
  locked: boolean;
  errorKey: MobileErrorKey | null;
  onBack: () => void;
  onSwitch: () => void;
  onLogout: () => void;
}) {
  const { t } = useTranslation();
  const current = session.instances.find((instance) => instance.id === session.selected_instance_id);
  return (
    <CloudScreen>
      <Brand back disabled={locked} onBack={onBack} />
      <PageTitle title={t('mobileApp.account.title')} />
      <section className={cn(raised, 'p-6')}>
        <p className="text-xs text-muted">{t('mobileApp.account.signedInAs')}</p>
        <p className="mt-2 break-all text-base font-semibold leading-6">{session.account.email}</p>
        <Button
          type="button"
          variant="outline"
          className="mt-5 w-full rounded-full"
          disabled={locked}
          onClick={onLogout}
        >
          {busy ? t('mobileApp.account.signingOut') : t('mobileApp.account.signOut')}
        </Button>
      </section>
      {errorKey ? (
        <div className="mt-6">
          <RecoveryCard errorKey={errorKey} locked={locked} onRetry={onLogout} />
        </div>
      ) : null}
      <section className="mt-8">
        <h2 className="mb-4 text-sm font-semibold">{t('mobileApp.account.current')}</h2>
        {current ? (
          <InstanceRow
            instance={current}
            current
            disabled={locked}
            onClick={onSwitch}
          />
        ) : (
          <div className={cn(raised, 'py-8 text-center text-sm text-muted')}>
            {t('mobileApp.account.none')}
          </div>
        )}
        <Button
          type="button"
          variant="outline"
          size="lg"
          disabled={locked}
          className="mt-4 w-full justify-between rounded-full"
          onClick={onSwitch}
        >
          {t(current ? 'mobileApp.account.switch' : 'mobileApp.account.choose')}
          <ChevronRight className="size-4" />
        </Button>
      </section>
    </CloudScreen>
  );
}

function Boot() {
  const { t } = useTranslation();
  return (
    <CloudScreen>
      <Brand account disabled />
      <PageTitle
        title={t('mobileApp.instances.title')}
        subtitle={t('mobileApp.instances.subtitle')}
      />
      <div className="flex flex-col gap-4">
        <InstanceSkeleton />
        <InstanceSkeleton />
      </div>
    </CloudScreen>
  );
}

function BootRecovery({
  errorKey,
  locked,
  onRetry,
}: {
  errorKey: MobileErrorKey;
  locked: boolean;
  onRetry: () => void;
}) {
  const { t } = useTranslation();
  return (
    <CloudScreen>
      <Brand account disabled />
      <PageTitle
        title={t('mobileApp.instances.title')}
        subtitle={t('mobileApp.instances.subtitle')}
      />
      <RecoveryCard errorKey={errorKey} locked={locked} onRetry={onRetry} />
    </CloudScreen>
  );
}

export function MobileOnboarding() {
  const { i18n } = useTranslation();
  const startsOnAccount = useRef(
    window.location.hash === '#account' || window.location.hash === RUNTIME_ACCOUNT_HASH,
  );
  // Account Back returns to the instance this page covers while native still
  // holds it; otherwise it shows the instance list.
  const coversInstance = useRef(window.location.hash === RUNTIME_ACCOUNT_HASH);
  // avibe.bot's second-factor throttle window; a completed code waits for the button.
  const secondFactorRetryAt = useRef(0);
  const [screen, setScreen] = useState<Screen>('boot');
  const [session, setSession] = useState<MobileSessionState>({ status: 'signedOut' });
  const [email, setEmail] = useState('');
  const [code, setCode] = useState('');
  const [errorKey, setErrorKey] = useState<MobileErrorKey | null>(null);
  const [cooldown, setCooldown] = useState(0);
  const [cooldownSource, setCooldownSource] = useState<CooldownSource>(null);
  const [cooldownScope, setCooldownScope] = useState<CooldownScope>(null);
  const [resent, setResent] = useState(false);
  const [offlineSelection, setOfflineSelection] = useState<MobileInstance | null>(null);
  const [factorStep, setFactorStep] = useState<SecondFactorStep>('passkey');
  const [factorCode, setFactorCode] = useState('');
  const [factorRetryMinutes, setFactorRetryMinutes] = useState(SECOND_FACTOR_RETRY_MINUTES);
  // Seconds before Sign in is offered again after a password rate limit.
  const [passwordRetry, setPasswordRetry] = useState(0);
  // A password sign-in avibe.bot reported as weak, kept through any second
  // factor until its one notice.
  const pendingWeakPassword = useRef<MobileWeakPasswordReason[] | null>(null);
  const [weakPasswordReasons, setWeakPasswordReasons] = useState<MobileWeakPasswordReason[]>([]);
  const {
    beginOperation,
    busy,
    finishOperation,
    isOperationLocked,
    locked,
    ownsOperation,
  } = useOperationOwner();

  const signedIn = session.status === 'signedIn' ? session : null;
  const owedSecondFactor = session.status === 'twoFactorRequired' ? session : null;
  const providerCooldownActive = cooldownScope === 'all' && cooldown > 0;
  const accountReturnScreen = useMemo(
    () => (signedIn?.instances.length ? 'instances' : 'empty') as Screen,
    [signedIn],
  );

  // Setters only, so the bootstrap callback can use it without a dependency.
  function showSecondFactorStart(next: MobileSessionState) {
    if (next.status !== 'twoFactorRequired') return;
    setFactorStep(firstSecondFactorStep(next));
    setFactorCode('');
  }

  const bootstrap = useCallback(async () => {
    const token = beginOperation('boot');
    if (token === null) return;
    setErrorKey(null);
    setScreen('boot');
    try {
      const next = await bootstrapMobileSession();
      if (!ownsOperation(token)) return;
      showSecondFactorStart(next);
      setSession(next);
      setScreen(
        startsOnAccount.current && next.status === 'signedIn'
          ? 'account'
          : screenForSession(next),
      );
      startsOnAccount.current = false;
    } catch (rawError) {
      if (!ownsOperation(token)) return;
      setErrorKey(pageErrorKey(normalizeMobileBridgeError(rawError), 'bootstrap'));
      setScreen('bootError');
    } finally {
      finishOperation(token);
    }
  }, [beginOperation, finishOperation, ownsOperation]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      void bootstrap();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [bootstrap]);

  useEffect(() => {
    if (cooldown <= 0) return;
    const timer = window.setTimeout(() => setCooldown((value) => Math.max(0, value - 1)), 1000);
    return () => window.clearTimeout(timer);
  }, [cooldown]);

  useEffect(() => {
    if (passwordRetry <= 0) return;
    const timer = window.setTimeout(
      () => setPasswordRetry((value) => Math.max(0, value - 1)),
      1000,
    );
    return () => window.clearTimeout(timer);
  }, [passwordRetry]);

  /** Where a session lands: a pending weak-password notice comes before the instances. */
  const screenAfterSession = (next: MobileSessionState): Screen => {
    if (next.status === 'signedOut') {
      pendingWeakPassword.current = null;
    } else if (next.status === 'signedIn' && pendingWeakPassword.current) {
      setWeakPasswordReasons(pendingWeakPassword.current);
      pendingWeakPassword.current = null;
      return 'weakPassword';
    }
    return screenForSession(next);
  };

  const applySession = (
    token: OperationToken,
    next: MobileSessionState,
    allowSignedOut: boolean,
  ) => {
    if (!ownsOperation(token)) return;
    if (next.status === 'signedOut' && !allowSignedOut) {
      setCode('');
      setErrorKey('mobileApp.errors.network');
      return;
    }
    showSecondFactorStart(next);
    setSession(next);
    setScreen(screenAfterSession(next));
    setErrorKey(null);
    setOfflineSelection(null);
  };

  const requestOtp = async (resend: boolean) => {
    if (cooldown > 0) return;
    const normalizedEmail = email.trim();
    if (!EMAIL_PATTERN.test(normalizedEmail)) {
      setErrorKey('mobileApp.email.invalid');
      return;
    }
    const token = beginOperation(resend ? 'resend' : 'send');
    if (token === null) return;
    setEmail(normalizedEmail);
    setErrorKey(null);
    setResent(false);
    pendingWeakPassword.current = null;
    try {
      const result = await requestMobileOtp(normalizedEmail);
      if (!ownsOperation(token)) return;
      const providerCooldown = Math.max(0, result.cooldown_seconds ?? 0);
      if (providerCooldown > 0) {
        setCooldown(providerCooldown);
        setCooldownSource('provider');
        setCooldownScope('send');
      } else if (resend) {
        setCooldown(DEFAULT_COOLDOWN_SECONDS);
        setCooldownSource('client');
        setCooldownScope('send');
      } else {
        setCooldown(0);
        setCooldownSource(null);
        setCooldownScope(null);
      }
      setResent(resend);
      // A new email sign-in owes its own second factor.
      secondFactorRetryAt.current = 0;
      setScreen('code');
    } catch (rawError) {
      if (!ownsOperation(token)) return;
      const error = normalizeMobileBridgeError(rawError);
      setErrorKey(pageErrorKey(error, 'request'));
      const providerCooldown = providerCooldownSeconds(error);
      if (providerCooldown > 0) {
        setCooldown(providerCooldown);
        setCooldownSource('provider');
        setCooldownScope('all');
      } else if (resend) {
        setCooldown(FAILED_COOLDOWN_SECONDS);
        setCooldownSource('client');
        setCooldownScope('send');
      }
    } finally {
      finishOperation(token);
    }
  };

  const verifyOtp = async (candidate: string = code) => {
    if (providerCooldownActive || !CODE_PATTERN.test(candidate)) return;
    const token = beginOperation('verify');
    if (token === null) return;
    setErrorKey(null);
    try {
      const next = await verifyMobileOtp(email, candidate);
      applySession(token, next, false);
    } catch (rawError) {
      if (!ownsOperation(token)) return;
      const error = normalizeMobileBridgeError(rawError);
      const nextErrorKey = pageErrorKey(error, 'verify');
      setErrorKey(nextErrorKey);
      if (nextErrorKey === 'mobileApp.errors.network') setCode('');
      const providerCooldown = providerCooldownSeconds(error);
      if (providerCooldown > 0) {
        setCooldown(providerCooldown);
        setCooldownSource('provider');
        setCooldownScope('all');
      }
    } finally {
      finishOperation(token);
    }
  };

  const signInWithPassword = async (password: string) => {
    if (passwordRetry > 0 || password.length === 0) return;
    const token = beginOperation('signIn');
    if (token === null) return;
    setErrorKey(null);
    pendingWeakPassword.current = null;
    try {
      const { weak_password: weakPassword, ...next } = await signInMobileWithPassword(
        email,
        password,
      );
      if (!ownsOperation(token)) return;
      if (next.status !== 'signedOut' && weakPassword) {
        pendingWeakPassword.current = weakPassword.reasons;
      }
      applySession(token, next as MobileSessionState, false);
    } catch (rawError) {
      if (!ownsOperation(token)) return;
      const error = normalizeMobileBridgeError(rawError);
      setErrorKey(pageErrorKey(error, 'password'));
      if (error.code === 'rate_limited') {
        setPasswordRetry(
          typeof error.retry_after_seconds === 'number' && error.retry_after_seconds > 0
            ? Math.ceil(error.retry_after_seconds)
            : PASSWORD_RETRY_SECONDS,
        );
      }
    } finally {
      finishOperation(token);
    }
  };

  const refresh = async () => {
    const token = beginOperation('refresh');
    if (token === null) return;
    setErrorKey(null);
    try {
      const next = await refreshMobileSession();
      applySession(token, next, true);
    } catch (rawError) {
      if (!ownsOperation(token)) return;
      const error = normalizeMobileBridgeError(rawError);
      setErrorKey(pageErrorKey(error, 'refresh'));
    } finally {
      finishOperation(token);
    }
  };

  const recordSecondFactorFailure = (error: MobileBridgeError) => {
    if (error.code === 'rate_limited') {
      secondFactorRetryAt.current = Date.now() + secondFactorRetrySeconds(error) * 1000;
    }
    setFactorRetryMinutes(secondFactorRetryMinutes(error));
  };

  const verifySecondFactorCode = async (candidate: string = factorCode) => {
    if (factorStep !== 'totp' && factorStep !== 'recovery') return;
    const method = factorStep;
    if (method === 'totp' ? !CODE_PATTERN.test(candidate) : candidate.trim().length === 0) {
      return;
    }
    const token = beginOperation('verify');
    if (token === null) return;
    setErrorKey(null);
    try {
      const next = await verifyMobileSecondFactor(method, candidate);
      applySession(token, next, true);
    } catch (rawError) {
      if (!ownsOperation(token)) return;
      const error = normalizeMobileBridgeError(rawError);
      recordSecondFactorFailure(error);
      setErrorKey(pageErrorKey(error, method));
    } finally {
      finishOperation(token);
    }
  };

  const verifyPasskey = async () => {
    const token = beginOperation('passkey');
    if (token === null) return;
    setErrorKey(null);
    try {
      const next = await verifyMobilePasskey();
      applySession(token, next, true);
    } catch (rawError) {
      if (!ownsOperation(token)) return;
      const error = normalizeMobileBridgeError(rawError);
      recordSecondFactorFailure(error);
      setErrorKey(pageErrorKey(error, 'passkey'));
    } finally {
      finishOperation(token);
    }
  };

  const select = async (instance: MobileInstance) => {
    if (isOperationLocked()) return;
    setOfflineSelection(null);
    if (!instance.available) {
      setOfflineSelection(instance);
      return;
    }
    const token = beginOperation('select');
    if (token === null) return;
    setErrorKey(null);
    try {
      await selectMobileInstance(instance.id);
    } catch (rawError) {
      if (!ownsOperation(token)) return;
      const error = normalizeMobileBridgeError(rawError);
      setErrorKey(pageErrorKey(error, 'select'));
    } finally {
      finishOperation(token);
    }
  };

  const logout = async () => {
    const token = beginOperation('logout');
    if (token === null) return;
    setErrorKey(null);
    try {
      const next = await logoutMobileSession();
      if (!ownsOperation(token)) return;
      if (next.status !== 'signedOut') {
        setErrorKey('mobileApp.errors.network');
        return;
      }
      setSession(next);
      pendingWeakPassword.current = null;
      setEmail('');
      setCode('');
      setCooldown(0);
      setCooldownSource(null);
      setCooldownScope(null);
      setScreen('welcome');
    } catch (rawError) {
      if (!ownsOperation(token)) return;
      const error = normalizeMobileBridgeError(rawError);
      setErrorKey(pageErrorKey(error, 'logout'));
    } finally {
      finishOperation(token);
    }
  };

  const leaveAccount = async () => {
    if (!coversInstance.current) {
      if (isOperationLocked()) return;
      setErrorKey(null);
      setScreen(accountReturnScreen);
      return;
    }
    const token = beginOperation('return');
    if (token === null) return;
    setErrorKey(null);
    try {
      // Native shows the covered instance again and removes this page.
      await returnToMobileInstance();
    } catch {
      if (!ownsOperation(token)) return;
      // A switch, refresh, or sign-out closed it; nothing is behind the page.
      coversInstance.current = false;
      setScreen(accountReturnScreen);
    } finally {
      finishOperation(token);
    }
  };

  const showEmail = () => {
    if (isOperationLocked()) return;
    setCode('');
    setErrorKey(null);
    setResent(false);
    if (cooldownSource === 'client') {
      setCooldown(0);
      setCooldownSource(null);
      setCooldownScope(null);
    }
    setScreen('email');
  };

  /** Both ways in validate the email first. */
  const showPassword = () => {
    if (isOperationLocked()) return;
    const normalizedEmail = email.trim();
    if (!EMAIL_PATTERN.test(normalizedEmail)) {
      setErrorKey('mobileApp.email.invalid');
      return;
    }
    setEmail(normalizedEmail);
    setErrorKey(null);
    setScreen('password');
  };

  const showPasswordStep = (next: 'password' | 'forgot') => {
    if (isOperationLocked()) return;
    setErrorKey(null);
    setScreen(next);
  };

  const openGuide = () => {
    if (isOperationLocked()) return;
    void openMobileExternal(installGuideUrl(i18n.language));
  };

  const openAccountSecurity = () => {
    if (isOperationLocked()) return;
    void openMobileExternal(ACCOUNT_SECURITY_URL);
  };

  if (screen === 'boot') return <Boot />;
  if (screen === 'bootError') {
    return (
      <BootRecovery
        errorKey={errorKey ?? 'mobileApp.errors.network'}
        locked={locked}
        onRetry={() => void bootstrap()}
      />
    );
  }
  if (screen === 'welcome') return <Welcome locked={locked} onContinue={showEmail} />;
  if (screen === 'email') {
    return (
      <EmailEntry
        email={email}
        busy={busy === 'send'}
        locked={locked}
        cooldown={cooldown}
        errorKey={errorKey}
        onBack={() => {
          if (!isOperationLocked()) {
            setErrorKey(null);
            setScreen('welcome');
          }
        }}
        onEmailChange={(value) => {
          if (isOperationLocked()) return;
          setEmail(value);
          setErrorKey(null);
        }}
        onSubmit={() => void requestOtp(false)}
        onPassword={showPassword}
      />
    );
  }
  if (screen === 'password') {
    return (
      <PasswordEntry
        email={email}
        busy={busy}
        locked={locked}
        errorKey={errorKey}
        retryIn={passwordRetry}
        codeCooldown={cooldown > 0}
        onBack={showEmail}
        onEdit={() => setErrorKey(null)}
        onSubmit={(password) => void signInWithPassword(password)}
        onForgot={() => showPasswordStep('forgot')}
        onUseCode={() => void requestOtp(false)}
      />
    );
  }
  if (screen === 'forgot') {
    return (
      <ForgotPassword
        email={email}
        busy={busy === 'send'}
        locked={locked}
        cooldown={cooldown}
        errorKey={errorKey}
        onBack={() => showPasswordStep('password')}
        onSendCode={() => void requestOtp(false)}
        onOpenSecurity={openAccountSecurity}
      />
    );
  }
  if (screen === 'code') {
    return (
      <CodeEntry
        email={email}
        code={code}
        busy={busy}
        locked={locked}
        errorKey={errorKey}
        cooldown={cooldown}
        providerCooldownActive={providerCooldownActive}
        resent={resent}
        onBack={showEmail}
        onCodeChange={(value) => {
          if (isOperationLocked()) return;
          const complete = completesCode(value, code);
          setCode(value);
          setErrorKey(null);
          if (complete) void verifyOtp(value);
        }}
        onVerify={() => void verifyOtp()}
        onResend={() => void requestOtp(true)}
      />
    );
  }
  if (screen === 'secondFactor' && owedSecondFactor) {
    return (
      <SecondFactor
        session={owedSecondFactor}
        step={factorStep}
        code={factorCode}
        busy={busy}
        locked={locked}
        errorKey={errorKey}
        retryMinutes={factorRetryMinutes}
        onStep={(step) => {
          if (isOperationLocked()) return;
          setFactorStep(step);
          setFactorCode('');
          setErrorKey(null);
        }}
        onCodeChange={(value) => {
          if (isOperationLocked()) return;
          // A recovery code is free text and waits for the button.
          const complete = factorStep === 'totp'
            && completesCode(value, factorCode)
            && Date.now() >= secondFactorRetryAt.current;
          setFactorCode(value);
          setErrorKey(null);
          if (complete) void verifySecondFactorCode(value);
        }}
        onVerify={() => void verifySecondFactorCode()}
        onPasskey={() => void verifyPasskey()}
        onOpenSecurity={openAccountSecurity}
        onSetupDone={() => void refresh()}
        onDifferentAccount={() => void logout()}
      />
    );
  }
  if (!signedIn) {
    return <Welcome locked={locked} onContinue={showEmail} />;
  }
  if (screen === 'weakPassword') {
    return (
      <WeakPasswordNotice
        email={signedIn.account.email}
        reasons={weakPasswordReasons}
        locked={locked}
        onContinue={() => {
          if (!isOperationLocked()) setScreen(screenForSession(signedIn));
        }}
        onOpenSecurity={openAccountSecurity}
      />
    );
  }
  if (screen === 'account') {
    return (
      <Account
        session={signedIn}
        busy={busy === 'logout'}
        locked={locked}
        errorKey={errorKey}
        onBack={() => void leaveAccount()}
        onSwitch={() => {
          if (!isOperationLocked()) {
            setErrorKey(null);
            setScreen(signedIn.instances.length ? 'instances' : 'empty');
          }
        }}
        onLogout={() => void logout()}
      />
    );
  }
  if (screen === 'empty' || screen === 'cli') {
    return (
      <EmptyInstances
        cli={screen === 'cli'}
        busy={busy === 'refresh'}
        locked={locked}
        errorKey={errorKey}
        onBack={() => {
          if (!isOperationLocked()) {
            setErrorKey(null);
            setScreen('empty');
          }
        }}
        onAccount={() => {
          if (!isOperationLocked()) {
            setErrorKey(null);
            setScreen('account');
          }
        }}
        onRefresh={() => void refresh()}
        onShowCli={() => {
          if (!isOperationLocked()) {
            setErrorKey(null);
            setScreen('cli');
          }
        }}
        onOpenGuide={openGuide}
      />
    );
  }
  return (
    <Instances
      session={signedIn}
      busy={busy}
      locked={locked}
      errorKey={errorKey}
      offlineSelection={offlineSelection}
      onRefresh={() => void refresh()}
      onSelect={(instance) => void select(instance)}
      onAccount={() => {
        if (!isOperationLocked()) {
          setErrorKey(null);
          setScreen('account');
        }
      }}
      onAdd={() => {
        if (!isOperationLocked()) {
          setErrorKey(null);
          setScreen('empty');
        }
      }}
    />
  );
}
