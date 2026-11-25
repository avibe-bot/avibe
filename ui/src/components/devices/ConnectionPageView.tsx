// Settings · Devices · 连接方式 (ui/design: settings/devices-connection/*), the page behind the
// Devices page's 「连接方式」 row. Nothing here is needed by an ordinary user: everything defaults to
// automatic. Groups split by whose choice it is: the path this device uses (the Avibe App only),
// what this Avibe offers (the Cloudflare tunnel, direct, the relay where enrolled), and the
// tunnel's own settings. The instance verifies a changed tunnel setting itself and puts the old
// value back: there is no keep button and no countdown, only one status line. Direct and the relay
// are ordinary switches, refused with one sentence when the change could cut the person off. Last,
// for the owner: the domain, its rename, and binding remote access again.
// A view: props only. `ConnectionPage` reads the APIs and passes handlers.
import type { ReactNode } from 'react';
import { Check, CircleHelp, History, LoaderCircle, Stethoscope, TriangleAlert, Undo2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { TFunction } from 'i18next';

import type { TranslationKey } from '@/i18n/types';
import { deviceUsingText } from '@/lib/remoteAccessDirect';
import {
  TunnelAutoRecoverySwitch, TunnelIpFamilyControl, TunnelOutboundSelect, TunnelProfileControl, TunnelProtocolControl,
} from '../remoteAccess/TunnelControls';
import { TunnelDetails, type TunnelDetailsProps } from '../remoteAccess/TunnelDetails';
import { SettingsPageShell } from '../settings/SettingsPageShell';
import { SettingsPanel, ToggleSwitch } from '../settings/SettingsPrimitives';
import { Badge } from '../ui/badge';
import { Button } from '../ui/button';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '../ui/dialog';
import { KeptTail } from '../ui/kept-tail';
import { deviceTime, longDay, timeParts } from './format';
import { ActionRow, BackToDevices, Notice, OptimizeRow } from './parts';
import { RebindDialogs, RebindRow, type RebindActions } from './RebindViews';
import { UnbindConfirm, UnbindLine } from './UnbindViews';
import { RenameDialogs, type RenameActions } from './RenameDialogs';
import { settingName, settingValueText } from './rules';
import { renameRefusalText } from './texts';
import type {
  AddressState, ConnectionSetting, ConnectionState, DeviceUsing, DirectPreference, DirectState, OptimizeState, RebindDialog, RebindState,
  RelayState, RenameDevice, RenameStep, RetiredName, SwitchRefusal, TunnelChange, TunnelSetting, TunnelStatus, UnbindConfirmState, UnbindState,
} from './types';

export type ConnectionPageActions = RenameActions & RebindActions & {
  /** Back to the Devices page. */
  onBack?: () => void;
  onRetry?: () => void;
  onPreferenceChange?: (mode: DirectPreference['mode']) => void;
  onSettingChange?: (setting: ConnectionSetting, to: string | boolean) => void;
  onOptimizeRoute?: () => void;
  onOpenDiagnostics?: () => void;
  onCloseDiagnostics?: () => void;
  onRunDiagnostics?: () => void;
  /** 「测速」 in the diagnostics. */
  onMeasure?: () => void;
  onChangeDomain?: () => void;
  /** 「解除绑定」: open its confirmation, confirm it, or close it. */
  onUnbind?: () => void;
  onUnbindConfirm?: () => void;
  onUnbindCancel?: () => void;
  onShowCleanup?: (retired: RetiredName) => void;
};

export type ConnectionPageViewProps = ConnectionPageActions & {
  /** `unavailable`: the state cannot be trusted right now, so no setting is shown. */
  load: 'loading' | 'ready' | 'unavailable';
  connection: ConnectionState | null;
  tunnel: TunnelStatus;
  /** The session comes from elsewhere, not from the computer that runs Avibe. */
  remote: boolean;
  /** How this page's own connection came in, once it is observed. */
  via?: 'tunnel' | 'direct' | null;
  /** The path this device uses, where its shell has one of its own (the Avibe App, a Desktop
   *  window of another computer's instance). Null: none reported yet. Absent in a browser. */
  path?: DeviceUsing | null;
  /** This device's own choice of path (the Avibe App, a Desktop window of another computer's
   *  instance); absent where the shell offers none. */
  preference?: DirectPreference;
  /** 「解除绑定」, the owner's: where it stands. Absent on a build without it. */
  unbind?: UnbindState;
  /** Its confirmation, open. */
  unbindConfirm?: UnbindConfirmState;
  /** Which shell is choosing: the App goes back to automatic from either choice that cannot
   *  connect, Avibe Desktop only from 「仅直连和中继」. */
  choiceShell?: 'app' | 'desktop';
  /** 「仅直连和中继」 is offered: the shell takes it and this instance has direct turned on. Not
   *  offered, it is not shown at all, unless it is the choice already made. */
  directOnly?: boolean;
  refused?: SwitchRefusal;
  /** A switch whose change is on its way: it waits. */
  sending?: ConnectionSetting;
  optimize?: OptimizeState;
  /** 「连接诊断与网络路径」, open. */
  diagnostics?: TunnelDetailsProps;
  /** This Avibe's domain, for the owner: the last panel of the page. */
  host?: string;
  address?: AddressState | null;
  /** The viewer's own devices, listed in the rename form. */
  renameDevices?: RenameDevice[];
  rename?: RenameStep;
  rebind?: RebindState;
  rebindDialog?: RebindDialog;
  /** This browser started the attempt avibe.bot refused, so it may confirm a reset. */
  canReset?: boolean;
  /** Inside Avibe Desktop: binding again is done in a browser on this computer. */
  rebindNeedsBrowser?: boolean;
  /** This tab's own bind-again request is still open at avibe.bot: it can be cancelled. */
  rebindOpen?: 'waiting' | 'retrying';
  /** A confirmed bind-again step is being carried out. */
  rebindBusy?: boolean;
};

type BadgeVariant = 'info' | 'success' | 'secondary' | 'warning';

const directStateKeys = {
  ready: 'devices.connection.available',
  off: 'devices.connection.off',
  'awaiting-permission': 'devices.connection.stateAwaitingPermission',
  denied: 'devices.connection.stateDenied',
  'awaiting-certificate': 'devices.connection.stateAwaitingCertificate',
  'not-running': 'devices.connection.stateNotRunning',
  unknown: 'devices.connection.stateUnknown',
} as const satisfies Record<DirectState, TranslationKey>;

const directWhyKeys = {
  'awaiting-permission': 'devices.connection.whyAwaitingPermission',
  denied: 'devices.connection.whyDenied',
  'awaiting-certificate': 'devices.connection.whyAwaitingCertificate',
  'not-running': 'devices.connection.whyNotRunning',
} as const satisfies Partial<Record<DirectState, TranslationKey>>;

/** What went back to automatic, said by which choice and which shell. Avibe Desktop never puts
 *  「仅 Cloudflare 隧道」 back. */
const revertedKeys = {
  tunnel: {
    undated: 'devices.connection.choiceRevertedUndated',
    today: 'devices.connection.choiceRevertedToday',
    yesterday: 'devices.connection.choiceRevertedYesterday',
    other: 'devices.connection.choiceRevertedDated',
  },
  direct: {
    undated: 'devices.connection.choiceRevertedDirectUndated',
    today: 'devices.connection.choiceRevertedDirectToday',
    yesterday: 'devices.connection.choiceRevertedDirectYesterday',
    other: 'devices.connection.choiceRevertedDirectDated',
  },
  directDesktop: {
    undated: 'devices.connection.choiceRevertedDirectDesktopUndated',
    today: 'devices.connection.choiceRevertedDirectDesktopToday',
    yesterday: 'devices.connection.choiceRevertedDirectDesktopYesterday',
    other: 'devices.connection.choiceRevertedDirectDesktopDated',
  },
} as const satisfies Record<string, Record<NonNullable<ReturnType<typeof timeParts>>['day'] | 'undated', TranslationKey>>;

const failedKeys = {
  saved_restored_runtime_pending: 'devices.connection.change.failedRuntimePending',
  storage_restore_failed: 'devices.connection.change.failedStorage',
  candidate_not_ready: 'devices.connection.change.failedNotReady',
  connection_change_busy: 'devices.connection.change.failedBusy',
  settings_not_applied: 'devices.connection.change.failedNotApplied',
  unknown: 'devices.connection.change.failedUnknown',
} as const satisfies Record<Extract<TunnelChange, { state: 'failed' }>['reason'], TranslationKey>;

const protocolName = (protocol: 'quic' | 'http2') => (protocol === 'quic' ? 'QUIC' : 'HTTP/2');

/** One status line for a tunnel change, in each state the instance can report. */
function changeLine(t: TFunction, change: TunnelChange): { icon: typeof Check; tone: string; text: string; busy?: boolean } {
  const from = 'from' in change ? settingValueText(t, change.setting, change.from) : '';
  switch (change.state) {
    case 'applying':
      return { icon: LoaderCircle, tone: 'text-muted', text: t('devices.connection.change.applying'), busy: true };
    case 'verifying':
      return { icon: LoaderCircle, tone: 'text-muted', text: t('devices.connection.change.verifying'), busy: true };
    case 'restoring':
      return { icon: LoaderCircle, tone: 'text-muted', text: t('devices.connection.change.restoring', { from }), busy: true };
    case 'applied':
      // Stored with no tunnel running: saved, and not said to be in effect.
      return change.saved
        ? { icon: Check, tone: 'text-muted', text: t('devices.connection.change.saved') }
        : { icon: Check, tone: 'text-mint-ink', text: t('devices.connection.change.applied') };
    case 'superseded':
      return { icon: History, tone: 'text-muted', text: t('devices.connection.change.superseded') };
    case 'restored':
      return {
        icon: Undo2,
        tone: 'text-gold-ink',
        text: t(change.reason === 'verification_failed'
          ? 'devices.connection.change.restoredFailed'
          : change.reason === 'verification_unavailable' ? 'devices.connection.change.restoredUnavailable' : 'devices.connection.change.restoredPlain', { from }),
      };
    case 'failed':
      return {
        icon: change.reason === 'saved_restored_runtime_pending' || change.reason === 'storage_restore_failed' ? TriangleAlert : Undo2,
        tone: change.reason === 'storage_restore_failed' ? 'text-destructive-ink' : 'text-gold-ink',
        text: t(failedKeys[change.reason], { from }),
      };
    case 'unknown':
      return { icon: CircleHelp, tone: 'text-muted', text: t('devices.connection.change.unknown') };
  }
}

function ChangeLine({ change }: { change: TunnelChange }) {
  const { t } = useTranslation();
  const { icon: Icon, tone, text, busy } = changeLine(t, change);
  return (
    <div role="status" className={`mt-1 flex items-start gap-1.5 text-[11px] leading-relaxed ${tone}`}>
      <Icon className={`mt-0.5 size-3.5 shrink-0 ${busy ? 'motion-safe:animate-spin' : ''}`} />
      <span><KeptTail text={text} /></span>
    </div>
  );
}

function Row({ title, hint, status, control, tone }: { title: string; hint?: ReactNode; status?: ReactNode; control?: ReactNode; tone?: 'info' }) {
  return (
    <div className={`flex flex-col gap-2 border-b border-border px-5 py-3 last:border-b-0 md:flex-row md:items-center md:justify-between md:gap-6 ${tone === 'info' ? 'bg-cyan/[0.06]' : ''}`}>
      <div className="min-w-0">
        <div className="text-[13px] font-medium text-foreground">{title}</div>
        {hint && <div className="mt-0.5 text-[11px] leading-relaxed text-muted">{typeof hint === 'string' ? <KeptTail text={hint} /> : hint}</div>}
        {status}
      </div>
      {control && <div className="shrink-0">{control}</div>}
    </div>
  );
}

function Refused({ refusal }: { refusal: SwitchRefusal }) {
  const { t } = useTranslation();
  const text = refusal.reason === 'tunnel_unhealthy' && refusal.row === 'direct'
    ? t('devices.connection.refusedDirect')
    : refusal.reason === 'tunnel_unhealthy' && refusal.row === 'relay'
      ? t('devices.connection.refusedRelay')
      : refusal.reason === 'connection_change_busy' ? t('devices.connection.refusedBusy') : t('devices.connection.refusedUnknown');
  return (
    <div role="alert" className="mt-1 flex items-start gap-1.5 text-[11px] leading-relaxed text-gold-ink">
      <TriangleAlert className="mt-0.5 size-3.5 shrink-0" />
      <span><KeptTail text={text} /></span>
    </div>
  );
}

/** This device's own choice: automatic first, the Cloudflare tunnel only, and, where the shell
 *  takes it and this instance has direct on, direct and the relay only. */
function PreferenceGroup({ path, preference, shell = 'app', directOnly, onChange }: {
  path?: DeviceUsing | null; preference: DirectPreference; shell?: 'app' | 'desktop'; directOnly?: boolean;
  onChange?: (mode: DirectPreference['mode']) => void;
}) {
  const { t, i18n } = useTranslation();
  // A choice already made stays listed, so the list never shows none chosen.
  const direct = directOnly || preference.mode === 'direct';
  const options = [
    ['automatic', t('devices.connection.automatic'), t('devices.connection.automaticHint')],
    ['tunnel', t('devices.connection.tunnelOnly'), t('devices.connection.tunnelOnlyHint')],
    ...(direct ? [['direct', t('devices.connection.directOnly'), t('devices.connection.directOnlyHint')] as const] : []),
  ] as const;
  const reverted = preference.reverted ? timeParts(preference.reverted.at, i18n.language) : null;
  const revertedFrom = preference.reverted?.from === 'direct' ? (shell === 'desktop' ? 'directDesktop' : 'direct') : 'tunnel';
  const hint = shell === 'desktop'
    ? t(direct ? 'devices.connection.choiceHintDesktop' : 'devices.connection.choiceHintDesktopPlain')
    : t('devices.connection.choiceHint');
  return (
    <SettingsPanel
      title={t('devices.connection.thisDevice')}
      description={path ? deviceUsingText(t, path) : t('devices.connection.noPath')}
    >
      <div role="radiogroup" aria-label={t('devices.connection.thisDevice')} className="flex flex-col">
        {options.map(([id, label, text]) => {
          const checked = preference.mode === id;
          return (
            <button
              key={id}
              type="button"
              role="radio"
              aria-checked={checked}
              onClick={() => { if (!checked) onChange?.(id); }}
              className="flex items-center gap-3 border-b border-border px-5 py-2.5 text-left transition-colors last:border-b-0 hover:bg-field/60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
            >
              <span className={`flex size-4 shrink-0 items-center justify-center rounded-full border ${checked ? 'border-mint bg-mint' : 'border-border-strong'}`}>
                {checked && <span className="size-1.5 rounded-full bg-background" />}
              </span>
              <span className="min-w-0">
                <span className="block text-[13px] font-medium text-foreground">{label}</span>
                <span className="block text-[11px] leading-relaxed text-muted"><KeptTail fill text={text} /></span>
                {id === 'direct' && preference.mode === 'direct' && preference.pendingRelaunch && (
                  <span role="status" className="mt-0.5 block text-[11px] leading-relaxed text-gold-ink">{t('devices.connection.directOnlyRelaunch')}</span>
                )}
              </span>
            </button>
          );
        })}
      </div>
      <p className="border-t border-border px-5 py-2.5 text-[11px] leading-relaxed text-muted"><KeptTail fill text={hint} /></p>
      {preference.reverted && (
        <p role="status" className="border-t border-border bg-gold/10 px-5 py-2.5 text-[11px] leading-relaxed text-foreground">
          <KeptTail fill text={t(revertedKeys[revertedFrom][reverted ? reverted.day : 'undated'], { date: reverted?.date, time: reverted?.time })} />
        </p>
      )}
    </SettingsPanel>
  );
}

function RelayRow({ relay, direct, inUse, refused, busy, onChange }: {
  relay: RelayState; direct: DirectState; /** This device's own path is the relay. */ inUse?: boolean;
  refused?: SwitchRefusal; busy?: boolean; onChange?: (on: boolean) => void;
}) {
  const { t } = useTranslation();
  const status = refused ? <Refused refusal={refused} /> : relay.unavailable ? (
    <div className="mt-1 text-[11px] leading-relaxed text-gold-ink">
      <KeptTail
        text={t(relay.unavailable === 'direct_permission_required'
          ? 'devices.connection.relayNeedsPermission'
          : relay.unavailable === 'relay_transport_unavailable' ? 'devices.connection.relayNoTransport' : 'devices.connection.relayUnavailable')}
      />
    </div>
  ) : relay.enabled && direct !== 'off' ? (
    <div className="mt-1 text-[11px] text-muted">{t(relay.connected ? 'devices.connection.relayConnected' : 'devices.connection.relayNotConnected')}</div>
  ) : null;
  return (
    <Row
      title={t('remoteAccess.direct.usingRelay')}
      tone={inUse ? 'info' : undefined}
      hint={t('devices.connection.relayDetail')}
      status={status}
      control={(
        <div className="flex items-center gap-2.5">
          {inUse && <Badge variant="info">{t('devices.connection.inUse')}</Badge>}
          <ToggleSwitch
            enabled={relay.enabled}
            disabled={busy || Boolean(relay.unavailable)}
            onClick={() => onChange?.(!relay.enabled)}
            label={t('remoteAccess.direct.usingRelay')}
          />
        </div>
      )}
    />
  );
}

export function ConnectionPageView({
  load, connection, tunnel, remote, via, path, preference, unbind, unbindConfirm, choiceShell, directOnly, refused, sending, optimize, diagnostics, host, address, renameDevices = [],
  rename, rebind, rebindDialog, canReset, rebindNeedsBrowser, rebindOpen, rebindBusy, ...actions
}: ConnectionPageViewProps) {
  const { t, i18n } = useTranslation();
  const unbindBusy = unbind?.state === 'unbinding' || unbind?.state === 'needsRepair';
  // While an unbinding runs or is left for this computer to sort out, the tunnel's own settings and
  // 「优化线路」 have nothing to act on.
  const unbinding = unbindBusy;
  const shell = (children: ReactNode) => (
    <SettingsPageShell title={t('devices.connection.title')} subtitle={t('devices.connection.subtitle')} activeTab="remote-access">
      <BackToDevices onBack={actions.onBack} />
      {children}
    </SettingsPageShell>
  );
  const ownerPanel = (host || rebind) ? (
    <SettingsPanel>
      {host && (
        <div className="flex items-center gap-3 px-5 py-3">
          <div className="min-w-0 flex-1">
            <div className="text-[13px] font-medium text-foreground">{t('devices.connection.domain')}</div>
            <div className="mt-0.5 break-all font-mono text-[11px] text-muted">{host}</div>
            {address?.operation && address.operation.state !== 'failed' && (
              <div role="status" className="mt-1 text-[11px] leading-relaxed text-muted">{t('devices.rename.pendingTitle', { host: address.operation.hostname })}</div>
            )}
            {/* Said only where the way out exists on this instance: the row below. */}
            {address?.stateError === 'rebind_required' && rebind && (
              <div role="alert" className="mt-1 text-[11px] leading-relaxed text-gold-ink"><KeptTail text={t('devices.rename.stateRebindRequired')} /></div>
            )}
            {address?.operation?.state === 'failed' && (
              <div role="alert" className="mt-1 text-[11px] leading-relaxed text-gold-ink">
                <KeptTail
                  text={t('devices.rename.earlierFailed', {
                    reason: address.operation.error
                      ? renameRefusalText(t, i18n.language, address.operation.error, Boolean(rebind))
                      : t('devices.rename.refused.unknown', { code: 'unknown' }),
                  })}
                />
              </div>
            )}
            {unbind && <UnbindLine state={unbind} />}
          </div>
          {/* 「更改域名」 and 「解除绑定」: the same place and the same weight, the owner's. Neither while
              an unbinding runs or this computer has to sort its binding out. */}
          <div className="flex shrink-0 flex-wrap justify-end gap-1">
            {address?.eligible && !unbindBusy && (
              <Button variant="ghost" size="compact" className="text-destructive-ink" onClick={actions.onChangeDomain}>
                {t('devices.connection.changeDomain')}
              </Button>
            )}
            {/* Refused as not eligible, it is not offered again: nothing here would change that. */}
            {(unbind?.state === 'ready' || unbind?.state === 'failed') && (
              <Button variant="ghost" size="compact" className="text-destructive-ink" onClick={actions.onUnbind}>
                {t('devices.unbind.action')}
              </Button>
            )}
          </div>
        </div>
      )}
      {/* Names given up and still kept for this account, each with its real day. */}
      {address?.retired.map((each) => (
        <div key={each.hostname} className="flex items-center gap-3 border-t border-border px-5 py-2.5">
          <div className="min-w-0 flex-1">
            <div className="break-all font-mono text-[11px] text-foreground">{each.hostname}</div>
            <div className="text-[11px] leading-relaxed text-muted">
              {each.releaseAt ? t('devices.rename.keptRow', { date: longDay(each.releaseAt, i18n.language) }) : t('devices.rename.keptRowPlain')}
            </div>
          </div>
          <Button variant="ghost" size="compact" className="shrink-0" onClick={() => actions.onShowCleanup?.(each)}>{t('devices.rename.showCleanup')}</Button>
        </div>
      ))}
      {/* Binding again waits while an unbinding runs or this computer has to sort it out. */}
      {rebind && !unbindBusy && <RebindRow state={rebind} canReset={canReset} needsBrowser={rebindNeedsBrowser} open={rebindOpen} actions={actions} />}
    </SettingsPanel>
  ) : null;
  const dialogs = (
    <>
      {unbindConfirm && host && <UnbindConfirm host={host} confirm={unbindConfirm} onConfirm={actions.onUnbindConfirm} onCancel={actions.onUnbindCancel} />}
      {rename && host && address && <RenameDialogs host={host} rename={rename} address={address} devices={renameDevices} rebindOffered={Boolean(rebind)} actions={actions} />}
      {rebindDialog && rebind && <RebindDialogs dialog={rebindDialog} state={rebind} host={host} busy={rebindBusy} actions={actions} />}
    </>
  );

  if (load !== 'ready' || !connection) {
    return shell(
      <>
        <SettingsPanel>
          {load === 'loading' ? (
            <div role="status" className="flex items-center gap-2.5 px-5 py-4 text-[13px] text-muted">
              <LoaderCircle className="size-4 motion-safe:animate-spin" />
              {t('common.loading')}
            </div>
          ) : (
            <div role="alert" className="flex flex-col gap-3 px-5 py-4 md:flex-row md:items-center md:justify-between">
              <p className="text-[13px] leading-relaxed text-foreground"><KeptTail text={t('devices.connection.unavailable')} /></p>
              <Button variant="secondary" size="sm" className="shrink-0" onClick={actions.onRetry}>{t('common.retry')}</Button>
            </div>
          )}
        </SettingsPanel>
        {ownerPanel}
        {dialogs}
      </>,
    );
  }

  const { settings, direct, relay, change, returnedToAutomatic, temporaryRoute } = connection;
  const pending = Boolean(change && (change.state === 'applying' || change.state === 'verifying' || change.state === 'restoring' || change.state === 'unknown'));
  const statusFor = (setting: TunnelSetting) => (change?.setting === setting ? <ChangeLine change={change} /> : null);
  const set = actions.onSettingChange;
  const connected = tunnel.state === 'up' || tunnel.state === 'slow' || tunnel.state === 'unsteady' || tunnel.state === 'optimizing' || tunnel.state === 'connected';
  const tunnelInUse = remote && via === 'tunnel' && connected;
  // Which path is in use. A stream that came in by the direct ingress may be relayed, and a
  // relayed path is never called direct: inside a shell that names its path (`path` is given),
  // the path decides, and while it names none nothing is marked. A browser has only the stream.
  const inShell = path !== undefined;
  const directInUse = inShell ? path?.kind === 'lan' || path?.kind === 'punched' || path?.kind === 'direct' : via === 'direct';
  const relayInUse = inShell && path?.kind === 'relay';
  const directBadge: { label: string; variant: BadgeVariant } = {
    label: t(directStateKeys[direct]),
    variant: direct === 'ready' ? 'success' : direct === 'off' || direct === 'unknown' ? 'secondary' : 'warning',
  };
  const why = direct in directWhyKeys ? t(directWhyKeys[direct as keyof typeof directWhyKeys]) : null;
  const names = (list: TunnelSetting[]) => list.map((each) => settingName(t, each)).join(t('devices.listSeparator'));
  const returnedAt = returnedToAutomatic ? deviceTime(returnedToAutomatic.at, t, i18n.language, 'stamp') : '';

  return shell(
    <>
      {returnedToAutomatic && returnedToAutomatic.changes.length > 0 && (
        <div role="status" className="flex gap-3 rounded-xl border border-gold/40 bg-gold/10 px-5 py-3">
          <History className="mt-0.5 size-4 shrink-0 text-gold-ink" />
          <p className="text-[12px] leading-relaxed text-foreground">
            <KeptTail
              text={t('devices.connection.returnedToAutomatic', {
                time: returnedAt,
                settings: returnedToAutomatic.changes
                  .map((each) => t('devices.connection.returnedSetting', { setting: settingName(t, each.setting), from: settingValueText(t, each.setting, each.from) }))
                  .join(t('devices.listSeparator')),
              })}
            />
          </p>
        </div>
      )}
      {/* The tunnel runs on a released combination while the saved settings are unchanged. */}
      {temporaryRoute && (
        <Notice tone="plain">
          <KeptTail
            text={temporaryRoute.differences.length > 0
              ? temporaryRoute.protocol
                ? t('devices.connection.temporaryRoute', { protocol: protocolName(temporaryRoute.protocol), settings: names(temporaryRoute.differences) })
                : t('devices.connection.temporaryRouteNoProtocol', { settings: names(temporaryRoute.differences) })
              : t('devices.connection.temporaryRoutePlain')}
          />
        </Notice>
      )}

      {preference && <PreferenceGroup path={path} preference={preference} shell={choiceShell} directOnly={directOnly} onChange={actions.onPreferenceChange} />}
      {/* A shell with a path of its own and no choice to offer: what this device uses, and no more. */}
      {!preference && path !== undefined && (
        <SettingsPanel title={t('devices.connection.thisDevice')} description={path ? deviceUsingText(t, path) : t('devices.connection.noPath')} />
      )}

      <SettingsPanel title={t('devices.connection.offered')} description={preference || path !== undefined ? undefined : t('devices.connection.offeredBrowser')}>
        <Row
          title={t('remoteAccess.direct.usingTunnel')}
          hint={t('devices.connection.tunnelDetail')}
          tone={tunnelInUse ? 'info' : undefined}
          status={tunnel.state === 'slow' || tunnel.state === 'unsteady' ? (
            <div role="status" className="mt-1 text-[11px] leading-relaxed text-gold-ink">
              {t(tunnel.state === 'slow' ? 'devices.reach.slow' : 'devices.reach.unsteady')}
            </div>
          ) : tunnel.state === 'optimizing' ? (
            <div role="status" className="mt-1 text-[11px] leading-relaxed text-muted">{t('devices.reach.optimizing')}</div>
          ) : null}
          control={tunnel.state === 'down'
            ? <Badge variant="warning">{t('devices.connection.notConnected')}</Badge>
            : tunnel.state === 'unknown'
              ? <Badge variant="secondary">{t('devices.connection.stateUnknown')}</Badge>
              : tunnel.state === 'connecting'
                ? <Badge variant="secondary">{t('devices.reach.connecting')}</Badge>
              : <Badge variant={tunnelInUse ? 'info' : 'success'}>{t(tunnelInUse ? 'devices.connection.inUse' : 'devices.connection.available')}</Badge>}
        />
        <Row
          title={t('remoteAccess.direct.label')}
          tone={directInUse ? 'info' : undefined}
          hint={(
            <>
              <KeptTail text={t('devices.connection.directDetail')} />
              <br />
              <KeptTail text={t('devices.connection.directOffWarning')} />
            </>
          )}
          status={refused?.row === 'direct' ? <Refused refusal={refused} /> : null}
          control={(
            <div className="flex items-center gap-2.5">
              {directInUse && <Badge variant="info">{t('devices.connection.inUse')}</Badge>}
              <ToggleSwitch
                enabled={direct !== 'off'}
                disabled={sending === 'direct' || direct === 'unknown'}
                onClick={() => set?.('direct', direct === 'off')}
                label={t('remoteAccess.direct.label')}
              />
            </div>
          )}
        />
        {direct !== 'off' && (
          <>
            <Row
              title={t('remoteAccess.direct.usingLan')}
              hint={why ?? t('devices.connection.lanDetail')}
              control={<Badge variant={directBadge.variant}>{directBadge.label}</Badge>}
            />
            <Row
              title={t('remoteAccess.direct.usingPunched')}
              hint={why ?? t('devices.connection.punchedDetail')}
              control={<Badge variant={directBadge.variant}>{directBadge.label}</Badge>}
            />
          </>
        )}
        {relay && (
          <RelayRow
            relay={relay}
            direct={direct}
            inUse={relayInUse}
            refused={refused?.row === 'relay' ? refused : undefined}
            busy={sending === 'relay'}
            onChange={(on) => set?.('relay', on)}
          />
        )}
      </SettingsPanel>

      {!unbinding && <SettingsPanel title={t('devices.connection.tunnelSettings')} description={<KeptTail text={t('devices.connection.verifyNote')} />}>
        <Row
          title={t('remoteAccess.protocol')}
          hint={settings.transport_protocol === 'auto'
            ? tunnel.active ? t('devices.connection.protocolAutomatic', { protocol: protocolName(tunnel.active) }) : undefined
            : t('devices.connection.protocolPinned')}
          status={statusFor('protocol')}
          control={<TunnelProtocolControl value={settings.transport_protocol} onChange={(value) => set?.('protocol', value)} disabled={pending} />}
        />
        <Row
          title={t('remoteAccess.ipFamily')}
          status={statusFor('ipFamily')}
          control={<TunnelIpFamilyControl value={settings.edge_ip_version} onChange={(value) => set?.('ipFamily', value)} disabled={pending} />}
        />
        <Row
          title={t('remoteAccess.outboundInterface')}
          status={statusFor('outbound')}
          control={(
            <TunnelOutboundSelect
              value={settings.edge_bind_address}
              onChange={(value) => set?.('outbound', value)}
              disabled={pending}
              interfaces={connection.interfaces}
              className="md:w-56"
            />
          )}
        />
        <Row
          title={t('remoteAccess.autoRecovery')}
          status={refused?.row === 'autoRecovery' ? <Refused refusal={refused} /> : null}
          control={<TunnelAutoRecoverySwitch value={settings.auto_recovery} onChange={(value) => set?.('autoRecovery', value)} disabled={sending === 'autoRecovery'} />}
        />
        <Row
          title={t('remoteAccess.optimizationProfile')}
          status={refused?.row === 'profile' ? <Refused refusal={refused} /> : null}
          control={<TunnelProfileControl value={settings.optimization_profile} onChange={(value) => set?.('profile', value)} disabled={sending === 'profile'} />}
        />
      </SettingsPanel>}

      <SettingsPanel>
        {!unbinding && <OptimizeRow optimize={optimize} onOptimize={actions.onOptimizeRoute} />}
        <ActionRow icon={Stethoscope} label={t('devices.connection.diagnostics')} onClick={actions.onOpenDiagnostics} />
      </SettingsPanel>

      {ownerPanel}
      {dialogs}

      {diagnostics && (
        <Dialog open onOpenChange={(open) => { if (!open) actions.onCloseDiagnostics?.(); }}>
          <DialogContent className="max-w-2xl content-start" mobileSheetHeight="tall" closeLabel={t('common.close')}>
            <DialogHeader>
              <DialogTitle>{t('devices.connection.diagnostics')}</DialogTitle>
            </DialogHeader>
            <TunnelDetails {...diagnostics} onRun={actions.onRunDiagnostics} onMeasure={actions.onMeasure} />
          </DialogContent>
        </Dialog>
      )}
    </>,
  );
}
