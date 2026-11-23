import { createContext, useContext, type ReactNode } from 'react';
import { Trans, useTranslation } from 'react-i18next';
import { Check, Laptop, TriangleAlert } from 'lucide-react';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog';
import { Skeleton } from '@/components/ui/skeleton';
import { DOMAIN_SUFFIX, DomainField, type DomainCheck } from '@/components/DomainField';
import { SUPPORT_EMAIL } from '@/lib/support';
import { SignInChrome, type SignInChromeValue, type SignInFrameProps } from '@/sign-in/chromeContext';
import { CodeEntry, SecondFactor } from '@/sign-in/screens';
import type { SecondFactorMethod } from '@/sign-in/types';

import { AccountSecondFactorView, RecoveryCodesView } from './AccountSecondFactorView';

/**
 * This computer's binding on the Account page. Each shape answers only "can I use it" and "what do
 * I do" (owner, 2026-11-23): anything native can settle itself (an older attempt, a read to retry,
 * an accepted sign-in to finish saving) is settled behind "Bind this computer", not shown.
 */
export type ComputerBinding =
  | { state: 'reading' }
  /** No binding here yet, including an older attempt that native replaces on Bind. */
  | { state: 'unbound' }
  /** Bound here and owned by this account (`owner_observed`), or bound here and not readable
   *  right now: shown as bound while it is read again quietly. Not a connection report. */
  | { state: 'bound'; host: string | null; url: string | null }
  /** Bound here, and this account's list does not show it as its own (`bound_owner_unconfirmed`:
   *  shared with this account, owned by another, or no longer listed). Not proof of another owner. */
  | { state: 'otherAccount' }
  /** The read failed and nothing here says this computer is bound. */
  | { state: 'unknown' }
  /** Something only the person can do, that no button here does for them. */
  | { state: 'upgrade' }
  /** Cannot be bound from here, and no press changes that: write to support. */
  | { state: 'unavailable' }
  | { state: 'repair' };

export type ComputerBindingCardProps = {
  binding: ComputerBinding;
  computer: string;
  busy?: boolean;
  onBind: () => void;
  onRefresh: () => void;
  onUpdates: () => void;
  onSignInOther: () => void;
};

/** ui/design: desktop/bind/account-*. Below the signed-in account on the Account page. */
export function ComputerBindingCard({ binding, computer, busy = false, onBind, onRefresh, onUpdates, onSignInOther }: ComputerBindingCardProps) {
  const { t } = useTranslation();
  const line = (text: ReactNode) => <p className="mt-3 text-sm leading-6 text-muted">{text}</p>;
  const action = (label: string, run: () => void, brand = false) => <Button variant={brand ? 'brand' : 'outline'} className="mt-4" disabled={busy} onClick={run}>{label}</Button>;
  return <div data-computer-binding={binding.state} className="mt-4 rounded-xl border border-edge bg-surface p-5">
    <div className="flex items-center justify-between gap-3">
      <p className="text-xs font-medium text-muted">{t('desktopAccount.bind.computer')}</p>
      {binding.state === 'bound' && <Badge variant="success">{t('desktopAccount.bind.bound')}</Badge>}
    </div>
    <div className="mt-3 flex items-start gap-3"><Laptop className="mt-0.5 size-5 shrink-0 text-muted" /><p className="min-w-0 break-words text-sm font-semibold [overflow-wrap:anywhere]">{computer}</p></div>
    {binding.state === 'reading' && <Skeleton className="mt-4 h-5 w-2/3" />}
    {binding.state === 'bound' && binding.host && <p className="mt-3 font-mono text-sm [overflow-wrap:anywhere]">
      {binding.url ? <a className="underline underline-offset-4" href={binding.url} target="_blank" rel="noreferrer">{binding.host}</a> : binding.host}
    </p>}
    {binding.state === 'unbound' && <>{line(t('desktopAccount.bind.notBound'))}{action(t('desktopAccount.bind.action'), onBind, true)}</>}
    {binding.state === 'otherAccount' && <>{line(t('desktopAccount.bind.otherAccount'))}
      <div className="mt-4 flex flex-wrap gap-3">
        <Button variant="outline" disabled={busy} onClick={onSignInOther}>{t('desktopAccount.bind.signInOther')}</Button>
        <Button variant="ghost" disabled={busy} onClick={onRefresh}>{t('desktopAccount.bind.checkAgain')}</Button>
      </div></>}
    {binding.state === 'unknown' && <>{line(t('desktopAccount.bind.unknown'))}{action(t('desktopAccount.bind.checkAgain'), onRefresh)}</>}
    {binding.state === 'upgrade' && <>{line(t('desktopAccount.bind.upgrade'))}{action(t('update.action.check'), onUpdates)}</>}
    {binding.state === 'repair' && line(t('desktopAccount.bind.repair'))}
    {binding.state === 'unavailable' && line(<Trans i18nKey="desktopAccount.bind.problem.unavailable" values={{ support: SUPPORT_EMAIL }}
      components={{ mail: <a className="font-medium underline underline-offset-4" href={`mailto:${SUPPORT_EMAIL}`} /> }} />)}
  </div>;
}

export type EligibleName = { prefix: string; host: string };
/** Why a step could not go on, as one sentence; never a raw code. */
export type BindProblem = 'unreachable' | 'signedOut' | 'rateLimited' | 'expired' | 'busy' | 'notResponding' | 'changed' | 'codeInvalid' | 'lost' | 'reauth' | 'storage' | 'unavailable' | 'unknown';
/** Problems whose only safe next step is closing, not another attempt right away. */
const CLOSE_ONLY: BindProblem[] = ['busy', 'rateLimited', 'changed', 'signedOut', 'storage', 'unavailable'];

/** What a review native returned after a verification changed from the one the person confirmed. */
export type BindField = 'account' | 'computer' | 'name' | 'availability';

export type BindStep =
  | { kind: 'opening' }
  /** `new`. `check`: what the last read of this exact name said. `changed`: after a verification. */
  | { kind: 'name'; name: string; check: DomainCheck; held?: EligibleName[]; changed?: BindField[] }
  /** `existing`: a fixed address this account already holds for this computer. */
  | { kind: 'existing'; host: string; changed?: BindField[] }
  /** `name_holding_limit_reached`. Details only when avibe.bot sent them. */
  | { kind: 'limit'; name: string; held: number | null; earliest: string | null; eligible: EligibleName[] }
  | { kind: 'proof' }
  /** avibe.bot accepted the verification; the new sign-in is being read back and saved. `recover`:
   *  that stopped, and the person may continue it (the account's own recovery, no new code). */
  | { kind: 'saving'; recover?: boolean }
  /** `invalid`: avibe.bot said the code typed was wrong or expired; the same email's code may be typed again.
   *  `waitUntil`: a 429's wait, in epoch ms; the step stays locked until it ends. */
  | { kind: 'code'; code: string; invalid?: boolean; limited?: boolean; waitUntil?: number }
  /** `error`: what the last try said, in place. `rateLimited` waits `retryMinutes` here. */
  | { kind: 'factor'; method: SecondFactorMethod | 'recovery'; methods: SecondFactorMethod[]; code: string; error?: 'code' | 'passkey' | 'rateLimited' | null; retryMinutes?: number; waitUntil?: number }
  /** The account must add an authenticator first. `material` once native issued it. */
  | { kind: 'factorSetup'; material: { uri: string; secret: string } | null; code: string; invalid?: boolean; limited?: boolean; retryMinutes?: number; waitUntil?: number }
  /** Recovery codes that setup returned, shown once. */
  | { kind: 'codes'; codes: string[] }
  | { kind: 'bound'; host: string }
  | { kind: 'conflict' }
  | { kind: 'upgrade' }
  | { kind: 'problem'; problem: BindProblem };

export type BindDialogProps = {
  step: BindStep;
  account: string;
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
  /** Cancel (`cancel`) or leave a finished step. */
  onClose: () => void;
};

/** The verification steps keep the sign-in's own screens; this column only changes the way out. */
const Close = createContext<() => void>(() => undefined);
function BindSignInFrame({ children }: SignInFrameProps) {
  const { t } = useTranslation();
  const onClose = useContext(Close);
  return <div data-bind-verify className="flex w-full flex-col">
    {children}
    <div className="mt-4 text-center"><Button variant="ghost" onClick={onClose}>{t('common.cancel')}</Button></div>
  </div>;
}
const BIND_CHROME: SignInChromeValue = { Frame: BindSignInFrame, platform: 'desktop' };

/**
 * ui/design: desktop/bind/*. Claiming this computer's own domain, in one dialog over the Account
 * page: the domain as an address, the account it binds to, one button. Verification, when avibe.bot
 * wants it, happens in the same dialog. Every step that reaches avibe.bot is a press here.
 */
export function BindDialog(props: BindDialogProps) {
  const { t } = useTranslation();
  // Recovery codes are shown once: the dialog does not close until the person says they saved them.
  const held = props.step.kind === 'codes';
  return <Dialog open onOpenChange={(open) => { if (!open && !held) props.onClose(); }}>
    <DialogContent data-bind-dialog className={held ? 'max-w-lg [&>button:last-child]:hidden' : 'max-w-lg'} closeLabel={t('common.close')}
      keepOnOutsideClick={held} onEscapeKeyDown={(event) => { if (held) event.preventDefault(); }}>
      <BindDialogBody {...props} />
    </DialogContent>
  </Dialog>;
}

function BindDialogBody({ step, account, busy = false, onNameChange, onPick, onConfirm, onSendCode, onCodeChange, onSubmitCode, onBeginSetup, onSavedCodes, onFactorStep, onPasskey, onRestart, onRecover, onUpdates, onClose }: BindDialogProps) {
  const { t, i18n } = useTranslation();
  const language = i18n.resolvedLanguage ?? i18n.language;

  if (step.kind === 'code' || step.kind === 'factor') {
    return <Close.Provider value={onClose}><SignInChrome value={BIND_CHROME}>
      <DialogTitle className="sr-only">{t('desktopAccount.bind.title')}</DialogTitle>
      {step.kind === 'code'
        ? <CodeEntry email={account} code={step.code} verifyLabel={t('mobileApp.twoFactor.verify')} busy={busy ? 'verify' : null} locked={busy || Boolean(step.waitUntil)} errorKey={null}
          invalidText={step.invalid ? t('desktopAccount.bind.problem.codeInvalid') : step.limited ? t('mobileApp.code.rateLimited') : undefined} cooldown={0} providerCooldownActive={false} resent={false}
          onCodeChange={onCodeChange} onVerify={() => onSubmitCode(step.code)} />
        : <SecondFactor session={{ status: 'secondFactor', email: account, gate: 'challenge', methods: step.methods }} step={step.method} code={step.code}
          busy={busy ? (step.method === 'passkey' ? 'passkey' : 'verify') : null} locked={busy || Boolean(step.waitUntil)} retryMinutes={step.retryMinutes ?? 1}
          errorKey={step.error === 'passkey' ? 'mobileApp.twoFactor.passkeyFailed' : step.error === 'rateLimited' ? 'mobileApp.twoFactor.rateLimited'
            : step.error === 'code' ? (step.method === 'recovery' ? 'mobileApp.twoFactor.recoveryInvalid' : 'mobileApp.twoFactor.codeInvalid') : null}
          onStep={(next) => { if (next !== 'setup') onFactorStep(next); }} onCodeChange={onCodeChange} onVerify={() => onSubmitCode(step.code)} onPasskey={onPasskey} />}
    </SignInChrome></Close.Provider>;
  }
  if (step.kind === 'factorSetup') {
    return <><DialogTitle className="sr-only">{t('desktopAccount.enrollFactor')}</DialogTitle><AccountSecondFactorView code={step.code} busy={busy || Boolean(step.waitUntil)}
      material={step.material ? { type: 'totp_enrollment', uri: step.material.uri, secret: step.material.secret, qrSvg: '' } : null}
      errorMessage={step.invalid ? t('desktopAccount.bind.problem.codeInvalid') : step.limited ? t('mobileApp.twoFactor.rateLimited', { count: step.retryMinutes ?? 1 }) : null}
      onCodeChange={onCodeChange} onVerify={() => onSubmitCode(step.code)} onEnroll={onBeginSetup} onLocal={onClose} exitLabel={t('common.cancel')} /></>;
  }
  if (step.kind === 'codes') return <><DialogTitle className="sr-only">{t('desktopAccount.recoveryTitle')}</DialogTitle><RecoveryCodesView codes={step.codes} busy={busy} onSaved={onSavedCodes} /></>;

  const title = (text: string, icon?: ReactNode) => <div className="mb-5">
    {icon && <div className="mb-4 grid size-12 place-items-center rounded-2xl border border-edge bg-field">{icon}</div>}
    <DialogTitle className="text-xl font-semibold tracking-tight">{text}</DialogTitle>
  </div>;
  const actions = (primary: ReactNode, cancel = true) => <div className="mt-6 flex flex-wrap justify-end gap-3">
    {cancel && <Button variant="ghost" onClick={onClose}>{t('common.cancel')}</Button>}
    {primary}
  </div>;
  /** 「绑定到 max@example.test」: the account this binds to, whole, on its own line. */
  const boundTo = <p className="mb-3 text-sm [overflow-wrap:anywhere]">{t('desktopAccount.bind.boundTo')} <strong className="font-semibold">{account}</strong></p>;

  if (step.kind === 'opening' || step.kind === 'name' || step.kind === 'existing' || step.kind === 'limit') {
    const changed = step.kind === 'name' || step.kind === 'existing' ? step.changed : undefined;
    const ready = (step.kind === 'name' && (step.check === 'free' || step.check === 'reclaim')) || step.kind === 'existing';
    // Held names other than the one already in the field: that one says 「你用过的」 itself.
    const typed = step.kind === 'name' || step.kind === 'limit' ? step.name : '';
    const held = (step.kind === 'name' ? step.held ?? [] : step.kind === 'limit' ? step.eligible : []).filter((each) => each.prefix !== typed);
    const at = step.kind === 'limit' && step.earliest ? Date.parse(step.earliest) : NaN;
    const date = Number.isFinite(at) ? new Intl.DateTimeFormat(language, { month: 'short', day: 'numeric' }).format(at) : null;
    return <>
      {title(step.kind === 'existing' ? t('desktopAccount.bind.existingTitle') : t('desktopAccount.bind.title'))}
      {changed ? <p role="status" className="mb-4 flex items-start gap-2 text-sm"><Check className="mt-0.5 size-4 shrink-0 text-mint-ink" />{changed.length
        ? t('desktopAccount.bind.changed', { fields: new Intl.ListFormat(language, { type: 'conjunction' }).format(changed.map((field) => t(`desktopAccount.bind.field.${field}` as const))) })
        : t('desktopAccount.bind.changedPlain')}</p> : null}
      {boundTo}
      {step.kind === 'opening' ? <Skeleton className="h-16 w-full rounded-xl" />
        : step.kind === 'existing' ? <DomainField name={step.host.replace(DOMAIN_SUFFIX, '').replace(/\.avibe\.bot$/, '')} check={null} readOnly label={t('desktopAccount.bind.title')} />
        : <DomainField name={step.name} check={step.kind === 'name' ? step.check : null} disabled={busy} onChange={onNameChange} label={t('desktopAccount.bind.title')} />}
      {step.kind === 'existing' && <p className="text-sm leading-6 text-muted">{t('desktopAccount.bind.existing')}</p>}
      {step.kind === 'limit' && <p className="text-sm leading-6">
        {step.held !== null ? t('desktopAccount.bind.limit', { count: step.held }) : t('desktopAccount.bind.limitPlain')}
        {date && <span className="text-muted"> {t('desktopAccount.bind.limitDate', { date })}</span>}
      </p>}
      {held.length > 0 && <div className="mt-3">
        <p className="text-xs text-muted">{t('desktopAccount.bind.takeBack')}</p>
        <div className="mt-2 flex flex-wrap gap-2">{held.map((each) => <Button key={each.prefix} variant="outline" size="sm" className="font-mono" disabled={busy} onClick={() => onPick(each.prefix)}>{each.prefix}</Button>)}</div>
      </div>}
      {actions(<Button variant="brand" disabled={busy || !ready} onClick={onConfirm}>{busy && ready ? t('desktopAccount.bind.binding') : t(step.kind === 'existing' ? 'desktopAccount.bind.confirmExisting' : 'desktopAccount.bind.confirm')}</Button>)}
    </>;
  }

  if (step.kind === 'proof') {
    return <>
      {title(t('desktopAccount.bind.proofTitle'))}
      <DialogDescription className="text-sm leading-6 text-muted">{t('desktopAccount.bind.proofBody', { email: account })}</DialogDescription>
      {actions(<Button variant="brand" disabled={busy} onClick={onSendCode}>{t('desktopAccount.bind.sendCode')}</Button>)}
    </>;
  }

  if (step.kind === 'saving') {
    return <>
      {title(t('desktopAccount.bind.title'))}
      {step.recover ? <p className="text-sm leading-6">{t('desktopAccount.bind.recovery')}</p>
        : <><p role="status" className="mb-4 text-sm text-muted">{t('desktopAccount.bind.saving')}</p><Skeleton className="h-16 w-full rounded-xl" /></>}
      {actions(step.recover ? <Button variant="brand" disabled={busy} onClick={onRecover}>{t('common.continue')}</Button> : null)}
    </>;
  }

  if (step.kind === 'bound') {
    return <>
      {title(t('desktopAccount.bind.boundTitle'), <Check className="size-6 text-mint-ink" />)}
      {boundTo}
      <DomainField name={step.host.replace(DOMAIN_SUFFIX, '').replace(/\.avibe\.bot$/, '')} check={null} readOnly label={t('desktopAccount.bind.boundTitle')} />
      {actions(<Button variant="brand" onClick={onClose}>{t('common.done')}</Button>, false)}
    </>;
  }

  if (step.kind === 'conflict') {
    return <>
      {title(t('desktopAccount.bind.conflictTitle'), <TriangleAlert className="size-6 text-gold-ink" />)}
      <DialogDescription className="text-sm leading-6 text-muted">{t('desktopAccount.bind.conflictBody')}</DialogDescription>
      {actions(<Button variant="brand" onClick={onClose}>{t('common.back')}</Button>, false)}
    </>;
  }

  // One sentence and one safe next step. A refusal that says to wait closes; the rest start a new
  // review from the beginning, never a repeat of the step that failed.
  const problem = step.kind === 'problem' ? step.problem : null;
  const next = step.kind === 'upgrade' ? <Button variant="brand" onClick={onUpdates}>{t('update.action.check')}</Button>
    : problem && CLOSE_ONLY.includes(problem) ? <Button variant="brand" onClick={onClose}>{t('common.close')}</Button>
    : <Button variant="brand" disabled={busy} onClick={onRestart}>{t('desktopAccount.bind.restart')}</Button>;
  return <>
    {title(t('desktopAccount.bind.title'))}
    <div role="alert" className="flex gap-3 rounded-xl border border-gold/40 bg-gold/10 p-4 text-sm leading-6">
      <TriangleAlert className="mt-1 size-4 shrink-0 text-gold-ink" />
      <p>{step.kind === 'upgrade' ? t('desktopAccount.bind.upgrade') : problem === 'unavailable' ? <Trans i18nKey="desktopAccount.bind.problem.unavailable" values={{ support: SUPPORT_EMAIL }}
          components={{ mail: <a className="font-medium underline underline-offset-4" href={`mailto:${SUPPORT_EMAIL}`} /> }} />
        : t(`desktopAccount.bind.problem.${problem ?? 'unknown'}` as const)}</p>
    </div>
    {actions(next, false)}
  </>;
}
