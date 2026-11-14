import { useCallback, useEffect, useRef, useState, type ReactNode, type Ref } from 'react';
import { useTranslation } from 'react-i18next';
import { ArrowLeft, ArrowRight, Check, Laptop, ShieldCheck, TriangleAlert } from 'lucide-react';
import { Button } from '../components/ui/button';
import { Badge } from '../components/ui/badge';
import { Skeleton } from '../components/ui/skeleton';
import { cn } from '../lib/utils';
import { AccountSecondFactorView } from './AccountSecondFactorView';
import { AccountLoginView } from './AccountLoginView';
import { InstanceSwitcherView } from './InstanceSwitcherView';
import { isDesktopClientView, type DesktopClientAction, type DesktopClientDirectResult, type DesktopClientState } from './bridge';
import { useAccountClient } from './useAccountClient';
import { UpdatePanel } from '../components/update/UpdatePanel';
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
      className="absolute bottom-2 left-2 w-80 overflow-hidden rounded-xl border border-edge bg-popover text-popover-foreground shadow-elevation-2 outline-none">
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

// Document-local replay suppression. Native owns one-time admission and consumption.
const attemptedSignOutIntents = new Set<string>();

export function AccountApp() {
  const { t } = useTranslation();
  const { state, error, send, signOutIntent } = useAccountClient();
  const [popover] = useState(() => new URLSearchParams(window.location.search).get('view') === UPDATE_POPOVER);
  const install = useAcceptedInstall(state?.desktop.update.phase);
  const [view, setView] = useState(() => {
    const initial = new URLSearchParams(window.location.search).get('view');
    return isDesktopClientView(initial) ? initial : 'account';
  });
  const [email, setEmail] = useState('');
  const [code, setCode] = useState('');
  const [password, setPassword] = useState('');
  const [passwordMode, setPasswordMode] = useState(false);
  const [passwordHelp, setPasswordHelp] = useState(false);
  const [editEmail, setEditEmail] = useState(false);
  const [method, setMethod] = useState<'totp' | 'recovery'>('totp');
  const [busy, setBusy] = useState(false);
  const [reviewChange, setReviewChange] = useState(false);
  const [selection, setSelection] = useState<{ id: string; generation: number; error: string | null } | null>(null);
  const [offlineId, setOfflineId] = useState<string | null>(null);
  const [material, setMaterial] = useState<{ generation: number; result: DesktopClientDirectResult } | null>(null);
  const epoch = useRef(0);
  const requestBusy = useRef(false);
  const direct = material && material.generation === state?.generation ? material.result : null;
  const [inputGeneration, setInputGeneration] = useState(state?.generation);
  if (inputGeneration !== state?.generation) {
    setInputGeneration(state?.generation);
    setCode('');
    setPassword('');
    setOfflineId(null);
    if (material && material.generation !== state?.generation) setMaterial(null);
  }
  useEffect(() => () => { epoch.current += 1; }, []);
  const act = useCallback(async (action: DesktopClientAction): Promise<boolean> => {
    if (requestBusy.current) return false;
    requestBusy.current = true;
    setBusy(true);
    const ticket = epoch.current;
    const generation = state?.generation ?? 0;
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
      if (action.type === 'request_otp') setEditEmail(false);
      if (action.type === 'begin_totp_enrollment' && reply.result?.type === 'totp_enrollment') {
        setMaterial({ generation: reply.state.generation, result: reply.result });
      } else if (action.type === 'finish_totp_enrollment' && reply.result?.type === 'totp_enrolled') {
        setMaterial({ generation: reply.state.generation, result: reply.result });
      }
      return true;
    } finally {
      requestBusy.current = false;
      setBusy(false);
    }
  }, [send, state?.generation, state?.consent?.instance_id]);
  const leave = (type: 'cancel' | 'return_local' | 'sign_out') => {
    epoch.current += 1;
    setMaterial(null);
    setCode('');
    setPassword('');
    setSelection(null);
    // Escape remains usable during a blocked or slow request. Native fences late completion.
    void send({ type });
  };
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || event.isComposing || event.defaultPrevented) return;
      event.preventDefault();
      // The popup has no account flow to cancel: Escape closes it, as it closes a popover.
      if (popover) { closeUpdatePopover(); return; }
      epoch.current += 1;
      setMaterial(null);
      setCode('');
      setPassword('');
      setSelection(null);
      void send({ type: 'cancel' });
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [send, popover]);
  useEffect(() => {
    if (popover || !signOutIntent || signOutIntent.generation !== state?.generation || attemptedSignOutIntents.has(signOutIntent.intent_id)) return;
    // Like the hook's initial native read, defer dispatch until this effect is
    // still mounted. A replaced document/effect must not start an old intent.
    let active = true;
    void Promise.resolve().then(() => {
      if (!active || attemptedSignOutIntents.has(signOutIntent.intent_id)) return;
      attemptedSignOutIntents.add(signOutIntent.intent_id);
      epoch.current += 1;
      setMaterial(null);
      setCode('');
      setPassword('');
      void send({ type: 'sign_out', intent_id: signOutIntent.intent_id });
    });
    return () => { active = false; };
  }, [signOutIntent, state?.generation, send, popover]);
  const localButton = <Button variant="ghost" onClick={() => leave('return_local')}>{t('desktopClient.continueLocal')}</Button>;
  const instances = () => { setMaterial(null); setView('instances'); void act({ type: 'refresh_instances' }); };
  const explainError = (error: string) => t(error === 'bridge_unavailable' || error === 'bridge_incompatible' ? 'desktopClient.errors.bridge' : error === 'instance_upgrade_required' ? 'desktopClient.errors.upgrade' : error === 'invalid_credentials' ? 'desktopClient.errors.credentials' : error === 'rate_limited' ? 'desktopClient.errors.rateLimit' : error === 'invalid_otp' || error === 'invalid_code' ? 'desktopClient.errors.code' : 'desktopClient.errors.request');
  const errorMessage = error ? explainError(error) : null;
  const activeSelection = selection?.generation === state?.generation ? selection : null;
  const failedPending = activeSelection?.error && activeSelection.id === state?.connection.pending?.instance_id;
  const switcher = state && <InstanceSwitcherView state={state} localInstanceId={state.local.instance_id ?? null}
    busy={busy} pendingId={failedPending ? null : state.connection.status === 'switching' ? state.connection.pending?.instance_id ?? null : busy && activeSelection ? activeSelection.id : null}
    failure={activeSelection?.error ? { instanceId: activeSelection.id, message: explainError(activeSelection.error) } : null}
    offlineId={offlineId} onOfflineChange={setOfflineId} onSelect={(instance_id) => void act({ type: 'select_instance', instance_id })}
    onLocal={() => leave('return_local')} onCancel={() => leave('cancel')} onRefresh={() => void act({ type: 'refresh_instances' })}
    onConsent={(approve) => void act({ type: 'decide_consent', approve })} consentError={state.consent ? errorMessage : null} />;
  const failure = errorMessage && (popover || view === 'updates' || view === 'setup-complete'
    ? <Notice warning>{errorMessage}</Notice>
    : <p role="alert" className="mt-4 text-sm leading-6 text-destructive-ink">{errorMessage}</p>);
  if (popover) {
    // Nothing is drawn until the state arrives; a failure to get it is still shown.
    return <UpdatePopover label={t('dashboard.versionAndUpdate')}>
      {(state || failure) && <>
        {state && <DesktopUpdatePanel state={state} act={act} busy={busy} install={install} close={closeUpdatePopover} />}
        {failure && <div className={state ? 'p-4 pt-0' : 'p-4'}>{failure}</div>}
      </>}
    </UpdatePopover>;
  }
  let content: ReactNode;
  let centerLogin = false;
  if (!state) {
    content = <div className="mx-auto max-w-xl pt-12"><Heading title={t('desktopClient.loading')} />{failure}<Button variant="outline" className="mt-6" onClick={() => void act({ type: 'get_state' })} disabled={busy}>{t('common.retry')}</Button></div>;
  } else if (view === 'updates') {
    // ui/design: update/panel/trusted-view
    content = <div className="mx-auto w-full max-w-md pt-12"><Heading title={t('update.trustedView.title')}>{t('update.trustedView.body')}</Heading><div className="overflow-hidden rounded-xl border border-edge bg-popover shadow-elevation-2"><DesktopUpdatePanel state={state} act={act} busy={busy} install={install} close={() => leave('cancel')} /></div>{failure}</div>;
  } else if (view === 'sign-out') {
    content = <div className="mx-auto max-w-xl pt-12"><Heading title={t(signOutIntent && !error ? 'desktopClient.signingOut' : 'desktopClient.signOut')}>{t('desktopClient.signOutReturn')}</Heading>{failure}<div className="mt-7 flex gap-3">{error && <Button variant="brand" onClick={() => leave('sign_out')}>{t('common.retry')}</Button>}<Button variant="outline" onClick={() => leave('cancel')}>{t('desktopClient.backWorkbench')}</Button></div></div>;
  } else if (view === 'setup-complete') {
    content = <div className="mx-auto max-w-xl pt-12"><ShieldCheck className="mb-6 size-10 text-mint-ink" /><Heading title={t('desktopClient.setupReady')}>{t('desktopClient.setupBody')}</Heading><Notice>{t('desktopClient.registrationDisclosure')}</Notice><div className="mt-7 flex flex-wrap gap-3"><Button variant="brand" onClick={() => leave('return_local')}>{t('desktopClient.startLocal')}</Button><Button variant="outline" onClick={() => setView('account')}>{t('desktopClient.signInAndAdd')}<ArrowRight className="size-4" /></Button></div><p className="mt-5 text-sm text-muted">{t('desktopClient.signInLater')}</p>{failure}</div>;
  } else if (direct?.type === 'totp_enrolled' && direct.recoveryCodes?.length) {
    content = <div className="mx-auto max-w-xl pt-12"><Heading title={t('desktopClient.recoveryTitle')}>{t('desktopClient.recoveryBody')}</Heading><ul className="grid grid-cols-2 gap-3 rounded-xl border border-edge bg-field p-5 font-mono text-sm select-text">{direct.recoveryCodes.map((value) => <li key={value}>{value}</li>)}</ul><Button variant="brand" className="mt-7" onClick={() => setMaterial(null)}>{t('desktopClient.savedCodes')}</Button>{failure}</div>;
  } else if (state.account.status === 'second_factor_required') {
    centerLogin = true;
    content = <AccountSecondFactorView enroll={state.account.gate === 'enroll'} methods={state.account.methods}
      method={method} code={code} busy={busy} material={direct?.type === 'totp_enrollment' ? direct : null} errorMessage={errorMessage}
      onCodeChange={setCode} onMethodChange={(method) => { setCode(''); setMethod(method); }}
      onVerify={() => void act(direct?.type === 'totp_enrollment' ? { type: 'finish_totp_enrollment', code } : { type: 'verify_second_factor', method, code })}
      onEnroll={() => void act({ type: 'begin_totp_enrollment' })} onPasskey={() => void act({ type: 'verify_passkey' })} onLocal={() => leave('return_local')} />;
  } else if (state.account.status !== 'signed_in') {
    centerLogin = true;
    const verify = state.account.status === 'verifying' && !editEmail;
    content = <AccountLoginView verify={verify} email={state.account.email ?? ''}
      value={verify ? code : email} busy={busy} errorMessage={errorMessage}
      onValueChange={verify ? setCode : setEmail}
      password={{ active: passwordMode, value: password, forgot: passwordHelp, onChange: setPassword,
        onToggle: () => { setPassword(''); setPasswordMode(!passwordMode); setPasswordHelp(false); },
        onForgot: () => { setPassword(''); setPasswordMode(false); setPasswordHelp(true); } }}
      onSubmit={() => {
        const action: DesktopClientAction = verify ? { type: 'verify_otp', code } : passwordMode
          ? { type: 'sign_in_password', email: email.trim(), password } : { type: 'request_otp', email: email.trim() };
        setPassword('');
        void act(action);
      }}
      onChangeEmail={() => { setCode(''); setEmail(state.account.email ?? ''); setEditEmail(true); }}
      onResendCode={() => void act({ type: 'request_otp', email: state.account.email ?? email })}
      onContinueLocal={() => leave('return_local')} />;
  } else if (state.consent || state.connection.status === 'switching' || state.connection.status === 'remote_unreachable') {
    content = switcher;
  } else if (view !== 'instances' && state.enrollment.status !== 'unknown' && state.enrollment.status !== 'unpaired') {
    const status = state.enrollment.status;
    const conflict = status === 'conflict';
    const failed = status === 'failed';
    const tunnel = status === 'paired_tunnel_unavailable';
    const registering = status === 'registering';
    content = <div className="mx-auto max-w-xl pt-12"><div className="mb-6 grid size-14 place-items-center rounded-2xl border border-edge bg-field">{conflict || failed || tunnel ? <TriangleAlert className="size-6 text-gold-ink" /> : registering ? <Laptop className="size-6 text-muted" /> : <ShieldCheck className="size-6 text-mint-ink" />}</div><Heading title={t(conflict ? 'desktopClient.conflictTitle' : failed ? 'desktopClient.registrationFailed' : tunnel ? 'desktopClient.tunnelUnavailable' : registering ? 'desktopClient.registering' : 'desktopClient.registered')}>{state.local.name}</Heading>
      {conflict ? <><Notice warning>{t('desktopClient.conflictBody')}</Notice>{reviewChange && <p role="status" className="mt-5 text-sm leading-6 text-muted">{t('desktopClient.transferSteps')}</p>}<div className="mt-7 flex flex-wrap gap-3"><Button variant="brand" onClick={() => leave('return_local')}>{t('desktopClient.keepLink')}</Button><Button variant="outline" onClick={() => setReviewChange(true)}>{t('desktopClient.reviewChange')}</Button></div><Button variant="link" className="mt-3 px-0" onClick={instances}>{t('desktopClient.otherInstances')}</Button></>
        : registering ? <><p className="mb-4 flex items-center gap-2 text-sm"><Check className="size-4 text-mint-ink" />{t('desktopClient.signedIn')}</p><Skeleton className="h-5 w-3/4" /><p className="mt-4 text-sm text-muted">{t('desktopClient.registeringBody')}</p><div className="mt-7">{localButton}</div></>
        : <><Notice warning={failed || tunnel}>{failed ? t('desktopClient.registrationFailedBody') : <><div className="flex items-center justify-between gap-3"><p className="font-medium">{t('desktopClient.remoteAccessTitle')}</p><Badge variant={tunnel ? 'warning' : 'success'}>{t(`desktopClient.remoteAccess.${state.local.remote_access}`)}</Badge></div><p className="mt-3">{t(tunnel ? 'desktopClient.tunnelBody' : 'desktopClient.registeredBody')}</p></>}</Notice><div className="mt-7 flex flex-wrap gap-3"><Button variant="brand" disabled={busy} onClick={() => failed ? void act({ type: 'retry_enrollment' }) : tunnel ? void act({ type: 'retry_local_remote_access' }) : instances()}>{t(failed ? 'common.retry' : tunnel ? 'desktopClient.retryRemoteAccess' : 'desktopClient.viewInstances')}</Button>{localButton}</div>{(failed || tunnel) && <Button variant="link" className="mt-3 px-0" onClick={instances}>{t('desktopClient.otherInstances')}</Button>}</>}{failure}</div>;
  } else {
    content = switcher;
  }
  return <div className="flex h-dvh flex-col bg-background text-foreground">
    <div aria-hidden="true" className="h-[var(--shell-titlebar-inset)] shrink-0 bg-surface" />
    <header className="flex shrink-0 items-center px-8 py-3"><Button variant="ghost" size="sm" onClick={() => leave('cancel')}><ArrowLeft className="size-4" />{t('desktopClient.backWorkbench')}</Button></header>
    <main className={cn('min-h-0 flex-1 overflow-y-auto px-10 pb-5', centerLogin && 'flex flex-col')} aria-busy={busy}>{content}</main>
  </div>;
}
