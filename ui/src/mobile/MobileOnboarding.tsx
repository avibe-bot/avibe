import {
  AlertTriangle,
  ArrowLeft,
  ArrowRight,
  Check,
  ChevronRight,
  ExternalLink,
  Laptop,
  MonitorDown,
  Plus,
  RotateCw,
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
  bootstrapMobileSession,
  logoutMobileSession,
  normalizeMobileBridgeError,
  openMobileExternal,
  refreshMobileSession,
  requestMobileOtp,
  selectMobileInstance,
  type MobileBridgeError,
  type MobileInstance,
  type MobileSessionState,
  verifyMobileOtp,
} from './bridge';

type Screen =
  | 'boot'
  | 'welcome'
  | 'email'
  | 'code'
  | 'instances'
  | 'empty'
  | 'cli'
  | 'account';

type BusyAction = 'boot' | 'send' | 'verify' | 'resend' | 'refresh' | 'select' | 'logout';
type CooldownSource = 'client' | 'provider' | null;
type MobileErrorKey =
  | 'mobileApp.email.invalid'
  | 'mobileApp.code.invalid'
  | 'mobileApp.code.rateLimited'
  | 'mobileApp.errors.network';

const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const DEFAULT_COOLDOWN_SECONDS = 60;
const FAILED_COOLDOWN_SECONDS = 15;

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
  return session.instances.length === 0 ? 'empty' : 'instances';
};

const providerErrorKey = (
  error: MobileBridgeError,
  fallback: 'network' | 'code',
): MobileErrorKey => {
  switch (error.code) {
  case 'invalid_email':
    return 'mobileApp.email.invalid';
  case 'invalid_or_expired':
    return 'mobileApp.code.invalid';
  case 'rate_limited':
    return 'mobileApp.code.rateLimited';
  case 'network':
  case 'unavailable':
    return 'mobileApp.errors.network';
  default:
    return fallback === 'code' ? 'mobileApp.code.invalid' : 'mobileApp.errors.network';
  }
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
}: {
  email: string;
  busy: boolean;
  locked: boolean;
  cooldown: number;
  errorKey: MobileErrorKey | null;
  onBack: () => void;
  onEmailChange: (value: string) => void;
  onSubmit: () => void;
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
          <div aria-busy={busy}>
            <Primary type="submit" disabled={locked || cooldown > 0}>
              {busy
                ? t('mobileApp.email.sending')
                : cooldown > 0
                  ? t('mobileApp.email.sendIn', { count: cooldown })
                  : t('mobileApp.email.send')}
            </Primary>
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
  invalid,
  busy,
  onChange,
}: {
  code: string;
  invalid: boolean;
  busy: boolean;
  onChange: (value: string) => void;
}) {
  const { t } = useTranslation();
  const input = useRef<HTMLInputElement>(null);
  return (
    <div className="relative rounded-2xl focus-within:ring-2 focus-within:ring-mint/40">
      <Label htmlFor="mobile-login-code" className="sr-only">
        {t('mobileApp.code.label')}
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

function CodeEntry({
  email,
  code,
  busy,
  locked,
  errorKey,
  cooldown,
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
  resent: boolean;
  onBack: () => void;
  onCodeChange: (value: string) => void;
  onVerify: () => void;
  onResend: () => void;
}) {
  const { t } = useTranslation();
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
      <OtpCells
        code={code}
        invalid={errorKey === 'mobileApp.code.invalid'}
        busy={locked}
        onChange={onCodeChange}
      />
      <p id="mobile-login-code-hint" className="mt-5 text-center text-sm leading-6 text-muted">
        {t('mobileApp.code.hint')}
      </p>
      <div id="mobile-login-code-feedback" className="mt-3 min-h-6 text-center text-xs leading-6">
        {errorKey ? (
          <p role="alert" className="text-destructive-ink">{t(errorKey as TranslationKey)}</p>
        ) : resent ? (
          <p role="status" className="text-mint-ink">{t('mobileApp.code.resent')}</p>
        ) : null}
      </div>
      <div className="mt-5" aria-busy={busy === 'verify'}>
        <Primary
          disabled={locked || code.length !== 6}
          onClick={onVerify}
        >
          {busy === 'verify' ? t('mobileApp.code.verifying') : t('mobileApp.code.verify')}
        </Primary>
      </div>
      <div className="mt-6 text-center text-sm leading-7">
        <p className="text-muted">{t('mobileApp.code.resendPrompt')}</p>
        <Button
          type="button"
          variant="link"
          size="compact"
          disabled={locked || cooldown > 0}
          className="h-auto max-w-full whitespace-normal px-0 text-sm text-foreground underline"
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
        <div className={cn(raised, 'py-8 text-center')}>
          <AlertTriangle className="mx-auto mb-4 size-7 text-gold-ink" />
          <p className="text-base font-semibold">{t('mobileApp.instances.unavailable')}</p>
          <p className="mt-3 text-sm leading-6 text-muted">{t(errorKey as TranslationKey)}</p>
          <Button
            type="button"
            variant="outline"
            disabled={locked}
            className="mt-5 rounded-full"
            onClick={onRefresh}
          >
            <RotateCw className="size-4" />
            {t('common.retry')}
          </Button>
        </div>
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
  onBack,
  onAccount,
  onRefresh,
  onShowCli,
  onOpenGuide,
}: {
  cli: boolean;
  busy: boolean;
  locked: boolean;
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
  onBack,
  onSwitch,
  onLogout,
}: {
  session: Extract<MobileSessionState, { status: 'signedIn' }>;
  busy: boolean;
  locked: boolean;
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

export function MobileOnboarding() {
  const { i18n } = useTranslation();
  const startsOnAccount = useRef(window.location.hash === '#account');
  const [screen, setScreen] = useState<Screen>('boot');
  const [session, setSession] = useState<MobileSessionState>({ status: 'signedOut' });
  const [email, setEmail] = useState('');
  const [code, setCode] = useState('');
  const [errorKey, setErrorKey] = useState<MobileErrorKey | null>(null);
  const [cooldown, setCooldown] = useState(0);
  const [cooldownSource, setCooldownSource] = useState<CooldownSource>(null);
  const [resent, setResent] = useState(false);
  const [offlineSelection, setOfflineSelection] = useState<MobileInstance | null>(null);
  const {
    beginOperation,
    busy,
    finishOperation,
    isOperationLocked,
    locked,
    ownsOperation,
  } = useOperationOwner();

  const signedIn = session.status === 'signedIn' ? session : null;
  const accountReturnScreen = useMemo(
    () => (signedIn?.instances.length ? 'instances' : 'empty') as Screen,
    [signedIn],
  );

  useEffect(() => {
    let active = true;
    const token = beginOperation('boot');
    if (token === null) return () => {
      active = false;
    };
    void bootstrapMobileSession()
      .then((next) => {
        if (!active || !ownsOperation(token)) return;
        setSession(next);
        setScreen(
          startsOnAccount.current && next.status === 'signedIn'
            ? 'account'
            : screenForSession(next),
        );
        startsOnAccount.current = false;
      })
      .catch(() => {
        if (!active || !ownsOperation(token)) return;
        setErrorKey('mobileApp.errors.network');
        setScreen('welcome');
      })
      .finally(() => {
        if (active) finishOperation(token);
      });
    return () => {
      active = false;
    };
  }, [beginOperation, finishOperation, ownsOperation]);

  useEffect(() => {
    if (cooldown <= 0) return;
    const timer = window.setTimeout(() => setCooldown((value) => Math.max(0, value - 1)), 1000);
    return () => window.clearTimeout(timer);
  }, [cooldown]);

  const applySignedInSession = (
    token: OperationToken,
    next: MobileSessionState,
  ) => {
    if (!ownsOperation(token)) return;
    setSession(next);
    setScreen(screenForSession(next));
    setErrorKey(null);
    setOfflineSelection(null);
  };

  const requestOtp = async (resend: boolean) => {
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
    try {
      const result = await requestMobileOtp(normalizedEmail);
      if (!ownsOperation(token)) return;
      const providerCooldown = Math.max(0, result.cooldown_seconds ?? 0);
      if (providerCooldown > 0) {
        setCooldown(providerCooldown);
        setCooldownSource('provider');
      } else if (resend) {
        setCooldown(DEFAULT_COOLDOWN_SECONDS);
        setCooldownSource('client');
      } else {
        setCooldown(0);
        setCooldownSource(null);
      }
      setResent(resend);
      setScreen('code');
    } catch (rawError) {
      if (!ownsOperation(token)) return;
      const error = normalizeMobileBridgeError(rawError);
      if (error.code === 'busy') return;
      setErrorKey(providerErrorKey(error, 'network'));
      if (error.retry_after_seconds) {
        setCooldown(error.retry_after_seconds);
        setCooldownSource('provider');
      } else if (resend) {
        setCooldown(FAILED_COOLDOWN_SECONDS);
        setCooldownSource('client');
      }
    } finally {
      finishOperation(token);
    }
  };

  const verifyOtp = async () => {
    if (!/^\d{6}$/.test(code)) return;
    const token = beginOperation('verify');
    if (token === null) return;
    setErrorKey(null);
    try {
      const next = await verifyMobileOtp(email, code);
      applySignedInSession(token, next);
    } catch (rawError) {
      if (!ownsOperation(token)) return;
      const error = normalizeMobileBridgeError(rawError);
      if (error.code === 'busy') return;
      setErrorKey(providerErrorKey(error, 'code'));
      if (error.code === 'network' || error.code === 'unavailable') setCode('');
      if (error.retry_after_seconds) setCooldown(error.retry_after_seconds);
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
      applySignedInSession(token, next);
    } catch (rawError) {
      if (!ownsOperation(token)) return;
      const error = normalizeMobileBridgeError(rawError);
      if (error.code === 'busy') return;
      setErrorKey('mobileApp.errors.network');
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
      if (error.code === 'busy') return;
      setErrorKey('mobileApp.errors.network');
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
      setSession(next);
      setEmail('');
      setCode('');
      setCooldown(0);
      setCooldownSource(null);
      setScreen('welcome');
    } catch (rawError) {
      if (!ownsOperation(token)) return;
      const error = normalizeMobileBridgeError(rawError);
      if (error.code === 'busy') return;
      setErrorKey('mobileApp.errors.network');
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
    }
    setScreen('email');
  };

  const openGuide = () => {
    if (isOperationLocked()) return;
    void openMobileExternal(installGuideUrl(i18n.language));
  };

  if (screen === 'boot') return <Boot />;
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
          if (!isOperationLocked()) setScreen('welcome');
        }}
        onEmailChange={(value) => {
          if (isOperationLocked()) return;
          setEmail(value);
          setErrorKey(null);
        }}
        onSubmit={() => void requestOtp(false)}
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
        resent={resent}
        onBack={showEmail}
        onCodeChange={(value) => {
          if (isOperationLocked()) return;
          setCode(value);
          setErrorKey(null);
        }}
        onVerify={() => void verifyOtp()}
        onResend={() => void requestOtp(true)}
      />
    );
  }
  if (!signedIn) {
    return <Welcome locked={locked} onContinue={showEmail} />;
  }
  if (screen === 'account') {
    return (
      <Account
        session={signedIn}
        busy={busy === 'logout'}
        locked={locked}
        onBack={() => {
          if (!isOperationLocked()) setScreen(accountReturnScreen);
        }}
        onSwitch={() => {
          if (!isOperationLocked()) {
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
        onBack={() => {
          if (!isOperationLocked()) setScreen('empty');
        }}
        onAccount={() => {
          if (!isOperationLocked()) setScreen('account');
        }}
        onRefresh={() => void refresh()}
        onShowCli={() => {
          if (!isOperationLocked()) setScreen('cli');
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
        if (!isOperationLocked()) setScreen('account');
      }}
      onAdd={() => {
        if (!isOperationLocked()) setScreen('empty');
      }}
    />
  );
}
