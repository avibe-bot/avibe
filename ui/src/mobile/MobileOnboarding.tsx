import {
  AlertTriangle,
  ArrowLeft,
  Check,
  ChevronLeft,
  ChevronRight,
  ExternalLink,
  Laptop,
  MonitorDown,
  RotateCw,
  Sparkles,
  UserRound,
  Loader2,
  Database,
} from 'lucide-react';
import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { useTranslation } from 'react-i18next';

import heroMascot from '../../../assets/mascot/cloud-tuanzi-v2.png';
import logo from '@/assets/logo.png';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Skeleton, SkeletonShimmer } from '@/components/ui/skeleton';
import type { TranslationKey } from '@/i18n/types';
import { cn } from '@/lib/utils';

import {
  ACCOUNT_SECURITY_URL,
  bootstrapMobileSession,
  dismissMobileSheet,
  logoutMobileSession,
  normalizeMobileBridgeError,
  openMobileExternal,
  openMobilePage,
  refreshMobileSession,
  returnToMobileInstance,
  selectMobileInstance,
  type MobileBridgeError,
  type MobileInstance,
  type MobileSessionState,
  isSheetHash,
  SHEET_ACCOUNT_HASH,
  SHEET_INSTANCES_HASH,
} from './bridge';
import {
  AccountRow,
  AddInstanceRow,
  CurrentInstance,
  InstanceGroup,
  InstanceListRow,
  NoOtherInstances,
  OtherInstancesHeader,
  type RowStatus,
  SheetHeader,
  StaleBanner,
} from './InstanceList';
import { accountHeaderAvatarClassName, accountInitialFor } from '@/components/AccountAvatar';
import { PULL_THRESHOLD_PX, usePullToRefresh } from './usePullToRefresh';
import { raised } from '@/sign-in/chrome';
import { SignInChrome, type SignInChromeValue, type SignInFrameProps } from '@/sign-in/chromeContext';
import { pageErrorKey, type SignInErrorKey } from '@/sign-in/errors';
import { useOperationOwner, type OperationToken } from '@/sign-in/operationOwner';
import { PasskeySignIn, SignInStart, WeakPasswordNotice } from '@/sign-in/screens';
import type { SignInProvider, WeakPasswordReason } from '@/sign-in/types';
import { type SignInBusyAction, useSignInFlow } from '@/sign-in/useSignInFlow';
import { mobileSignInAdapter, mobileSignInStep } from './signInAdapter';
import { mobileSignInMethods } from './useSignInMethods';
import { AboutSection, AccountDataContent, AccountLinkRow, LicensesContent } from './LicensesPage';
import { type AppLicenses, loadAppLicenses } from './appLicenses';

type Screen =
  | 'boot'
  | 'bootError'
  | 'signIn'
  | 'weakPassword'
  | 'instances'
  | 'empty'
  | 'cli'
  | 'account'
  | 'licenses'
  | 'accountData'
  | 'sheet';

type BusyAction =
  | 'boot'
  | 'refresh'
  | 'select'
  | 'logout'
  | 'deleteAccount'
  | 'return';
type MobileErrorKey = SignInErrorKey;

// Native opens the account page with this fragment over a live instance, and
// then answers `app.returnToInstance`; `#account` has no instance behind it.
const RUNTIME_ACCOUNT_HASH = '#runtime-account';

const installGuideUrl = (language: string): string =>
  language.toLowerCase().startsWith('zh')
    ? 'https://github.com/avibe-bot/avibe/blob/master/docs/INSTALL_FOR_AI_ZH.md'
    : 'https://github.com/avibe-bot/avibe/blob/master/docs/INSTALL_FOR_AI.md';

const desktopDownloadUrl = 'https://avibe.bot/#install';

const screenForSession = (session: MobileSessionState): Screen => {
  if (session.status !== 'signedIn') return 'signIn';
  return session.instances.length === 0 ? 'empty' : 'instances';
};

/** In the instance sheet (`#sheet`) the page scrolls its document, which native hands to the sheet. */
const DocumentScroll = createContext(false);

function CloudScreen({
  children,
  onPull,
  fill = false,
}: {
  children: ReactNode;
  /** Pull to refresh at the top (full-screen pages only). */
  onPull?: () => void;
  /**
   * In the sheet too, the column is at least the sheet high, so `mt-auto` reaches the bottom.
   * `cqh` is the sheet's own viewport in the App (no container there) and the drawn sheet's
   * height in design frames, which make it a size container.
   */
  fill?: boolean;
}) {
  const { t } = useTranslation();
  const documentScroll = useContext(DocumentScroll);
  const scroller = useRef<HTMLElement>(null);
  // The sheet's document scroller is native's: no ref, so no pull to refresh there.
  const pulled = usePullToRefresh(scroller, onPull ?? noop, Boolean(onPull));
  if (documentScroll) {
    return (
      <main className="min-h-dvh w-full bg-bg text-foreground">
        <div className={cn('mx-auto flex w-full max-w-xl flex-col px-4 pt-5 pb-[max(1.5rem,env(safe-area-inset-bottom))]', fill && 'min-h-[100cqh]')}>
          {children}
        </div>
      </main>
    );
  }
  return (
    <main
      ref={scroller}
      className="h-dvh w-full overflow-y-auto bg-bg text-foreground"
      style={{ background: 'var(--gradient-wizard-platforms)' }}
    >
      {pulled > 0 ? (
        <p role="status" className="pt-[max(0.75rem,env(safe-area-inset-top))] text-center text-sm text-muted">
          {t(pulled >= PULL_THRESHOLD_PX ? 'mobileApp.sheet.releaseToRefresh' : 'mobileApp.sheet.pullToRefresh')}
        </p>
      ) : null}
      <div className="mx-auto flex min-h-full w-full max-w-xl shrink-0 flex-col px-6 pt-[max(1.5rem,env(safe-area-inset-top))] pb-[max(1.5rem,env(safe-area-inset-bottom))]">
        {children}
      </div>
    </main>
  );
}

const noop = () => {};

function Brand({
  back,
  account,
  email,
  disabled,
  onBack,
  onAccount,
}: {
  back?: boolean;
  account?: boolean;
  /** The signed-in account: the account button shows its initial, as Home's avatar does. */
  email?: string | null;
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
            // The App's left-edge swipe presses this button (MobileOnboardingBridge).
            data-mobile-back=""
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
          className={email ? 'size-11 rounded-full' : 'rounded-full border border-edge bg-field'}
          aria-label={t('mobileApp.account.title')}
          onClick={onAccount}
        >
          {email ? (
            <span className={accountHeaderAvatarClassName} aria-hidden="true">{accountInitialFor(email)}</span>
          ) : (
            <UserRound className="size-4" />
          )}
        </Button>
      ) : (
        <span className="text-xs text-muted">{t('mobileApp.brand.agentOs')}</span>
      )}
    </header>
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

/**
 * Welcome: the App's first page around the sign-in flow's ways in (ui/design:
 * mobile-app/welcome/providers-row). Email is the one primary action; Google, GitHub and Apple
 * follow below avibe.bot's 「或」 as one row of marks.
 */
function WelcomeHero({ children }: { children: ReactNode }) {
  const { t } = useTranslation();
  return (
    <CloudScreen>
      <Brand />
      <div className="relative flex flex-1 flex-col items-center justify-center py-8">
        <div className="relative mb-8 grid size-64 place-items-center">
          <div className="absolute inset-0 rounded-full border border-edge bg-surface/30 shadow-elevation-0" />
          <div className="absolute inset-6 rounded-full border border-surface/70 bg-surface/30" />
          <img
            src={heroMascot}
            alt={t('mobileApp.welcome.mascotAlt')}
            className="relative size-52 object-contain motion-safe:animate-[mobile-mascot-arrive_var(--motion-spring-smooth-duration)_var(--motion-spring-smooth)_both]"
          />
          <span className="absolute -right-1 top-9 grid size-12 rotate-12 place-items-center rounded-2xl border border-edge bg-surface shadow-elevation-0">
            <Sparkles className="size-5 text-mint-ink" />
          </span>
          <span className="absolute -left-1 bottom-5 rounded-full border border-edge bg-surface px-4 py-2 text-sm shadow-elevation-0">
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
        {children}
        <p className="text-center text-xs leading-relaxed text-muted">
          {t('mobileApp.welcome.footerLine1')}
          <br />
          {t('mobileApp.welcome.footerLine2')}
        </p>
      </div>
    </CloudScreen>
  );
}

/** Welcome as a view (props only); design frames draw it. */
export function WelcomeScreen({
  locked,
  errorKey = null,
  providers = [],
  onContinue,
  onProvider,
  onPasskey,
}: {
  locked: boolean;
  errorKey?: MobileErrorKey | null;
  /** The sign-ins avibe.bot offers the App now; none shows email alone. */
  providers?: readonly SignInProvider[];
  onContinue: () => void;
  onProvider?: (provider: SignInProvider) => void;
  /** Passkey sign-in with no email first, below the marks (ui/design: mobile-app/passkey/passkey-link). */
  onPasskey?: () => void;
}) {
  return (
    <WelcomeHero>
      <SignInStart
        locked={locked}
        errorKey={errorKey}
        methods={{ providers, passkey: Boolean(onPasskey) }}
        onContinue={onContinue}
        onProvider={onProvider}
        onPasskey={onPasskey}
      />
    </WelcomeHero>
  );
}

/** The passkey sign-in step in the App's frame, as a view (props only); design frames draw it. */
export function PasskeySignInScreen(props: Parameters<typeof PasskeySignIn>[0]) {
  return (
    <SignInChrome value={MOBILE_SIGN_IN_CHROME}>
      <PasskeySignIn {...props} />
    </SignInChrome>
  );
}

/** The empty-instances page as a view (props only); design frames draw it. */
export function EmptyInstancesScreen(props: Parameters<typeof EmptyInstances>[0]) {
  return <EmptyInstances {...props} />;
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

/** Where a row stands: the one opening, the others waiting, or the one that failed to open. */
type RowFailure = { id: string; reason: TranslationKey };
const rowStatus = (
  instance: MobileInstance,
  openingId: string | null,
  failure: RowFailure | null,
): RowStatus => {
  if (openingId) return instance.id === openingId ? { kind: 'opening' } : { kind: 'waiting' };
  if (failure?.id === instance.id) return { kind: 'failed', reason: failure.reason };
  return { kind: 'idle' };
};

/** Why an instance did not open, on its row. Each says what happened, not a guessed cause. */
const openFailureKey = (error: MobileBridgeError): TranslationKey => {
  if (error.code === 'instance_offline') return 'mobileApp.sheet.openFailedOffline';
  if (error.code === 'unauthorized_instance') return 'mobileApp.sheet.openFailedGone';
  return 'mobileApp.sheet.openFailed';
};

type RowsProps = {
  instances: MobileInstance[];
  locked: boolean;
  openingId: string | null;
  failure: RowFailure | null;
  expandedId: string | null;
  onOpen: (instance: MobileInstance) => void;
  onRefresh: () => void;
};

function InstanceRows({ instances, locked, openingId, failure, expandedId, onOpen, onRefresh }: RowsProps) {
  return (
    <InstanceGroup>
      {instances.map((instance) => (
        <InstanceListRow
          key={instance.id}
          instance={instance}
          status={rowStatus(instance, openingId, failure)}
          expanded={expandedId === instance.id}
          disabled={locked}
          onOpen={() => onOpen(instance)}
          onRetry={() => onOpen(instance)}
          onRefresh={onRefresh}
        />
      ))}
    </InstanceGroup>
  );
}

/**
 * The instance list the full page and the sheet share (ui/design: mobile-app/switcher/*): where you
 * are, the other instances by recent use (or none yet), and Add an instance. With nothing selected
 * (first use) there is no current block and no recency to name, only the rows.
 */
function InstanceListBody({
  session,
  locked,
  refreshing,
  stale,
  rows,
  firstUse,
  current,
  onRefresh,
  onAdd,
}: {
  session: Extract<MobileSessionState, { status: 'signedIn' }>;
  locked: boolean;
  refreshing: boolean;
  /** Nothing selected yet: only the rows. Otherwise where you are, then the others by recent use. */
  firstUse: boolean;
  /** A refresh failed: the list stays, under a banner that retries. */
  stale: boolean;
  rows: Omit<RowsProps, 'instances' | 'locked' | 'onRefresh'>;
  /** The current block: what tapping it does, and how it says so. */
  current: { label: (name: string) => string; onClick: (instance: MobileInstance) => void; reportsOpening: boolean };
  onRefresh: () => void;
  onAdd: () => void;
}) {
  const { t } = useTranslation();
  const selected = session.instances.find((instance) => instance.id === session.selected_instance_id) ?? null;
  const others = session.instances.filter((instance) => instance.id !== selected?.id);
  return (
    <>
      {stale ? <StaleBanner disabled={refreshing || locked} onRetry={onRefresh} /> : null}
      {selected ? (
        <CurrentInstance
          instance={selected}
          label={current.label(selected.name)}
          {...(current.reportsOpening ? {
            status: rowStatus(selected, rows.openingId, rows.failure),
            expanded: rows.expandedId === selected.id,
            disabled: locked,
            onRetry: () => current.onClick(selected),
            onRefresh,
          } : {})}
          onClick={() => current.onClick(selected)}
        />
      ) : null}
      {!firstUse ? (
        <section className={selected ? 'mt-6' : undefined} aria-busy={refreshing || Boolean(rows.openingId)}>
          <OtherInstancesHeader refreshing={refreshing} />
          {others.length === 0 ? (
            <NoOtherInstances />
          ) : (
            <InstanceRows {...rows} instances={others} locked={locked} onRefresh={onRefresh} />
          )}
        </section>
      ) : (
        <section aria-busy={refreshing || Boolean(rows.openingId)}>
          {refreshing ? (
            <p role="status" className="mb-2 flex items-center gap-1.5 px-1 text-xs text-muted">
              <Loader2 className="size-3 animate-spin" />
              {t('mobileApp.sheet.updating')}
            </p>
          ) : null}
          <InstanceRows {...rows} instances={others} locked={locked} onRefresh={onRefresh} />
        </section>
      )}
      <AddInstanceRow disabled={locked} onClick={onAdd} />
    </>
  );
}

/**
 * The full-screen list: first use, or no instance open (say after one failed to load). The sheet's
 * list in a page: the current instance opens again from here, and the page refreshes by its button
 * or a pull.
 */
function Instances({
  session,
  busy,
  locked,
  errorKey,
  rows,
  onRefresh,
  onAccount,
  onAdd,
  onBack,
  onReopen,
}: {
  session: Extract<MobileSessionState, { status: 'signedIn' }>;
  busy: BusyAction | SignInBusyAction | null;
  locked: boolean;
  /** The last refresh failed: a banner over the list, or the recovery card when there is no list. */
  errorKey: MobileErrorKey | null;
  rows: Omit<RowsProps, 'instances' | 'locked' | 'onRefresh'>;
  onRefresh: () => void;
  onAccount: () => void;
  onAdd: () => void;
  /** Present while the page covers an instance: Back returns to it. */
  onBack?: () => void;
  /** Opens the selected instance again. */
  onReopen: (instance: MobileInstance) => void;
}) {
  const { t } = useTranslation();
  const refreshing = busy === 'refresh';
  const switching = Boolean(session.selected_instance_id);
  // Over a live instance (`#runtime-account` → Switch) the current block returns to it, as in the
  // sheet; with nothing open it opens it again.
  const current = onBack
    ? { label: (name: string) => t('mobileApp.sheet.returnTo', { name }), onClick: onBack, reportsOpening: false }
    : { label: (name: string) => t('mobileApp.sheet.reopen', { name }), onClick: onReopen, reportsOpening: true };
  return (
    <CloudScreen onPull={locked ? undefined : onRefresh}>
      <Brand
        back={Boolean(onBack)}
        account
        email={session.account.email}
        disabled={locked}
        onBack={onBack}
        onAccount={onAccount}
      />
      <div className="flex items-start justify-between gap-3">
        <PageTitle
          title={switching ? t('mobileApp.sheet.title') : t('mobileApp.instances.title')}
          subtitle={switching ? undefined : t('mobileApp.sheet.firstRunSubtitle')}
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
      {errorKey && session.instances.length === 0 ? (
        <RecoveryCard errorKey={errorKey} locked={locked} onRetry={onRefresh} />
      ) : (
        <InstanceListBody
          session={session}
          locked={locked}
          refreshing={refreshing}
          stale={Boolean(errorKey)}
          rows={rows}
          firstUse={!switching}
          current={current}
          onRefresh={onRefresh}
          onAdd={onAdd}
        />
      )}
    </CloudScreen>
  );
}

/** The full-screen list as a view (props only); design frames draw it. */
export function InstancesPage(props: Parameters<typeof Instances>[0]) {
  return <Instances {...props} />;
}

/**
 * The sheet native opens over the selected instance (`#sheet`, ui/design: mobile-app/switcher/*):
 * where you are, the other instances by recent use, Add an instance, and the account. Close, the
 * current instance and native's pull-down all return to the instance, unchanged.
 */
/** The sheet as a view (props only), scrolling its document as it does in the App; design frames draw it. */
export function InstanceSheetPage(props: Parameters<typeof InstanceSheet>[0]) {
  return (
    <DocumentScroll.Provider value>
      <InstanceSheet {...props} />
    </DocumentScroll.Provider>
  );
}

function InstanceSheet({
  session,
  locked,
  refreshing,
  stale,
  rows,
  onClose,
  onRefresh,
  onAdd,
  onAccount,
}: {
  session: Extract<MobileSessionState, { status: 'signedIn' }>;
  locked: boolean;
  refreshing: boolean;
  stale: boolean;
  rows: Omit<RowsProps, 'instances' | 'locked' | 'onRefresh'>;
  onClose: () => void;
  onRefresh: () => void;
  onAdd: () => void;
  /** Absent when the sheet was opened from the instance's name (`#sheet-instances`). */
  onAccount?: () => void;
}) {
  const { t } = useTranslation();
  const current = session.instances.find((instance) => instance.id === session.selected_instance_id) ?? null;
  return (
    <CloudScreen>
      <SheetHeader currentName={current?.name ?? null} onClose={onClose} />
      <InstanceListBody
        session={session}
        locked={locked}
        refreshing={refreshing}
        stale={stale}
        rows={rows}
        firstUse={false}
        current={{ label: (name) => t('mobileApp.sheet.returnTo', { name }), onClick: onClose, reportsOpening: false }}
        onRefresh={onRefresh}
        onAdd={onAdd}
      />
      {onAccount ? <AccountRow email={session.account.email} disabled={locked} onClick={onAccount} /> : null}
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
  onOpenDownload,
  onExit,
  newAccount = false,
  signingOut = false,
  onLogout,
}: {
  /** In the sheet: Back returns to the instance list there. */
  onExit?: () => void;
  cli: boolean;
  busy: boolean;
  locked: boolean;
  errorKey: MobileErrorKey | null;
  /**
   * A Google, GitHub or Apple sign-in in the App just created this account and it has no
   * instances (Product, 2026-11-18): one small grey line after the install hint tells someone who
   * already had an account how to get back, with sign-out as a text link. Most people here are
   * new, so the install choices stay the main path. A hint only; it decides nothing.
   */
  newAccount?: boolean;
  signingOut?: boolean;
  onLogout?: () => void;
  onBack: () => void;
  onAccount: () => void;
  onRefresh: () => void;
  onShowCli: () => void;
  onOpenGuide: () => void;
  onOpenDownload: () => void;
}) {
  const { t } = useTranslation();
  return (
    <CloudScreen>
      <Brand
        back={cli || Boolean(onExit)}
        account={!cli && !onExit}
        disabled={locked}
        onBack={cli ? onBack : onExit}
        onAccount={onAccount}
      />
      <div className="pt-7">
        <span className="grid size-16 place-items-center rounded-3xl border border-edge bg-surface/80 shadow-elevation-0">
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
          <p className={cn('text-sm leading-7 text-muted', newAccount && onLogout ? 'mb-2' : 'mb-6')}>
            {t('mobileApp.empty.body')}
          </p>
          {newAccount && onLogout ? (
            <p className="mb-6 text-xs leading-6 text-muted">
              {t('mobileApp.empty.newAccount')}
              <Button
                type="button"
                variant="link"
                disabled={locked}
                className="ml-1 h-auto p-0 align-baseline text-xs"
                onClick={onLogout}
              >
                {signingOut ? t('mobileApp.account.signingOut') : t('mobileApp.empty.newAccountSignOut')}
              </Button>
            </p>
          ) : null}
          <div className="flex flex-col gap-4">
            <button
              type="button"
              disabled={locked}
              className={cn(
                raised,
                'flex items-start gap-3 text-left disabled:opacity-60',
              )}
              onClick={onOpenDownload}
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
  licenses,
  onBack,
  onSwitch,
  onLogout,
  onLicenses,
  onAccountData,
  inSheet = false,
  showCurrent = !inSheet,
  onClose,
}: {
  session: Extract<MobileSessionState, { status: 'signedIn' }>;
  busy: boolean;
  locked: boolean;
  errorKey: MobileErrorKey | null;
  licenses: boolean;
  /** Pushed inside the instance sheet. */
  inSheet?: boolean;
  /**
   * The current instance and Switch: only where Back does not lead to the list, which already
   * shows the current instance (the sheet, and the full page).
   */
  showCurrent?: boolean;
  /**
   * The sheet's root (`#sheet-account`, ui/design: mobile-app/switcher/b-account): ✕ returns to the
   * instance, and there is no Back to swipe to.
   */
  onClose?: () => void;
  onBack: () => void;
  onSwitch: () => void;
  onLogout: () => void;
  onLicenses: () => void;
  /** Account and data: the privacy policy and, at its bottom, deleting the account. */
  onAccountData: () => void;
}) {
  const { t } = useTranslation();
  const current = session.instances.find((instance) => instance.id === session.selected_instance_id);
  return (
    <CloudScreen>
      {inSheet && onClose ? (
        <SheetHeader title={t('mobileApp.account.title')} currentName={null} disabled={locked} onClose={onClose} />
      ) : inSheet ? (
        // A page pushed inside the sheet names where Back goes (ui/design: mobile-app/switcher/account).
        <header className="pb-4">
          <Button
            type="button"
            variant="ghost"
            size="sm"
            disabled={locked}
            // The App's left-edge swipe presses this button (MobileOnboardingBridge).
            data-mobile-back=""
            className="-ml-2 gap-1 px-2 text-mint-ink"
            onClick={onBack}
          >
            <ChevronLeft className="size-4" />
            {t('mobileApp.sheet.title')}
          </Button>
          <h1 className="mt-2 text-xl font-semibold">{t('mobileApp.account.title')}</h1>
        </header>
      ) : (
        <>
          <Brand back disabled={locked} onBack={onBack} />
          <PageTitle title={t('mobileApp.account.title')} />
        </>
      )}
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
      {showCurrent ? <section className="mt-8">
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
      </section> : null}
      <div className="mt-8">
        <AccountLinkRow icon={Database} label={t('mobileApp.accountData.title')} disabled={locked} onClick={onAccountData} />
      </div>
      <AboutSection disabled={locked} licenses={licenses} onLicenses={onLicenses} />
    </CloudScreen>
  );
}

/**
 * Account in the sheet, as a view (props only); design frames draw it. With `onClose` it is the
 * sheet's root (`#sheet-account`); without, it is pushed from the list, with Back.
 */
export function AccountSheetPage(props: Omit<Parameters<typeof Account>[0], 'inSheet'>) {
  return (
    <DocumentScroll.Provider value>
      <Account {...props} inSheet />
    </DocumentScroll.Provider>
  );
}

/**
 * Account and data, pushed from Account in the sheet and on the full page alike, with Back to
 * Account (ui/design: mobile-app/switcher/account-data, mobile-app/account/data).
 */
function AccountData({
  locked,
  inSheet = false,
  onBack,
  onPrivacy,
  onDelete,
}: {
  locked: boolean;
  /** Pushed inside the sheet: Back names Account, as Account's own Back names the list. */
  inSheet?: boolean;
  onBack: () => void;
  onPrivacy: () => void;
  onDelete: () => void;
}) {
  const { t } = useTranslation();
  return (
    <CloudScreen fill>
      {inSheet ? (
        <header className="pb-4">
          <Button
            type="button"
            variant="ghost"
            size="sm"
            disabled={locked}
            // The App's left-edge swipe presses this button (MobileOnboardingBridge).
            data-mobile-back=""
            className="-ml-2 gap-1 px-2 text-mint-ink"
            onClick={onBack}
          >
            <ChevronLeft className="size-4" />
            {t('mobileApp.account.title')}
          </Button>
          <h1 className="mt-2 text-xl font-semibold">{t('mobileApp.accountData.title')}</h1>
        </header>
      ) : (
        <>
          <Brand back disabled={locked} onBack={onBack} />
          <PageTitle title={t('mobileApp.accountData.title')} />
        </>
      )}
      <AccountDataContent disabled={locked} onPrivacy={onPrivacy} onDelete={onDelete} />
    </CloudScreen>
  );
}

/** Account and data as a full page (`#account`, `#runtime-account`), as a view; design frames draw it. */
export function AccountDataPage(props: Omit<Parameters<typeof AccountData>[0], 'inSheet'>) {
  return <AccountData {...props} />;
}

/** Account and data inside the sheet, as a view; design frames draw it. */
export function AccountDataSheetPage(props: Omit<Parameters<typeof AccountData>[0], 'inSheet'>) {
  return (
    <DocumentScroll.Provider value>
      <AccountData {...props} inSheet />
    </DocumentScroll.Provider>
  );
}

function Licenses({
  licenses,
  onBack,
  onOpenLink,
}: {
  licenses: AppLicenses;
  onBack: () => void;
  onOpenLink: (url: string) => void;
}) {
  const { t } = useTranslation();
  return (
    <CloudScreen>
      <Brand back onBack={onBack} />
      <PageTitle title={t('mobileApp.licenses.title')} subtitle={t('mobileApp.licenses.intro')} />
      <LicensesContent licenses={licenses} onOpenLink={onOpenLink} />
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
  onBack,
  onClose,
}: {
  errorKey: MobileErrorKey;
  locked: boolean;
  onRetry: () => void;
  /** Present while the page covers an instance: Back returns to it. */
  onBack?: () => void;
  /** In the instance sheet: its header, whose Close returns to the instance. */
  onClose?: () => void;
}) {
  const { t } = useTranslation();
  if (onClose) {
    return (
      <CloudScreen>
        <SheetHeader currentName={null} disabled={locked} onClose={onClose} />
        <RecoveryCard errorKey={errorKey} locked={locked} onRetry={onRetry} />
      </CloudScreen>
    );
  }
  return (
    <CloudScreen>
      {onBack
        ? <Brand back disabled={locked} onBack={onBack} />
        : <Brand account disabled />}
      <PageTitle
        title={t('mobileApp.instances.title')}
        subtitle={t('mobileApp.instances.subtitle')}
      />
      <RecoveryCard errorKey={errorKey} locked={locked} onRetry={onRetry} />
    </CloudScreen>
  );
}

/** The App's frame around a shared sign-in step: the cloud page and its brand header. */
function MobileSignInFrame({ children, back }: SignInFrameProps) {
  return (
    <CloudScreen>
      {back ? <Brand back disabled={back.disabled} onBack={back.onBack} /> : <Brand />}
      {children}
    </CloudScreen>
  );
}

const MOBILE_SIGN_IN_CHROME: SignInChromeValue = { Frame: MobileSignInFrame, platform: 'ios' };

export function MobileOnboarding() {
  // Native opens `#sheet` as a sheet over the selected instance, which scrolls its document.
  const [sheet] = useState(() => isSheetHash(window.location.hash));
  return (
    <DocumentScroll.Provider value={sheet}>
      <SignInChrome value={MOBILE_SIGN_IN_CHROME}>
        <MobileOnboardingScreens />
      </SignInChrome>
    </DocumentScroll.Provider>
  );
}

function MobileOnboardingScreens() {
  const { t, i18n } = useTranslation();
  const startsOnAccount = useRef(
    window.location.hash === '#account' || window.location.hash === RUNTIME_ACCOUNT_HASH,
  );
  // Account Back returns to the instance this page covers while native still
  // holds it; otherwise it shows the instance list.
  const [coversInstance, setCoversInstance] = useState(
    () => window.location.hash === RUNTIME_ACCOUNT_HASH,
  );
  const [screen, setScreen] = useState<Screen>('boot');
  // Bundled with the App; absent while it carries no native notices.
  const [licenses, setLicenses] = useState<AppLicenses | null>(null);
  const [session, setSession] = useState<MobileSessionState>({ status: 'signedOut' });
  const [errorKey, setErrorKey] = useState<MobileErrorKey | null>(null);
  // The page native opens as a sheet over the selected instance: a ref for the first read, the
  // state for rendering. Both end only if no instance is behind the sheet any more.
  const sheetPresentation = useRef(isSheetHash(window.location.hash));
  const [inSheet, setInSheet] = useState(() => isSheetHash(window.location.hash));
  // Opened from the instance's name: the list only, without the account row.
  const [instancesOnly] = useState(() => window.location.hash === SHEET_INSTANCES_HASH);
  // Opened from the avatar where the header names the instance: the Account page is the sheet's root.
  const [accountRoot] = useState(() => window.location.hash === SHEET_ACCOUNT_HASH);
  // A second factor owed at launch (not after a sign-in on this page), with the account's email
  // when native knows it: passing it continues into the selection native restored, as a launch
  // without a second factor does.
  const launchSecondFactor = useRef<{ email: string | null } | null>(null);
  const sheetOpened = useRef(false);
  const sheetRefreshToken = useRef(0);
  // Native runs one account operation at a time: while the sheet's background refresh is in
  // flight, an open, a refresh or a sign-out waits for it rather than being refused as busy.
  const sheetRefreshSettled = useRef<Promise<void>>(Promise.resolve());
  const [sheetRefresh, setSheetRefresh] = useState<'idle' | 'refreshing' | 'failed'>('idle');
  // The instance rows: the one opening, the one that failed to open, the offline one explained.
  const [openingId, setOpeningId] = useState<string | null>(null);
  const [rowFailure, setRowFailure] = useState<RowFailure | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  // A password sign-in avibe.bot reported as weak, kept through any second
  // factor until its one notice.
  const pendingWeakPassword = useRef<WeakPasswordReason[] | null>(null);
  const [weakPasswordReasons, setWeakPasswordReasons] = useState<WeakPasswordReason[]>([]);
  const owner = useOperationOwner<BusyAction | SignInBusyAction>();
  const {
    beginOperation,
    busy,
    finishOperation,
    isOperationLocked,
    locked,
    ownsOperation,
  } = owner;

  const signedIn = session.status === 'signedIn' ? session : null;
  const offered = mobileSignInMethods(session);
  // The sign-in steps are the shared flow's (#3946); this page keeps the session and what follows.
  const signIn = useSignInFlow<MobileSessionState, BusyAction>({
    adapter: mobileSignInAdapter,
    // Only the ways the latest signed-out answer names (#3945's `providers`, and `passkey`);
    // passkey sign-in below the marks (ui/design: mobile-app/passkey/passkey-link).
    methods: offered,
    owner,
    onSession: (next, origin) => {
      // The selection native restored at launch, once the same account passed its second factor.
      const resume = origin.kind === 'secondFactor' ? restoredSelection(next) : null;
      if (origin.weakPassword) pendingWeakPassword.current = origin.weakPassword;
      setSession(next);
      setScreen(next.status === 'signedIn' && sheetPresentation.current ? 'sheet' : screenAfterSession(next));
      setErrorKey(null);
      setOpeningId(null);
      setRowFailure(null);
      setExpandedId(null);
      return resume ? () => void select(resume) : undefined;
    },
    onBeginSignIn: () => {
      pendingWeakPassword.current = null;
      launchSecondFactor.current = null;
    },
    onDifferentAccount: () => void logout(),
    onOpenSecurity: () => openAccountSecurity(),
  });
  const { follow: followSignIn, reset: resetSignIn } = signIn;
  const accountReturnScreen = useMemo(
    () => (inSheet
      ? 'sheet'
      : signedIn?.instances.length ? 'instances' : 'empty') as Screen,
    [inSheet, signedIn],
  );

  const bootstrap = useCallback(async () => {
    const token = beginOperation('boot');
    if (token === null) return;
    setErrorKey(null);
    setScreen('boot');
    try {
      const next = await bootstrapMobileSession();
      if (!ownsOperation(token)) return;
      followSignIn(mobileSignInStep(next));
      launchSecondFactor.current = next.status === 'twoFactorRequired' ? { email: next.email } : null;
      setSession(next);
      setScreen(
        next.status === 'signedIn' && sheetPresentation.current
          ? accountRoot ? 'account' : 'sheet'
          : startsOnAccount.current && next.status === 'signedIn'
            ? 'account'
            : screenForSession(next),
      );
      startsOnAccount.current = false;
    } catch (rawError) {
      if (!ownsOperation(token)) return;
      setErrorKey(pageErrorKey(normalizeMobileBridgeError(rawError), 'other'));
      setScreen('bootError');
    } finally {
      finishOperation(token);
    }
  }, [accountRoot, beginOperation, finishOperation, followSignIn, ownsOperation]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      void bootstrap();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [bootstrap]);

  useEffect(() => {
    let active = true;
    void loadAppLicenses().then((data) => {
      if (active) setLicenses(data);
    });
    return () => {
      active = false;
    };
  }, []);

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
      setErrorKey('mobileApp.errors.network');
      return;
    }
    followSignIn(mobileSignInStep(next));
    setSession(next);
    // A refresh from a page pushed inside the sheet returns to the sheet's list.
    setScreen(next.status === 'signedIn' && sheetPresentation.current ? 'sheet' : screenAfterSession(next));
    setErrorKey(null);
    setOpeningId(null);
    setRowFailure(null);
    setExpandedId(null);
  };

  const refresh = async () => {
    const token = beginOperation('refresh');
    if (token === null) return;
    setErrorKey(null);
    try {
      await sheetRefreshSettled.current;
      const next = await refreshMobileSession();
      applySession(token, next, true);
    } catch (rawError) {
      if (!ownsOperation(token)) return;
      const error = normalizeMobileBridgeError(rawError);
      setErrorKey(pageErrorKey(error, 'other'));
    } finally {
      finishOperation(token);
    }
  };

  /** The selection native restored at launch, once the same account has passed its second factor. */
  const restoredSelection = (next: MobileSessionState): MobileInstance | null => {
    const owed = launchSecondFactor.current;
    if (!owed || next.status !== 'signedIn') return null;
    if (owed.email !== null && owed.email !== next.account.email) return null;
    launchSecondFactor.current = null;
    return next.instances.find(
      (instance) => instance.id === next.selected_instance_id && instance.available,
    ) ?? null;
  };

  const select = async (instance: MobileInstance) => {
    if (isOperationLocked()) return;
    setRowFailure(null);
    if (!instance.available) {
      // Offline: the row explains how to bring it back, in place.
      setExpandedId((open) => (open === instance.id ? null : instance.id));
      return;
    }
    const token = beginOperation('select');
    if (token === null) return;
    setErrorKey(null);
    setOpeningId(instance.id);
    try {
      await sheetRefreshSettled.current;
      // Native opens the instance and takes this page away; the row says Opening until then.
      await selectMobileInstance(instance.id);
    } catch (rawError) {
      if (!ownsOperation(token)) return;
      setOpeningId(null);
      setRowFailure({ id: instance.id, reason: openFailureKey(normalizeMobileBridgeError(rawError)) });
    } finally {
      finishOperation(token);
    }
  };

  /** Back to the instance under the sheet, unchanged. */
  const closeSheet = async () => {
    if (isOperationLocked()) return;
    try {
      await dismissMobileSheet();
    } catch {
      // No instance is behind the sheet any more: show the full list instead.
      sheetPresentation.current = false;
      setInSheet(false);
      setScreen(signedIn?.instances.length ? 'instances' : 'empty');
    }
  };

  /** One refresh when the sheet opens, or on Retry: the list stays usable while it runs or fails. */
  const runSheetRefresh = useCallback(async () => {
    const token = sheetRefreshToken.current + 1;
    sheetRefreshToken.current = token;
    setSheetRefresh('refreshing');
    let settle = () => {};
    sheetRefreshSettled.current = new Promise<void>((resolve) => {
      settle = resolve;
    });
    try {
      const next = await refreshMobileSession();
      if (sheetRefreshToken.current !== token) return;
      setSheetRefresh('idle');
      setSession(next);
      // A sign-out or an account change leaves the sheet; native shows the full-screen flow.
      if (next.status !== 'signedIn') {
        followSignIn(mobileSignInStep(next));
        setScreen(screenForSession(next));
      }
    } catch {
      if (sheetRefreshToken.current !== token) return;
      setSheetRefresh('failed');
    } finally {
      settle();
    }
  }, [followSignIn]);

  const sheetRefreshInFlight = useRef<Promise<void> | null>(null);
  /** One refresh at a time: a second request joins the one in flight (native would refuse it as busy). */
  const refreshSheet = useCallback((): Promise<void> => {
    sheetRefreshInFlight.current ??= runSheetRefresh().finally(() => {
      sheetRefreshInFlight.current = null;
    });
    return sheetRefreshInFlight.current;
  }, [runSheetRefresh]);


  // The sheet's root (the list, or Account from `#sheet-account`) refreshes once when it opens.
  useEffect(() => {
    if (screen !== (accountRoot ? 'account' : 'sheet') || sheetOpened.current) return undefined;
    sheetOpened.current = true;
    const timer = window.setTimeout(() => void refreshSheet(), 0);
    return () => window.clearTimeout(timer);
  }, [accountRoot, screen, refreshSheet]);

  /** Back to Welcome with nothing of the old account kept on the page. */
  const showSignedOut = (next: MobileSessionState) => {
    setSession(next);
    pendingWeakPassword.current = null;
    resetSignIn();
    setScreen('signIn');
  };

  /** A page-level failure, on the sign-in step when that is what the page shows. */
  const showError = (key: MobileErrorKey) => {
    if (screen === 'signIn') signIn.setErrorKey(key);
    else setErrorKey(key);
  };

  const logout = async () => {
    const token = beginOperation('logout');
    if (token === null) return;
    setErrorKey(null);
    try {
      await sheetRefreshSettled.current;
      const next = await logoutMobileSession();
      if (!ownsOperation(token)) return;
      if (next.status !== 'signedOut') {
        showError('mobileApp.errors.network');
        return;
      }
      showSignedOut(next);
    } catch (rawError) {
      if (!ownsOperation(token)) return;
      const error = normalizeMobileBridgeError(rawError);
      showError(pageErrorKey(error, 'other'));
    } finally {
      finishOperation(token);
    }
  };

  /**
   * avibe.bot's deletion page, over Account with the App's sign-in. However it closes (deleted,
   * Done, or a swipe), the session says whether the account is still there: signed out goes to
   * Welcome, still signed in stays on Account. When the page reported the account deleted, any
   * other answer (a failed or stale read) signs this device out instead, so nothing of a deleted
   * account stays. A page that could not open leaves Account as it was.
   */
  const deleteAccount = async () => {
    const token = beginOperation('deleteAccount');
    if (token === null) return;
    setErrorKey(null);
    let completed = false;
    try {
      await sheetRefreshSettled.current;
      ({ completed } = await openMobilePage('accountDeletion'));
      if (!ownsOperation(token)) return;
      const next = await refreshMobileSession().catch((error: unknown) => {
        if (completed) return null;
        throw error;
      });
      if (!ownsOperation(token)) return;
      if (next?.status === 'signedOut') {
        showSignedOut(next);
      } else if (completed) {
        const signedOut = await logoutMobileSession();
        if (ownsOperation(token) && signedOut.status === 'signedOut') showSignedOut(signedOut);
      } else if (next?.status === 'signedIn') {
        setSession(next);
      } else if (next) {
        applySession(token, next, true);
      }
    } catch {
      // Nothing to show: Account's error card belongs to sign-out.
    } finally {
      finishOperation(token);
    }
  };

  const openPrivacyPolicy = () => {
    if (isOperationLocked()) return;
    void openMobilePage('privacyPolicy').catch(() => {});
  };

  const leaveAccount = async () => {
    if (!coversInstance) {
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
      setCoversInstance(false);
      setScreen(accountReturnScreen);
    } finally {
      finishOperation(token);
    }
  };

  const openGuide = () => {
    if (isOperationLocked()) return;
    void openMobileExternal(installGuideUrl(i18n.language));
  };

  const openDesktopDownload = () => {
    if (isOperationLocked()) return;
    void openMobileExternal(desktopDownloadUrl);
  };

  const openAccountSecurity = () => {
    if (isOperationLocked()) return;
    void openMobileExternal(ACCOUNT_SECURITY_URL);
  };

  if (screen === 'boot') {
    return inSheet
      ? <CloudScreen><SheetHeader title={accountRoot ? t('mobileApp.account.title') : undefined} currentName={null} disabled onClose={noop} /></CloudScreen>
      : <Boot />;
  }
  if (screen === 'bootError') {
    return (
      <BootRecovery
        errorKey={errorKey ?? 'mobileApp.errors.network'}
        locked={locked}
        onRetry={() => void bootstrap()}
        onBack={coversInstance ? () => void leaveAccount() : undefined}
        onClose={inSheet ? () => void closeSheet() : undefined}
      />
    );
  }
  if (screen === 'signIn' || !signedIn) {
    return signIn.render((actions) => <WelcomeHero>{actions}</WelcomeHero>);
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
        licenses={licenses !== null}
        inSheet={inSheet}
        // Back leads to the list, which shows the current instance, unless this page covers it.
        showCurrent={!inSheet && coversInstance}
        onClose={inSheet && accountRoot ? () => void closeSheet() : undefined}
        onBack={() => void leaveAccount()}
        onSwitch={() => {
          if (!isOperationLocked()) {
            setErrorKey(null);
            setScreen(signedIn.instances.length ? 'instances' : 'empty');
          }
        }}
        onLogout={() => void logout()}
        onLicenses={() => {
          if (!isOperationLocked()) {
            setErrorKey(null);
            setScreen('licenses');
          }
        }}
        onAccountData={() => {
          if (!isOperationLocked()) {
            setErrorKey(null);
            setScreen('accountData');
          }
        }}
      />
    );
  }
  if (screen === 'accountData') {
    return (
      <AccountData
        locked={locked}
        inSheet={inSheet}
        onBack={() => {
          if (!isOperationLocked()) setScreen('account');
        }}
        onPrivacy={openPrivacyPolicy}
        onDelete={() => void deleteAccount()}
      />
    );
  }
  if (screen === 'licenses' && licenses) {
    return (
      <Licenses
        licenses={licenses}
        onBack={() => setScreen('account')}
        onOpenLink={(url) => void openMobileExternal(url)}
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
        newAccount={signedIn?.new_account === true && signedIn.instances.length === 0}
        signingOut={busy === 'logout'}
        onLogout={() => void logout()}
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
        onOpenDownload={openDesktopDownload}
        onExit={inSheet ? () => {
          if (!isOperationLocked()) {
            setErrorKey(null);
            setScreen('sheet');
          }
        } : undefined}
      />
    );
  }
  const rows = { openingId, failure: rowFailure, expandedId, onOpen: (instance: MobileInstance) => void select(instance) };
  if (screen === 'sheet') {
    return (
      <InstanceSheet
        session={signedIn}
        locked={locked}
        refreshing={sheetRefresh === 'refreshing'}
        stale={sheetRefresh === 'failed'}
        rows={rows}
        onClose={() => void closeSheet()}
        onRefresh={() => void refreshSheet()}
        onAdd={() => {
          if (!isOperationLocked()) setScreen('empty');
        }}
        onAccount={instancesOnly ? undefined : () => {
          if (!isOperationLocked()) {
            setErrorKey(null);
            setScreen('account');
          }
        }}
      />
    );
  }
  return (
    <Instances
      session={signedIn}
      busy={busy}
      locked={locked}
      errorKey={errorKey}
      rows={rows}
      onRefresh={() => void refresh()}
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
      onBack={coversInstance ? () => void leaveAccount() : undefined}
      onReopen={(instance) => void select(instance)}
    />
  );
}
