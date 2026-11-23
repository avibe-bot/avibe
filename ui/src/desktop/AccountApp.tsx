import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode, type Ref } from 'react';
import { useTranslation } from 'react-i18next';
import { ArrowLeft } from 'lucide-react';
import { Button } from '../components/ui/button';
import { cn } from '../lib/utils';
import { AccountSecondFactorView, RecoveryCodesView } from './AccountSecondFactorView';
import { DesktopSignIn } from './DesktopSignIn';
import { openLinkInNewContext } from '../lib/pwaNavigation';
import { ACCOUNT_SECURITY_URL } from '../sign-in/links';
import { InstanceSwitcherView } from './InstanceSwitcherView';
import { AccountSignOutView } from './AccountSignOutView';
import { AccountDetailsView } from './AccountDetailsView';
import { SetupCompleteView } from './SetupCompleteView';
import { BindDialog, ComputerBindingCard } from './BindComputerView';
import { useDesktopClaim } from './useDesktopClaim';
import { AddInstanceView } from './AddInstanceView';
import { DESKTOP_CLAIM_VIEW, DESKTOP_PRESENT_EVENT, desktopLocale, hasDesktopClient, presentDesktopClient, isDesktopClientView, type DesktopClientAction, type DesktopClientDirectResult, type DesktopClientState, type DesktopClientView } from './bridge';
import { useAccountClient } from './useAccountClient';
import { UpdatePanel, UpdateSubjectsPanel } from '../components/update/UpdatePanel';
import { instanceSection } from './instanceUpdateModel';
import { modelFromDesktopState } from '../components/update/updatePanelModel';
import { useAcceptedInstall } from '../components/update/useAcceptedInstall';

function Heading({ title, children }: { title: string; children?: ReactNode }) {
  const ref = useRef<HTMLHeadingElement>(null);
  useEffect(() => { ref.current?.focus(); }, [title]);
  return <div className="mb-7"><h1 ref={ref} tabIndex={-1} className="text-2xl font-semibold tracking-tight outline-none">{title}</h1>{children && <p className="mt-3 max-w-xl text-sm leading-6 text-muted">{children}</p>}</div>;
}
function Notice({ children, warning = false }: { children: ReactNode; warning?: boolean }) {
  return <div role={warning ? 'alert' : 'status'} className={cn('rounded-xl border p-4 text-sm leading-6', warning ? 'border-gold/40 bg-gold/10 text-foreground' : 'border-edge bg-field text-muted')}>{children}</div>;
}

/** The update popup's window closes itself (Escape, its backdrop); the shell also closes it on blur. */
function closeUpdatePopover() {
  const internals = (window as unknown as {
    __TAURI_INTERNALS__?: { invoke: (command: string, args: { label: string }) => Promise<unknown> };
  }).__TAURI_INTERNALS__;
  void internals?.invoke('plugin:window|close', { label: UPDATE_POPOVER }).catch(() => undefined);
}

/** This computer's update panel as a popup over another computer's page (ui/design:
 *  chrome/remote-update-popup): the panel at the window's bottom-left, the rest a
 *  transparent backdrop, so a click outside the panel closes it like a popover. The
 *  shell makes the page transparent before its first paint and shows it once loaded. */
export function UpdatePopoverView({ label, onClose, panelRef, children }: {
  label: string;
  onClose: () => void;
  panelRef?: Ref<HTMLDivElement>;
  /** The panel; absent until the desktop client's state arrives, so no empty frame is drawn. */
  children?: ReactNode;
}) {
  return <div data-update-popover className="fixed inset-0" onClick={onClose}>
    {children && <div ref={panelRef} role="dialog" aria-label={label} tabIndex={-1} onClick={(event) => event.stopPropagation()}
      // Both subjects' details open can be taller than a small window: the panel then scrolls inside it.
      className="absolute bottom-2 left-2 max-h-[calc(100%-1rem)] w-80 overflow-x-hidden overflow-y-auto rounded-xl border border-edge bg-popover text-popover-foreground shadow-elevation-2 outline-none">
      {children}
    </div>}
  </div>;
}

function UpdatePopover({ label, children }: { label: string; children?: ReactNode }) {
  const panel = useRef<HTMLDivElement>(null);
  const ready = Boolean(children);
  // The shell's injected style makes the page transparent before first paint; this
  // keeps it transparent should that script not have run.
  useEffect(() => {
    const root = document.documentElement;
    const rootBackground = root.style.background;
    const bodyBackground = document.body.style.background;
    root.style.background = 'transparent';
    document.body.style.background = 'transparent';
    return () => { root.style.background = rootBackground; document.body.style.background = bodyBackground; };
  }, []);
  useEffect(() => { if (ready) panel.current?.focus(); }, [ready]);
  return <UpdatePopoverView label={label} onClose={closeUpdatePopover} panelRef={panel}>{children}</UpdatePopoverView>;
}

const UPDATE_POPOVER = 'update-popover';

/** The same panel as everywhere else, acting on this computer's desktop app. */
export function DesktopUpdatePanel({ state, act, busy, install, close }: {
  state: DesktopClientState;
  act: (action: DesktopClientAction) => Promise<boolean>;
  busy: boolean;
  install: ReturnType<typeof useAcceptedInstall>;
  close: () => void;
}) {
  const update = state.desktop.update;
  const model = modelFromDesktopState(update, { current: state.desktop.version, installFailed: install.failed });
  const target = update.latest;
  const accepts = (action: string) => update.hostActions?.includes(action) ?? false;
  return <UpdatePanel
    model={model}
    actions={{
      check: { run: () => void act({ type: 'update_check' }), disabled: busy || ['checking', 'downloading', 'installing'].includes(update.phase) },
      ...(update.phase === 'available' && target ? {
        // Only an accepted request can fail as an install; a rejected one installed nothing.
        update: { run: () => void act({ type: 'update_install', target_version: target }).then((ok) => { if (ok) install.accept(); }), disabled: busy },
      } : {}),
      ...(accepts('prereleases') ? { setPrereleases: (enabled: boolean) => void act({ type: 'update_set_prereleases', enabled }) } : {}),
      ...(accepts('auto_update') ? { setAutoUpdate: (enabled: boolean) => void act({ type: 'update_set_auto_update', enabled }) } : {}),
      settingsBusy: busy,
      close,
    }}
  />;
}

/** The panel while the app shows an instance on another computer (ui/design: update/instance-and-app): that
 *  instance first, then this computer's desktop app, each with its own state and button. The shell reads and
 *  updates the instance itself, with the app's sign-in; this page only asks it to. On this computer's own
 *  instance there is one thing to update, and the panel is the single one. */
export function DesktopUpdatePanels(props: Parameters<typeof DesktopUpdatePanel>[0]) {
  const { state, act, busy, install, close } = props;
  // The time bounds how long an install the instance reported itself stands while it does not answer.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 30_000);
    return () => window.clearInterval(timer);
  }, []);
  const committed = state.connection.committed;
  const instance = state.instance_update;
  if (committed.kind !== 'remote' || !instance) return <DesktopUpdatePanel {...props} />;
  const shown = instanceSection(instance, now);
  const update = state.desktop.update;
  const appTarget = update.latest;
  const accepts = (action: string) => update.hostActions?.includes(action) ?? false;
  const appChecking = ['checking', 'downloading', 'installing'].includes(update.phase);
  return <UpdateSubjectsPanel
    instanceName={committed.name}
    instance={{
      model: shown.model,
      unavailable: shown.unavailable,
      actions: {
        // Its own check only where the shell would take one, so the section offers no retry the shell refuses.
        ...(shown.canCheck ? { check: { run: () => void act({ type: 'instance_update_check' }), disabled: busy || shown.busy } } : {}),
        ...(shown.canUpdate && shown.target ? {
          update: { run: () => void act({ type: 'instance_update_install', target_version: shown.target! }), disabled: busy },
        } : {}),
        ...(shown.canSet.prereleases ? { setPrereleases: (enabled: boolean) => void act({ type: 'instance_update_set_prereleases', enabled }) } : {}),
        ...(shown.canSet.autoUpdate ? { setAutoUpdate: (enabled: boolean) => void act({ type: 'instance_update_set_auto_update', enabled }) } : {}),
        settingsBusy: busy || shown.busy,
      },
    }}
    app={{
      model: modelFromDesktopState(update, { current: state.desktop.version, installFailed: install.failed }),
      actions: {
        check: { run: () => void act({ type: 'update_check' }), disabled: busy || appChecking },
        ...(update.phase === 'available' && appTarget ? {
          // Only an accepted request can fail as an install; a rejected one installed nothing.
          update: { run: () => void act({ type: 'update_install', target_version: appTarget }).then((ok) => { if (ok) install.accept(); }), disabled: busy },
        } : {}),
        ...(accepts('prereleases') ? { setPrereleases: (enabled: boolean) => void act({ type: 'update_set_prereleases', enabled }) } : {}),
        ...(accepts('auto_update') ? { setAutoUpdate: (enabled: boolean) => void act({ type: 'update_set_auto_update', enabled }) } : {}),
        settingsBusy: busy,
      },
    }}
    // One check asks both updaters: this computer's, then the instance's. Where the shell would refuse the
    // instance's check (a member, an instance that does not answer, a host too old to take one), it reads
    // the instance again instead, which any reader may.
    check={{
      run: () => void act({ type: 'update_check' }).then(() => act({ type: shown.canCheck ? 'instance_update_check' : 'instance_update_refresh' })),
      disabled: busy || appChecking || shown.busy,
    }}
    close={close}
  />;
}

const noop = () => undefined;

// Document-local replay suppression. Native owns one-time admission and consumption.
const attemptedSignOutIntents = new Set<string>();

export function AccountApp() {
  const { t, i18n } = useTranslation();
  const { state, error, send, signOutIntent } = useAccountClient();
  // The shell projects the configured logical control origin before this document mounts. The back
  // button's edge (its hover wash) starts there; its arrow keeps the button's own inset.
  const [controlsLeft] = useState(() => getComputedStyle(document.documentElement).getPropertyValue('--shell-window-controls-left').trim());
  const [popover] = useState(() => new URLSearchParams(window.location.search).get('view') === UPDATE_POPOVER);
  const install = useAcceptedInstall(state?.desktop.update.phase);
  const [view, setView] = useState<DesktopClientView | typeof DESKTOP_CLAIM_VIEW | 'profile' | 'add'>(() => {
    const initial = new URLSearchParams(window.location.search).get('view');
    return initial === DESKTOP_CLAIM_VIEW ? initial : isDesktopClientView(initial) ? initial : 'account';
  });
  // The view native opened this document for. Claim commands are admitted only in an Account or
  // claim document, so another document opens a real one rather than drawing the page itself.
  const [documentView] = useState(() => new URLSearchParams(window.location.search).get('view') ?? 'account');
  // ui/design: desktop/bind-home/*. This computer's binding: read on the Account page, claimed only
  // in the claim view, which the Workbench's 「开启远程访问」 asks the shell to open.
  const claimDocument = documentView === DESKTOP_CLAIM_VIEW;
  const claim = useDesktopClaim(state, !popover && (claimDocument ? view === 'claim'
    : documentView === 'account' && (view === 'account' || view === 'profile')), claimDocument ? 'claim' : 'account');
  // The in-app authenticator setup's code; the sign-in steps keep their own (DesktopSignIn).
  const [code, setCode] = useState('');
  const [busy, setBusy] = useState(false);
  // When the last action was one sent to the selected instance's updater: the time of that click.
  const [instanceAction, setInstanceAction] = useState<number | null>(null);
  const [selection, setSelection] = useState<{ id: string; generation: number; error: string | null } | null>(null);
  const [material, setMaterial] = useState<{ generation: number; result: DesktopClientDirectResult } | null>(null);
  // "Use a different account" that could not forget the stored account: shown until the next step.
  const [forgetFailure, setForgetFailure] = useState<{ code: string; generation: number } | null>(null);
  const epoch = useRef(0);
  const requestBusy = useRef(false);
  // Synchronous eligibility spans the request and direct result before React
  // commits material. Native still owns authoritative invalidation/cleanup.
  const recoveryProtection = useRef<{ generation: number } | null>(null);
  const direct = material && material.generation === state?.generation ? material.result : null;
  // Recovery codes a binding's authenticator setup returned are shown once, like the ordinary ones:
  // nothing leaves the document until the person says they saved them.
  const claimCodes = claim.step?.kind === 'codes';
  const claimCodesShown = useRef(false);
  useLayoutEffect(() => { claimCodesShown.current = claimCodes; }, [claimCodes]);
  const awaitingRecoveryAcknowledgement = (direct?.type === 'totp_enrolled' && !!direct.recoveryCodes?.length) || claimCodes;
  const [inputGeneration, setInputGeneration] = useState(state?.generation);
  if (inputGeneration !== state?.generation) {
    setInputGeneration(state?.generation);
    setCode('');
    if (material && material.generation !== state?.generation) setMaterial(null);
  }
  useLayoutEffect(() => {
    if (recoveryProtection.current && recoveryProtection.current.generation !== state?.generation) recoveryProtection.current = null;
  }, [state?.generation]);
  useEffect(() => () => { epoch.current += 1; recoveryProtection.current = null; }, []);
  useEffect(() => {
    if (popover) return;
    const present = (event: Event) => {
      if (!hasDesktopClient() || !event.cancelable) return;
      // Consume malformed input as well: it must never become a tokenless fallback.
      event.preventDefault();
      const detail: unknown = (event as CustomEvent).detail;
      const view: unknown = (detail as { view?: unknown } | null)?.view;
      const token: unknown = (detail as { presentationToken?: unknown } | null)?.presentationToken;
      if (!isDesktopClientView(view) || typeof token !== 'string' || !token.length
        || new TextEncoder().encode(token).length > 128 || recoveryProtection.current || claimCodesShown.current) return;
      presentDesktopClient(view, token, desktopLocale(i18n.resolvedLanguage ?? i18n.language));
    };
    window.addEventListener(DESKTOP_PRESENT_EVENT, present);
    return () => window.removeEventListener(DESKTOP_PRESENT_EVENT, present);
  }, [i18n, popover]);
  const act = useCallback(async (action: DesktopClientAction): Promise<boolean> => {
    if (requestBusy.current) return false;
    setInstanceAction(action.type.startsWith('instance_update_') ? Date.now() : null);
    requestBusy.current = true;
    setBusy(true);
    const ticket = epoch.current;
    const generation = state?.generation ?? 0;
    const finalization = action.type === 'finish_totp_enrollment' ? { generation } : null;
    if (finalization) recoveryProtection.current = finalization;
    const selectedId = action.type === 'select_instance' ? action.instance_id
      : action.type === 'decide_consent' && action.approve ? state?.consent?.instance_id : null;
    if (selectedId) setSelection({ id: selectedId, generation, error: null });
    if (action.type === 'refresh_instances') setSelection(null);
    try {
      const reply = await send(action);
      if (ticket !== epoch.current) return false;
      if (reply && 'kind' in reply) {
        if (selectedId) setSelection({ id: selectedId, generation: reply.generation, error: reply.code });
        return false;
      }
      if (!reply?.ok) {
        if (selectedId && reply) setSelection({ id: selectedId, generation: reply.state.generation, error: reply.error.code });
        return false;
      }
      setCode('');
      if (action.type === 'begin_totp_enrollment' && reply.result?.type === 'totp_enrollment') {
        setMaterial({ generation: reply.state.generation, result: reply.result });
      } else if (action.type === 'finish_totp_enrollment' && reply.result?.type === 'totp_enrolled') {
        recoveryProtection.current = reply.result.recoveryCodes?.length ? { generation: reply.state.generation } : null;
        setMaterial({ generation: reply.state.generation, result: reply.result });
      }
      return true;
    } finally {
      if (finalization && recoveryProtection.current === finalization) recoveryProtection.current = null;
      requestBusy.current = false;
      setBusy(false);
    }
  }, [send, state?.generation, state?.consent?.instance_id]);
  const leave = useCallback((type: 'cancel' | 'return_local' | 'sign_out', recoveryAcknowledged = false) => {
    // Only the saved-codes action acknowledges; native invalidation remains independent.
    if ((recoveryProtection.current || claimCodesShown.current) && !recoveryAcknowledged) return;
    epoch.current += 1;
    recoveryProtection.current = null;
    setMaterial(null);
    setCode('');
    setSelection(null);
    // Escape remains usable during a blocked or slow request. Native fences late completion.
    void send({ type });
  }, [send]);
  // Leaving the claim view ends its review (Cancel) before the shell returns to the Workbench; the
  // shell also retires it when the document is hidden.
  const { close: closeClaim } = claim;
  const exitClaim = useCallback(() => {
    if (claimCodesShown.current) return;
    closeClaim();
    leave('cancel');
  }, [closeClaim, leave]);
  const exit = claimDocument && view === 'claim' ? exitClaim : () => leave('cancel');
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || event.isComposing || event.defaultPrevented) return;
      event.preventDefault();
      // The popup has no account flow to cancel: Escape closes it, as it closes a popover.
      if (popover) { closeUpdatePopover(); return; }
      // The claim dialog closes itself on Escape (and leaves through exitClaim).
      if (claimDocument && view === 'claim') return;
      leave('cancel');
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [leave, popover, claimDocument, view]);
  useEffect(() => {
    if (popover || !signOutIntent || signOutIntent.generation !== state?.generation || attemptedSignOutIntents.has(signOutIntent.intent_id)) return;
    // Like the hook's initial native read, defer dispatch until this effect is
    // still mounted. A replaced document/effect must not start an old intent.
    let active = true;
    void Promise.resolve().then(() => {
      if (!active || attemptedSignOutIntents.has(signOutIntent.intent_id)) return;
      attemptedSignOutIntents.add(signOutIntent.intent_id);
      epoch.current += 1;
      recoveryProtection.current = null;
      setMaterial(null);
      setCode('');
      void send({ type: 'sign_out', intent_id: signOutIntent.intent_id });
    });
    return () => { active = false; };
  }, [signOutIntent, state?.generation, send, popover]);
  // The Account page in an Account document: here if this is one, else native opens one.
  const openAccount = () => {
    if (documentView === 'account') { setView('profile'); return; }
    if (recoveryProtection.current) return;
    presentDesktopClient('account', undefined, desktopLocale(i18n.resolvedLanguage ?? i18n.language));
  };
  const instances = () => {
    if (recoveryProtection.current) return;
    setMaterial(null); setView('instances'); void act({ type: 'refresh_instances' });
  };
  const explainError = (error: string) => t(error === 'bridge_unavailable' || error === 'bridge_incompatible' ? 'desktopAccount.errors.bridge' : error === 'instance_upgrade_required' ? 'desktopAccount.errors.upgrade' : error === 'invalid_credentials' ? 'desktopAccount.errors.credentials' : error === 'rate_limited' ? 'desktopAccount.errors.rateLimit' : error === 'invalid_otp' || error === 'invalid_code' ? 'desktopAccount.errors.code' : 'desktopAccount.errors.request');
  // An install the instance refused or failed is told by its section (the shell's receipt says `failed`). Any
  // other failed action there, a check or a switch, has no such place, and keeps this page's notice.
  // A refusal that leaves no receipt (the shell busy, a selection under way, the offer changed) keeps it too.
  const instanceRequest = state?.instance_update?.request;
  const toldBySection = instanceAction !== null && instanceRequest?.action === 'install' && instanceRequest.phase === 'failed'
    && Date.parse(instanceRequest.updated_at) >= instanceAction - 1000;
  // The update views, the popup included, are not where an enrollment failure is told.
  const displayedError = (toldBySection ? null : error) ?? (popover || view === 'updates' || view === 'setup-complete' ? null : state?.enrollment.error_code);
  const errorMessage = displayedError ? explainError(displayedError) : null;
  const activeSelection = selection?.generation === state?.generation ? selection : null;
  const failedPending = activeSelection?.error && activeSelection.id === state?.connection.pending?.instance_id;
  const switcher = state && <InstanceSwitcherView state={state} localInstanceId={state.local.instance_id ?? null}
    busy={busy} pendingId={failedPending ? null : state.connection.status === 'switching' ? state.connection.pending?.instance_id ?? null : busy && activeSelection ? activeSelection.id : null}
    failure={activeSelection?.error ? { instanceId: activeSelection.id, message: explainError(activeSelection.error) } : null}
    onAccount={openAccount} onAdd={() => setView('add')} onSelect={(instance_id) => void act({ type: 'select_instance', instance_id })}
    onLocal={() => leave('return_local')} onCancel={() => leave('cancel')} onRefresh={() => void act({ type: 'refresh_instances' })}
    onConsent={(approve) => void act({ type: 'decide_consent', approve })} consentError={state.consent ? errorMessage : null} />;
  const failure = errorMessage && (popover || view === 'updates' || view === 'setup-complete'
    ? <Notice warning>{errorMessage}</Notice>
    : <p role="alert" className="mt-4 text-sm leading-6 text-destructive-ink">{errorMessage}</p>);
  if (popover) {
    // Nothing is drawn until the state arrives; a failure to get it is still shown.
    return <UpdatePopover label={t('dashboard.versionAndUpdate')}>
      {(state || failure) && <>
        {state && <DesktopUpdatePanels state={state} act={act} busy={busy} install={install} close={closeUpdatePopover} />}
        {failure && <div className={state ? 'p-4 pt-0' : 'p-4'}>{failure}</div>}
      </>}
    </UpdatePopover>;
  }
  /** The App's own sign-in, as its page or as the claim dialog's first face (whose way out cancels). */
  const signIn = (inClaim: boolean) => state && <><DesktopSignIn state={state} send={send}
      onLocal={() => { if (inClaim) exitClaim(); else leave('return_local'); }} exitLabel={inClaim ? t('common.cancel') : undefined}
      onDifferentAccount={() => void send({ type: 'abort_sign_in' }).then((reply) => {
        setForgetFailure(reply && 'ok' in reply && !reply.ok ? { code: reply.error.code, generation: reply.state.generation } : null);
      })}
      onOpenSecurity={() => { openLinkInNewContext(ACCOUNT_SECURITY_URL, 'noopener,noreferrer'); }}
      renderEnroll={() => <AccountSecondFactorView code={code} busy={busy}
        material={direct?.type === 'totp_enrollment' ? direct : null} errorMessage={errorMessage}
        onCodeChange={setCode}
        onVerify={() => void act({ type: 'finish_totp_enrollment', code })}
        onEnroll={() => void act({ type: 'begin_totp_enrollment' })} onLocal={() => { if (inClaim) exitClaim(); else leave('return_local'); }} exitLabel={inClaim ? t('common.cancel') : undefined} />} />
      {forgetFailure?.generation === state.generation && <p role="alert" className="mx-auto mt-4 w-full max-w-md text-sm leading-6 text-destructive-ink">{explainError(forgetFailure.code)}</p>}</>;
  let content: ReactNode;
  let centerLogin = false;
  if (!state) {
    content = <div className="mx-auto max-w-xl pt-12"><Heading title={t('desktopAccount.loading')} />{failure}<Button variant="outline" className="mt-6" onClick={() => void act({ type: 'get_state' })} disabled={busy}>{t('common.retry')}</Button></div>;
  } else if (view === 'updates') {
    // ui/design: update/panel/trusted-view
    // With the instance's section beside the app's, the panel says whose each is; the heading only names the page.
    const both = state.connection.committed.kind === 'remote' && Boolean(state.instance_update);
    content = <div className="mx-auto w-full max-w-md pt-12">{both ? <Heading title={t('dashboard.versionAndUpdate')} /> : <Heading title={t('update.trustedView.title')}>{t('update.trustedView.body')}</Heading>}<div className="overflow-hidden rounded-xl border border-edge bg-popover shadow-elevation-2"><DesktopUpdatePanels state={state} act={act} busy={busy} install={install} close={() => leave('cancel')} /></div>{failure}</div>;
  } else if (view === 'sign-out') {
    content = <AccountSignOutView signingOut={!!signOutIntent} errorMessage={errorMessage} onRetry={() => leave('sign_out')} onBack={() => leave('cancel')} />;
  } else if (view === 'setup-complete') {
    content = <SetupCompleteView onLocal={() => leave('return_local')} onSignIn={openAccount} failure={failure} />;
  } else if (direct?.type === 'totp_enrolled' && direct.recoveryCodes?.length) {
    content = <RecoveryCodesView codes={direct.recoveryCodes} busy={busy} onSaved={() => {
      recoveryProtection.current = null;
      setMaterial(null);
      // The claim view goes on to the name; elsewhere a finished sign-in returns to the Workbench.
      if (claimDocument) setView('claim');
      else if (state.account.status === 'signed_in' && state.enrollment.status === 'paired_this_account' && !errorMessage) leave('cancel', true);
      else setView('account');
    }}>{failure}</RecoveryCodesView>;
  } else if (claimDocument && view === 'claim') {
    // ui/design: desktop/bind-home/*-sheet. The shell's own document in front of the Workbench, and
    // the one dialog over it: the App's sign-in first if needed, then the name.
    content = claim.step && <BindDialog step={claim.step} account={claim.account || (state.account.identity?.email ?? '')} busy={claim.busy}
      signIn={signIn(true)}
      onNameChange={claim.changeName} onPick={(prefix) => void claim.pick(prefix)} onConfirm={() => void claim.confirm()}
      onSendCode={() => void claim.sendCode()} onCodeChange={claim.setCode} onSubmitCode={(code) => void claim.submitCode(code)}
      onBeginSetup={() => void claim.beginSetup()} onSavedCodes={() => void claim.savedCodes()}
      onFactorStep={claim.factorStep} onPasskey={() => void claim.passkey()}
      onRestart={claim.restart} onRecover={() => void claim.recoverSaving()} onUpdates={() => setView('updates')}
      onCheckAgain={() => void claim.recheck()} onSignInOther={() => { setView('sign-out'); leave('sign_out'); }} onClose={exitClaim} />;
  // A binding in progress keeps its dialog, also while its new sign-in owes a second factor.
  } else if (state.account.status !== 'signed_in') {
    // ui/design: desktop/sign-in/*. The iPhone's steps and words (Max, 2026-11-18); the window keeps
    // "continue on this computer", Back to Workbench and the in-app authenticator setup below.
    centerLogin = true;
    content = signIn(false);
  } else if (state.consent || (state.connection.status === 'switching' && !failedPending) || (state.connection.status === 'remote_unreachable' && view === 'instances')) {
    content = switcher;
  } else if (view === 'profile' || view === 'account') {
    content = <AccountDetailsView email={state.account.identity?.email ?? state.account.email ?? ''}
      busy={busy} errorMessage={errorMessage} onBack={instances}
      onSignOut={() => { setView('sign-out'); leave('sign_out'); }}>
      {/* ui/design: desktop/bind-home/account-*. The Account page keeps only the account: this
          computer shows only when bound, as one read-only line with its domain. Binding lives in
          Settings › Devices (the claim view). */}
      {claim.binding.state === 'bound' && <ComputerBindingCard binding={claim.binding} computer={state.local.name}
        onBind={noop} onRefresh={noop} onUpdates={noop} onSignInOther={noop} />}
    </AccountDetailsView>;
  } else if (view === 'add') {
    content = <AddInstanceView onBack={() => setView('instances')} onRefresh={instances} busy={busy} />;
  } else {
    content = switcher;
  }
  return <div className="flex h-dvh flex-col bg-background text-foreground">
    <div aria-hidden="true" className="h-[var(--shell-titlebar-inset)] shrink-0 bg-surface" />
    {!awaitingRecoveryAcknowledgement && <header className="flex shrink-0 items-center px-8 py-3" style={controlsLeft ? { paddingLeft: 'var(--shell-window-controls-left)' } : undefined}><Button variant="ghost" size="sm" onClick={exit}><ArrowLeft className="size-4" />{t('desktopAccount.backWorkbench')}</Button></header>}
    <main className={cn('min-h-0 flex-1 overflow-y-auto px-10 pb-5', centerLogin && 'flex flex-col')} aria-busy={busy}>{content}</main>
  </div>;
}
