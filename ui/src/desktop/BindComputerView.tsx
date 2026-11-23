import { createContext, useContext, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { ArrowLeft, Check, Laptop, TriangleAlert } from 'lucide-react';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Skeleton } from '@/components/ui/skeleton';
import { accountInitialFor } from '@/components/AccountAvatar';
import { SignInChrome, type SignInChromeValue, type SignInFrameProps } from '@/sign-in/chromeContext';
import { CodeEntry, SecondFactor } from '@/sign-in/screens';
import type { SecondFactorMethod } from '@/sign-in/types';

import { AccountSecondFactorView, RecoveryCodesView } from './AccountSecondFactorView';

/**
 * This computer's binding, as the Account page shows it. Each state is one native answer
 * (`observe_current_binding`), never a guess from a missing row or a failed read.
 */
export type ComputerBinding =
  | { state: 'reading' }
  /** `no_published_binding`; `previous` is `legacy_review_required`. */
  | { state: 'unbound'; previous?: boolean }
  /** `owner_observed`: bound, and avibe.bot says this account owns it. Not a connection report. */
  | { state: 'bound'; host: string | null; url: string | null }
  /** `bound_owner_unconfirmed`. */
  | { state: 'unconfirmed'; host: string | null }
  /** `legacy_wait` or `legacy_publication_pending`. */
  | { state: 'pending' }
  /** `legacy_recovery_required`. */
  | { state: 'repair' }
  | { state: 'upgrade' }
  /** The read itself failed: unknown, not unbound. */
  | { state: 'unknown' };

/** An ordinary account step avibe.bot accepted that this App has not finished saving. */
export type AccountRecovery = 'available' | 'blocked' | 'gone';

/** The name a prefix becomes. Kept here, not imported from the Devices page: the account document
 *  must not pull the Workbench's data layer into its bundle. */
const DOMAIN_SUFFIX = '-app.avibe.bot';

export type ComputerBindingCardProps = {
  binding: ComputerBinding;
  account: string;
  computer: string;
  recovery?: AccountRecovery | null;
  busy?: boolean;
  onBind: () => void;
  onRefresh: () => void;
  onUpdates: () => void;
  onRecover: () => void;
};

function Row({ label, children }: { label: string; children: ReactNode }) {
  return <>
    <span className="text-xs text-muted">{label}</span>
    <div className="min-w-0 text-sm [overflow-wrap:anywhere]">{children}</div>
  </>;
}

function AccountLine({ email }: { email: string }) {
  return <span className="flex min-w-0 items-center gap-2">
    <span className="flex size-6 shrink-0 items-center justify-center rounded-full bg-mint-soft text-2xs font-semibold text-mint-ink">{accountInitialFor(email)}</span>
    <span className="min-w-0 truncate font-medium">{email}</span>
  </span>;
}

/** ui/design: desktop/bind/account-*. Below the signed-in account on the Account page. */
export function ComputerBindingCard({ binding, account, computer, recovery, busy = false, onBind, onRefresh, onUpdates, onRecover }: ComputerBindingCardProps) {
  const { t } = useTranslation();
  const bound = binding.state === 'bound';
  // An accepted verification waiting to be saved comes first: it is the one safe next step.
  const recovering = recovery === 'available';
  const host = binding.state === 'bound' || binding.state === 'unconfirmed' ? binding.host : null;
  const line = (text: string) => <p className="mt-3 text-sm leading-6 text-muted">{text}</p>;
  const action = (label: string, run: () => void, brand = false) => <Button variant={brand ? 'brand' : 'outline'} className="mt-4" disabled={busy} onClick={run}>{label}</Button>;
  return <div data-computer-binding={binding.state} className="mt-4 rounded-xl border border-edge bg-surface p-5">
    <div className="flex items-center justify-between gap-3">
      <p className="text-xs font-medium text-muted">{t('desktopAccount.bind.computer')}</p>
      {bound && <Badge variant="success">{t('desktopAccount.bind.bound')}</Badge>}
    </div>
    <div className="mt-3 flex items-start gap-3"><Laptop className="mt-0.5 size-5 shrink-0 text-muted" /><p className="min-w-0 break-words text-sm font-semibold [overflow-wrap:anywhere]">{computer}</p></div>
    {binding.state === 'reading' && <Skeleton className="mt-4 h-5 w-2/3" />}
    {(bound || binding.state === 'unconfirmed') && host && <div className="mt-4 grid grid-cols-[72px_minmax(0,1fr)] items-center gap-x-4 gap-y-2 border-t border-edge pt-4">
      <Row label={t('desktopAccount.bind.domain')}>{bound && binding.url ? <a className="font-mono underline underline-offset-4" href={binding.url} target="_blank" rel="noreferrer">{host}</a> : <span className="font-mono">{host}</span>}</Row>
      {bound && <Row label={t('devices.claim.account')}><AccountLine email={account} /></Row>}
    </div>}
    {binding.state === 'unbound' && <>{binding.previous && line(t('desktopAccount.bind.previous'))}{line(t('desktopAccount.bind.notBound'))}{!recovering && action(t('desktopAccount.bind.action'), onBind, true)}</>}
    {binding.state === 'unconfirmed' && <>{line(t('desktopAccount.bind.unconfirmed'))}{action(t('desktopAccount.bind.checkAgain'), onRefresh)}</>}
    {binding.state === 'pending' && <>{line(t('desktopAccount.bind.pending'))}{action(t('desktopAccount.bind.checkAgain'), onRefresh)}</>}
    {binding.state === 'repair' && line(t('desktopAccount.bind.repair'))}
    {binding.state === 'upgrade' && <>{line(t('desktopAccount.bind.upgrade'))}{action(t('update.action.check'), onUpdates)}</>}
    {binding.state === 'unknown' && <>{line(t('desktopAccount.bind.unknown'))}{action(t('desktopAccount.bind.checkAgain'), onRefresh)}</>}
    {recovery && <div role="status" className="mt-4 rounded-xl border border-gold/40 bg-gold/10 p-4 text-sm leading-6">
      <p>{t(recovery === 'available' ? 'desktopAccount.bind.recovery' : recovery === 'blocked' ? 'desktopAccount.bind.recoveryBlocked' : 'desktopAccount.bind.recoveryGone')}</p>
      {recovery === 'available' && <Button variant="brand" size="sm" className="mt-3" disabled={busy} onClick={onRecover}>{t('common.continue')}</Button>}
    </div>}
  </div>;
}

export type EligibleName = { prefix: string; host: string };
/** Why a step could not go on, as one sentence; never a raw code. */
export type BindProblem = 'unreachable' | 'signedOut' | 'rateLimited' | 'expired' | 'busy' | 'notResponding' | 'changed' | 'codeInvalid' | 'lost' | 'reauth' | 'storage' | 'unknown';
/** Problems whose only safe next step is going back, not another attempt right away. */
const BACK_ONLY: BindProblem[] = ['busy', 'rateLimited', 'changed', 'signedOut', 'storage'];

/** What a review native returned after a verification changed from the one the person confirmed. */
export type BindField = 'account' | 'computer' | 'name' | 'availability';

export type BindStep =
  | { kind: 'opening' }
  /** `new`. `check`: what the last read of this exact name said. `changed`: after a verification. */
  | { kind: 'name'; name: string; check: 'free' | 'taken' | 'checking' | 'invalid' | 'reclaim'; changed?: BindField[] }
  /** `existing`: a fixed address this account already holds for this computer. */
  | { kind: 'existing'; host: string; changed?: BindField[] }
  /** `name_holding_limit_reached`. Details only when avibe.bot sent them. */
  | { kind: 'limit'; name: string; held: number | null; earliest: string | null; eligible: EligibleName[] }
  | { kind: 'proof' }
  /** avibe.bot accepted the verification; the new sign-in is being read back and saved. `recover`:
   *  that stopped, and the person may continue it (the account's own recovery, no new code). */
  | { kind: 'saving'; recover?: boolean }
  /** `invalid`: avibe.bot said the code typed was wrong or expired; the same email's code may be typed again. */
  | { kind: 'code'; code: string; invalid?: boolean; limited?: boolean }
  /** `error`: what the last try said, in place. `rateLimited` waits `retryMinutes` here. */
  | { kind: 'factor'; method: SecondFactorMethod | 'recovery'; methods: SecondFactorMethod[]; code: string; error?: 'code' | 'passkey' | 'rateLimited' | null; retryMinutes?: number }
  /** The account must add an authenticator first. `material` once native issued it. */
  | { kind: 'factorSetup'; material: { uri: string; secret: string } | null; code: string; invalid?: boolean }
  /** Recovery codes that setup returned, shown once. */
  | { kind: 'codes'; codes: string[] }
  | { kind: 'bound'; host: string }
  | { kind: 'conflict' }
  | { kind: 'upgrade' }
  | { kind: 'problem'; problem: BindProblem };

export type BindComputerViewProps = {
  step: BindStep;
  account: string;
  computer: string;
  /** A confirm, send, submit or check is on its way. */
  busy?: boolean;
  onNameChange: (name: string) => void;
  onPick: (prefix: string) => void;
  onConfirm: () => void;
  onSendCode: () => void;
  onCodeChange: (code: string) => void;
  onSubmitCode: (code: string) => void;
  onBeginSetup: () => void;
  onSavedCodes: () => void;
  onFactorStep: (method: SecondFactorMethod | 'recovery') => void;
  onPasskey: () => void;
  onRestart: () => void;
  /** Continue saving an accepted sign-in (`desktop_account_recovery` resume). */
  onRecover: () => void;
  onUpdates: () => void;
  /** Cancel (`cancel`) or leave a finished step: back to the Account page. */
  onClose: () => void;
};

/** The verification steps keep the sign-in's own screens; this column only changes the way out. */
const Close = createContext<() => void>(() => undefined);
function BindSignInFrame({ children }: SignInFrameProps) {
  const { t } = useTranslation();
  const onClose = useContext(Close);
  return <div data-bind-verify className="mx-auto my-auto flex w-full max-w-md shrink-0 flex-col py-4">
    {children}
    <div className="mt-6 text-center"><Button variant="ghost" onClick={onClose}>{t('common.cancel')}</Button></div>
  </div>;
}
const BIND_CHROME: SignInChromeValue = { Frame: BindSignInFrame, platform: 'desktop' };

function Field({ label, children }: { label: string; children: ReactNode }) {
  return <>
    <span className="text-xs text-muted max-md:mt-2">{label}</span>
    <div className="min-w-0">{children}</div>
  </>;
}

/**
 * ui/design: desktop/bind/*. Binding this computer to the signed-in account, in the Account page.
 * The page shows exactly what native read for this ticket, and every step that reaches avibe.bot
 * is a button the person pressed: nothing here runs on mount, reshow or reconnect.
 */
export function BindComputerView({ step, account, computer, busy = false, onNameChange, onPick, onConfirm, onSendCode, onCodeChange, onSubmitCode, onBeginSetup, onSavedCodes, onFactorStep, onPasskey, onRestart, onRecover, onUpdates, onClose }: BindComputerViewProps) {
  const { t, i18n } = useTranslation();
  const language = i18n.resolvedLanguage ?? i18n.language;

  if (step.kind === 'code' || step.kind === 'factor') {
    return <Close.Provider value={onClose}><SignInChrome value={BIND_CHROME}>
      {step.kind === 'code'
        ? <CodeEntry email={account} code={step.code} verifyLabel={t('mobileApp.twoFactor.verify')} busy={busy ? 'verify' : null} locked={busy} errorKey={null}
          invalidText={step.invalid ? t('desktopAccount.bind.problem.codeInvalid') : step.limited ? t('mobileApp.code.rateLimited') : undefined} cooldown={0} providerCooldownActive={false} resent={false}
          onCodeChange={onCodeChange} onVerify={() => onSubmitCode(step.code)} />
        : <SecondFactor session={{ status: 'secondFactor', email: account, gate: 'challenge', methods: step.methods }} step={step.method} code={step.code}
          busy={busy ? (step.method === 'passkey' ? 'passkey' : 'verify') : null} locked={busy} retryMinutes={step.retryMinutes ?? 1}
          errorKey={step.error === 'passkey' ? 'mobileApp.twoFactor.passkeyFailed' : step.error === 'rateLimited' ? 'mobileApp.twoFactor.rateLimited'
            : step.error === 'code' ? (step.method === 'recovery' ? 'mobileApp.twoFactor.recoveryInvalid' : 'mobileApp.twoFactor.codeInvalid') : null}
          onStep={(next) => { if (next !== 'setup') onFactorStep(next); }} onCodeChange={onCodeChange} onVerify={() => onSubmitCode(step.code)} onPasskey={onPasskey} />}
    </SignInChrome></Close.Provider>;
  }

  if (step.kind === 'factorSetup') {
    return <div className="flex min-h-full flex-col"><AccountSecondFactorView code={step.code} busy={busy}
      material={step.material ? { type: 'totp_enrollment', uri: step.material.uri, secret: step.material.secret, qrSvg: '' } : null}
      errorMessage={step.invalid ? t('desktopAccount.bind.problem.codeInvalid') : null}
      onCodeChange={onCodeChange} onVerify={() => onSubmitCode(step.code)} onEnroll={onBeginSetup} onLocal={onClose} exitLabel={t('common.cancel')} /></div>;
  }
  if (step.kind === 'codes') return <RecoveryCodesView codes={step.codes} busy={busy} onSaved={onSavedCodes} />;

  const heading = (title: string, icon?: ReactNode) => <div className="mb-6">
    {icon && <div className="mb-6 grid size-14 place-items-center rounded-2xl border border-edge bg-field">{icon}</div>}
    <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
  </div>;
  const back = <Button variant="ghost" size="sm" className="mb-5 -ml-3" onClick={onClose}><ArrowLeft className="size-4" />{t('desktopAccount.accountTitle')}</Button>;
  const page = (children: ReactNode) => <div data-bind-step={step.kind} className="mx-auto w-full max-w-2xl pt-6">{back}{children}</div>;
  const warn = <TriangleAlert className="size-6 text-gold-ink" />;

  if (step.kind === 'opening' || step.kind === 'name' || step.kind === 'existing' || step.kind === 'limit') {
    const editable = step.kind === 'name' || step.kind === 'limit';
    const name = editable ? step.name : '';
    const check = step.kind === 'name' ? step.check : null;
    const changed = (step.kind === 'name' || step.kind === 'existing') ? step.changed : undefined;
    const ready = (step.kind === 'name' && (check === 'free' || check === 'reclaim')) || step.kind === 'existing';
    const hint = step.kind === 'existing' ? <span className="text-muted">{t('desktopAccount.bind.existing')}</span>
      : check === 'taken' ? <span className="text-destructive-ink">{t('devices.claim.taken')}</span>
      : check === 'checking' ? <span className="text-muted">{t('desktopAccount.bind.checking')}</span>
      : check === 'invalid' ? <span className="text-muted">{t('desktopAccount.bind.invalid')}</span>
      : check === 'reclaim' ? <span className="text-muted">{t('desktopAccount.bind.reclaim')}</span>
      : check === 'free' ? <span className="text-muted">{t('devices.claim.domain', { host: `${name}${DOMAIN_SUFFIX}` })}</span>
      : null;
    const at = step.kind === 'limit' && step.earliest ? Date.parse(step.earliest) : NaN;
    const date = Number.isFinite(at) ? new Intl.DateTimeFormat(language, { month: 'short', day: 'numeric' }).format(at) : null;
    return page(<>
      {heading(t('desktopAccount.bind.title'))}
      {changed ? <p role="status" className="-mt-3 mb-5 flex items-center gap-2 text-sm"><Check className="size-4 shrink-0 text-mint-ink" />{changed.length
        ? t('desktopAccount.bind.changed', { fields: new Intl.ListFormat(language, { type: 'conjunction' }).format(changed.map((field) => t(`desktopAccount.bind.field.${field}` as const))) })
        : t('desktopAccount.bind.changedPlain')}</p> : null}
      <div className="rounded-xl border border-edge bg-surface">
        <div className="grid gap-x-6 gap-y-2 px-5 py-4 md:grid-cols-[72px_minmax(0,1fr)] md:items-center md:gap-y-3">
          <Field label={t('devices.claim.account')}><span className="flex min-w-0 items-center gap-2 text-sm"><AccountLine email={account} /><span className="shrink-0 text-xs text-muted">{t('devices.claim.signedInHere')}</span></span></Field>
          <Field label={t('desktopAccount.bind.computerLabel')}><span className="text-sm [overflow-wrap:anywhere]">{computer}</span></Field>
          {step.kind !== 'limit' && <Field label={t('devices.claim.name')}>
            {step.kind === 'opening' ? <Skeleton className="h-9 w-64" />
              : step.kind === 'existing' ? <span className="font-mono text-sm font-semibold">{step.host}</span>
              : <div className="flex min-w-0 items-center gap-2">
                <Input className="w-44" value={name} disabled={busy} onChange={(event) => onNameChange(event.target.value)} aria-label={t('devices.claim.name')} aria-invalid={check === 'taken' || undefined} />
                <span className="truncate font-mono text-xs text-muted">{t('devices.claim.suffix')}</span>
              </div>}
          </Field>}
          {hint && <><span className="max-md:hidden" /><span className="text-xs leading-5 md:-mt-1.5">{hint}</span></>}
        </div>
        {step.kind === 'limit' && <div className="border-t border-edge px-5 py-4 text-sm leading-6">
          <p>{step.held !== null ? t('desktopAccount.bind.limit', { count: step.held }) : t('desktopAccount.bind.limitPlain')}</p>
          {date && <p className="text-muted">{t('desktopAccount.bind.limitDate', { date })}</p>}
          {step.eligible.length > 0 && <>
            <p className="mt-3 text-muted">{t('desktopAccount.bind.takeBack')}</p>
            <ul className="mt-2 flex flex-col gap-2">{step.eligible.map((each) => <li key={each.prefix} className="flex items-center justify-between gap-3">
              <span className="min-w-0 truncate font-mono text-sm">{each.host}</span>
              <Button variant="outline" size="sm" disabled={busy} onClick={() => onPick(each.prefix)}>{t('desktopAccount.bind.useName')}</Button>
            </li>)}</ul>
          </>}
        </div>}
      </div>
      <div className="mt-6 flex flex-wrap gap-3">
        {step.kind !== 'limit' && <Button variant="brand" disabled={busy || !ready} onClick={onConfirm}>{busy && ready ? t('desktopAccount.bind.binding') : t('desktopAccount.bind.confirm')}</Button>}
        <Button variant={step.kind === 'limit' ? 'outline' : 'ghost'} onClick={onClose}>{t('common.cancel')}</Button>
      </div>
    </>);
  }

  if (step.kind === 'proof') {
    return page(<>
      {heading(t('desktopAccount.bind.proofTitle'))}
      <p className="max-w-xl text-sm leading-6 text-muted">{t('desktopAccount.bind.proofBody', { email: account })}</p>
      <div className="mt-6 flex flex-wrap gap-3">
        <Button variant="brand" disabled={busy} onClick={onSendCode}>{t('desktopAccount.bind.sendCode')}</Button>
        <Button variant="ghost" onClick={onClose}>{t('common.cancel')}</Button>
      </div>
    </>);
  }

  if (step.kind === 'saving') {
    return page(<>
      {heading(t('desktopAccount.bind.title'))}
      <p role="status" className="mb-5 flex items-center gap-2 text-sm"><Check className="size-4 shrink-0 text-mint-ink" />{t('desktopAccount.bind.saving')}</p>
      {step.recover ? <div className="max-w-xl rounded-xl border border-gold/40 bg-gold/10 p-4 text-sm leading-6">
        <p>{t('desktopAccount.bind.recovery')}</p>
        <Button variant="brand" size="sm" className="mt-3" disabled={busy} onClick={onRecover}>{t('common.continue')}</Button>
      </div> : <Skeleton className="h-5 w-2/3" />}
      <Button variant="ghost" className="mt-6" onClick={onClose}>{t('common.cancel')}</Button>
    </>);
  }

  if (step.kind === 'bound') {
    return page(<>
      {heading(t('desktopAccount.bind.boundTitle'), <Check className="size-6 text-mint-ink" />)}
      <div className="grid grid-cols-[72px_minmax(0,1fr)] items-center gap-x-4 gap-y-3 rounded-xl border border-edge bg-surface px-5 py-4">
        <Row label={t('desktopAccount.bind.domain')}><span className="font-mono font-semibold">{step.host}</span></Row>
        <Row label={t('devices.claim.account')}><AccountLine email={account} /></Row>
        <Row label={t('desktopAccount.bind.computerLabel')}>{computer}</Row>
      </div>
      <Button variant="brand" className="mt-6" onClick={onClose}>{t('common.done')}</Button>
    </>);
  }

  if (step.kind === 'conflict') {
    return page(<>
      {heading(t('desktopAccount.bind.conflictTitle'), warn)}
      <p className="max-w-xl text-sm leading-6 text-muted">{t('desktopAccount.bind.conflictBody')}</p>
      <Button variant="brand" className="mt-6" onClick={onClose}>{t('common.back')}</Button>
    </>);
  }

  // One sentence and one safe next step. A refusal that says to wait goes back; the rest start a
  // new review from the beginning, never a repeat of the step that failed.
  const problem = step.kind === 'problem' ? step.problem : null;
  const next = step.kind === 'upgrade' ? <Button variant="brand" onClick={onUpdates}>{t('update.action.check')}</Button>
    : problem && BACK_ONLY.includes(problem) ? <Button variant="brand" onClick={onClose}>{t('common.back')}</Button>
    : <Button variant="brand" disabled={busy} onClick={onRestart}>{t('desktopAccount.bind.restart')}</Button>;
  return page(<>
    {heading(t('desktopAccount.bind.title'))}
    <div role="alert" className="flex max-w-xl gap-3 rounded-xl border border-gold/40 bg-gold/10 p-4 text-sm leading-6">
      <TriangleAlert className="mt-1 size-4 shrink-0 text-gold-ink" />
      <p>{step.kind === 'upgrade' ? t('desktopAccount.bind.upgrade') : t(`desktopAccount.bind.problem.${problem ?? 'unknown'}` as const)}</p>
    </div>
    <div className="mt-6 flex flex-wrap gap-3">{next}</div>
  </>);
}
