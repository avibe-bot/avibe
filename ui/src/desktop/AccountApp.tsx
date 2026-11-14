import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { ArrowLeft, ArrowRight, Check, ChevronRight, Laptop, RefreshCw, Server, ShieldCheck, TriangleAlert, WifiOff } from 'lucide-react';
import { QRCodeSVG } from 'qrcode.react';
import { Button } from '../components/ui/button';
import { Badge } from '../components/ui/badge';
import { Input } from '../components/ui/input';
import { Label } from '../components/ui/label';
import { Skeleton } from '../components/ui/skeleton';
import { cn } from '../lib/utils';
import { AccountLoginView } from './AccountLoginView';
import { isDesktopClientView, type DesktopClientAction, type DesktopClientDirectResult, type Instance } from './bridge';
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

export function AccountApp() {
  const { t } = useTranslation();
  const { state, error, send } = useAccountClient();
  const install = useAcceptedInstall(state?.desktop.update.phase);
  const [view, setView] = useState(() => {
    const initial = new URLSearchParams(window.location.search).get('view');
    return isDesktopClientView(initial) ? initial : 'account';
  });
  const [email, setEmail] = useState('');
  const [code, setCode] = useState('');
  const [editEmail, setEditEmail] = useState(false);
  const [method, setMethod] = useState<'totp' | 'recovery'>('totp');
  const [busy, setBusy] = useState(false);
  const [reviewChange, setReviewChange] = useState(false);
  const [material, setMaterial] = useState<{ generation: number; result: DesktopClientDirectResult } | null>(null);
  const epoch = useRef(0);
  const requestBusy = useRef(false);
  const direct = material && material.generation === state?.generation ? material.result : null;
  const [inputGeneration, setInputGeneration] = useState(state?.generation);
  if (inputGeneration !== state?.generation) {
    setInputGeneration(state?.generation);
    setCode('');
    if (material && material.generation !== state?.generation) setMaterial(null);
  }
  useEffect(() => () => { epoch.current += 1; }, []);
  const act = useCallback(async (action: DesktopClientAction): Promise<boolean> => {
    if (requestBusy.current) return false;
    requestBusy.current = true;
    setBusy(true);
    const ticket = epoch.current;
    try {
      const reply = await send(action);
      if (ticket !== epoch.current || !reply?.ok) return false;
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
  }, [send]);
  const leave = (type: 'cancel' | 'return_local' | 'sign_out') => {
    epoch.current += 1;
    setMaterial(null);
    setCode('');
    // Escape remains usable during a blocked or slow request. Native fences late completion.
    void send({ type });
  };
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || event.isComposing) return;
      event.preventDefault();
      epoch.current += 1;
      setMaterial(null);
      setCode('');
      void send({ type: 'cancel' });
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [send]);
  const localButton = <Button variant="ghost" onClick={() => leave('return_local')}>{t('desktopClient.continueLocal')}</Button>;
  const instances = () => { setMaterial(null); setView('instances'); void act({ type: 'refresh_instances' }); };
  const errorMessage = error && t(error === 'bridge_unavailable' || error === 'bridge_incompatible' ? 'desktopClient.errors.bridge' : error === 'instance_upgrade_required' ? 'desktopClient.errors.upgrade' : error === 'invalid_otp' || error === 'invalid_code' ? 'desktopClient.errors.code' : 'desktopClient.errors.request');
  const failure = errorMessage && <Notice warning>{errorMessage}</Notice>;
  let content: ReactNode;
  let centerLogin = false;
  if (!state) {
    content = <div className="mx-auto max-w-xl pt-12"><Heading title={t('desktopClient.loading')} />{failure}<Button variant="outline" className="mt-6" onClick={() => void act({ type: 'get_state' })} disabled={busy}>{t('common.retry')}</Button></div>;
  } else if (view === 'updates') {
    // The same panel as everywhere else, acting on this computer's desktop app (ui/design: update/panel/trusted-view).
    const update = state.desktop.update;
    const model = modelFromDesktopState(update, { current: state.desktop.version, installFailed: install.failed });
    const target = update.latest;
    const accepts = (action: string) => update.hostActions?.includes(action) ?? false;
    content = <div className="mx-auto w-full max-w-md pt-12"><Heading title={t('update.trustedView.title')}>{t('update.trustedView.body')}</Heading><div className="overflow-hidden rounded-xl border border-edge bg-popover shadow-elevation-2"><UpdatePanel
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
        close: () => leave('cancel'),
      }}
    /></div>{failure}</div>;
  } else if (view === 'sign-out') {
    content = <div className="mx-auto max-w-xl pt-12"><Heading title={t('desktopClient.signOut')}>{t('desktopClient.signOutBody')}</Heading>{failure}<div className="mt-7 flex gap-3"><Button variant="brand" onClick={() => leave('sign_out')}>{t('desktopClient.signOut')}</Button><Button variant="outline" onClick={() => leave('cancel')}>{t('common.cancel')}</Button></div></div>;
  } else if (view === 'setup-complete') {
    content = <div className="mx-auto max-w-xl pt-12"><ShieldCheck className="mb-6 size-10 text-mint-ink" /><Heading title={t('desktopClient.setupReady')}>{t('desktopClient.setupBody')}</Heading><Notice>{t('desktopClient.registrationDisclosure')}</Notice><div className="mt-7 flex flex-wrap gap-3"><Button variant="brand" onClick={() => leave('return_local')}>{t('desktopClient.startLocal')}</Button><Button variant="outline" onClick={() => setView('account')}>{t('desktopClient.signInAndAdd')}<ArrowRight className="size-4" /></Button></div><p className="mt-5 text-sm text-muted">{t('desktopClient.signInLater')}</p>{failure}</div>;
  } else if (direct?.type === 'totp_enrolled' && direct.recoveryCodes?.length) {
    content = <div className="mx-auto max-w-xl pt-12"><Heading title={t('desktopClient.recoveryTitle')}>{t('desktopClient.recoveryBody')}</Heading><ul className="grid grid-cols-2 gap-3 rounded-xl border border-edge bg-field p-5 font-mono text-sm select-text">{direct.recoveryCodes.map((value) => <li key={value}>{value}</li>)}</ul><Button variant="brand" className="mt-7" onClick={() => setMaterial(null)}>{t('desktopClient.savedCodes')}</Button>{failure}</div>;
  } else if (state.account.status === 'second_factor_required') {
    const enroll = state.account.gate === 'enroll';
    content = <div className="mx-auto max-w-xl pt-12"><ShieldCheck className="mb-6 size-10 text-mint-ink" /><Heading title={t(enroll ? 'desktopClient.enrollFactor' : 'desktopClient.verifyFactor')}>{t(enroll ? 'desktopClient.enrollFactorBody' : 'desktopClient.verifyFactorBody')}</Heading>
      {direct?.type === 'totp_enrollment' ? <>
        {/* Render the URI with our QR component; never inject backend SVG markup. */}
        <div className="mb-5 flex flex-wrap items-start gap-5"><QRCodeSVG value={direct.uri} size={160} marginSize={2} /><div className="min-w-0 flex-1"><p className="text-sm text-muted">{t('desktopClient.setupKey')}</p><p className="mt-2 break-all font-mono text-sm select-text">{direct.secret}</p></div></div>
        <form onSubmit={(event) => { event.preventDefault(); void act({ type: 'finish_totp_enrollment', code }); }}><Label htmlFor="factor-code">{t('desktopClient.authenticatorCode')}</Label><Input id="factor-code" className="mt-2 h-12" value={code} onChange={(event) => setCode(event.target.value)} autoComplete="one-time-code" inputMode="numeric" required /><Button variant="brand" className="mt-6" disabled={busy || !code.trim()}>{t('desktopClient.finishFactor')}</Button></form>
      </> : enroll ? <Button variant="brand" disabled={busy} onClick={() => void act({ type: 'begin_totp_enrollment' })}>{t('desktopClient.addAuthenticator')}</Button> : <>
        {state.account.methods.includes('passkey') && <Button variant="outline" className="mb-5" disabled={busy} onClick={() => void act({ type: 'verify_passkey' })}>{t('desktopClient.usePasskey')}</Button>}
        {(state.account.methods.includes('totp') || method === 'recovery') && <form onSubmit={(event) => { event.preventDefault(); void act({ type: 'verify_second_factor', method, code }); }}><Label htmlFor="factor-code">{t(method === 'recovery' ? 'desktopClient.recoveryCode' : 'desktopClient.authenticatorCode')}</Label><Input id="factor-code" className="mt-2 h-12" value={code} onChange={(event) => setCode(event.target.value)} autoComplete="one-time-code" inputMode={method === 'totp' ? 'numeric' : 'text'} required /><Button variant="brand" className="mt-6" disabled={busy || !code.trim()}>{t('desktopClient.verify')}</Button></form>}
        <Button variant="link" className="mt-3 px-0" onClick={() => { setCode(''); setMethod(method === 'totp' ? 'recovery' : 'totp'); }}>{t(method === 'totp' ? 'desktopClient.useRecovery' : 'desktopClient.useAuthenticator')}</Button>
      </>}<div className="mt-6">{failure}{localButton}</div></div>;
  } else if (state.account.status !== 'signed_in') {
    centerLogin = true;
    const verify = state.account.status === 'verifying' && !editEmail;
    content = <AccountLoginView verify={verify} email={state.account.email ?? ''}
      value={verify ? code : email} busy={busy} errorMessage={errorMessage}
      onValueChange={verify ? setCode : setEmail}
      onSubmit={() => void act(verify ? { type: 'verify_otp', code } : { type: 'request_otp', email: email.trim() })}
      onChangeEmail={() => { setCode(''); setEmail(state.account.email ?? ''); setEditEmail(true); }}
      onResendCode={() => void act({ type: 'request_otp', email: state.account.email ?? email })}
      onContinueLocal={() => leave('return_local')} />;
  } else if (state.consent) {
    content = <div className="mx-auto max-w-xl pt-12"><Heading title={t('desktopClient.consentTitle', { name: state.consent.instance_name })}>{t('desktopClient.consentBody')}</Heading><Notice><p>{state.consent.host}</p><p className="mt-2 break-words">{state.consent.scope}</p></Notice>{failure}<div className="mt-7 flex gap-3"><Button variant="brand" disabled={busy} onClick={() => void act({ type: 'decide_consent', approve: true })}>{t('desktopClient.allow')}</Button><Button variant="outline" disabled={busy} onClick={() => void act({ type: 'decide_consent', approve: false })}>{t('desktopClient.deny')}</Button></div></div>;
  } else if (state.connection.status === 'switching' || state.connection.status === 'remote_unreachable') {
    const switching = state.connection.status === 'switching';
    const target = state.connection.pending ?? state.connection.committed;
    content = <div className="mx-auto max-w-xl pt-20">{switching ? <RefreshCw className="mb-6 size-8 animate-spin text-muted" /> : <WifiOff className="mb-6 size-8 text-gold-ink" />}<Heading title={t(switching ? 'desktopClient.connectingTo' : 'desktopClient.unreachable', { name: target.name })}>{t(switching ? 'desktopClient.switchBody' : 'desktopClient.offlineBody')}</Heading>{switching ? <Skeleton className="h-2 w-full rounded-full" /> : <Notice>{t('desktopClient.localStillAvailable')}</Notice>}{failure}<div className="mt-7 flex gap-3"><Button variant="brand" onClick={() => leave('return_local')}><Laptop className="size-4" />{t('desktopClient.backLocal')}</Button>{!switching && target.instance_id && <Button variant="outline" disabled={busy} onClick={() => void act({ type: 'select_instance', instance_id: target.instance_id! })}>{t('common.retry')}</Button>}</div></div>;
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
    const current = state.connection.committed;
    const remote = state.catalog.instances.filter((item) => item.id !== state.enrollment.instance_id);
    content = <div className="mx-auto max-w-2xl pt-6"><div className="flex items-start justify-between gap-4"><Heading title={t('desktopClient.switchInstance')}>{t('desktopClient.instancesBody')}</Heading><Button variant="ghost" size="icon" aria-label={t('desktopClient.refreshInstances')} disabled={busy} onClick={() => void act({ type: 'refresh_instances' })}><RefreshCw className={cn('size-4', state.catalog.status === 'loading' && 'animate-spin')} /></Button></div><div className="mb-5"><InstanceRow name={t('desktopClient.localName', { name: state.local.name })} detail={t('desktopClient.localDetail')} selected={current.kind === 'local'} local offline={false} busy={busy} onClick={() => leave('return_local')} /></div>
      {state.catalog.status === 'loading' ? <div className="space-y-4" aria-label={t('desktopClient.loading')}><Skeleton className="h-20 w-full rounded-xl" /><Skeleton className="h-20 w-full rounded-xl" /></div> : state.catalog.status === 'error' ? <Notice warning>{t('desktopClient.catalogError')}<Button variant="link" onClick={() => void act({ type: 'refresh_instances' })} disabled={busy}>{t('common.retry')}</Button></Notice> : remote.length === 0 ? <Notice><p className="font-semibold text-foreground">{t('desktopClient.emptyTitle')}</p><p className="mt-2">{t('desktopClient.emptyBody')}</p></Notice> : <InstanceGroups instances={remote} selectedId={current.instance_id} busy={busy} onSelect={(instance_id) => void act({ type: 'select_instance', instance_id })} />}{failure}</div>;
  }
  return <div className="flex h-dvh flex-col bg-background text-foreground">
    <div aria-hidden="true" className="h-[var(--shell-titlebar-inset)] shrink-0 bg-surface" />
    <header className="flex shrink-0 items-center px-8 py-3"><Button variant="ghost" size="sm" onClick={() => leave('cancel')}><ArrowLeft className="size-4" />{t('desktopClient.backWorkbench')}</Button></header>
    <main className={cn('min-h-0 flex-1 overflow-y-auto px-10 pb-5', centerLogin && 'flex flex-col')} aria-busy={busy}>{content}</main>
    {state?.remote_auxiliary === 'disabled_incomplete' && <footer className="shrink-0 border-t border-edge bg-surface px-4 py-2 text-xs text-muted">{t('desktopClient.auxiliaryIncomplete')}</footer>}
  </div>;
}

function InstanceRow({ name, detail, selected, local, offline, busy, onClick }: {
  name: string; detail: string; selected: boolean; local: boolean; offline: boolean; busy: boolean; onClick: () => void;
}) {
  const { t } = useTranslation();
  return <Button variant="ghost" className={cn('h-auto min-h-20 w-full justify-start gap-4 whitespace-normal rounded-xl border px-4 py-4 text-left', selected ? 'border-mint/40 bg-mint-soft' : 'border-edge bg-surface')} onClick={onClick} disabled={busy && !local}>
    <span className="grid size-10 shrink-0 place-items-center rounded-xl border border-edge bg-field">{local ? <Laptop className="size-5" /> : <Server className="size-5" />}</span>
    <span className="min-w-0 flex-1"><span className="block text-sm font-semibold">{name}</span><span className="mt-1 block break-words text-xs font-normal leading-5 text-muted">{detail}</span></span>
    <span className="flex shrink-0 items-center gap-2"><Badge variant={offline ? 'secondary' : 'success'}>{t(offline ? 'desktopClient.offline' : 'desktopClient.online')}</Badge>{selected ? <Check className="size-4 text-mint-ink" aria-label={t('desktopClient.current')} /> : <ChevronRight className="size-4 text-muted" />}</span>
  </Button>;
}

function InstanceGroups({ instances, selectedId, busy, onSelect }: {
  instances: Instance[]; selectedId: string | null; busy: boolean; onSelect: (id: string) => void;
}) {
  const { t } = useTranslation();
  return (['owner', 'shared'] as const).map((relationship) => {
    const items = instances.filter((item) => item.relationship === relationship);
    return items.length ? <section className="mb-6" key={relationship}>
      <h2 className="mb-3 text-sm font-medium text-muted">{t(relationship === 'owner' ? 'desktopClient.owned' : 'desktopClient.shared')}</h2>
      <div className="space-y-3">{items.map((item) => <InstanceRow key={item.id} name={item.name} detail={item.host} selected={selectedId === item.id} local={false} offline={!item.available} busy={busy} onClick={() => onSelect(item.id)} />)}</div>
    </section> : null;
  });
}
